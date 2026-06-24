# Phase 4 — Packaging 🚧

**Status.** Durable named pipelines implemented: `app/pipeline_store.py` (CRUD for
`pipeline.json` records on the work volume + composite `.fskxp` archive build/import),
server routes (`/api/pipelines` GET/POST, `/api/pipelines/<id>` GET, `/delete`, `/export`,
`/import`), and the builder's "Saved pipelines" bar (save / save-as-new / load / delete /
export / import; the working draft also autosaves to the browser). Unit-tested
(`tests/test_pipeline_store.py`, 12 checks) and HTTP-smoke-tested end to end incl. a
real-model export/import round-trip. Remaining: a fully OMEX-conformant manifest (current
archive is a plain inspectable zip), a per-run reproducibility manifest, and a *reference*
(non-embedding) archive option.

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
