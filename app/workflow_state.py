"""
workflow_state.py — per-workflow execution state (stateful-workflow Phase B).

Tracks, for one workflow (a saved pipeline id, or a client draft id), which node is currently
"executed" and with what cache identity:

    $FSKX_WORK_DIR/_workflow_state/<workflow_id>.json
    { "n1": {"run_id": "...", "cache_hash": "...", "ok": true, "executed_at": "..."}, ... }

The map is the source of truth for cache reuse in ``pipeline.execute_to``: a node is reused
(not re-run) iff its freshly computed ``cache_hash`` equals the recorded one and its run is
still present. It only **references** ``_results`` runs (by ``run_id``) — resetting a node
forgets the association, it never deletes artifacts, so Phase-A history stays intact.

Boundary with Phase C: this is deliberately a *side file* (an execution cache). Phase C
promotes it into the saved workflow document + the ``.fskxp`` export; Phase B keeps the
pipeline-definition format and archive untouched.

Pure stdlib; no Flask/engine import, so it is unit-testable in isolation (mirrors
``pipeline_store`` / ``pipeline_runs``).
"""

import json
import os
import time

WORK_DIR = os.environ.get("FSKX_WORK_DIR", "/tmp/fskx_work")
STATE_DIR = os.path.join(WORK_DIR, "_workflow_state")


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def safe_id(wid):
    """A workflow id must be a bare token (guards the file path against traversal)."""
    return bool(wid) and "/" not in wid and "\\" not in wid and ".." not in wid


def _path(wid):
    return os.path.join(STATE_DIR, wid + ".json")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load(wid):
    """Return the node-state map for a workflow (``{}`` if none / unreadable / unsafe id)."""
    if not safe_id(wid):
        return {}
    p = _path(wid)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}  # best-effort: a corrupt file degrades to "all stale"


def save(wid, state):
    """Persist a node-state map. Best-effort; returns the state (or it unchanged on failure)."""
    if not safe_id(wid):
        return state
    _ensure_dir()
    try:
        with open(_path(wid), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError:
        pass
    return state


def set_node(wid, node_id, run_id, cache_hash, ok=True):
    """Record (or overwrite) one node's executed state. Returns the updated map."""
    state = load(wid)
    state[node_id] = {"run_id": run_id, "cache_hash": cache_hash,
                      "ok": bool(ok), "executed_at": _now()}
    return save(wid, state)


def reset(wid, node_ids):
    """Forget the given node ids (caller passes the node + its downstream). Returns the
    updated map. Does NOT delete any _results artifacts."""
    state = load(wid)
    changed = False
    for nid in node_ids:
        if nid in state:
            del state[nid]
            changed = True
    return save(wid, state) if changed else state


def clear(wid):
    """Drop all execution state for a workflow (e.g. a force re-run from scratch)."""
    if not safe_id(wid):
        return False
    p = _path(wid)
    if os.path.exists(p):
        try:
            os.remove(p)
            return True
        except OSError:
            return False
    return False
