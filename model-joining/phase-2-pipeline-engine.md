# Phase 2 — Pipeline Engine 🚧

**Status.** Core engine implemented and unit-tested with stubbed model loading/execution
(no Docker): `app/pipeline.py` (DAG validate + topological run + transforms + provenance),
the `injections` seam in `engine.execute`, and the joined-block ordering in
`write_param_script`. End-to-end run against real Docker-backed models is the remaining
verification (R path included). API/CLI only — the UI is Phase 3.

**Goal.** Execute a two-node join end to end (API/CLI, no UI yet): run model A, take a
declared OUTPUT, inject it as a declared INPUT of model B, run B.

## Scope

- **`app/pipeline.py`** — the join model and executor:
  - data model:
    - **nodes** — a model + scenario + fixed param overrides.
    - **edges** — `(sourceNode, paramId) → (targetNode, inputId)`. The source param may be
      any classification (OUTPUT *or* a model's INPUT/CONSTANT), since the bundle now carries
      all of them (Phase 1 revision).
    - **shared parameters** — a pipeline-level value bound to *several* nodes' inputs at once
      (e.g. `niter` fanned out across models). Modeled directly rather than as edges, so a
      shared input does **not** impose a false run-order dependency between the nodes.
  - `validate(pipeline)`: enforce DAG (no cycles), check dataType compatibility via the
    Phase 0 coercion table (exact = ok, listed coercions = warn, else error), and **warn on
    unit mismatch** (INTERCHANGE_SPEC §5.1).
  - `run(pipeline)`: topological order; for each node, resolve incoming edges →
    `interchange.read_value` from the source run's `outputs.json` → **apply the edge
    `transform`** if present → `interchange.render_rhs(language_of_target)` → pass as injected
    overrides.

- **Unit conversion on edges.** An edge may carry an optional `transform`
  (`{scale, offset}` affine, applied numerically; or `{expression}` wrapping the literal) —
  the explicit, auditable conversion mechanism from INTERCHANGE_SPEC §5.1. No silent
  auto-conversion; a unit mismatch without a transform runs but warns.
- **`engine.execute` seam** — add an optional `injected_overrides` argument (already-rendered
  `id → RHS` literals). The normal single-run path is unchanged; the pipeline simply supplies
  extra overrides appended after the scenario, reusing `write_param_script`.

## Parameter identity & binding (the "t in every model" problem)

Never use a global parameter namespace — that is what makes a `t` in model A collide with a
`t` in model B. The rules:

- **Address every parameter as `(node-instance, paramId)`.** `paramId` only needs to be
  unique *within one model's script* (it is the variable name in that model's own namespace),
  which it always is. The node-instance qualifier disambiguates the rest.
- **Node-instance, not model id.** A pipeline node has its own stable instance id, because the
  same model may appear twice (e.g. two dose-response nodes with different scenarios).
  Disambiguation is by node-instance / run folder, **never** by `modelId` (two instances
  share it).
- **Bindings are explicit, never name-matched.** An edge wires a chosen source port to a
  chosen target port; the UI offers ports from each model's own `metaData.json`. Auto-wiring
  same-named parameters is the trap and is not done.
- **Per-run bundles already enforce this.** Model A's `t` lives in A's run-folder
  `outputs.json`; B's `t` in B's. The engine reads the *source node's* bundle for the source
  id and injects into the *target node's* script for the target id — there is no shared table
  of `t` to collide.

## Bound parameters: how injection wins over the input field

- A **bound** target input (one with an incoming edge or shared-parameter binding) is
  **excluded from the run form** — the engine collects no submitted value for it, so there is
  no competition.
- `write_param_script` writes, in order: scenario base → user form overrides → a clearly
  delimited **joined block last** (`### --- joined parameters (from upstream models) ---`).
  Last-assignment-wins guarantees the upstream value applies even if a stray value slipped
  through — belt and suspenders.
- **Missing/failed upstream:** the scheduler runs sources before targets; if a source run is
  absent or did not produce the bound param, the run fails with a message naming the exact
  `(node, param)` — never a silent fallback to a form value.

## Design notes

- **The injection point already exists.** `write_param_script` writes scenario assignments
  then appended overrides (last wins). A joined input is just one more appended assignment —
  so no new execution machinery, only a new *source* for the override value.
- **Provenance per edge.** Record source `runId` + a hash of the materialized value in the
  pipeline run record, so a joined result is traceable to the exact upstream values.
- **Sidecar paths.** Ref-backed values (matrices, dataframe OBJECTs, FILEs) are read from the
  source run dir; the rendered literal points the target at the right path on the shared
  volume (copied/symlinked into the target work dir before the run).

## Exit criteria

- A scripted two-node pipeline (e.g. dose-response → population-risk) runs to completion with
  B's input demonstrably taken from A's output.
- A shared parameter fans out to two nodes' inputs without forcing an edge between them.
- An edge `transform` (e.g. h→s `{scale: 3600}`) converts a value on transfer, recorded in
  provenance; a unit mismatch without a transform warns but still runs.
- Validation rejects a cyclic or type-incompatible join with a clear message.
