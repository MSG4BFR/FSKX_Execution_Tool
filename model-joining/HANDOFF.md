# Model Joining — Project Handoff & TODOs

A self-contained continuation guide: what the feature is, what was built across the four
phases, how to run and test it, the key invariants not to break, and a prioritized list of
open work. Start here in a new session.

> Index: [README.md](README.md) · Format: [../INTERCHANGE_SPEC.md](../INTERCHANGE_SPEC.md),
> [../interchange-schema.json](../interchange-schema.json) · Per-phase detail:
> [phase-0](phase-0-interchange-spec.md) · [phase-1](phase-1-serialization-layer.md) ·
> [phase-2](phase-2-pipeline-engine.md) · [phase-3](phase-3-ui-and-dag.md) ·
> [phase-4](phase-4-packaging.md)

---

## 1. The idea

Chain FSKX model simulations so a downstream model's **input** is taken from an upstream
model's **parameter** (the "dream" slide: Single-Hit → Exposure → Population Risk). Models
may be in different languages and versions (R, Python 3, Python 2), so values must cross
between them through a neutral, typed, on-disk format — never as shared in-process objects.

The whole design rests on one observation: each model already runs in its **own isolated
environment** (micromamba env or Docker container), so the language boundary is already a
**file boundary**. Cross-language / cross-version interop therefore falls out for free — we
just needed a typed interchange format and an orchestration layer that injects values.

---

## 2. Architecture & invariants (do not break these)

- **File boundary, not runtime boundary.** Values cross as JSON + sidecar files on the shared
  `fskx_work` volume. No model ever imports another's objects.
- **Parameter identity = `(node-instance, paramId)`.** `paramId` is only unique within one
  model; the node-instance qualifier disambiguates. Two instances of the same model are
  distinguished by run folder, **never** by `modelId`. Never auto-match parameters by name
  across models — this is what makes a `t` in model A not collide with a `t` in model B.
- **Injection wins by ordering.** The generated parameter script writes scenario base → user
  overrides → **joined block last**; last-assignment-wins guarantees the upstream value
  applies. Bound inputs are also dropped from the form so they can't compete.
- **Table-driven dataType handling.** Encoders/decoders/renderers dispatch on `dataType` via
  dicts, never long if/else chains — new types extend a table.
- **Writer in two languages, reader/renderer in one.** Emitting `outputs.json` happens inside
  each model env, so the writer exists in both `interchange.py` and `interchange.R`. Reading
  and rendering-to-a-literal always happen in the Python orchestration layer, so they live
  only in `interchange.py`.
- **Self-contained injected literals.** Python read-literals use `__import__('pandas')` /
  `__import__('json')` so they assume nothing about the target model's imports.
- **No silent unit conversion.** Units travel with values; a mismatch warns; conversion is
  explicit per-edge (`{scale, offset}` or `{expression}`).
- **Injectable seams for testability.** `pipeline.validate(..., load_ctx=)` and
  `pipeline.run(..., execute_fn=, load_ctx=, on_node=)` let the orchestration be tested
  without Docker or real model runs.

---

## 3. What was built, by phase

### Phase 0 — Interchange spec ✅
- `INTERCHANGE_SPEC.md` + `interchange-schema.json` (workspace root). FSKX Model-Interchange
  **v1.0.0**: typed `data` per FSK-ML dataType (inline vs ref sidecar), NaN/Inf handling,
  **OBJECT = dataframe-first**, units carried, explicit per-edge transforms. Superset of the
  RAKIP `parameters-schema.json`.

### Phase 1 — Serialization layer ✅
- `app/interchange.py` — writer + reader + renderer (table-driven; optional pandas).
- `app/interchange.R` — R-side writer (jsonlite).
- `app/engine.py` — `serializable_param_specs` (all classifications + unit), `write_run_plan`
  records `serialize_params` + `model_id`, `_sync_runners` ships the interchange modules.
- `app/run_python_model.py` / `app/run_r_model.R` — emit `outputs.json` after a run.
- `tests/test_interchange.py` (24 checks).

