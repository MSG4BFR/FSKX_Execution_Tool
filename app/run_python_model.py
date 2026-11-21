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
import hashlib
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


saved_figures = []
figure_failures = []
figure_states = []
figure_exports = []
figure_captures = []
capturing_figures = []
collected_paths = {}
IMAGE_EXTS = ('.png', '.svg', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.pdf')


def _inside(path, root):
    path, root = os.path.abspath(path), os.path.abspath(root)
    if not path.startswith(root + os.sep):
        return False
    current = root
    for part in os.path.relpath(path, root).split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return False
    return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def _image_hash(path):
    if (not os.path.isfile(path) or os.path.islink(path)
            or not 0 < os.path.getsize(path) <= 64 * 1024 * 1024):
        return None
    digest, size = hashlib.sha256(), 0
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            size += len(block)
            if size > 64 * 1024 * 1024:
                return None
            digest.update(block)


def _figure_state(figure):
    for state in figure_states:
        if state['figure'] is figure:
            return state
    state = {'figure': figure, 'revision': 0, 'saving': 0, 'monitored': False}
    if len(figure_states) >= 64:
        return state
    figure_states.append(state)
    if hasattr(figure, 'stale_callback'):
        previous = figure.stale_callback

        def changed(artist, value):
            if value and not state['saving']:
                state['revision'] += 1
            if previous is not None:
                previous(artist, value)

        try:
            figure.stale_callback = changed
            state.update(monitored=True, callback=changed)
        except Exception:
            pass
    return state


def _record_export(figure, destination, options, state):
    if not state['monitored'] or len(figure_exports) >= 256:
        return
    if not isinstance(destination, (str, bytes)):
        method = getattr(destination, '__fspath__', None)
        if not callable(method):
            return  # File-like streams do not prove a collectible export exists.
        destination = method()
    destination = os.fsdecode(destination)
    extension = os.path.splitext(destination)[1].lower()
    if not extension and options.get('format') is None:
        destination += '.' + str(matplotlib.rcParams['savefig.format'])
        extension = os.path.splitext(destination)[1].lower()
    expected = str(options.get('format') or extension.lstrip('.')).lower()
    if expected == 'jpg': expected = 'jpeg'
    actual = extension.lstrip('.')
    if actual == 'jpg': actual = 'jpeg'
    path = os.path.abspath(destination)
    if (extension not in IMAGE_EXTS or actual != expected
            or not (_inside(path, workdir) or _inside(path, outdir))):
        return
    digest = _image_hash(path)
    if digest:
        figure_exports.append({'figure': figure, 'path': path, 'sha256': digest,
                               'revision': state['revision']})


def _save_figure(figure, number=0):
    # Keep object references: pyplot can reuse number 1 after the first figure closes.
    if any(previous is figure for previous in saved_figures + capturing_figures):
        return
    if len(saved_figures) >= 64:
        if "Figure capture limit (64) reached" not in status["warnings"]:
            status["warnings"].append("Figure capture limit (64) reached")
        return
    # A private capture directory avoids collisions with model-authored plot_N.png.
    directory = os.path.join(outdir, 'automatic')
    index = len(saved_figures) + 1
    filename = 'automatic/plot_{}.png'.format(index)
    while os.path.exists(os.path.join(outdir, filename)):
        index += 1
        filename = 'automatic/plot_{}.png'.format(index)
    state = _figure_state(figure)
    revision = state['revision']
    tracked = state['monitored'] and getattr(figure, 'stale_callback', None) is state.get('callback')
    capturing_figures.append(figure)
    try:
        state['saving'] += 1
        if not _inside(os.path.join(outdir, filename), outdir):
            raise ValueError('Unsafe automatic plot path')
        os.makedirs(directory, exist_ok=True)
        original_savefig(figure, os.path.join(outdir, filename), dpi=120, bbox_inches="tight")
        status["plots"].append(filename)
        saved_figures.append(figure)
        figure_captures.append({'figure': figure, 'filename': filename,
                                'revision': revision, 'tracked': tracked})
    except Exception as exc:
        figure_failures.append({'figure': figure, 'revision': revision, 'tracked': tracked,
                                'message': "figure {} save failed: {}".format(number, exc)})
    finally:
        state['saving'] -= 1
        capturing_figures.remove(figure)


if have_mpl:
    from matplotlib._pylab_helpers import Gcf
    from matplotlib.figure import Figure
    original_savefig = Figure.savefig
    original_close = plt.close

    def _save_with_export_record(figure, *args, **kwargs):
        destination = args[0] if args else kwargs.get('fname')
        state = _figure_state(figure)
        state['saving'] += 1
        try:
            result = original_savefig(figure, *args, **kwargs)
        finally:
            state['saving'] -= 1
        try:
            _record_export(figure, destination, kwargs, state)
        except Exception:
            pass  # Export bookkeeping must not turn a successful model save into failure.
        return result

    Figure.savefig = _save_with_export_record

    def _close_with_capture(figure=None):
        managers = Gcf.get_all_fig_managers()
        if isinstance(figure, str) and figure == "all":
            selected = managers
        elif figure is None:
            active = Gcf.get_active()
            selected = [active] if active is not None else []
        elif hasattr(figure, "savefig"):
            selected = [manager for manager in managers if manager.canvas.figure is figure]
            if not selected:
                _save_figure(figure, getattr(figure, "number", 0))
        else:
            selected = [manager for manager in managers if manager.num == figure
                        or (isinstance(figure, str) and manager.canvas.figure.get_label() == figure)]
        for manager in selected:
            _save_figure(manager.canvas.figure, manager.num)
        return original_close(figure)

    plt.close = _close_with_capture


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

# Capture open figures using the same path as explicit pyplot.close calls.
if have_mpl:
    for manager in Gcf.get_all_fig_managers():
        _save_figure(manager.canvas.figure, manager.num)

# Collect output files the model created or overwrote during the run (e.g. CSVs, and
# images/files written into subfolders), preserving their relative paths under outdir.
import shutil
for rel, src in _walk_rel(workdir):
    if rel in SCRIPT_FILES:
        continue
    try:
        prev = before_mtimes.get(rel)
        tracked_export = any(item['path'] == os.path.abspath(src) for item in figure_exports)
        if prev is not None and os.path.getmtime(src) == prev and not tracked_export:
            continue  # untouched input file
        dest = os.path.join(outdir, rel)
        if rel == "plot-selection.json" or (rel in status["plots"] and os.path.isfile(dest)):
            # Preserve both the captured figure and an explicit model-written image.
            rel = "model-files/" + rel
            dest = os.path.join(outdir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        collected_paths[os.path.abspath(src)] = rel
        status["files"].append(rel)
        if rel.lower().endswith((".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf")) and rel not in status["plots"]:
            status["plots"].append(rel)
    except Exception:
        pass

# Keep captures as evidence, but prefer a verified model export of the SAME
# figure revision for presentation. Check after collection, so overwritten or
# deleted exports cannot accidentally suppress the remaining fallback.
selection = {'schema_version': 1, 'preference': 'model_export', 'pairs': []}


def _usable_export(capture):
    if not capture['tracked']:
        return None
    for exported in reversed(figure_exports):
        if exported['figure'] is not capture['figure'] or exported['revision'] != capture['revision']:
            continue
        relative = collected_paths.get(exported['path'])
        if relative is None and _inside(exported['path'], outdir):
            relative = os.path.relpath(exported['path'], outdir).replace(os.sep, '/')
        if not relative or relative == capture.get('filename'):
            continue
        path = os.path.join(outdir, relative)
        try:
            if not _inside(path, outdir) or _image_hash(path) != exported['sha256']:
                continue
        except OSError:
            continue
        return exported, relative
    return None


for capture in figure_captures:
    automatic = capture['filename']
    usable = _usable_export(capture)
    if usable:
        exported, relative = usable
        try:
            automatic_hash = _image_hash(os.path.join(outdir, automatic))
        except OSError:
            automatic_hash = None
        if not automatic_hash:
            continue
        selection['pairs'].append({'automatic': automatic, 'export': relative,
            'automatic_sha256': automatic_hash, 'export_sha256': exported['sha256']})
        status['plots'] = [name for name in status['plots'] if name != automatic]
        if relative not in status['plots']: status['plots'].append(relative)
        if automatic not in status['files']: status['files'].append(automatic)
for failure in figure_failures:
    status['stdout_note'] += failure['message'] + '\n'
    usable = _usable_export(failure)
    if usable:
        relative = usable[1]
        if relative not in status['plots']: status['plots'].append(relative)
        status['warnings'].append('Automatic plot backup unavailable; retained the verified model export.')
    else:
        status['warnings'].append(failure['message'])
with open(os.path.join(outdir, 'plot-selection.json'), 'w', encoding='utf-8') as stream:
    json.dump(selection, stream, sort_keys=True)


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
