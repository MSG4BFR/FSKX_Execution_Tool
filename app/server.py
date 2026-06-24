"""
FSKX Runner — local web UI.

Lists the .fskx models in the mounted models folder, renders an auto-generated
parameter form per model, runs the model in its own environment (background thread),
and shows the resulting plot(s) plus downloadable outputs.

Also provides:
  - an online-repository browser to download new models at runtime, and
  - an AI-assisted environment builder (Claude → Dockerfile → per-model image) for
    models whose dependencies the standard build cannot satisfy.
"""

import os
import tempfile
import threading
import time
import uuid

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)

import aienv
import engine
import pipeline
import pipeline_runs
import pipeline_store
import repo
import workflow_state

app = Flask(__name__)

# In-memory job registry: job_id -> {state, message, result, error, kind}
JOBS = {}
JOBS_LOCK = threading.Lock()

# In-memory settings (never written to disk). Prefills the API key from the environment
# if the launcher provided one, but the UI field remains the source of truth.
SETTINGS = {
    # Prefilled from the environment (the launcher loads a local .env), with API_KEY
    # accepted as a friendly alias. Settings-UI edits override it for the session.
    "api_key": os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("API_KEY", ""),
    "model_id": os.environ.get("FSKX_CLAUDE_MODEL") or "claude-sonnet-4-6",
    # Which AI backend to use: "anthropic" (Claude, default) or "local" (an OpenAI-
    # compatible server such as LM Studio). Plus the local endpoint + model id.
    "provider": (os.environ.get("FSKX_AI_PROVIDER") or "anthropic").strip().lower(),
    "local_url": os.environ.get("FSKX_LOCAL_API_URL") or aienv.DEFAULT_LOCAL_URL,
    "local_model": os.environ.get("FSKX_LOCAL_MODEL", ""),
    # Result of verifying the active provider. None = not checked yet / in progress;
    # True/False once known. `api_key_status` is the human-readable reason.
    "api_key_valid": None,
    "api_key_status": "",
}


def _cfg():
    """Provider config for the AI calls, derived from the live SETTINGS."""
    return aienv.provider_config(SETTINGS)


def _ai_label():
    """Short, human-friendly name of the currently selected AI backend, used in the UI.

    "Claude" for the Anthropic backend; for a local model the bare model name with any
    org/path prefix dropped (e.g. "google/gemma-4-26b-a4b" -> "gemma-4-26b-a4b")."""
    cfg = _cfg()
    if cfg["provider"] == "local":
        name = cfg["local_model"] or "local model"
        return name.split("/")[-1]
    return "Claude"


@app.context_processor
def _inject_ai_label():
    """Make `ai_label` available to every template so UI copy reflects the active model."""
    return {"ai_label": _ai_label()}


def _api_key_usable():
    """Gate for AI features (chat, AI env builder), for whichever provider is selected.

    Usable only if the provider is plausibly configured AND verification hasn't failed.
    While the background check is still running (`api_key_valid is None`) a plausibly-
    configured provider passes optimistically so it isn't briefly blocked at startup; a
    confirmed failure (placeholder key, rejected key, unreachable endpoint) blocks it.
    """
    if not aienv.provider_usable(_cfg()):
        return False
    return SETTINGS.get("api_key_valid") is not False


def _verify_api_key_async():
    """Verify the active provider once, off the request path; store the outcome."""
    cfg = _cfg()
    if not aienv.provider_usable(cfg):
        SETTINGS["api_key_valid"] = False
        if cfg["provider"] == "local":
            SETTINGS["api_key_status"] = "No local endpoint URL set (Settings / .env)."
        else:
            SETTINGS["api_key_status"] = (
                "The configured key is still the .env.example placeholder (sk-ant-...). "
                "Add your real Anthropic key in .env or under Settings."
                if (cfg["api_key"] or "").strip() else "No API key set.")
        return
    SETTINGS["api_key_valid"] = None
    SETTINGS["api_key_status"] = ("Checking the local endpoint…"
                                  if cfg["provider"] == "local"
                                  else "Checking the API key…")
    ok, detail = aienv.verify_provider(cfg)
    SETTINGS["api_key_valid"] = ok
    SETTINGS["api_key_status"] = detail


def _start_key_check():
    threading.Thread(target=_verify_api_key_async, daemon=True).start()

# Last failure log and last generated Dockerfile per model, used to let the AI iterate.
LAST_ERROR = {}
LAST_DOCKERFILE = {}


