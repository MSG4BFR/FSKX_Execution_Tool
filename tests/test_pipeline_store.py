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


def _xml_ok(text):
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(text)
        return True
    except ET.ParseError:
        return False


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

    # --- Phase C: node-state persisted with the record ---
    ns = {"n1": {"run_id": "r1", "cache_hash": "h1", "ok": True},
          "n2": {"run_id": "r2", "cache_hash": "h2", "ok": True}}
    recns = ps.save(pipe, "With state", pid=pid, node_state=ns)
    check("save stores node_state",
          recns.get("node_state") == ns and ps.load(pid).get("node_state") == ns)
    # a metadata-only save (node_state omitted) must NOT wipe the execution state
    ps.save(pipe, "Renamed again", pid=pid)
    check("update without node_state preserves it", ps.load(pid).get("node_state") == ns)
    # an explicit state overwrites; explicit {} clears
    ps.save(pipe, "Cleared", pid=pid, node_state={})
    check("explicit node_state overwrites", ps.load(pid).get("node_state") == {})
    # a brand-new record defaults to an empty state
    fresh = ps.save(pipe, "Fresh")
    check("new record defaults node_state to {}", fresh.get("node_state") == {})
    ps.delete(fresh["id"])
    # leave the known state in place for the export/import round-trip below
    ps.save(pipe, "With state", pid=pid, node_state=ns)
    # back-compat: a record file lacking node_state loads fine (absent key tolerated)
    oldrec = {"id": "oldcompat", "name": "Old", "created": "x", "modified": "x",
              "pipeline": pipe}
    with open(os.path.join(ps.PIPELINES_DIR, "oldcompat.json"), "w") as fh:
        __import__("json").dump(oldrec, fh)
    loaded_old = ps.load("oldcompat")
    check("record without node_state still loads",
          loaded_old is not None and "node_state" not in loaded_old)
    ps.delete("oldcompat")

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
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(arch) as z:
        names = z.namelist()
        manifest_xml = z.read("manifest.xml").decode("utf-8")
        metadata_rdf = z.read("metadata.rdf").decode("utf-8")
    check("archive embeds pipeline.json + members + manifests",
          "pipeline.json" in names and "models/A.fskx" in names
          and "models/B.fskx" in names and "manifest.json" in names
          and "manifest.xml" in names and "metadata.rdf" in names)

    # --- OMEX manifest is well-formed and registers every entry ---
    MNS = "{http://identifiers.org/combine.specifications/omex-manifest}"
    try:
        root = ET.fromstring(manifest_xml)
        parsed = True
    except ET.ParseError:
        parsed = False
    check("manifest.xml is well-formed omexManifest",
          parsed and root.tag == MNS + "omexManifest")
    locs = {c.get("location"): c for c in root.iter(MNS + "content")} if parsed else {}
    check("manifest registers root + self + members + master",
          "." in locs and "./manifest.xml" in locs
          and "./pipeline.json" in locs
          and "./models/A.fskx" in locs and "./models/B.fskx" in locs)
    check("pipeline.json is flagged master with json format",
          locs.get("./pipeline.json") is not None
          and locs["./pipeline.json"].get("master") == "true"
          and locs["./pipeline.json"].get("format", "").endswith("json"))
    check("member .fskx use the OMEX format URI",
          locs.get("./models/A.fskx") is not None
          and locs["./models/A.fskx"].get("format")
          == "http://identifiers.org/combine.specifications/omex")
    check("metadata.rdf is well-formed and carries the title",
          metadata_rdf.startswith("<?xml") and "My pipeline" in metadata_rdf
          and _xml_ok(metadata_rdf))

    # import into a FRESH models dir -> members restored, new pipeline id
    fresh_models = tempfile.mkdtemp()
    imported = ps.import_archive(arch, models_dir=fresh_models)
    check("import creates a new id", imported["id"] != pid)
    check("import restores member models",
          os.path.exists(os.path.join(fresh_models, "A.fskx"))
          and os.path.exists(os.path.join(fresh_models, "B.fskx")))
    check("imported pipeline matches original definition",
          imported["pipeline"]["edges"][0]["transform"] == {"scale": 3600})
    check("export -> import round-trips node_state (Phase C)",
          imported.get("node_state") == ns)

    # --- portable export: bundle + restore result artifacts (KNIME-style) ---
    import json as _json
    # the lean archive (built above) must NOT carry result artifacts
    with zipfile.ZipFile(arch) as z:
        check("lean export carries no result artifacts",
              not any(n.startswith("results/") for n in z.namelist()))
    # create the run folders the node-state references: _results/<base>/<run_id>/
    for base, rid in (("A", "r1"), ("B", "r2")):
        rd = os.path.join(ps.RESULTS_ROOT, base, rid)
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, "outputs.json"), "w") as fh:
            fh.write('{"params":{}}')
        with open(os.path.join(rd, "plot.png"), "wb") as fh:
            fh.write(b"\x89PNG fake")
    parch = ps.build_archive(pid, include_results=True)[0]
    with zipfile.ZipFile(parch) as z:
        pnames = z.namelist()
        pmani = _json.loads(z.read("manifest.json"))
        proot = ET.fromstring(z.read("manifest.xml"))
    check("portable export bundles the referenced run folders",
          "results/A/r1/outputs.json" in pnames and "results/A/r1/plot.png" in pnames
          and "results/B/r2/outputs.json" in pnames)
    check("manifest.json flags with_results + counts files",
          pmani.get("with_results") is True and pmani.get("result_files") >= 4)
    plocs = {c.get("location") for c in proot.iter(MNS + "content")}
    check("OMEX manifest registers bundled results",
          "./results/A/r1/outputs.json" in plocs)
    # import into FRESH models + results roots -> run folders restored, node_state intact
    fm, fr = tempfile.mkdtemp(), tempfile.mkdtemp()
    pimp = ps.import_archive(parch, models_dir=fm, results_root=fr)
    check("portable import restores run artifacts into a fresh results root",
          os.path.exists(os.path.join(fr, "A", "r1", "outputs.json"))
          and os.path.exists(os.path.join(fr, "B", "r2", "plot.png")))
    check("portable import keeps node_state (references now resolve)",
          pimp.get("node_state") == ns)
    # never clobber an existing local run
    keep = os.path.join(fr, "A", "r1", "outputs.json")
    with open(keep, "w") as fh:
        fh.write("LOCAL")
    ps.import_archive(parch, models_dir=fm, results_root=fr)
    with open(keep) as fh:
        check("import does not clobber an existing local run", fh.read() == "LOCAL")

    # --- backward-compat: an OLD plain-zip archive (no manifest.xml) still imports ---
    fd, old_arch = tempfile.mkstemp(suffix=".fskxp")
    os.close(fd)
    with zipfile.ZipFile(old_arch, "w") as z:
        z.writestr("pipeline.json", __import__("json").dumps(ps.load(pid)))
        z.writestr("models/A.fskx", b"PK\x03\x04 fake A")
        z.writestr("manifest.json", '{"format": "fskx-pipeline"}')
    legacy_models = tempfile.mkdtemp()
    legacy = ps.import_archive(old_arch, models_dir=legacy_models)
    check("legacy plain-zip archive still imports",
          legacy and legacy["pipeline"]["edges"][0]["transform"] == {"scale": 3600}
          and os.path.exists(os.path.join(legacy_models, "A.fskx")))

    # --- delete ---
    check("delete removes the record", ps.delete(pid) and ps.load(pid) is None)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
