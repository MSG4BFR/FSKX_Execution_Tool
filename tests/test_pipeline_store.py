"""
Tests for pipeline_store (Phase 4): CRUD + composite archive round-trip.

Run: python3 tests/test_pipeline_store.py   (from the FSKX_Execution_Tool dir)

Uses temp WORK_DIR / MODELS_DIR via env, so nothing touches a real volume.
"""

import os
import sys
import tempfile

# Point storage + models at throwaway dirs BEFORE importing the module.
_WORK = tempfile.mkdtemp()
_MODELS = tempfile.mkdtemp()
os.environ["FSKX_WORK_DIR"] = _WORK
os.environ["FSKX_MODELS_DIR"] = _MODELS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))
import pipeline_store as ps  # noqa: E402

_failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def main():
    pipe = {"version": "1.0.0",
            "nodes": [{"id": "n1", "fskx": "A.fskx", "params": {"x": "1"}},
                      {"id": "n2", "fskx": "B.fskx", "params": {}}],
            "edges": [{"source": {"node": "n1", "param": "t"},
                       "target": {"node": "n2", "param": "duration"},
                       "transform": {"scale": 3600}}],
            "shared": []}

    # --- create ---
    rec = ps.save(pipe, "My pipeline")
    pid = rec["id"]
    check("save returns id + timestamps", bool(pid) and rec["created"] and rec["modified"])

    # --- load round-trip ---
    loaded = ps.load(pid)
    check("load round-trips the pipeline", loaded["pipeline"] == pipe)

    # --- list ---
    lst = ps.list_all()
    check("list shows the saved pipeline with counts",
          any(x["id"] == pid and x["n_nodes"] == 2 and x["n_edges"] == 1 for x in lst))

    # --- update preserves id + created, bumps modified ---
    import time
    time.sleep(1)
    pipe2 = dict(pipe); pipe2["nodes"] = pipe["nodes"][:1]
    rec2 = ps.save(pipe2, "Renamed", pid=pid)
    check("update keeps id + created", rec2["id"] == pid and rec2["created"] == rec["created"])
    check("update changes content + modified",
          ps.load(pid)["pipeline"]["nodes"][0]["id"] == "n1"
          and len(ps.load(pid)["pipeline"]["nodes"]) == 1
          and rec2["modified"] >= rec["modified"])

    # --- traversal guard ---
    check("rejects unsafe id", ps.load("../etc/passwd") is None and not ps.safe_id("a/b"))

    # --- export / import round-trip with member models ---
    # restore the 2-node pipeline and create fake member archives
    ps.save(pipe, "My pipeline", pid=pid)
    for f in ("A.fskx", "B.fskx"):
        with open(os.path.join(_MODELS, f), "wb") as fh:
            fh.write(b"PK\x03\x04 fake fskx " + f.encode())

    res = ps.build_archive(pid)
    check("build_archive returns a file", res and os.path.exists(res[0]))
    arch = res[0]

    import zipfile
    with zipfile.ZipFile(arch) as z:
        names = z.namelist()
    check("archive embeds pipeline.json + members + manifest",
          "pipeline.json" in names and "models/A.fskx" in names
          and "models/B.fskx" in names and "manifest.json" in names)

    # import into a FRESH models dir -> members restored, new pipeline id
    fresh_models = tempfile.mkdtemp()
    imported = ps.import_archive(arch, models_dir=fresh_models)
    check("import creates a new id", imported["id"] != pid)
    check("import restores member models",
          os.path.exists(os.path.join(fresh_models, "A.fskx"))
          and os.path.exists(os.path.join(fresh_models, "B.fskx")))
    check("imported pipeline matches original definition",
          imported["pipeline"]["edges"][0]["transform"] == {"scale": 3600})

    # --- delete ---
    check("delete removes the record", ps.delete(pid) and ps.load(pid) is None)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