def _new_job(kind):
    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS[job_id] = {"kind": kind, "state": "running", "message": "Starting…",
                        "result": None, "error": None}
    return job_id


def _set(job_id, **kw):
    with JOBS_LOCK:
        JOBS[job_id].update(kw)


def _progress(job_id):
    def fn(msg):
        with JOBS_LOCK:
            JOBS[job_id]["message"] = msg
    return fn


# ---------------------------------------------------------------------------
# Model list + parameters
# ---------------------------------------------------------------------------

def _local_entry(fskx, remote=None, image_tags=None):
    """A unified-list entry for a locally available model (downloaded or bundled).

    Reads the model's badges from its metadata and its live status (runnable /
    executed) from the actual cached artifacts. Falls back gracefully if the
    archive can't be read.
    """
    entry = {
        "kind": "local",
        "fskx": fskx,
        "downloaded": True,
        "id": (remote or {}).get("id"),
        "repository_name": (remote or {}).get("repository_name", ""),
        "upload_date": ((remote or {}).get("upload_date") or "")[:10],
    }
    # model_info extracts the archive; do it first so model_status reuses that dir.
    try:
        info = engine.model_info(fskx)
        entry.update({
            "name": info["name"], "language": info["language"],
            "version": info["version"], "hazard": info["hazard"],
            "product": info["product"], "n_params": len(info["fields"]),
        })
    except Exception as exc:  # noqa: BLE001
        entry.update({
            "name": (remote or {}).get("name") or fskx, "language": "?",
            "version": None, "hazard": "", "product": "", "n_params": 0,
            "error": str(exc),
        })
    status = engine.model_status(fskx, existing_image_tags=image_tags)
    entry["prepared"] = status["prepared"]
    entry["executed"] = status["executed"]
    return entry


@app.route("/")
def index():
    """Unified list: every local model plus the online catalogue, each tagged with
    its download / runnable / executed status. Online models that aren't downloaded
    yet are fetched on click."""
    remote = []
    remote_error = None
    try:
        remote = repo.list_remote()
    except Exception as exc:  # noqa: BLE001
        remote_error = str(exc)

    local_files = engine.list_models()
    matched = set()
    entries = []

    # One docker query for all existing per-model images; reused for every card so
    # runnable status reflects reality without a docker call per model.
    try:
        image_tags = engine.existing_model_images()
    except Exception:  # noqa: BLE001
        image_tags = set()

    # Catalogue entries: downloaded ones become full local cards, the rest stay remote.
    for m in remote:
        fname = repo.downloaded_filename(m["id"])
        if fname:
            matched.add(fname)
            entries.append(_local_entry(fname, remote=m, image_tags=image_tags))
        else:
            entries.append({
                "kind": "remote", "downloaded": False, "id": m["id"],
                "name": m["name"], "repository_name": m.get("repository_name", ""),
                "upload_date": (m.get("upload_date") or "")[:10],
            })

    # Local files with no catalogue match (bundled or manually added).
    for fname in local_files:
        if fname not in matched:
            entries.append(_local_entry(fname, image_tags=image_tags))

    # Downloaded/local first (alphabetical); undownloaded catalogue after (newest first).
    downloaded = sorted((e for e in entries if e["downloaded"]),
                        key=lambda e: e["name"].lower())
    not_downloaded = [e for e in entries if not e["downloaded"]]
    models = downloaded + not_downloaded

    return render_template("index.html", models=models,
                           remote_error=remote_error,
                           n_downloaded=len(downloaded), n_total=len(models),
                           api_key_set=_api_key_usable())


@app.route("/model/<path:fskx>")
def model(fskx):
    if fskx not in engine.list_models():
        abort(404)
    scenario = request.args.get("scenario")
    info = engine.model_info(fskx)
    if scenario and scenario in info["scenarios"]:
        info["fields"] = engine.fields_for_scenario(fskx, scenario)
        info["selected_scenario"] = scenario
    else:
        info["selected_scenario"] = info["scenarios"][0] if info["scenarios"] else None
    inputs = [f for f in info["fields"] if f["classification"] == "INPUT"]
    consts = [f for f in info["fields"] if f["classification"] != "INPUT"]
    return render_template("model.html", info=info, inputs=inputs, consts=consts,
                           docker=engine.docker_status()["docker_available"],
                           api_key_set=_api_key_usable())


# ---------------------------------------------------------------------------
# Running a model
# ---------------------------------------------------------------------------

