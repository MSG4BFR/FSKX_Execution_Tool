"""
pipeline.py — model joining engine (model-joining Phase 2).

Executes a DAG of FSKX model runs where a target model's INPUT is taken from a source
model's parameter (its OUTPUT, or another INPUT/CONSTANT). See ../model-joining/ for the
design and ../INTERCHANGE_SPEC.md for the value format.

Pipeline definition (JSON-friendly dicts, so it can later be persisted as pipeline.json):

    {
      "version": "1.0.0",
      "nodes": [{"id": "n1", "fskx": "DoseResponse.fskx", "scenario": null, "params": {}}, ...],
      "edges": [
        {"source": {"node": "n1", "param": "response"},
         "target": {"node": "n2", "param": "dose"},
         "transform": {"scale": 3600} | {"expression": "{value} * 3600"} | null}
      ],
      "shared": [
        {"value": 1000, "dataType": "INTEGER", "unit": "[]",
         "targets": [{"node": "n2", "param": "niter"}, {"node": "n3", "param": "niter"}]}
      ]
    }

Identity rule: a parameter is addressed as (node-instance id, paramId); paramId is only
unique within a model, and the node id disambiguates the rest — so a `t` in one model never
collides with a `t` in another (see model-joining/phase-2). Nothing here matches parameters
across models by name.

The metadata/language loader and the run executor are injectable so the orchestration logic
is unit-testable without Docker or a live model run.
"""

import hashlib
import json
import os

import interchange

NUMERIC = {"NUMBER", "DOUBLE", "INTEGER"}


class PipelineError(Exception):
    """Raised for a structurally invalid pipeline (e.g. a cycle) during execution."""


# ---------------------------------------------------------------------------
# Default injectable dependencies (real engine; lazy import keeps tests light)
# ---------------------------------------------------------------------------

def _default_load_ctx(fskx_name):
    """Return (language, {paramId: metadata_dict}) for a model archive."""
    import engine
    import depresolve
    model_dir = engine.extract_model(fskx_name)
    language = depresolve.resolve(model_dir)["language"]
    meta = engine.load_metadata(model_dir)
    return language, engine.metadata_param_index(meta)


def _default_execute(fskx_name, scenario, params, injections, progress=None):
    import engine
    return engine.execute(fskx_name, scenario, params, progress=progress,
                          injections=injections)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def check_compat(src_dt, tgt_dt):
    """(status, message) for wiring src_dt -> tgt_dt. status in {ok, warn, error}."""
    s, t = (src_dt or "").upper(), (tgt_dt or "").upper()
    if not s or not t:
        return "warn", f"missing dataType ('{s}' -> '{t}'); cannot fully check"
    if s == t:
        return "ok", ""
    if s in NUMERIC and t in NUMERIC:
        return "ok", ""
    if s in NUMERIC and t == "VECTOROFNUMBERS":
        return "ok", ""
    if s == "VECTOROFNUMBERS" and t in NUMERIC:
        return "warn", "vector -> scalar (a length-1 vector is expected)"
    if s == "MATRIXOFNUMBERS" and t == "VECTOROFNUMBERS":
        return "warn", "matrix -> vector (a single row/column is expected)"
    if s == "VECTOROFNUMBERS" and t == "MATRIXOFNUMBERS":
        return "error", "vector -> matrix is ambiguous (shape undefined)"
    return "error", f"incompatible types {s} -> {t}"


def _norm_unit(u):
    u = (u or "").strip().strip("[]").lower()
    return "" if u in ("", "none", "-", "dimensionless") else u


def unit_mismatch(src_unit, tgt_unit):
    su, tu = _norm_unit(src_unit), _norm_unit(tgt_unit)
    return bool(su) and bool(tu) and su != tu


def topo_order(pipeline):
    """Node ids in dependency order. Raises PipelineError on a cycle. Shared parameters do
    NOT create ordering dependencies (only edges do)."""
    ids = [n["id"] for n in pipeline.get("nodes", [])]
    deps = {nid: set() for nid in ids}
    for e in pipeline.get("edges", []):
        sn, tn = e["source"]["node"], e["target"]["node"]
        if sn in deps and tn in deps:
            deps[tn].add(sn)
    order, seen = [], set()
    while len(order) < len(ids):
        ready = sorted(n for n in ids if n not in seen and deps[n] <= seen)
        if not ready:
            raise PipelineError("pipeline has a cycle (must be a DAG)")
        order.extend(ready)
        seen.update(ready)
    return order


