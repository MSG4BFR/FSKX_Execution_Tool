"""
Tests for pipeline_runs (stateful-workflow Phase A): execution-history records.

Run: python3 tests/test_pipeline_runs.py   (from the FSKX_Execution_Tool dir)

Uses a temp WORK_DIR via env, so nothing touches a real volume. No Docker needed.
"""

import os
import sys
import tempfile
import time

# Point storage at a throwaway dir BEFORE importing the module.
_WORK = tempfile.mkdtemp()
os.environ["FSKX_WORK_DIR"] = _WORK

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))
import pipeline_runs as pr  # noqa: E402

_failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def _pipe():
    return {"version": "1.0.0",
            "nodes": [{"id": "n1", "fskx": "A.fskx", "params": {}, "x": 10, "y": 20},
                      {"id": "n2", "fskx": "B.fskx", "params": {}, "x": 300, "y": 20}],
            "edges": [{"source": {"node": "n1", "param": "resp"},
                       "target": {"node": "n2", "param": "dose"}, "transform": None}],
            "shared": []}


def _ok_rec():
    """A successful two-node pipeline.run result (full exec dicts, as run() returns)."""
    return {
        "ok": True, "order": ["n1", "n2"],
        "results": {
            "n1": {"ok": True, "run_id": "20260624_141500", "fskx": "A.fskx",
                   "outdir": "/x/n1", "plots": ["p.png"], "files": ["o.csv"]},
            "n2": {"ok": True, "run_id": "20260624_141512", "fskx": "B.fskx",
                   "outdir": "/x/n2", "plots": [], "files": []},
        },
        "provenance": [{"target": {"node": "n2", "param": "dose"},
                        "source": {"node": "n1", "param": "resp"},
                        "source_run": "20260624_141500", "transform": None,
                        "value_hash": "abc123"}],
    }


def main():
    # --- build_record projects exec dicts down to per-node references (no artifact copy) ---
    rid = pr.new_run_id()
    rec = pr.build_record(rid, _pipe(), _ok_rec(), name="My run", pipeline_id="def456")
    check("build_record carries id/name/pipeline_id/ok",
          rec["run_id"] == rid and rec["name"] == "My run"
          and rec["pipeline_id"] == "def456" and rec["ok"] is True)
    check("build_record references node runs (fskx + run_id + phase)",
          rec["nodes"]["n1"] == {"fskx": "A.fskx", "run_id": "20260624_141500",
                                 "ok": True, "phase": "done"}
          and rec["nodes"]["n2"]["phase"] == "done")
    check("build_record copies NO plots/files into node refs",
          all("plots" not in v and "files" not in v and "outdir" not in v
              for v in rec["nodes"].values()))
    check("build_record keeps order + provenance + wiring snapshot",
          rec["order"] == ["n1", "n2"] and len(rec["provenance"]) == 1
          and rec["pipeline"]["nodes"][0]["x"] == 10)

    # --- save + load round-trip ---
    saved = pr.save_run(rec)
    check("save_run returns the record", saved and saved["run_id"] == rid)
    loaded = pr.load_run(rid)
    check("load_run round-trips the record",
          loaded and loaded["nodes"]["n1"]["run_id"] == "20260624_141500"
          and loaded["provenance"][0]["value_hash"] == "abc123")

    # --- a failed run records the failed node + outcome per node ---
    fail_rec = {
        "ok": False, "stage": "run", "failed_node": "n2", "order": ["n1", "n2"],
        "errors": ["node 'n2' failed: see its log"],
        "results": {
            "n1": {"ok": True, "run_id": "20260624_150000", "fskx": "A.fskx"},
            "n2": {"ok": False, "run_id": "20260624_150010", "fskx": "B.fskx"},
        },
        "provenance": [],
    }
    time.sleep(1)  # ensure a later, distinct timestamp id
    frid = pr.new_run_id()
    pr.save_run(pr.build_record(frid, _pipe(), fail_rec, name="Bad run"))
    floaded = pr.load_run(frid)
    check("failed run stores failed_node + per-node outcome",
          floaded["ok"] is False and floaded["failed_node"] == "n2"
          and floaded["nodes"]["n2"]["phase"] == "failed"
          and floaded["nodes"]["n1"]["phase"] == "done")

    # --- list: summaries, newest first ---
    lst = pr.list_runs()
    check("list_runs returns both, newest (failed) first",
          len(lst) == 2 and lst[0]["run_id"] == frid and lst[1]["run_id"] == rid)
    check("list summary carries name/ok/failed_node/n_nodes",
          lst[0]["name"] == "Bad run" and lst[0]["ok"] is False
          and lst[0]["failed_node"] == "n2" and lst[0]["n_nodes"] == 2)

    # --- name fallback for an unnamed draft run ---
    drid = pr.new_run_id()
    pr.save_run(pr.build_record(drid, _pipe(), _ok_rec()))
    check("unnamed run falls back to 'Unsaved draft'",
          pr.load_run(drid)["name"] == "Unsaved draft")

    # --- build_record_from_state: a manual snapshot of node-state (Phase B manual save) ---
    state = {"n1": {"run_id": "20260624_141500", "cache_hash": "h1", "ok": True,
                    "executed_at": "t"},
             "n2": {"run_id": "20260624_141512", "cache_hash": "h2", "ok": True,
                    "executed_at": "t"}}
    srid = pr.new_run_id()
    srec = pr.build_record_from_state(srid, _pipe(), state, order=["n1", "n2"], name="My snapshot")
    check("from_state: stage 'snapshot', ok, name",
          srec["stage"] == "snapshot" and srec["ok"] is True and srec["name"] == "My snapshot")
    check("from_state: nodes summary references run ids + fskx",
          srec["nodes"]["n1"]["run_id"] == "20260624_141500"
          and srec["nodes"]["n1"]["fskx"] == "A.fskx" and srec["nodes"]["n1"]["phase"] == "done")
    check("from_state: embeds raw node_state incl. cache_hash (for restore)",
          srec["node_state"]["n1"]["cache_hash"] == "h1"
          and srec["node_state"]["n2"]["run_id"] == "20260624_141512")
    pr.save_run(srec)
    check("from_state: round-trips with node_state",
          pr.load_run(srid)["node_state"]["n2"]["cache_hash"] == "h2")
    # a snapshot containing a failed node
    bad_state = {"n1": {"run_id": "r", "cache_hash": "h", "ok": False}}
    brec = pr.build_record_from_state(pr.new_run_id(), _pipe(), bad_state)
    check("from_state: failed node -> ok False + failed_node",
          brec["ok"] is False and brec["failed_node"] == "n1")

    # --- traversal guard ---
    check("rejects unsafe id",
          pr.load_run("../etc/passwd") is None and not pr.safe_id("a/b")
          and pr.save_run({"run_id": "a/b"}) is None)

    # --- back-compat: a record missing optional fields still loads ---
    import json
    pr._ensure_dir()
    with open(os.path.join(pr.RUNS_DIR, "legacy01.json"), "w", encoding="utf-8") as fh:
        json.dump({"run_id": "legacy01", "pipeline": {"nodes": []}}, fh)
    leg = pr.load_run("legacy01")
    check("legacy/partial record loads and lists",
          leg is not None and any(x["run_id"] == "legacy01" for x in pr.list_runs()))

    # --- delete ---
    check("delete_run removes the record",
          pr.delete_run(rid) and pr.load_run(rid) is None)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