def _run_job(job_id, fskx, scenario, values):
    try:
        result = engine.execute(fskx, scenario, values, progress=_progress(job_id))
        if not result["ok"]:
            LAST_ERROR[fskx] = result.get("log", "")
        _set(job_id, state=("done" if result["ok"] else "error"), result=result,
             message=("Finished." if result["ok"] else "Model did not complete."))
    except Exception as exc:  # noqa: BLE001
        import traceback
        LAST_ERROR[fskx] = traceback.format_exc()
        _set(job_id, state="error", error=traceback.format_exc(),
             message=f"Failed: {exc}")


@app.route("/run", methods=["POST"])
def run():
    fskx = request.form["fskx"]
    scenario = request.form.get("scenario")
    info = engine.model_info(fskx)
    fields = (engine.fields_for_scenario(fskx, scenario)
              if scenario and scenario in info["scenarios"] else info["fields"])
    values = {f["id"]: request.form["param__" + f["id"]]
              for f in fields if "param__" + f["id"] in request.form}

    job_id = _new_job("run")
    _set(job_id, fskx=fskx)
    threading.Thread(target=_run_job, args=(job_id, fskx, scenario, values),
                     daemon=True).start()
    return redirect(url_for("run_page", job_id=job_id))


@app.route("/run/<job_id>")
def run_page(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return render_template("run.html", job_id=job_id, job=job,
                           api_key_set=_api_key_usable(),
                           api_key_status=SETTINGS.get("api_key_status", ""))


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        payload = {"state": job["state"], "message": job["message"],
                   "kind": job.get("kind")}
        r = job.get("result")
        if r:
            payload.update({
                "ok": r.get("ok"), "plots": r.get("plots", []),
                "files": r.get("files", []), "language": r.get("language"),
                "scenario": r.get("scenario"), "backend": r.get("backend"),
                "log": (r.get("log") or "")[-8000:],
                "warnings": r.get("status", {}).get("warnings", []),
                "can_ai_fix": r.get("can_ai_fix", False),
                "fskx": r.get("fskx"),
                "run_id": r.get("run_id"),
            })
        if job.get("error"):
            payload["error"] = job["error"]
        # extra fields some jobs set directly
        for k in ("filename", "dockerfile", "tag", "build_log"):
            if k in job:
                payload[k] = job[k]
    return jsonify(payload)


@app.route("/result/<job_id>/file/<path:name>")
def result_file(job_id, name):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or not job.get("result"):
        abort(404)
    outdir = job["result"]["outdir"]
    path = os.path.normpath(os.path.join(outdir, name))
    if not path.startswith(os.path.normpath(outdir)) or not os.path.isfile(path):
        abort(404)
    # Display images/PDFs inline; everything else downloads.
    inline = name.lower().endswith(engine.IMAGE_EXTS)
    return send_file(path, as_attachment=not inline)


# ---------------------------------------------------------------------------
# Stored run history + comparison (read straight from the persisted volume)
# ---------------------------------------------------------------------------

@app.route("/model/<path:fskx>/runs")
def runs_page(fskx):
    if fskx not in engine.list_models():
        abort(404)
    info = engine.model_info(fskx)
    runs = engine.list_runs(fskx)
    return render_template("runs.html", fskx=fskx, name=info["name"], runs=runs,
                           api_key_set=_api_key_usable())


@app.route("/compare/<path:fskx>")
def compare_page(fskx):
    if fskx not in engine.list_models():
        abort(404)
    info = engine.model_info(fskx)
    wanted = [r for r in request.args.get("runs", "").split(",") if r]
    all_runs = {r["run_id"]: r for r in engine.list_runs(fskx)}
    chosen = [all_runs[r] for r in wanted if r in all_runs]

    # Union of parameter names across the chosen runs, marking which differ.
    param_names = []
    for r in chosen:
        for k in (r.get("params") or {}):
            if k not in param_names:
                param_names.append(k)
    param_rows = []
    for k in param_names:
        vals = [str((r.get("params") or {}).get(k, "")) for r in chosen]
        param_rows.append({"name": k, "cells": vals,
                           "differs": len(set(vals)) > 1})

    return render_template("compare.html", fskx=fskx, name=info["name"],
                           runs=chosen, param_rows=param_rows,
                           api_key_set=_api_key_usable())


@app.route("/runs/<path:fskx>/<run_id>")
def run_view_page(fskx, run_id):
    """Result page for a single stored run (read from the persisted volume)."""
    if fskx not in engine.list_models():
        abort(404)
    if not engine.run_dir(fskx, run_id):
        abort(404)
    info = engine.model_info(fskx)
    run = next((r for r in engine.list_runs(fskx) if r["run_id"] == run_id), None)
    if not run:
        abort(404)
    return render_template("run_view.html", fskx=fskx, name=info["name"], run=run,
                           api_key_set=_api_key_usable())


@app.route("/runs/<path:fskx>/<run_id>/file/<path:name>")
def run_stored_file(fskx, run_id, name):
    path = engine.run_file_path(fskx, run_id, name)
    if not path:
        abort(404)
    inline = name.lower().endswith(engine.IMAGE_EXTS)
    return send_file(path, as_attachment=not inline)


# ---------------------------------------------------------------------------
# Model joining — build and run a pipeline (DAG) of chained model runs
# ---------------------------------------------------------------------------

def _join_label(fskx):
    """A readable node-menu label from a filename (strip the __id8 suffix + extension)."""
    base = fskx[:-5] if fskx.lower().endswith(".fskx") else fskx
    if "__" in base:
        base = base.rsplit("__", 1)[0]
    return base.replace("_", " ").strip() or fskx


@app.route("/join")
def join_page():
    """Edge-first join builder: add models as nodes, wire output/input ports into edges,
    optionally with a unit transform, then validate and run the whole DAG."""
    models = [{"fskx": f, "label": _join_label(f)} for f in engine.list_models()]
    models.sort(key=lambda m: m["label"].lower())
    return render_template("join.html", models=models, api_key_set=_api_key_usable())


@app.route("/api/model-params/<path:fskx>")
def api_model_params(fskx):
    """Declared parameters of one model, for populating the builder's port menus."""
    if fskx not in engine.list_models():
        abort(404)
    try:
        specs = engine.model_param_specs(fskx)
        try:
            info = engine.model_info(fskx)
            name, language, fields = info["name"], info["language"], info["fields"]
        except Exception:  # noqa: BLE001
            name, language, fields = _join_label(fskx), "?", []
        return jsonify({"ok": True, "name": name, "language": language,
                        "params": specs, "fields": fields})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/pipeline/validate", methods=["POST"])
def api_pipeline_validate():
    p = request.get_json(force=True, silent=True) or {}
    try:
        errors, warnings = pipeline.validate(p)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"errors": [str(exc)], "warnings": []})
    return jsonify({"errors": errors, "warnings": warnings})


