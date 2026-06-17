"""
Tiny persistent store for per-model UI status.

Records two progressive flags per model so the main page can show, at a glance,
how far a model has come: whether its environment was successfully prepared
("prepared" — a micromamba env built or an AI Docker image built) and whether it
has executed successfully at least once ("executed").

State is keyed by the model's .fskx filename and written as a small JSON file on
the persistent work volume, so it survives app restarts (like the env/image
cache). Nothing sensitive is ever stored here.
"""

import json
import os
import threading

WORK_DIR = os.environ.get("FSKX_WORK_DIR", "/tmp/fskx_work")
STATE_PATH = os.path.join(WORK_DIR, "_state", "model_status.json")

_LOCK = threading.Lock()


def _load():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, STATE_PATH)


def get(fskx):
    """Return {'prepared': bool, 'executed': bool} for one model.

    A successful execution implies the environment was prepared, so 'executed'
    forces 'prepared' True even if it was never recorded explicitly.
    """
    with _LOCK:
        rec = _load().get(fskx, {})
    executed = bool(rec.get("executed"))
    prepared = bool(rec.get("prepared")) or executed
    return {"prepared": prepared, "executed": executed}


def _mark(fskx, **flags):
    if not fskx:
        return
    with _LOCK:
        data = _load()
        rec = data.get(fskx, {})
        rec.update(flags)
        data[fskx] = rec
        _save(data)


def mark_prepared(fskx):
    """Record that this model's environment built successfully (env or image)."""
    _mark(fskx, prepared=True)


def mark_executed(fskx):
    """Record a successful run (also implies the environment is prepared)."""
    _mark(fskx, prepared=True, executed=True)


def clear(fskx):
    """Forget a model's status (e.g. after its env/image were removed)."""
    with _LOCK:
        data = _load()
        if fskx in data:
            del data[fskx]
            _save(data)


def clear_all():
    """Forget all recorded status."""
    with _LOCK:
        _save({})
