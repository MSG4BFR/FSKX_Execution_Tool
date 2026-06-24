# Phase 4 — Packaging 🚧

**Status.** Durable named pipelines implemented: `app/pipeline_store.py` (CRUD for
`pipeline.json` records on the work volume + composite `.fskxp` archive build/import),
server routes (`/api/pipelines` GET/POST, `/api/pipelines/<id>` GET, `/delete`, `/export`,
`/import`), and the builder's "Saved pipelines" bar (save / save-as-new / load / delete /
export / import; the working draft also autosaves to the browser). Unit-tested
(`tests/test_pipeline_store.py`, 18 checks) and HTTP-smoke-tested end to end incl. a
real-model export/import round-trip.

The `.fskxp` archive is now **COMBINE/OMEX-conformant**: it carries an `manifest.xml`
(`omexManifest`) registering every entry with its COMBINE format URI — the root and member
`.fskx` files as `…/omex`, the manifest as `…/omex-manifest`, `metadata.rdf` as
`…/omex-metadata`, and `pipeline.json` (the app-private join wiring) flagged `master="true"`
with the JSON media type — plus a `metadata.rdf` with archive title/created/conformsTo. The
legacy `manifest.json` is still written for human inspection and older readers. `import_archive`
reads the manifest's `master` to locate the pipeline record and falls back to `pipeline.json`,
so both new OMEX archives and older plain zips import. See `pipeline_store.build_manifest_xml`
/ `build_metadata_rdf` / `build_archive` / `import_archive`.

Remaining: expressing the cross-model join **wiring** in a standard construct (no SED-ML
element models cross-model parameter joins today, so the wiring stays in `pipeline.json`); a
per-run reproducibility manifest; and a *reference* (non-embedding) archive option.

**Goal.** Make a join a first-class, shareable artifact — not just an in-session wiring.

## Scope

- **`pipeline.json`** — persist a join definition (nodes, edges, fixed overrides, format
  version) on the work volume; list/load/duplicate from the UI like models.
- **Composite FSKX** — package a pipeline plus its member models as a single OMEX archive so
  it can be shared and re-run elsewhere. Two options to evaluate:
  - *reference* — the composite stores the join + member model ids, resolving members from
    the repository on load (small, but needs the members available);
  - *embed* — the composite bundles the member `.fskx` files (self-contained, larger).
- **Reproducibility manifest** — record, per pipeline run, the member model versions, the
  source `runId`s, and value hashes per edge, so a shared pipeline reproduces identically.

## Design notes

- **Standards alignment.** Prefer expressing the join with existing FSKX/SED-ML/OMEX
  constructs where possible (e.g. SED-ML for cross-model parameter wiring) over an
  app-private format, so other FSKX tools have a path to read it.
- **Defer until 1–3 are stable** — the persisted format should follow real pipeline usage,
  not precede it.

## Exit criteria

- A pipeline can be saved, reloaded, and shared; a shared pipeline re-runs and reproduces the
  original joined result.