def _pipeline_exec_job(job_id, p, run_id, targets, workflow_id=None, force=False,
                       name=None, pipeline_id=None):
    """Run a (possibly partial) execution via the caching engine: only ``targets`` and their
    stale ancestors run, valid caches are reused. Persists the updated node-state map and a
    Phase-A history record."""
    def on_node(nid, phase, info):
        with JOBS_LOCK:
            nodes = JOBS[job_id].setdefault("nodes", {})
            nodes[nid] = {"phase": phase, **info}
    state = {} if force else workflow_state.load(workflow_id)
    try:
        rec = pipeline.execute_to(p, targets, state=state,
                                  progress=_progress(job_id), on_node=on_node)
    except Exception as exc:  # noqa: BLE001
        import traceback
        _set(job_id, state="error", error=traceback.format_exc(),
             message=f"Pipeline failed: {exc}")
        return
    # Persist the updated execution state *before* announcing done, so the UI never sees a
    # finished run whose node-state isn't yet readable. History records are NOT auto-saved —
    # the user saves a state snapshot explicitly (see /api/pipeline-runs/save). Best-effort.
    try:
        workflow_state.save(workflow_id, rec.get("state", state))
    except Exception:  # noqa: BLE001
        pass
    n_ran, n_reused = len(rec.get("ran", [])), len(rec.get("reused", []))
    if rec.get("ok"):
        msg = "Done — %d run, %d reused from cache." % (n_ran, n_reused)
    else:
        msg = "Pipeline failed: " + "; ".join(rec.get("errors", []) or ["see logs"])
    _set(job_id, state=("done" if rec.get("ok") else "error"), result=rec, message=msg)


def _start_exec(p, targets, workflow_id, force, name, pipeline_id):
    job_id = _new_job("pipeline")
    run_id = pipeline_runs.new_run_id()
    with JOBS_LOCK:
        JOBS[job_id]["nodes"] = {}
        JOBS[job_id]["pipeline_run_id"] = run_id
    threading.Thread(target=_pipeline_exec_job, args=(job_id, p, run_id, targets),
                     kwargs={"workflow_id": workflow_id, "force": force,
                             "name": name, "pipeline_id": pipeline_id}, daemon=True).start()
    return jsonify({"job_id": job_id, "pipeline_run_id": run_id})


