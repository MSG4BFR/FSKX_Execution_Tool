"""
Tests for workflow_state (Phase B): per-workflow node execution state.

Run: python3 tests/test_workflow_state.py   (from the FSKX_Execution_Tool dir)

Uses a temp WORK_DIR via env, so nothing touches a real volume. No Docker needed.
"""

import os
import sys
import tempfile

os.environ["FSKX_WORK_DIR"] = tempfile.mkdtemp()

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))
import workflow_state as ws  # noqa: E402

_failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def main():
    wid = "wf123"
    check("empty workflow loads as {}", ws.load(wid) == {})

    # set_node records run id + hash + ok + timestamp
    ws.set_node(wid, "n1", "run_A1", "hashA", ok=True)
    ws.set_node(wid, "n2", "run_B1", "hashB", ok=True)
    st = ws.load(wid)
    check("set_node records run_id + cache_hash + ok",
          st["n1"]["run_id"] == "run_A1" and st["n1"]["cache_hash"] == "hashA"
          and st["n1"]["ok"] is True and "executed_at" in st["n1"])
    check("two nodes present", set(st) == {"n1", "n2"})

    # overwrite a node
    ws.set_node(wid, "n1", "run_A2", "hashA2", ok=True)
    check("set_node overwrites", ws.load(wid)["n1"]["run_id"] == "run_A2")

    # reset forgets only the given ids (caller passes node + downstream)
    ws.set_node(wid, "n3", "run_C1", "hashC", ok=True)
    ws.reset(wid, ["n2", "n3"])
    st2 = ws.load(wid)
    check("reset forgets the given nodes, keeps the rest",
          set(st2) == {"n1"} and "n2" not in st2 and "n3" not in st2)

    # reset is non-destructive of unrelated nodes / idempotent on missing ids
    ws.reset(wid, ["does_not_exist"])
    check("reset of a missing id is a no-op", set(ws.load(wid)) == {"n1"})

    # persistence across a fresh load (simulates a new request / restart)
    check("state persists on disk", ws.load(wid)["n1"]["run_id"] == "run_A2")

    # workflow isolation
    ws.set_node("other_wf", "n1", "x", "y")
    check("workflows are isolated",
          ws.load("other_wf")["n1"]["run_id"] == "x"
          and ws.load(wid)["n1"]["run_id"] == "run_A2")

    # traversal guard
    check("unsafe id rejected",
          ws.load("../etc/passwd") == {} and not ws.safe_id("a/b")
          and ws.save("../x", {"n": 1}) == {"n": 1})  # save no-ops but returns state

    # corrupt file degrades to {} (best-effort -> all stale)
    ws._ensure_dir()
    with open(ws._path("corrupt"), "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    check("corrupt state file degrades to {}", ws.load("corrupt") == {})

    # clear drops the whole workflow
    check("clear removes the workflow file",
          ws.clear(wid) and ws.load(wid) == {})

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