### Phase 2 — Pipeline engine ✅ (core; Docker/R end-to-end still to verify)
- `app/pipeline.py` — DAG model, `validate` (dataType coercion table, unit-mismatch warnings,
  cycle rejection), `run` (topological; reads source `outputs.json`, applies transform,
  renders literal, injects), per-edge provenance, `on_node` hook.
- `app/engine.py` — `execute(..., injections=)` seam: stages ref sidecars, drops bound ids
  from the form, writes the joined block last, records `run_meta.injected`.
- `tests/test_pipeline.py` (20 checks, stubbed executor — no Docker needed).

### Phase 3 — UI & DAG ✅ (drag-and-drop node-graph canvas)
- `app/templates/join.html` — **dependency-free node-graph canvas**: draggable boxes with
  input (left) / output (right) ports, bezier edges with a dotted "ghost" where a wire passes
  under a node (live during a drag too), pan/zoom/Fit/fullscreen, live execution-step badge per
  node (client topo sort), reachability-based loop blocking (canvas + table), connected-first
  collapsible ports, two-line names. The old per-node parameter editor (**locked bound fields**)
  + edge transform table live under a collapsible "Advanced editor". Working draft (incl. node
  positions + view) autosaves to `localStorage`. The pipeline JSON contract is unchanged.
  **Two-view sync:** transform-value inputs (scale/offset/expr) carry `data-ei`/`data-ek` tags;
  `edgeRaw` pushes each edit into the matching sibling input across the canvas panel ↔ table
  (skipping the focused one, so no cursor jump) without a full re-render — fixes a lag where a
  transform edited on the canvas updated the table only after a save / "Add connection".
- `app/server.py` — `/join`, `/api/model-params/<fskx>`, `/api/pipeline/validate|run|status`.
- `app/engine.py` — `model_param_specs`; `app/templates/run_view.html` shows "Joined
  parameters"; `index.html` nav link.
- `app/templates/_canvas_preview.html` — a standalone, backend-free demo of the canvas (mock
  models) for quick visual review; not routed by Flask (underscore filename), safe to delete.

### Phase 4 — Packaging ✅ (persistence + sharing)
- `app/pipeline_store.py` — CRUD for `pipeline.json` records on the work volume; composite
  `.fskxp` archive **build/import** (embeds member `.fskx`). The archive is now
  **COMBINE/OMEX-conformant**: `build_manifest_xml` emits an `omexManifest` registering every
  entry with its COMBINE format URI (`pipeline.json` flagged `master`), `build_metadata_rdf`
  emits archive `metadata.rdf`; `import_archive` reads the manifest's `master` (falling back to
  `pipeline.json`) so new OMEX and old plain-zip archives both import. **Live-verified:** a real
  export → import round-trip was run in the app and works.
- `app/server.py` — `/api/pipelines` (GET/POST), `/api/pipelines/<id>` (GET),
  `/delete`, `/export`, `/import`.
- `app/templates/join.html` — "Saved pipelines" bar (save / save-as-new / load / delete /
  export / import).
- `tests/test_pipeline_store.py` (18 checks — incl. OMEX manifest well-formedness + back-compat).

---

## 4. How to run & test

**Run the app** (needs Docker running): `./run.sh` from `FSKX_Execution_Tool/`. Open the
home page → **🔗 Join models**.

**Run the test suites** (no Docker needed) from `FSKX_Execution_Tool/`:

```
python3 -m py_compile app/*.py
python3 tests/test_interchange.py
python3 tests/test_pipeline.py
python3 tests/test_pipeline_store.py
```

All three pass in a plain Python env with `pandas` + `jsonschema` installed. The Python model
path is exercised; **the R writer and any real Docker-backed run must be validated on a
machine with Docker + R** (see DEVELOPER.md §7) — the dev sandbox has neither.

---

## 5. Open TODOs