@app.route("/api/pipeline/run", methods=["POST"])
def api_pipeline_run():
    """Cache-aware whole-graph run: every node is a target, so only stale nodes execute and
    valid caches are reused. ``force=true`` ignores the cache (re-run all from scratch)."""
    body = request.get_json(force=True, silent=True) or {}
    # Accept either a bare pipeline (legacy) or {pipeline, name, pipeline_id, workflow_id, force}.
    wrapped = "pipeline" in body
    p = (body.get("pipeline") if wrapped else body) or {}
    name = body.get("name") if wrapped else None
    pipeline_id = body.get("pipeline_id") if wrapped else None
    workflow_id = body.get("workflow_id") if wrapped else None
    force = bool(body.get("force")) if wrapped else False
    if not p.get("nodes"):
        return jsonify({"error": "Add at least one model node."}), 400
    errors, _warn = pipeline.validate(p)
    if errors:
        return jsonify({"error": "Cannot run: " + "; ".join(errors)}), 400
    targets = [n["id"] for n in p["nodes"]]
    return _start_exec(p, targets, workflow_id, force, name, pipeline_id)


@app.route("/api/pipeline/execute-node", methods=["POST"])
def api_pipeline_execute_node():
    """Execute one or more nodes 'up to here': run the targets and only their stale ancestors,
    reusing valid caches. Body: {pipeline, targets:[nid], workflow_id, name?, pipeline_id?}."""
    body = request.get_json(force=True, silent=True) or {}
    p = body.get("pipeline") or {}
    targets = body.get("targets") or []
    if not p.get("nodes"):
        return jsonify({"error": "Add at least one model node."}), 400
    if not targets:
        return jsonify({"error": "No node selected to execute."}), 400
    errors, _warn = pipeline.validate(p)
    if errors:
        return jsonify({"error": "Cannot run: " + "; ".join(errors)}), 400
    return _start_exec(p, targets, body.get("workflow_id"), bool(body.get("force")),
                       body.get("name"), body.get("pipeline_id"))


@app.route("/api/pipeline/reset-node", methods=["POST"])
def api_pipeline_reset_node():
    """Reset a node + everything downstream of it (forget their cached executions; artifacts in
    _results are NOT deleted). Body: {pipeline, node, workflow_id}."""
    body = request.get_json(force=True, silent=True) or {}
    p = body.get("pipeline") or {}
    node = body.get("node")
    if not node:
        return jsonify({"error": "No node given."}), 400
    targets = sorted(pipeline.descendants(p, [node]))
    workflow_state.reset(body.get("workflow_id"), targets)
    return jsonify({"ok": True, "reset": targets})


@app.route("/api/pipeline/plan", methods=["POST"])
def api_pipeline_plan():
    """Dry-run: per node, whether the next execute would reuse the cache, run, or is blocked —
    plus the executed-state summary (run ids) for canvas badges + result links. Body:
    {pipeline, workflow_id}."""
    body = request.get_json(force=True, silent=True) or {}
    p = body.get("pipeline") or {}
    wid = body.get("workflow_id")
    if not p.get("nodes"):
        return jsonify({"plan": {}, "state": {}})
    state = workflow_state.load(wid)
    fskx_of = {n["id"]: n.get("fskx") for n in p["nodes"]}
    try:
        pmap = pipeline.plan(p, state=state)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"plan": {}, "state": {}, "error": str(exc)})
    state_nodes = {nid: {"run_id": s.get("run_id"), "ok": s.get("ok"),
                         "fskx": fskx_of.get(nid)}
                   for nid, s in state.items()}
    return jsonify({"plan": pmap, "state": state_nodes})


@app.route("/api/pipeline/status/<job_id>")
def api_pipeline_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        payload = {"state": job["state"], "message": job["message"],
                   "nodes": job.get("nodes", {}),
                   "pipeline_run_id": job.get("pipeline_run_id")}
        r = job.get("result")
        if r:
            payload.update({
                "ok": r.get("ok"), "order": r.get("order", []),
                "provenance": r.get("provenance", []),
                "errors": r.get("errors", []), "failed_node": r.get("failed_node"),
                "ran": r.get("ran", []), "reused": r.get("reused", []),
            })
            payload["results"] = {
                nid: {"ok": res.get("ok"), "run_id": res.get("run_id"),
                      "fskx": res.get("fskx"), "plots": res.get("plots", []),
                      "files": res.get("files", [])}
                for nid, res in (r.get("results") or {}).items()
            }
        if job.get("error"):
            payload["error"] = job["error"]
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Saved states — durable, *manually saved* snapshots of a workflow's node executions
# (stateful-workflow Phase A, now manual per user request). A record references the per-node
# model runs in _results AND carries the raw node-state (cache hashes) so loading one restores
# the working state — caching/reset keep functioning. See model-joining/phase-A-run-history.md.
# ---------------------------------------------------------------------------

