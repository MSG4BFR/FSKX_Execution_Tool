"""
Executed INSIDE a model's micromamba environment.

Reproduces FSK-Lab execution semantics: the parameter script, the model script and
the (optional) visualization script all run in ONE shared namespace, in that order.
Any matplotlib figures are saved as PNGs; the resulting namespace and any files the
model wrote are collected as results.

Usage: python run_python_model.py <workdir> <outdir>
  workdir  extracted model dir, already containing a generated params.py
  outdir   where plots / results.json / output files are written
"""

import json
import os
import sys

workdir = os.path.abspath(sys.argv[1])
outdir = os.path.abspath(sys.argv[2])
os.makedirs(outdir, exist_ok=True)

os.environ.setdefault("MPLBACKEND", "Agg")
os.chdir(workdir)

# The engine resolves the actual script names (which need NOT be model.py /
# visualization.py — the roles come from metadata.rdf) and writes them here. Fall back
# to the conventional names if the plan is absent (older extractions).
PLAN = {}
_plan_path = os.path.join(workdir, "_run_plan.json")
if os.path.exists(_plan_path):
    try:
        with open(_plan_path, encoding="utf-8") as _fh:
            PLAN = json.load(_fh)
    except (ValueError, OSError):
        PLAN = {}
PARAM_SCRIPT = PLAN.get("param_script") or "params.py"
MODEL_SCRIPT = PLAN.get("model_script") or "model.py"
VIZ_SCRIPT = PLAN.get("visualization_script")  # may be None → no visualization step

# Snapshot file modification times (RECURSIVELY) so we can capture any file created OR
# overwritten during the run — models may write outputs into subfolders, not just the
# root. Keys are paths relative to workdir.
SCRIPT_FILES = {p for p in (PARAM_SCRIPT, MODEL_SCRIPT, VIZ_SCRIPT, "_run_plan.json") if p}


def _walk_rel(base):
    for root, dirs, fnames in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in fnames:
            full = os.path.join(root, fn)
            yield os.path.relpath(full, base).replace(os.sep, "/"), full


before_mtimes = {}
for _rel, _p in _walk_rel(workdir):
    try:
        before_mtimes[_rel] = os.path.getmtime(_p)
    except OSError:
        pass

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    have_mpl = True
except Exception:
    have_mpl = False


def _install_mpl_compat():
    """
    Old FSK-Lab visualization scripts target an older matplotlib. The common breakage:
    `fig.colorbar(sm)` with a free-standing ScalarMappable — older matplotlib stole space
    from the current axes, modern matplotlib raises "Unable to determine Axes…". Restore
    the old behaviour by defaulting `ax` to the figure's current axes when none is given.
    Non-invasive (we don't edit the model) and only activates on the otherwise-failing path.
    """
    if not have_mpl:
        return
    import matplotlib.figure as _mfig
    _orig = _mfig.Figure.colorbar

    def _colorbar(self, mappable, cax=None, ax=None, **kw):
        if cax is None and ax is None and getattr(mappable, "axes", None) is None and self.axes:
            return _orig(self, mappable, ax=self.gca(), **kw)
        return _orig(self, mappable, cax=cax, ax=ax, **kw)

    _mfig.Figure.colorbar = _colorbar


_install_mpl_compat()

ns = {"__name__": "__main__", "__file__": os.path.join(workdir, MODEL_SCRIPT)}


def _resolve_ci(filename):
    """Resolve a filename case-insensitively (archives vary: model.py vs Model.py)."""
    path = os.path.join(workdir, filename)
    if os.path.exists(path):
        return path
    low = filename.lower()
    for n in os.listdir(workdir):
        if n.lower() == low:
            return os.path.join(workdir, n)
    return None


def run_script(filename):
    path = _resolve_ci(filename)
    if not path:
        return
    with open(path, encoding="utf-8", errors="ignore") as fh:
        code = fh.read()
    exec(compile(code, os.path.basename(path), "exec"), ns)


status = {"ok": True, "error": None, "warnings": [], "plots": [], "files": [],
          "stdout_note": ""}


def _fmt_exc(exc):
    import traceback
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# Params + model are the core: a failure here is fatal.
try:
    run_script(PARAM_SCRIPT)       # generated parameter overrides
    run_script(MODEL_SCRIPT)       # the model itself