def validate(pipeline, load_ctx=None):
    """Return (errors, warnings). errors block execution; warnings are advisory."""
    load_ctx = load_ctx or _default_load_ctx
    errors, warnings = [], []
    nodes = {n["id"]: n for n in pipeline.get("nodes", [])}
    if len(nodes) != len(pipeline.get("nodes", [])):
        errors.append("duplicate node id(s) in pipeline")

    ctx = {}
    for nid, n in nodes.items():
        try:
            _lang, pidx = load_ctx(n["fskx"])
            ctx[nid] = pidx
        except Exception as exc:  # noqa: BLE001
            errors.append(f"node '{nid}': cannot load model '{n.get('fskx')}': {exc}")

    def _wire_label(s, t):
        return f"{s['node']}.{s['param']} -> {t['node']}.{t['param']}"

    for e in pipeline.get("edges", []):
        s, t = e["source"], e["target"]
        if s["node"] not in nodes or t["node"] not in nodes:
            errors.append(f"edge references unknown node: {_wire_label(s, t)}")
            continue
        sp = ctx.get(s["node"], {}).get(s["param"])
        tp = ctx.get(t["node"], {}).get(t["param"])
        if sp is None:
            errors.append(f"edge {_wire_label(s, t)}: source param not in node '{s['node']}'")
            continue
        if tp is None:
            errors.append(f"edge {_wire_label(s, t)}: target param not in node '{t['node']}'")
            continue
        status, msg = check_compat(sp.get("dataType"), tp.get("dataType"))
        if status == "error":
            errors.append(f"edge {_wire_label(s, t)}: {msg}")
        elif status == "warn":
            warnings.append(f"edge {_wire_label(s, t)}: {msg}")
        if unit_mismatch(sp.get("unit"), tp.get("unit")) and not e.get("transform"):
            warnings.append(f"edge {_wire_label(s, t)}: unit mismatch "
                            f"'{sp.get('unit')}' -> '{tp.get('unit')}' and no transform set")

    for sp in pipeline.get("shared", []):
        for tgt in sp.get("targets", []):
            if tgt["node"] not in nodes:
                errors.append(f"shared parameter targets unknown node '{tgt['node']}'")
            elif ctx.get(tgt["node"]) is not None and tgt["param"] not in ctx[tgt["node"]]:
                warnings.append(f"shared parameter target '{tgt['node']}.{tgt['param']}' "
                                "is not a declared parameter")

    try:
        topo_order(pipeline)
    except PipelineError as exc:
        errors.append(str(exc))

    return errors, warnings


# ---------------------------------------------------------------------------
# Value transfer (transform + render)
# ---------------------------------------------------------------------------

def _apply_affine(value, scale, offset):
    if isinstance(value, (list, tuple)):
        return [_apply_affine(v, scale, offset) for v in value]
    try:
        return value * scale + offset
    except TypeError:
        return value  # non-numeric: leave as-is (e.g. a dataframe; caller should warn)


def build_injection(target_language, data, source_dir, transform, target_id):
    """
    Turn a source parameter's typed ``data`` into an injection for the target model:
    ``{"rhs": <literal>, "sidecars": [(src_abspath, dest_basename), ...]}``.

    - affine transform ``{scale, offset}``: applied numerically, then re-rendered inline.
    - expression transform ``{expression}``: substitutes the rendered literal for the
      ``{value}`` token (which arrives already parenthesized), e.g. ``"{value} * 3600"``.
    - no transform: render directly; a ref-encoded value's sidecar is staged into the model
      dir under a collision-free name and the read call points at it.
    """
    transform = transform or {}
    is_affine = "scale" in transform or "offset" in transform
    is_expr = "expression" in transform

    if is_affine:
        value = interchange.read_value(data, source_dir)
        value = _apply_affine(value, transform.get("scale", 1.0), transform.get("offset", 0.0))
        reencoded = interchange.encode_value(value, data.get("dataType"))
        return {"rhs": interchange.render_rhs(reencoded, target_language), "sidecars": []}

    sidecars = []
    if data.get("encoding") == "ref":
        ext = os.path.splitext(data["ref"]["path"])[1] or ".dat"
        dest = f"_joined_{interchange._safe_name(target_id)}{ext}"
        sidecars.append((os.path.join(source_dir, data["ref"]["path"]), dest))
        rhs = interchange.render_rhs(data, target_language, path_override=dest)
    else:
        rhs = interchange.render_rhs(data, target_language)

    if is_expr:
        rhs = transform["expression"].replace("{value}", f"({rhs})")
    return {"rhs": rhs, "sidecars": sidecars}