@app.route("/api/pipeline-runs", methods=["GET"])
def api_pipeline_runs_list():
    return jsonify(pipeline_runs.list_runs())


@app.route("/api/pipeline-runs/save", methods=["POST"])
def api_pipeline_runs_save():
    """Manually save the current workflow's node-state as a named snapshot."""
    body = request.get_json(force=True, silent=True) or {}
    p = body.get("pipeline") or {}
    wid = body.get("workflow_id")
    if not p.get("nodes"):
        return jsonify({"error": "Nothing to save — add at least one node."}), 400
    state = workflow_state.load(wid)
    if not state:
        return jsonify({"error": "Nothing to save yet — run or execute some nodes first."}), 400
    try:
        order = pipeline.topo_order(p)
    except Exception:  # noqa: BLE001
        order = [n["id"] for n in p["nodes"]]
    rec = pipeline_runs.build_record_from_state(
        pipeline_runs.new_run_id(), p, state, order=order,
        name=body.get("name"), pipeline_id=body.get("pipeline_id"))
    pipeline_runs.save_run(rec)
    return jsonify({"ok": True, "run_id": rec["run_id"], "name": rec["name"]})


@app.route("/api/pipeline-runs/<run_id>", methods=["GET"])
def api_pipeline_runs_get(run_id):
    rec = pipeline_runs.load_run(run_id)
    if not rec:
        abort(404)
    return jsonify(rec)


@app.route("/api/pipeline-runs/<run_id>/delete", methods=["POST"])
def api_pipeline_runs_delete(run_id):
    return jsonify({"ok": pipeline_runs.delete_run(run_id)})


@app.route("/api/pipeline/restore-state", methods=["POST"])
def api_pipeline_restore_state():
    """Restore a saved node-state map into a workflow (used when loading a saved state), so the
    canvas shows the executed nodes and cache reuse keeps working. Body: {workflow_id, state}."""
    body = request.get_json(force=True, silent=True) or {}
    wid = body.get("workflow_id")
    if not wid:
        return jsonify({"error": "no workflow id"}), 400
    workflow_state.save(wid, body.get("state") or {})
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Saved pipelines — durable, named join configurations (+ shareable archives)
# ---------------------------------------------------------------------------

@app.route("/api/pipelines", methods=["GET"])
def api_pipelines_list():
    return jsonify(pipeline_store.list_all())


@app.route("/api/pipelines", methods=["POST"])
def api_pipelines_save():
    d = request.get_json(force=True, silent=True) or {}
    p = d.get("pipeline") or {}
    if not p.get("nodes"):
        return jsonify({"error": "Nothing to save — add at least one node."}), 400
    rec = pipeline_store.save(p, d.get("name"), d.get("id"))
    return jsonify({"id": rec["id"], "name": rec["name"], "modified": rec["modified"]})


@app.route("/api/pipelines/<pid>", methods=["GET"])
def api_pipelines_get(pid):
    rec = pipeline_store.load(pid)
    if not rec:
        abort(404)
    return jsonify(rec)


@app.route("/api/pipelines/<pid>/delete", methods=["POST"])
def api_pipelines_delete(pid):
    return jsonify({"ok": pipeline_store.delete(pid)})


@app.route("/api/pipelines/<pid>/export")
def api_pipelines_export(pid):
    res = pipeline_store.build_archive(pid, models_dir=engine.MODELS_DIR)
    if not res:
        abort(404)
    tmp, name = res
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "pipeline"
    return send_file(tmp, as_attachment=True,
                     download_name=safe + pipeline_store.ARCHIVE_EXT)


@app.route("/api/pipelines/import", methods=["POST"])
def api_pipelines_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded."}), 400
    fd, tmp = tempfile.mkstemp(suffix=pipeline_store.ARCHIVE_EXT)
    os.close(fd)
    f.save(tmp)
    try:
        rec = pipeline_store.import_archive(tmp, models_dir=engine.MODELS_DIR)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Could not import: {exc}"}), 400
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return jsonify({"id": rec["id"], "name": rec["name"]})


