"""
Core engine: discover FSKX models, parse their parameters, run them.

A model is run by reconstructing FSK-Lab semantics:
  1. pick a simulation scenario (a file under simulations/) as the base parameter set
  2. let the user override individual parameters via the web form
  3. write a generated parameter script, then execute  params -> model -> visualization
     inside the model's own micromamba environment (see depresolve.py)
  4. collect plots, serialized results and any output files
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

import depresolve
import omex
import state

# Directory layout (overridable via env for the container).
# Image/document formats we display inline after a run (everything else is download-only).
IMAGE_EXTS = (".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf")

MODELS_DIR = os.environ.get("FSKX_MODELS_DIR", "/models")
WORK_DIR = os.environ.get("FSKX_WORK_DIR", "/tmp/fskx_work")
ENV_LOGS_DIR = os.environ.get("FSKX_ENV_LOGS", "/tmp/fskx_envlogs")
APP_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Model discovery & extraction
# ---------------------------------------------------------------------------

def list_models():
    """All .fskx files in the models directory."""
    out = []
    if not os.path.isdir(MODELS_DIR):
        return out
    for name in sorted(os.listdir(MODELS_DIR)):
        if name.lower().endswith(".fskx"):
            out.append(name)
    return out


def extract_model(fskx_name):
    """Extract an archive into a fresh work dir; return that dir."""
    src = os.path.join(MODELS_DIR, fskx_name)
    dest = os.path.join(WORK_DIR, os.path.splitext(fskx_name)[0])
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(src, "r") as zf:
        zf.extractall(dest)
    return dest


# ---------------------------------------------------------------------------
# Metadata & parameter parsing
# ---------------------------------------------------------------------------

def load_metadata(model_dir):
    path = os.path.join(model_dir, "metaData.json")
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except (ValueError, OSError):
            pass
    return {}


def metadata_param_index(metadata):
    """id -> metadata dict for each parameter."""
    idx = {}
    for p in metadata.get("modelMath", {}).get("parameter", []) or []:
        pid = p.get("id")
        if pid:
            idx[pid] = p
    return idx


def list_scenarios(model_dir, language):
    """Simulation parameter files under simulations/ (defaultSimulation first)."""
    sim_dir = os.path.join(model_dir, "simulations")
    ext = ".py" if language == "python" else ".r"
    found = []
    if os.path.isdir(sim_dir):
        for n in sorted(os.listdir(sim_dir)):
            if n.lower().endswith(ext):
                found.append(n)
    found.sort(key=lambda n: (not n.lower().startswith("default"), n.lower()))
    return found


def scenario_names(model_dir, language):
    """
    Ordered scenario names for a model. Prefers SED-ML (`<model id>` per scenario, the
    standard's source of truth); falls back to the `simulations/` folder when there is
    no usable SED-ML.
    """
    sed = omex.sedml_scenarios(model_dir)
    if sed:
        return [s["id"] or f"scenario{i + 1}" for i, s in enumerate(sed)]
    return list_scenarios(model_dir, language)


def scenario_assignments(model_dir, language, scenario_name):
    """
    Ordered `(name, raw_value)` parameter assignments for a scenario. Prefers the SED-ML
    `changeAttribute` list; falls back to parsing the `simulations/<scenario>` script.
    """
    sed = omex.sedml_scenarios(model_dir)
    if sed:
        names = [s["id"] or f"scenario{i + 1}" for i, s in enumerate(sed)]
        chosen = sed[0]
        for name, s in zip(names, sed):
            if name == scenario_name:
                chosen = s
                break
        return list(chosen["changes"])
    sim_path = os.path.join(model_dir, "simulations", scenario_name or "")
    if os.path.exists(sim_path):
        text = open(sim_path, encoding="utf-8", errors="ignore").read()
        return parse_assignments(text, language)
    return []


_ASSIGN_R = re.compile(r"^\s*([A-Za-z.][A-Za-z0-9._]*)\s*(?:<-|=)\s*(.+?)\s*$")
_ASSIGN_PY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")


def parse_assignments(text, language):
    """
    Parse single-line `name <- value` (R) / `name = value` (Python) assignments,
    preserving order and the raw right-hand-side expression.
    """
    pat = _ASSIGN_PY if language == "python" else _ASSIGN_R
    out = []
    seen = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = pat.match(line)
        if not m:
            continue
        name, rhs = m.group(1), m.group(2).strip()
        # Skip comparison operators that the python regex might catch.
        if language == "python" and rhs.startswith("="):
            continue
        if name in seen:
            # keep last value but don't duplicate field
            out = [(n, rhs if n == name else v) for n, v in out]
            continue
        seen.add(name)
        out.append((name, rhs))
    return out


def _is_quoted(expr):
    return len(expr) >= 2 and expr[0] in "\"'" and expr[-1] == expr[0]


def build_param_fields(assignments, metadata):
    """
    Build the list of editable parameter fields for the web form.

    `assignments` is an ordered list of `(name, raw_value)` pairs (from SED-ML
    changeAttributes, or parsed from a scenario script). Each is enriched with
    metaData.json (names, units, descriptions, ranges, classification).
    """
    midx = metadata_param_index(metadata)
    fields = []
    for name, raw in assignments:
        meta = midx.get(name, {})
        classification = (meta.get("classification") or "INPUT").upper()
        if classification == "OUTPUT":
            continue
        dtype = (meta.get("dataType") or "").upper()
        display = raw
        is_string = dtype == "STRING" or _is_quoted(raw)
        if is_string and _is_quoted(raw):
            display = raw[1:-1]
        fields.append({
            "id": name,
            "raw": raw,
            "display": display,
            "is_string": is_string,
            "label": meta.get("name", name),
            "unit": (meta.get("unit") or "").strip("[]") if meta.get("unit") else "",
            "description": meta.get("description", ""),
            "dataType": dtype,
            "classification": classification,
            "minValue": meta.get("minValue"),
            "maxValue": meta.get("maxValue"),
        })
    return fields


def render_value(field, submitted_value, language):
    """Turn a submitted form value back into a valid R/Python RHS expression."""
    val = submitted_value
    if field["is_string"]:
        # Re-quote, escaping embedded quotes.
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return val  # numeric / vector expressions are passed through verbatim


def write_param_script(model_dir, language, scenario_name, fields, submitted, work_dir):
    """
    Write the generated parameter script: the scenario's base parameters first, then
    user overrides appended (later assignment wins).

    The base comes from SED-ML (one assignment per changeAttribute) when available — the
    standard's source of truth. With no SED-ML we write the `simulations/` script
    verbatim, which preserves any non-assignment lines it may contain.
    """
    assign = " = " if language == "python" else " <- "
    sed = omex.sedml_scenarios(model_dir)
    if sed:
        base_lines = [f"{name}{assign}{raw}"
                      for name, raw in scenario_assignments(model_dir, language, scenario_name)]
    else:
        sim_path = os.path.join(model_dir, "simulations", scenario_name or "")
        if os.path.exists(sim_path):
            base_lines = [open(sim_path, encoding="utf-8", errors="ignore").read()]
        else:
            base_lines = [f"{name}{assign}{raw}"
                          for name, raw in scenario_assignments(model_dir, language, scenario_name)]

    lines = [f"### --- scenario parameters ({scenario_name}) ---", *base_lines,
             "", "### --- user parameter overrides ---"]
    for f in fields:
        if f["id"] not in submitted:
            continue
        rhs = render_value(f, submitted[f["id"]], language)
        lines.append(f"{f['id']}{assign}{rhs}")

    out_name = "params.py" if language == "python" else "params.R"
    out_path = os.path.join(work_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return out_path


def write_run_plan(work_dir, language, scripts):
    """
    Write `_run_plan.json` so the in-environment wrapper knows the ACTUAL script names
    (resolved from metadata.rdf / SED-ML / convention) instead of assuming model.<ext>.
    """
    plan = {
        "language": language,
        "param_script": "params.py" if language == "python" else "params.R",
        "model_script": scripts.get("model"),
        "visualization_script": scripts.get("visualization"),
    }
    with open(os.path.join(work_dir, "_run_plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh)
    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

WORK_VOLUME = os.environ.get("FSKX_WORK_VOLUME", "fskx_work")
RUNNER_DIR = os.path.join(WORK_DIR, "_runner")  # wrappers staged on the shared volume


def _sync_runners():
    """
    Copy the wrapper scripts onto the shared work volume.

    Docker-backed runs execute the wrapper inside a SIBLING container. If they used a
    wrapper baked into the per-model image (`COPY app/ /app/`), every wrapper change would
    require rebuilding that image. Instead we run the wrapper from `/work/_runner/…` on the
    shared `fskx_work` volume — refreshed here each time — so wrapper updates take effect
    immediately, with no per-model image rebuild.
    """
    os.makedirs(RUNNER_DIR, exist_ok=True)
    for fn in ("run_python_model.py", "run_r_model.R"):
        try:
            shutil.copy2(os.path.join(APP_DIR, fn), os.path.join(RUNNER_DIR, fn))
        except OSError:
            pass


def run_model(work_dir, language, backend, outdir):
    """
    Invoke the in-env runner via the chosen backend. Returns (returncode, log).

    backend is one of:
      {"type": "micromamba", "env": <name>}
      {"type": "direct"}                       # interpreter from PATH (native/local test)
      {"type": "docker", "image": <tag>}       # per-model image, run as sibling container
    """
    os.makedirs(outdir, exist_ok=True)
    btype = backend["type"]

    # For the sibling (docker) backend, run the wrapper from the shared volume so it is
    # always the current version, independent of when the per-model image was built.
    if btype == "docker":
        _sync_runners()

    if language == "python":
        runner = os.path.join(RUNNER_DIR, "run_python_model.py") if btype == "docker" \
            else os.path.join(APP_DIR, "run_python_model.py")
        interp = "python" if btype != "direct" else sys.executable
        inner = [interp, runner, work_dir, outdir]
    else:
        runner = os.path.join(RUNNER_DIR, "run_r_model.R") if btype == "docker" \
            else os.path.join(APP_DIR, "run_r_model.R")
        inner = ["Rscript", runner, work_dir, outdir]

    if btype == "micromamba":
        cmd = ["micromamba", "run", "-n", backend["env"], *inner]
    elif btype == "docker":
        # Sibling container shares the work named volume so it sees the extracted model
        # at the same /work/... paths and writes results back where the app can read them.
        cmd = ["docker", "run", "--rm",
               "-v", f"{WORK_VOLUME}:{WORK_DIR}",
               backend["image"], *inner]
    else:
        cmd = inner

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    return proc.returncode, proc.stdout


def choose_backend(model_dir, spec, progress=None):
    """
    Decide how to run this model and ensure the environment is ready.

    Preference order:
      1. An AI-built per-model Docker image, if one exists for this dependency set.
      2. The standard micromamba environment (built on demand).
      3. Direct interpreter from PATH (no micromamba — local/native).

    Returns (backend_dict, env_log_path). Raises on micromamba build failure so the
    caller can offer the AI fallback.
    """
    import aienv  # local import: only needed when docker may be involved
    tag = aienv.image_tag(spec)
    if aienv.docker_available() and aienv.image_exists(tag):
        if progress:
            progress("Using the AI-built model image…")
        return {"type": "docker", "image": tag}, None

    if progress:
        progress(f"Preparing {spec['language'].upper()} environment "
                 "(first run may take a few minutes)…")
    env_name, _spec, env_log = depresolve.ensure_env(model_dir, ENV_LOGS_DIR)
    if env_name:
        return {"type": "micromamba", "env": env_name}, env_log
    return {"type": "direct"}, env_log


def classify_outputs(outdir):
    """
    Split a run's output directory into (plots, files), recursing into subfolders —
    models may write outputs anywhere under the work dir (e.g. a `visualizations/`
    folder), not just at the root. Paths are returned relative to `outdir` (posix).

    `plots` are every displayable image a run produced — the device-captured
    plot_NNN.png AND any image files the model wrote itself (svg/pdf/jpg/…), with the
    device captures first and the rest alphabetical. `files` is everything else
    (download-only), excluding the internal status.json at the root.
    """
    if not os.path.isdir(outdir):
        return [], []
    rels = []
    for root, dirs, fnames in os.walk(outdir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(fnames):
            rel = os.path.relpath(os.path.join(root, fn), outdir).replace(os.sep, "/")
            rels.append(rel)
    plots = sorted(
        (r for r in rels if r.lower().endswith(IMAGE_EXTS)),
        key=lambda n: (not os.path.basename(n).lower().startswith("plot_"), n.lower()),
    )
    files = [r for r in rels if not r.lower().endswith(IMAGE_EXTS) and r != "status.json"]
    return plots, files


def execute(fskx_name, scenario_name, submitted_values, progress=None):
    """
    Full pipeline for one run. `progress` is an optional callable(str) for status
    updates. Returns a result dict consumed by the web UI.
    """
    def note(msg):
        if progress:
            progress(msg)

    note("Extracting archive…")
    model_dir = extract_model(fskx_name)

    spec = depresolve.resolve(model_dir)
    language = spec["language"]
    metadata = load_metadata(model_dir)

    import aienv  # local import keeps module load light when docker isn't used

    scenarios = scenario_names(model_dir, language)
    if not scenario_name or scenario_name not in scenarios:
        scenario_name = scenarios[0] if scenarios else None

    # Fail fast on a non-conformant archive: locate the scripts by their declared roles
    # (metadata.rdf → SED-ML source → filename convention) and confirm a parameter source.
    scripts = omex.resolve_scripts(model_dir, language)
    problems = []
    if not scripts.get("model"):
        problems.append(
            "no model script could be located (checked metadata.rdf, the sim.sedml "
            "model source, and the model.<ext> convention)")
    if not scenarios:
        problems.append(
            "no simulation scenarios/parameters were found (checked sim.sedml and the "
            "simulations/ folder)")
    if problems:
        return {
            "ok": False, "language": language, "scenario": scenario_name,
            "outdir": None, "run_id": None, "plots": [], "files": [],
            "log": "This FSKX archive is not runnable as a standard FSK-ML model:\n  - "
                   + "\n  - ".join(problems),
            "status": {}, "fskx": fskx_name, "can_ai_fix": False, "stage": "format",
        }

    assignments = scenario_assignments(model_dir, language, scenario_name)
    fields = build_param_fields(assignments, metadata)

    write_param_script(model_dir, language, scenario_name, fields, submitted_values, model_dir)
    write_run_plan(model_dir, language, scripts)

    # Select backend; a micromamba build failure becomes an AI-fixable result.
    try:
        backend, env_log = choose_backend(model_dir, spec, progress=progress)
    except Exception as exc:  # noqa: BLE001
        env_log_text = ""
        try:
            key = depresolve.env_key(spec)
            p = os.path.join(ENV_LOGS_DIR, f"env_{key}.log")
            if os.path.exists(p):
                env_log_text = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": False, "language": language, "scenario": scenario_name,
            "outdir": None, "run_id": None, "plots": [], "files": [],
            "log": f"Environment build failed:\n{exc}\n\n{env_log_text[-4000:]}",
            "status": {}, "fskx": fskx_name,
            "can_ai_fix": aienv.docker_available(),
            "stage": "env_build",
        }

    # Environment is ready (micromamba env, AI image, or direct interpreter).
    state.mark_prepared(fskx_name)

    run_id = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(WORK_DIR, "_results", os.path.splitext(fskx_name)[0], run_id)
    note("Running the model…")
    rc, log = run_model(model_dir, language, backend, outdir)

    status = {}
    status_path = os.path.join(outdir, "status.json")
    if os.path.exists(status_path):
        try:
            status = json.load(open(status_path))
        except ValueError:
            pass

    plots, files = classify_outputs(outdir)

    if rc == 0:
        state.mark_executed(fskx_name)

    return {
        "ok": rc == 0,
        "language": language,
        "backend": backend["type"],
        "scenario": scenario_name,
        "outdir": outdir,
        "run_id": run_id,
        "plots": plots,
        "files": files,
        "log": log,
        "status": status,
        "fskx": fskx_name,
        # A runtime failure on the non-docker backend is AI-fixable (e.g. missing
        # system library); regenerating is also offered for a failed docker image.
        "can_ai_fix": (rc != 0) and aienv.docker_available(),
        "stage": "run",
    }


def ai_generate_dockerfile(fskx_name, api_key, model_id, error_log="", prev_dockerfile=""):
    """Generate a Dockerfile for a model via Claude. Returns (tag, dockerfile_text)."""
    import aienv
    model_dir = os.path.join(WORK_DIR, os.path.splitext(fskx_name)[0])
    if not os.path.isdir(model_dir):
        model_dir = extract_model(fskx_name)
    spec, dockerfile = aienv.generate_dockerfile(
        model_dir, api_key, model_id, error_log=error_log,
        prev_dockerfile=prev_dockerfile)
    return aienv.image_tag(spec), dockerfile


def ai_build_image(fskx_name, dockerfile_text, progress=None):
    """Build (and thereby register) the per-model AI image. Returns (ok, tag, log_path)."""
    import aienv
    model_dir = os.path.join(WORK_DIR, os.path.splitext(fskx_name)[0])
    if not os.path.isdir(model_dir):
        model_dir = extract_model(fskx_name)
    spec = depresolve.resolve(model_dir)
    tag = aienv.image_tag(spec)
    os.makedirs(ENV_LOGS_DIR, exist_ok=True)
    log_path = os.path.join(ENV_LOGS_DIR, f"aibuild_{tag}.log")
    ok = aienv.build_image(dockerfile_text, tag, log_path, progress=progress)
    if ok:
        state.mark_prepared(fskx_name)
    return ok, tag, log_path


def existing_model_images():
    """The set of per-model AI image tags that currently exist (one docker call)."""
    import aienv
    return set(aienv.list_model_images())


def model_status(fskx_name, existing_image_tags=None):
    """
    UI status for a model: {'prepared': bool, 'executed': bool}.

    'prepared' (runnable) is derived LIVE from whether the model's actual execution
    artifact exists — its AI Docker image OR its micromamba env — keyed by the
    dependency fingerprint. Because that key is shared by any models with identical
    dependencies, removing a shared image/env is reflected in every sharing model
    automatically, with no per-model bookkeeping.

    'executed' is a per-model historical fact (recorded on a successful run) but is
    only reported True while the environment is actually present, so removing the
    shared artifact drops both badges for every model that shared it.

    Pass `existing_image_tags` (from existing_model_images()) to avoid one docker
    call per model when rendering a list.
    """
    import aienv
    executed_hist = bool(state.get(fskx_name)["executed"])

    prepared = False
    try:
        model_dir = os.path.join(WORK_DIR, os.path.splitext(fskx_name)[0])
        if not os.path.isdir(model_dir):
            model_dir = extract_model(fskx_name)
        spec = depresolve.resolve(model_dir)
        key = depresolve.env_key(spec)
        tag = aienv.image_tag(spec)
        if existing_image_tags is not None:
            image_present = tag in existing_image_tags
        else:
            image_present = aienv.docker_available() and aienv.image_exists(tag)
        prepared = bool(image_present or depresolve.env_exists(key))
    except Exception:  # noqa: BLE001
        prepared = False

    return {"prepared": prepared, "executed": executed_hist and prepared}


def _count_runs(results_dir):
    """Number of run (timestamp) subfolders under a results dir."""
    if not os.path.isdir(results_dir):
        return 0
    return sum(1 for n in os.listdir(results_dir)
               if os.path.isdir(os.path.join(results_dir, n)))


def _remove_results(results_dir):
    """Delete a results directory tree. Returns the run count removed (0 if none)."""
    n = _count_runs(results_dir)
    if os.path.isdir(results_dir):
        shutil.rmtree(results_dir, ignore_errors=True)
    return n


def cleanup_model(fskx_name, progress=None):
    """
    Remove a single model's cached environment — its AI-built Docker image (if any)
    and its micromamba env — plus its stored run results, then reset its status
    badges. The .fskx file itself is kept. Returns {'removed': [...], ...}.
    """
    import aienv

    def note(msg):
        if progress:
            progress(msg)

    note("Resolving model environment…")
    model_dir = os.path.join(WORK_DIR, os.path.splitext(fskx_name)[0])
    if not os.path.isdir(model_dir):
        model_dir = extract_model(fskx_name)
    spec = depresolve.resolve(model_dir)
    key = depresolve.env_key(spec)
    tag = aienv.image_tag(spec)

    removed = []
    if aienv.docker_available() and aienv.image_exists(tag):
        note("Removing Docker image…")
        if aienv.remove_image(tag):
            removed.append(f"image {tag}")
    if depresolve.env_exists(key):
        note("Removing environment…")
        if depresolve.remove_env(key):
            removed.append(f"environment {key}")

    note("Removing stored run results…")
    results_dir = os.path.join(WORK_DIR, "_results", os.path.splitext(fskx_name)[0])
    n_runs = _remove_results(results_dir)
    if n_runs:
        removed.append(f"{n_runs} stored run result(s)")

    state.clear(fskx_name)
    return {"removed": removed, "key": key, "tag": tag, "results_removed": n_runs}


def cleanup_all(progress=None):
    """
    Remove every per-model cache: all fskx-model-* Docker images, all micromamba
    envs, and all stored run results, then reset all status badges. The .fskx files
    are kept. Returns {'images': [...], 'envs': [...], 'results': <run count>}.
    """
    import aienv

    def note(msg):
        if progress:
            progress(msg)

    removed = {"images": [], "envs": [], "results": 0}
    note("Removing per-model Docker images…")
    for tag in aienv.list_model_images():
        if aienv.remove_image(tag):
            removed["images"].append(tag)
    note("Removing per-model environments…")
    for name in depresolve.list_envs():
        if depresolve.remove_env(name):
            removed["envs"].append(name)

    note("Removing stored run results…")
    results_root = os.path.join(WORK_DIR, "_results")
    total = 0
    if os.path.isdir(results_root):
        for model_name in os.listdir(results_root):
            total += _remove_results(os.path.join(results_root, model_name))
        shutil.rmtree(results_root, ignore_errors=True)
    removed["results"] = total

    state.clear_all()
    return removed


def docker_status():
    """Whether the per-model Docker backend is usable from inside the app."""
    import aienv
    return {"docker_available": aienv.docker_available()}


def model_info(fskx_name):
    """Metadata + parameter fields + scenarios for the parameter form.

    Best-effort: a non-conformant archive yields an `error` message (and empty
    scenarios/fields) rather than raising, so the page can show it and disable Run.
    """
    model_dir = extract_model(fskx_name)
    spec = depresolve.resolve(model_dir)
    language = spec["language"]
    metadata = load_metadata(model_dir)
    scenarios = scenario_names(model_dir, language)
    assignments = (scenario_assignments(model_dir, language, scenarios[0])
                   if scenarios else [])
    fields = build_param_fields(assignments, metadata)
    scripts = omex.resolve_scripts(model_dir, language)

    error = None
    if not scripts.get("model"):
        error = ("No model script is declared in metadata.rdf or found by the "
                 "model.<ext> convention — this archive can't be run.")
    elif not scenarios:
        error = ("No simulation scenarios were found in sim.sedml or a simulations/ "
                 "folder — this archive has no parameters to run.")

    gi = metadata.get("generalInformation", {})
    scope = metadata.get("scope", {})
    return {
        "fskx": fskx_name,
        "language": language,
        "version": spec["version"],
        "packages": spec["packages"],
        "name": gi.get("name", os.path.splitext(fskx_name)[0]),
        "description": gi.get("description", ""),
        "hazard": _scope_name(scope.get("hazard")),
        "product": _scope_name(scope.get("product")),
        "scenarios": scenarios,
        "fields": fields,
        "scripts": scripts,
        "error": error,
    }


def _scope_name(val):
    if isinstance(val, list) and val:
        val = val[0]
    if isinstance(val, dict):
        return val.get("name") or val.get("hazardName") or val.get("productName") or ""
    return val or ""


def fields_for_scenario(fskx_name, scenario_name):
    """Re-derive parameter fields when the user switches scenario."""
    model_dir = os.path.join(WORK_DIR, os.path.splitext(fskx_name)[0])
    if not os.path.isdir(model_dir):
        model_dir = extract_model(fskx_name)
    spec = depresolve.resolve(model_dir)
    language = spec["language"]
    metadata = load_metadata(model_dir)
    assignments = scenario_assignments(model_dir, language, scenario_name)
    return build_param_fields(assignments, metadata)
