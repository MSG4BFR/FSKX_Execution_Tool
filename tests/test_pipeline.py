"""
Tests for the model-joining pipeline engine (Phase 2).

Run: python3 tests/test_pipeline.py   (from the FSKX_Execution_Tool dir)

The orchestration is exercised with STUBBED model loading + execution (injectable seams),
so no Docker or real model run is needed. The engine.write_param_script joined-block ordering
is tested against the real engine.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, APP)

import interchange as ic   # noqa: E402
import pipeline as pl      # noqa: E402
import engine              # noqa: E402
import pandas as pd        # noqa: E402

_failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


# --- fake model metadata + executor ----------------------------------------

# id -> (language, {paramId: {dataType, unit, classification}})
MODELS = {
    "A.fskx": ("python", {"t": {"dataType": "DOUBLE", "unit": "h", "classification": "OUTPUT"}}),
    "B.fskx": ("python", {"duration": {"dataType": "DOUBLE", "unit": "s", "classification": "INPUT"},
                          "niter": {"dataType": "INTEGER", "unit": "[]", "classification": "INPUT"}}),
    "T.fskx": ("python", {"tbl": {"dataType": "OBJECT", "classification": "OUTPUT"}}),
    "C.fskx": ("python", {"data": {"dataType": "OBJECT", "classification": "INPUT"}}),
}


def fake_load_ctx(fskx):
    lang, pidx = MODELS[fskx]
    return lang, pidx


def make_executor(run_root, produced):
    """produced: fskx -> list of param dicts the node 'outputs'. Returns (execute_fn, captured)
    where captured[fskx] is the injections dict that node received."""
    captured = {}

    def execute_fn(fskx, scenario, params, injections, progress=None):
        captured[fskx] = injections
        outdir = os.path.join(run_root, fskx.replace(".fskx", ""))
        os.makedirs(outdir, exist_ok=True)
        ic.write_bundle(produced.get(fskx, []), outdir, "Python",
                        {"tool": "test", "modelId": fskx, "runId": os.path.basename(outdir)})
        return {"ok": True, "outdir": outdir, "run_id": os.path.basename(outdir), "fskx": fskx}

    return execute_fn, captured


def main():
    # --- topo_order + cycle ---
    p3 = {"nodes": [{"id": "n1", "fskx": "A.fskx"}, {"id": "n2", "fskx": "B.fskx"},
                    {"id": "n3", "fskx": "C.fskx"}],
          "edges": [{"source": {"node": "n1", "param": "t"}, "target": {"node": "n2", "param": "duration"}},
                    {"source": {"node": "n2", "param": "x"}, "target": {"node": "n3", "param": "data"}}]}
    check("topo_order chains correctly", pl.topo_order(p3) == ["n1", "n2", "n3"])

    pcyc = {"nodes": [{"id": "a", "fskx": "A.fskx"}, {"id": "b", "fskx": "B.fskx"}],
            "edges": [{"source": {"node": "a", "param": "t"}, "target": {"node": "b", "param": "duration"}},
                      {"source": {"node": "b", "param": "t"}, "target": {"node": "a", "param": "duration"}}]}
    try:
        pl.topo_order(pcyc)
        check("cycle detected", False)
    except pl.PipelineError:
        check("cycle detected", True)

    # --- check_compat ---
    check("compat: DOUBLE->INTEGER ok", pl.check_compat("DOUBLE", "INTEGER")[0] == "ok")
    check("compat: NUMBER->VECTOR ok", pl.check_compat("NUMBER", "VECTOROFNUMBERS")[0] == "ok")
    check("compat: VECTOR->NUMBER warn", pl.check_compat("VECTOROFNUMBERS", "NUMBER")[0] == "warn")
    check("compat: VECTOR->MATRIX error", pl.check_compat("VECTOROFNUMBERS", "MATRIXOFNUMBERS")[0] == "error")
    check("compat: STRING->DOUBLE error", pl.check_compat("STRING", "DOUBLE")[0] == "error")

    # --- unit mismatch ---
    check("unit mismatch h vs s", pl.unit_mismatch("h", "s") is True)
    check("unit '[]' treated as none", pl.unit_mismatch("[]", "s") is False)

    # --- build_injection: affine h->s ---
    data_h = ic.encode_value(2.0, "DOUBLE")
    inj = pl.build_injection("python", data_h, ".", {"scale": 3600}, "duration")
    check("affine transform h->s renders 7200", inj["rhs"] == "7200.0" and inj["sidecars"] == [])

    # --- build_injection: expression wrap ({value} arrives pre-parenthesized) ---
    inj2 = pl.build_injection("python", data_h, ".", {"expression": "{value} * 60"}, "duration")
    check("expression transform wraps literal", inj2["rhs"] == "(2.0) * 60")

    # --- build_injection: ref dataframe stages a sidecar ---
    tmp = tempfile.mkdtemp()
    df = pd.DataFrame({"region": ["A", "B"], "flow": [1.0, 2.0]})
    data_df = ic.encode_value(df, "OBJECT", outdir=tmp, sidecar_basename="tbl")
    inj3 = pl.build_injection("python", data_df, tmp, None, "data")
    check("ref dataframe -> sidecar + pandas read",
          inj3["rhs"] == '__import__("pandas").read_csv("_joined_data.csv")'
          and len(inj3["sidecars"]) == 1
          and inj3["sidecars"][0][1] == "_joined_data.csv")

    # --- validate: type error + unit warning ---
    bad = {"nodes": [{"id": "n1", "fskx": "A.fskx"}, {"id": "n2", "fskx": "B.fskx"}],
           "edges": [{"source": {"node": "n1", "param": "t"},
                      "target": {"node": "n2", "param": "duration"}}]}
    errs, warns = pl.validate(bad, load_ctx=fake_load_ctx)
    check("validate: clean types, no errors", errs == [])
    check("validate: unit mismatch warns (h->s, no transform)",
          any("unit mismatch" in w for w in warns))

    # --- run end-to-end (stubbed): affine join A.t(h) -> B.duration(s) ---
    run_root = tempfile.mkdtemp()
    produced = {"A.fskx": [{"id": "t", "dataType": "DOUBLE", "unit": "h",
                            "classification": "OUTPUT", "value": 2.0}]}
    execute_fn, captured = make_executor(run_root, produced)
    p = {"nodes": [{"id": "n1", "fskx": "A.fskx"}, {"id": "n2", "fskx": "B.fskx"}],
         "edges": [{"source": {"node": "n1", "param": "t"},
                    "target": {"node": "n2", "param": "duration"},
                    "transform": {"scale": 3600}}],
         "shared": [{"value": 1000, "dataType": "INTEGER",
                     "targets": [{"node": "n2", "param": "niter"}]}]}
    rec = pl.run(p, execute_fn=execute_fn, load_ctx=fake_load_ctx)
    check("run: pipeline ok", rec["ok"] and rec["order"] == ["n1", "n2"])
    check("run: B received transformed t (7200) as duration",
          captured["B.fskx"]["duration"]["rhs"] == "7200.0")
    check("run: B received shared niter=1000",
          captured["B.fskx"]["niter"]["rhs"] == "1000")
    check("run: provenance records the edge + value hash",
          len(rec["provenance"]) == 1 and rec["provenance"][0]["source_run"] == "A"
          and rec["provenance"][0]["value_hash"])

    # --- run: missing upstream output is a clear failure ---
    execute_fn2, _ = make_executor(tempfile.mkdtemp(), {"A.fskx": []})  # A produces nothing
    rec2 = pl.run(p, execute_fn=execute_fn2, load_ctx=fake_load_ctx)
    check("run: missing source param fails with message",
          not rec2["ok"] and any("not found" in e for e in rec2["errors"]))

    # --- engine.write_param_script: joined block is written LAST ---
    md = tempfile.mkdtemp()
    path = engine.write_param_script(md, "python", "s", [], {}, md,
                                     injected={"dose": "7200.0"})
    text = open(path).read()
    check("param script: joined block present and last",
          "### --- joined parameters (from upstream models) ---" in text
          and text.rstrip().endswith("dose = 7200.0"))

    # =====================================================================
    # Phase B — per-node caching + execute_to + plan
    # =====================================================================

    def make_counting_executor(run_root):
        """Each call writes a UNIQUE run dir; A.fskx outputs t = params['k'] (default 2.0) so
        a config change can alter the produced value. Returns execute_fn, run_dir_fn, calls."""
        calls, dirs = {}, {}

        def execute_fn(fskx, scenario, params, injections, progress=None):
            calls[fskx] = calls.get(fskx, 0) + 1
            run_id = "%s_%d" % (fskx.replace(".fskx", ""), calls[fskx])
            outdir = os.path.join(run_root, run_id)
            os.makedirs(outdir, exist_ok=True)
            outs = []
            if fskx == "A.fskx":
                k = float(params.get("k", 2.0))
                outs = [{"id": "t", "dataType": "DOUBLE", "unit": "h",
                         "classification": "OUTPUT", "value": k}]
            ic.write_bundle(outs, outdir, "Python",
                            {"tool": "test", "modelId": fskx, "runId": run_id})
            dirs[run_id] = outdir
            return {"ok": True, "outdir": outdir, "run_id": run_id, "fskx": fskx}

        return execute_fn, (lambda fskx, rid: dirs.get(rid)), calls

    # A.t(h) --scale 3600--> B.duration(s); plus an unrelated node n3 (C) with no edges.
    pB = {"nodes": [{"id": "n1", "fskx": "A.fskx", "params": {"k": 2.0}},
                    {"id": "n2", "fskx": "B.fskx", "params": {}},
                    {"id": "n3", "fskx": "C.fskx", "params": {}}],
          "edges": [{"source": {"node": "n1", "param": "t"},
                     "target": {"node": "n2", "param": "duration"},
                     "transform": {"scale": 3600}}],
          "shared": []}

    ex, rdir, calls = make_counting_executor(tempfile.mkdtemp())
    ek = dict(execute_fn=ex, load_ctx=fake_load_ctx, run_dir_fn=rdir)

    # 1) execute_to n2 from empty state -> runs n1+n2 only (n3 untouched)
    r1 = pl.execute_to(pB, ["n2"], state={}, **ek)
    check("execute_to: runs only the needed subgraph (n1,n2; not n3)",
          r1["ok"] and set(r1["ran"]) == {"n1", "n2"} and "n3" not in r1["results"])
    st = r1["state"]

    # 2) re-execute with the returned state -> both reused, no new model calls
    r2 = pl.execute_to(pB, ["n2"], state=st, **ek)
    check("execute_to: clean re-run reuses everything (0 new calls)",
          r2["reused"] == ["n1", "n2"] and r2["ran"] == []
          and calls["A.fskx"] == 1 and calls["B.fskx"] == 1)
    st = r2["state"]

    # 3) edit n2's own config -> n1 reused, n2 re-runs
    pB["nodes"][1]["params"] = {"niter": 5}
    r3 = pl.execute_to(pB, ["n2"], state=st, **ek)
    check("execute_to: config edit re-runs only that node (n1 reused, n2 ran)",
          r3["reused"] == ["n1"] and r3["ran"] == ["n2"]
          and calls["A.fskx"] == 1 and calls["B.fskx"] == 2)
    st = r3["state"]

    # 4) change n1's output value (param k) -> n1 re-runs AND n2 re-runs (upstream changed)
    pB["nodes"][0]["params"] = {"k": 5.0}
    r4 = pl.execute_to(pB, ["n2"], state=st, **ek)
    check("execute_to: upstream output change propagates (n1+n2 both ran)",
          set(r4["ran"]) == {"n1", "n2"} and r4["reused"] == []
          and calls["A.fskx"] == 2 and calls["B.fskx"] == 3)
    st = r4["state"]

    # 5) content-based reuse: reset only n1 (forget it), re-execute -> n1 re-runs but produces
    #    the SAME output, so n2 stays cached (deterministic upstream keeps downstream valid)
    st_no_n1 = {k: v for k, v in st.items() if k != "n1"}
    r5 = pl.execute_to(pB, ["n2"], state=st_no_n1, **ek)
    check("execute_to: identical re-run of upstream keeps downstream cached",
          "n1" in r5["ran"] and r5.get("reused") == ["n2"]
          and calls["A.fskx"] == 3 and calls["B.fskx"] == 3)
    st = r5["state"]

    # 6) plan dry-run agrees with reality: now everything clean -> all reuse
    pmap = pl.plan(pB, state=st, load_ctx=fake_load_ctx, run_dir_fn=rdir)
    check("plan: all-clean reports reuse for n1,n2", pmap["n1"] == "reuse" and pmap["n2"] == "reuse")
    # edit n2 -> plan shows n2 will run, n1 still reuse
    pB["nodes"][1]["params"] = {"niter": 9}
    pmap2 = pl.plan(pB, state=st, load_ctx=fake_load_ctx, run_dir_fn=rdir)
    check("plan: config edit -> n2 'run', n1 'reuse'",
          pmap2["n1"] == "reuse" and pmap2["n2"] == "run")

    # 7) descendants(): reset target = node + downstream
    check("descendants: n1 -> {n1,n2}; n2 -> {n2}",
          pl.descendants(pB, ["n1"]) == {"n1", "n2"} and pl.descendants(pB, ["n2"]) == {"n2"})

    # 8) cache_hash sensitivity
    nA = pB["nodes"][0]
    h0 = pl.cache_hash(nA, pB, {})
    nA2 = dict(nA, params={"k": 99})
    check("cache_hash: changes when config changes", pl.cache_hash(nA2, pB, {}) != h0)
    check("cache_hash: changes when an incoming edge value changes",
          pl.cache_hash(nA, pB, {"duration": "abc"}) != pl.cache_hash(nA, pB, {"duration": "xyz"}))

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