### High value / user-requested
1. ~~**OMEX-conformant `.fskxp` manifest.**~~ ✅ DONE. The export is now a COMBINE/OMEX
   archive: `manifest.xml` (`omexManifest`) registers every entry by COMBINE format URI with
   `pipeline.json` as `master`, plus archive `metadata.rdf`; the legacy `manifest.json` is kept
   for human/older readers; `import_archive` reads the manifest `master` and stays back-compat.
   See `pipeline_store.build_manifest_xml` / `build_metadata_rdf` / `build_archive` /
   `import_archive` and `tests/test_pipeline_store.py`. **Still open:** expressing the join
   **wiring** in a standard construct — there is no SED-ML element for cross-model parameter
   joins, so the wiring stays app-private in `pipeline.json` (registered as `master`). Revisit
   if a standard cross-model construct emerges, or pair with a reference archive (#6).
2. **Scenario-per-node selection.** Nodes currently run the model's default scenario
   (`node.scenario` is unused in the UI). Add a scenario dropdown per node in `join.html`
   (data already available from `engine.scenario_names` / `model_info["scenarios"]`); pass
   `scenario` through `pipeline.run` → `engine.execute` (already accepts it). Update
   `/api/model-params` to also return `scenarios`.
3. **Drag-and-drop graph canvas.** ✅ DONE (dependency-free, in `join.html`). Draggable node
   boxes with blue input ports (left) and violet output ports (right); drag any port → an input
   to wire an edge; click a wire to edit its transform / delete. Pan (background drag) + zoom
   (buttons / wheel) + Fit + fullscreen (`toggleMax`); `Auto-layout` arranges by dependency
   depth then fits. The old row-based table + per-node parameter editors are preserved in the
   collapsible "Advanced editor". The `validate`/`run` API and pipeline JSON shape are
   unchanged; node `x`/`y` ride along in the saved pipeline (backend ignores them) so positions
   persist. Design decisions baked in (see also §8):
   - **Box chip = derived execution step (1…N)**, from a client-side topo sort mirroring
     `pipeline.topo_order`; the raw `n#` id is internal only. So the visible number always
     reflects run order and never shows a confusing "n5 for 2 models".
   - **Cycles blocked at creation** by a reachability check (`reachable(tgt, src)`), in both the
     canvas and the table editor — not by id ordering, so dragging boxes around is free.
   - **Input ports can be edge sources too** (the dream's grey wires), since the backend already
     allows any classification as a source.
   - **Ports collapse when a column exceeds `PORT_CAP` (6):** connected ports are always shown,
     the rest hide behind a "+ N more…" toggle, so a wired port never detaches from its wire.
3a. **Live, animated run status on the canvas.** *(User idea — the join builder is effectively
    a small workflow engine, so the canvas should come alive while the pipeline runs.)* During a
    run, reflect each node's state **on its box in the graph**: pending → running → done →
    failed, ideally animated (e.g. a pulsing/spinner ring on the executing node, a travelling
    dash along edges whose value has just been produced, a green check / red cross on
    completion). The data is already there — `pipeline.run`'s `on_node(nid, phase, info)` hook
    feeds `/api/pipeline/status/<job>`, which the page already polls for the (plain-list) node
    states and provenance; this TODO is about **rendering those phases onto the canvas nodes**
    instead of (or alongside) the current text list under "Run". Be honest about **failure**:
    a failed node should be visually unmistakable, the downstream nodes it blocked shown as
    "skipped/blocked" (not silently pending), and the failing node link straight to its run log.
    Mostly front-end on top of the existing `on_node`/status contract; a richer animation might
    tempt a graph library, but the current dependency-free SVG layer can do pulses (CSS
    keyframes on the node box) and edge dash-offset animation without one — try it ourselves
    first. Keep node identity stable during a run so status maps cleanly to boxes.

### Correctness / completeness
4. **Docker + R end-to-end verification.** Run a real two-model join across R↔Python and a
   ref-backed value (dataframe OUTPUT → OBJECT INPUT, which stages a sidecar). The user's
   BUGS→JAGS scalar join already worked; the dataframe/sidecar path is untested live.
5. **Per-run reproducibility manifest.** Provenance already records source run id + value
   hash per edge; extend to capture member model versions and persist a manifest with the
   pipeline run for exact reproduction.
6. **Reference (non-embedding) archive option.** Besides the self-contained `.fskxp`, offer a
   lightweight archive that references member models by repository id (resolved on import).
7. **Known-units suggestion table.** From INTERCHANGE_SPEC §5.1: when an edge has a unit
   mismatch, *suggest* a transform from a small table (h/min/s/day, °C/K/°F, CFU↔log10CFU),
   applied only on user confirmation. Pure UX on top of the existing transform mechanism.
8. **INPUT/CONSTANT capture caveat.** Inputs are serialized **post-run**; a model that
   reassigns an input in place records the mutated value. If sourcing the *as-configured*
   value ever matters, capture from the resolved param script instead. Documented in
   INTERCHANGE_SPEC §2.
9. **Non-numeric transform guard.** `pipeline._apply_affine` leaves non-numeric values
   unchanged; an affine transform on a dataframe/string edge should surface a warning rather
   than silently no-op.
10. **Cyclic chains (loops) for iterative models.** The DAG is acyclic by design and the UI now
    blocks creating a cycle (reachability check) — `topo_order` would otherwise reject it at
    validate time. There is a real use-case for a *controlled* loop (e.g. a Monte-Carlo /
    fixed-point iteration where model B feeds A and A feeds B until convergence). Supporting it
    needs first-class **iteration semantics in `pipeline.run`**: a loop construct with a
    **break/termination condition** (max iterations and/or a convergence tolerance on a chosen
    parameter), value carry-over between iterations, and provenance per iteration. Until that
    exists, loops stay disallowed. Do **not** simply remove the cycle guard — `run` has no
    termination condition and would not converge.

### Housekeeping
10. **Pipeline cleanup integration.** `engine.cleanup_all` wipes `_results` but not
    `_pipelines`; decide whether saved pipelines should be cleanable from the UI (probably a
    separate, explicit action so saves aren't lost with a cache clear).
11. **Validation ergonomics.** Auto-validate on edit (debounced), disable Run while errors
    exist, and label ports more richly in the menus.
12. **Matrix/expression transform edge cases.** Expression transforms on vectors/matrices are
    allowed but only meaningfully tested on scalars; add tests or constrain.

---

## 6. Suggested sequencing for the next session

1. **Docker/R end-to-end verification (#4)** — confirms the live R writer and sidecar staging
   before more is built on top; do it on a Docker+R machine with the bundled BUGS/JAGS and an
   R↔Python pair. Also a first live pass over the new canvas (pointer interactions vs. real
   model ports) — it's only been static/headless-checked here (no browser/Docker in the dev box).
2. **Scenario-per-node (#2)** — small, high user value, unblocks realistic multi-scenario
   pipelines. ~1 file of UI + a field through two functions.
3. **Live, animated run status on the canvas (#3a)** — the natural next UX step now that the
   graph exists: drive node boxes from the `on_node`/status phases (running/done/failed,
   animated), with honest failure/blocked rendering. Front-end on the existing contract.
4. **OMEX-conformant manifest (#1)** + **reference archive (#6)** — standards alignment for
   sharing, once the format has seen real use.
5. Polish items (#7, #11, #12) as time allows.

✅ Done this session: **Drag-and-drop canvas (#3)** — the biggest UX win; the API/JSON contract
stayed stable and tested.

Keep every change behind the invariants in §2 and add/extend a test in the matching
`tests/test_*.py` — the stubbed seams mean most logic is testable without Docker.

---

## 7. File map (quick reference)

| Area | New | Modified |
|---|---|---|
| Format | `INTERCHANGE_SPEC.md`, `interchange-schema.json` | — |
| Serialization | `app/interchange.py`, `app/interchange.R` | `app/run_python_model.py`, `app/run_r_model.R` |
| Engine | — | `app/engine.py` (specs, run-plan, execute injections, run-meta, sync) |
| Pipeline | `app/pipeline.py`, `app/pipeline_store.py` | — |
| Server | — | `app/server.py` (join + pipeline + persistence routes) |
| UI | `app/templates/join.html` (node-graph canvas), `app/templates/_canvas_preview.html` (standalone demo) | `app/templates/index.html`, `app/templates/run_view.html` |
| Tests | `tests/test_interchange.py`, `tests/test_pipeline.py`, `tests/test_pipeline_store.py` | — |
| Docs | `model-joining/` (this folder) | — |