# ---------------------------------------------------------------------------
# Online repository
# ---------------------------------------------------------------------------

@app.route("/repository")
def repository():
    # The online catalogue now lives on the main page; keep this path working.
    return redirect(url_for("index"))


def _download_job(job_id, model_id, name):
    try:
        fname = repo.download(model_id, name, progress=_progress(job_id))
        _set(job_id, state="done", filename=fname, message=f"Downloaded {fname}")
    except Exception as exc:  # noqa: BLE001
        _set(job_id, state="error", error=str(exc), message=f"Download failed: {exc}")


@app.route("/repository/download", methods=["POST"])
def repository_download():
    model_id = request.form["id"]
    name = request.form.get("name", "model")
    job_id = _new_job("download")
    threading.Thread(target=_download_job, args=(job_id, model_id, name),
                     daemon=True).start()
    return redirect(url_for("download_page", job_id=job_id))


@app.route("/download/<job_id>")
def download_page(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return render_template("download.html", job_id=job_id, job=job)


# ---------------------------------------------------------------------------
# Cleanup — remove cached environments / images to reclaim disk
# ---------------------------------------------------------------------------

def _cleanup_job(job_id, fskx=None):
    try:
        if fskx:
            summary = engine.cleanup_model(fskx, progress=_progress(job_id))
            removed = summary["removed"]
            msg = ("Removed " + ", ".join(removed) + "." if removed else
                   "Nothing to remove — no cached image or environment for this model.")
        else:
            summary = engine.cleanup_all(progress=_progress(job_id))
            n_img, n_env, n_res = (len(summary["images"]), len(summary["envs"]),
                                   summary["results"])
            msg = (f"Removed {n_img} image(s), {n_env} environment(s) and "
                   f"{n_res} stored run result(s)."
                   if (n_img or n_env or n_res) else
                   "Nothing to remove — all caches were already clear.")
        _set(job_id, state="done", message=msg)
    except Exception as exc:  # noqa: BLE001
        _set(job_id, state="error", error=str(exc), message=f"Cleanup failed: {exc}")


@app.route("/cleanup/model", methods=["POST"])
def cleanup_model_route():
    fskx = request.form["fskx"]
    job_id = _new_job("cleanup")
    threading.Thread(target=_cleanup_job, args=(job_id, fskx), daemon=True).start()
    return redirect(url_for("cleanup_page", job_id=job_id))


@app.route("/cleanup/all", methods=["POST"])
def cleanup_all_route():
    job_id = _new_job("cleanup")
    threading.Thread(target=_cleanup_job, args=(job_id, None), daemon=True).start()
    return redirect(url_for("cleanup_page", job_id=job_id))


@app.route("/cleanup/job/<job_id>")
def cleanup_page(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return render_template("cleanup.html", job_id=job_id, job=job)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings():
    saved = False
    if request.method == "POST":
        provider = request.form.get("provider", "anthropic").strip().lower()
        SETTINGS["provider"] = provider if provider in ("anthropic", "local") else "anthropic"
        SETTINGS["api_key"] = request.form.get("api_key", "").strip()
        SETTINGS["model_id"] = request.form.get("model_id", "").strip() or "claude-sonnet-4-6"
        SETTINGS["local_url"] = (request.form.get("local_url", "").strip()
                                 or aienv.DEFAULT_LOCAL_URL)
        SETTINGS["local_model"] = request.form.get("local_model", "").strip()
        # Re-verify synchronously here so the page can show the result immediately.
        _verify_api_key_async()
        saved = True
    return render_template("settings.html", settings=SETTINGS, saved=saved,
                           api_key_valid=SETTINGS.get("api_key_valid"),
                           api_key_status=SETTINGS.get("api_key_status", ""),
                           docker=engine.docker_status()["docker_available"])


# ---------------------------------------------------------------------------
# AI-assisted environment
# ---------------------------------------------------------------------------

@app.route("/ai/<path:fskx>")
def ai_page(fskx):
    if fskx not in engine.list_models():
        abort(404)
    info = engine.model_info(fskx)
    return render_template("ai.html", fskx=fskx, name=info["name"],
                           language=info["language"],
                           docker=engine.docker_status()["docker_available"],
                           api_key_set=_api_key_usable(),
                           model_id=_cfg()["local_model"] if SETTINGS["provider"] == "local"
                                    else SETTINGS["model_id"],
                           has_error=bool(LAST_ERROR.get(fskx)))


def _ai_generate_job(job_id, fskx):
    try:
        tag, dockerfile = engine.ai_generate_dockerfile(
            fskx, _cfg(),
            error_log=LAST_ERROR.get(fskx, ""),
            prev_dockerfile=LAST_DOCKERFILE.get(fskx, ""))
        LAST_DOCKERFILE[fskx] = dockerfile
        _set(job_id, state="done", dockerfile=dockerfile, tag=tag,
             message="Dockerfile generated.")
    except Exception as exc:  # noqa: BLE001
        _set(job_id, state="error", error=str(exc), message=f"Generation failed: {exc}")


@app.route("/api/ai/generate", methods=["POST"])
def ai_generate():
    fskx = request.form["fskx"]
    if not _api_key_usable():
        return jsonify({"error": SETTINGS.get("api_key_status")
                        or "No usable API key. Add one under Settings."}), 400
    job_id = _new_job("ai_generate")
    threading.Thread(target=_ai_generate_job, args=(job_id, fskx), daemon=True).start()
    return jsonify({"job_id": job_id})


def _ai_build_job(job_id, fskx, dockerfile):
    try:
        LAST_DOCKERFILE[fskx] = dockerfile  # remember what was actually built/attempted
        ok, tag, log_path = engine.ai_build_image(fskx, dockerfile,
                                                  progress=_progress(job_id))
        log = ""
        try:
            log = open(log_path, encoding="utf-8", errors="ignore").read()
        except OSError:
            pass
        if not ok:
            # Feed the build failure back so the next "Generate" can iterate on it.
            LAST_ERROR[fskx] = log[-4000:]
        _set(job_id, state=("done" if ok else "error"), tag=tag,
             build_log=log[-8000:],
             message=("Image built — you can run the model now." if ok
                      else "Image build failed. See the log, then regenerate to fix it."))
    except Exception as exc:  # noqa: BLE001
        _set(job_id, state="error", error=str(exc), message=f"Build failed: {exc}")


@app.route("/api/ai/build", methods=["POST"])
def ai_build():
    fskx = request.form["fskx"]
    dockerfile = request.form["dockerfile"]
    job_id = _new_job("ai_build")
    threading.Thread(target=_ai_build_job, args=(job_id, fskx, dockerfile),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


# ---------------------------------------------------------------------------
# Talk to your model — grounded chat over the archive + stored results
# ---------------------------------------------------------------------------

@app.route("/chat/<path:fskx>")
def chat_page(fskx):
    if fskx not in engine.list_models():
        abort(404)
    info = engine.model_info(fskx)
    return render_template("chat.html", fskx=fskx, name=info["name"],
                           n_runs=len(engine.list_runs(fskx)),
                           api_key_set=_api_key_usable(),
                           api_key_status=SETTINGS.get("api_key_status", ""),
                           model_id=_cfg()["local_model"] if SETTINGS["provider"] == "local"
                                    else SETTINGS["model_id"])


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    fskx = data.get("fskx")
    messages = data.get("messages") or []
    run_ids = data.get("run_ids") or None
    if not fskx or fskx not in engine.list_models():
        return jsonify({"error": "Unknown model."}), 404
    if not _api_key_usable():
        return jsonify({"error": SETTINGS.get("api_key_status")
                        or "No usable AI backend. Configure one under Settings."}), 400
    if not messages:
        return jsonify({"error": "No message to send."}), 400
    try:
        reply = engine.chat_about_model(fskx, messages, _cfg(), run_ids=run_ids)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    return jsonify({"reply": reply})


# ---------------------------------------------------------------------------
# Quit — stop the server (and, since the container runs with --rm, the whole
# container) straight from the UI, so users don't have to hunt for the terminal
# window or force-stop the container in Docker Desktop.
# ---------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    """Cheap readiness probe the launchers poll before opening the browser (so the
    user never lands on an ERR_EMPTY_RESPONSE page while the server is still starting)."""
    return "ok", 200, {"Content-Type": "text/plain"}


def _shutdown_later():
    # Brief delay so the HTTP response is flushed to the browser before we exit.
    time.sleep(0.4)
    os._exit(0)


@app.route("/api/quit", methods=["POST"])
def api_quit():
    threading.Thread(target=_shutdown_later, daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # Verify the API key once at startup, in the background, so the UI reflects whether
    # Claude is actually reachable (and the .env.example placeholder is caught) without
    # delaying the first page load.
    _start_key_check()
    app.run(host="0.0.0.0", port=port, threaded=True)
