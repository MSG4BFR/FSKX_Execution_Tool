"""
pipeline_runs.py — durable history of pipeline EXECUTIONS (stateful-workflow Phase A).

A pipeline *definition* (the wiring) is stored by ``pipeline_store``. This module stores a
record of each *execution* of such a pipeline, so a past run can be listed and reloaded onto
the canvas — replaying its finished status, provenance, and links to each node's results.

A run record is a small JSON file on the persistent work volume:

    $FSKX_WORK_DIR/_pipeline_runs/<run_id>.json

parallel to ``_pipelines`` (definitions) and ``_results`` (per-node model runs). The record
only **references** the per-node runs (by ``fskx`` + ``run_id``) — it never copies plots or
output files, which stay owned by ``_results`` and remain reachable via ``/runs/<fskx>/<id>``.
It embeds a snapshot of the wiring so a reload is faithful even if the saved definition later
changes or is deleted.

Pure stdlib; no Flask/engine import, so it is unit-testable in isolation (mirrors
``pipeline_store``). This is the storage groundwork the live node-state phases (B–C) build on:
the ``nodes: {nid: {fskx, run_id, ...}}`` association here is exactly what Phase C will promote
from an immutable snapshot into current, mutable, saved-with-workflow node state.
"""

import json
import os
import time
import uuid

WORK_DIR = os.environ.get("FSKX_WORK_DIR", "/tmp/fskx_work")
RUNS_DIR = os.path.join(WORK_DIR, "_pipeline_runs")


def _ensure_dir():
    os.makedirs(RUNS_DIR, exist_ok=True)


def safe_id(rid):
    """A stored id must be a bare token (guards the file path against traversal)."""
    return bool(rid) and "/" not in rid and "\\" not in rid and ".." not in rid


def _path(rid):
    return os.path.join(RUNS_DIR, rid + ".json")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_run_id():
    """A sortable, collision-safe id: timestamp + short uuid (two runs can finish in the
    same second, so the uuid suffix disambiguates)."""
    return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]


def build_record(run_id, pipeline_def, rec, name=None, pipeline_id=None, created=None):
    """Assemble a run-history record from a ``pipeline.run`` result ``rec`` plus the submitted
    pipeline definition. Projects ``rec["results"]`` (full exec dicts) down to per-node
    references — ``{fskx, run_id, ok, phase}`` — so nothing heavy is duplicated. Only the
    per-node outcome and ``failed_node`` are stored; the UI derives blocked/skipped, so a
    snapshot and a live run render identically."""
    results = rec.get("results") or {}
    nodes = {}
    for nid, res in results.items():
        ok = bool(res.get("ok"))
        nodes[nid] = {
            "fskx": res.get("fskx"),
            "run_id": res.get("run_id"),
            "ok": ok,
            "phase": "done" if ok else "failed",
        }
    return {
        "run_id": run_id,
        "created": created or _now(),
        "name": (name or "Unsaved draft"),
        "pipeline_id": pipeline_id,
        "ok": bool(rec.get("ok")),
        "stage": rec.get("stage", "run"),
        "failed_node": rec.get("failed_node"),
        "errors": rec.get("errors", []),
        "order": rec.get("order", []),
        "pipeline": pipeline_def or {},
        "nodes": nodes,
        "provenance": rec.get("provenance", []),
    }


def build_record_from_state(run_id, pipeline_def, state, order=None, name=None,
                            pipeline_id=None, created=None):
    """Assemble a record from a *saved workflow state* (a manual snapshot) rather than a single
    execution. Carries the Phase-A ``nodes`` summary (for replay badges + result links) AND the
    raw ``node_state`` map (incl. ``cache_hash``) so loading it can restore the working state so
    caching/reset keep functioning. See model-joining/phase-A-run-history.md (manual save)."""
    fskx_of = {n["id"]: n.get("fskx") for n in (pipeline_def or {}).get("nodes", [])}
    nodes, all_ok, failed = {}, True, None
    for nid, s in (state or {}).items():
        ok = bool(s.get("ok"))
        nodes[nid] = {"fskx": fskx_of.get(nid), "run_id": s.get("run_id"),
                      "ok": ok, "phase": "done" if ok else "failed"}
        if not ok and failed is None:
            failed = nid
        all_ok = all_ok and ok
    return {
        "run_id": run_id, "created": created or _now(),
        "name": (name or "Saved state"), "pipeline_id": pipeline_id,
        "ok": all_ok, "stage": "snapshot", "failed_node": failed,
        "errors": [], "order": order or [], "pipeline": pipeline_def or {},
        "nodes": nodes, "node_state": dict(state or {}), "provenance": [],
    }


def save_run(record):
    """Persist a run record (must carry a ``run_id``). Returns the record, or None if the id
    is unusable. Best-effort on write errors (returns None)."""
    rid = record.get("run_id")
    if not (rid and safe_id(rid)):
        return None
    record.setdefault("created", _now())
    _ensure_dir()
    try:
        with open(_path(rid), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
    except OSError:
        return None
    return record


def load_run(rid):
    if not safe_id(rid):
        return None
    p = _path(rid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def list_runs():
    """Summaries of all stored runs, newest first (ids are timestamp-prefixed, so a plain
    reverse sort is chronological)."""
    _ensure_dir()
    out = []
    for fn in os.listdir(RUNS_DIR):
        if not fn.endswith(".json"):
            continue
        rec = load_run(fn[:-5])
        if not rec:
            continue
        out.append({
            "run_id": rec.get("run_id"),
            "created": rec.get("created"),
            "name": rec.get("name", "Unsaved draft"),
            "pipeline_id": rec.get("pipeline_id"),
            "ok": rec.get("ok"),
            "failed_node": rec.get("failed_node"),
            "n_nodes": len(rec.get("pipeline", {}).get("nodes", [])),
        })
    out.sort(key=lambda r: r.get("run_id") or "", reverse=True)
    return out


def delete_run(rid):
    if not safe_id(rid):
        return False
    p = _path(rid)
    if os.path.exists(p):
        try:
            os.remove(p)
            return True
        except OSError:
            return False
    return False