except Exception as exc:  # noqa: BLE001
    status["ok"] = False
    status["error"] = _fmt_exc(exc)

# Visualization is best-effort: many older models target an older matplotlib API.
# Even if it raises partway through, any figure already drawn is still captured below.
if status["ok"] and VIZ_SCRIPT:
    try:
        run_script(VIZ_SCRIPT)
    except Exception as exc:  # noqa: BLE001
        status["warnings"].append(
            f"{VIZ_SCRIPT} raised an error (a partial plot may still have been "
            "produced):\n" + _fmt_exc(exc)
        )

# Save every figure that exists, regardless of whether visualization finished.
if have_mpl:
    for i, num in enumerate(plt.get_fignums(), start=1):
        fname = f"plot_{i}.png"
        try:
            plt.figure(num).savefig(os.path.join(outdir, fname),
                                    dpi=120, bbox_inches="tight")
            status["plots"].append(fname)
        except Exception as exc:  # noqa: BLE001
            status["stdout_note"] += f"figure {num} save failed: {exc}\n"

# Collect output files the model created or overwrote during the run (e.g. CSVs, and
# images/files written into subfolders), preserving their relative paths under outdir.
import shutil
for rel, src in _walk_rel(workdir):
    if rel in SCRIPT_FILES:
        continue
    try:
        prev = before_mtimes.get(rel)
        if prev is not None and os.path.getmtime(src) == prev:
            continue  # untouched input file
        dest = os.path.join(outdir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        status["files"].append(rel)
    except Exception:
        pass

# Serialize the namespace: JSON-friendly values directly, DataFrames to CSV.
results = {}
try:
    import pandas as pd  # noqa: WPS433
except Exception:
    pd = None

MAX_JSON_CHARS = 4000  # keep results.json readable; large objects are summarized

for key, val in list(ns.items()):
    if key.startswith("__") or callable(val) or isinstance(val, type(os)):
        continue
    try:
        encoded = json.dumps(val)
        if len(encoded) <= MAX_JSON_CHARS:
            results[key] = val
        else:
            results[key] = f"<{type(val).__name__} (large, {len(encoded)} chars omitted)>"
        continue
    except (TypeError, ValueError):
        pass
    if pd is not None and isinstance(val, pd.DataFrame):
        csv_name = f"{key}.csv"
        try:
            val.to_csv(os.path.join(outdir, csv_name), index=False)
            results[key] = f"<DataFrame shape={val.shape} -> {csv_name}>"
            if csv_name not in status["files"]:
                status["files"].append(csv_name)
        except Exception:
            results[key] = f"<DataFrame shape={getattr(val, 'shape', '?')}>"
    else:
        results[key] = f"<{type(val).__name__}>"

with open(os.path.join(outdir, "results.json"), "w") as fh:
    json.dump(results, fh, default=str, indent=2)

# Emit the typed interchange bundle (model-joining Phase 1): the model's declared
# parameters (INPUT/CONSTANT/OUTPUT, each tagged with its classification), serialized in the
# language-neutral format so another model can consume them. Best-effort — a serialization
# failure becomes a warning, never a failed run.
if status["ok"] and PLAN.get("serialize_params"):
    try:
        import interchange  # ships alongside this wrapper (see engine._sync_runners)
        params = []
        for spec in PLAN["serialize_params"]:
            pid = spec.get("id")
            if pid in ns:
                params.append({**spec, "value": ns[pid]})
            elif (spec.get("classification") or "") == "OUTPUT":
                status["warnings"].append(
                    f"declared OUTPUT '{pid}' not found in the model namespace; skipped")
        _gen = {"tool": "FSKX Runner", "modelId": PLAN.get("model_id") or "",
                "runId": os.path.basename(outdir)}
        _, _w = interchange.write_bundle(params, outdir, generator_language="Python",
                                         generated_by=_gen)
        status["warnings"].extend(_w)
    except Exception as exc:  # noqa: BLE001
        status["warnings"].append("interchange bundle (outputs.json) not written: "
                                  + _fmt_exc(exc))

with open(os.path.join(outdir, "status.json"), "w") as fh:
    json.dump(status, fh, indent=2)

for w in status["warnings"]:
    sys.stderr.write(w + "\n")
if not status["ok"]:
    sys.stderr.write(status["error"] or "")
    sys.exit(1)