def _source_param_data(run_outdir, param_id):
    """Look up a parameter's typed ``data`` in a finished run's outputs.json."""
    path = os.path.join(run_outdir, "outputs.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        bundle = json.load(fh)
    for p in bundle.get("parameters", []):
        if p["metadata"]["id"] == param_id:
            return p["data"]
    return None


def _hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run(pipeline, execute_fn=None, load_ctx=None, progress=None, validate_first=True,
        on_node=None):
    """
    Execute the pipeline in topological order. Returns a run record:
    ``{ok, order, results: {node_id: exec_result}, provenance: [...], failed_node?}``.

    ``provenance`` records, per resolved binding, the source run id, the wiring, the
    transform and a hash of the injected literal — so a joined result is traceable.

    ``on_node(node_id, phase, info)`` is an optional UI hook called as each node starts
    (``phase="start"``) and finishes (``"done"`` / ``"failed"``); ``info`` carries the fskx
    and, on completion, the run id / ok flag.
    """
    execute_fn = execute_fn or _default_execute
    load_ctx = load_ctx or _default_load_ctx

    def _emit(nid, phase, **info):
        if on_node:
            try:
                on_node(nid, phase, info)
            except Exception:  # noqa: BLE001 — a UI hook must never break the run
                pass

    if validate_first:
        errors, _warn = validate(pipeline, load_ctx=load_ctx)
        if errors:
            return {"ok": False, "stage": "validate", "errors": errors,
                    "results": {}, "provenance": []}

    nodes = {n["id"]: n for n in pipeline["nodes"]}
    order = topo_order(pipeline)
    results, provenance = {}, []

    def note(msg):
        if progress:
            progress(msg)

    for nid in order:
        node = nodes[nid]
        fskx = node["fskx"]
        language, _pidx = load_ctx(fskx)
        injections = {}
        _emit(nid, "start", fskx=fskx)

        # incoming edges
        for e in pipeline.get("edges", []):
            if e["target"]["node"] != nid:
                continue
            src_nid, src_pid = e["source"]["node"], e["source"]["param"]
            src_res = results.get(src_nid)
            if not src_res or not src_res.get("ok"):
                _emit(nid, "failed", fskx=fskx)
                return {"ok": False, "stage": "run", "failed_node": nid,
                        "errors": [f"node '{nid}': source node '{src_nid}' did not run "
                                   "successfully"], "results": results,
                        "provenance": provenance}
            data = _source_param_data(src_res["outdir"], src_pid)
            if data is None:
                _emit(nid, "failed", fskx=fskx)
                return {"ok": False, "stage": "run", "failed_node": nid,
                        "errors": [f"node '{nid}': source param '{src_nid}.{src_pid}' not "
                                   "found in upstream outputs.json"], "results": results,
                        "provenance": provenance}
            inj = build_injection(language, data, src_res["outdir"], e.get("transform"),
                                  e["target"]["param"])
            injections[e["target"]["param"]] = inj
            provenance.append({
                "target": e["target"], "source": e["source"],
                "source_run": src_res.get("run_id"), "transform": e.get("transform"),
                "value_hash": _hash(inj["rhs"]),
            })

        # shared parameters fanned out to this node
        for sp in pipeline.get("shared", []):
            for tgt in sp.get("targets", []):
                if tgt["node"] != nid:
                    continue
                d = interchange.encode_value(sp.get("value"), sp.get("dataType", "STRING"))
                injections[tgt["param"]] = {
                    "rhs": interchange.render_rhs(d, language), "sidecars": []}

        note(f"Running node '{nid}' ({fskx})…")
        res = execute_fn(fskx, node.get("scenario"), dict(node.get("params", {})),
                         injections, progress)
        results[nid] = res
        if not res.get("ok"):
            _emit(nid, "failed", fskx=fskx, run_id=res.get("run_id"))
            return {"ok": False, "stage": "run", "failed_node": nid,
                    "errors": [f"node '{nid}' failed: see its log"], "results": results,
                    "provenance": provenance}
        _emit(nid, "done", fskx=fskx, run_id=res.get("run_id"))

    return {"ok": True, "order": order, "results": results, "provenance": provenance}
