# Phase B — Per-node caching + "execute up to here" ✅

**Status.** Implemented (core; live browser/Docker pass still pending — the dev sandbox has
neither). The **heart of the KNIME feel**: run a *single* node and have only its *stale*
upstream auto-execute, reusing cached outputs for everything still valid. Second phase of the
[stateful workflow engine](workflow-engine.md) arc; builds on the node→run persistence proven
in [phase-A](phase-A-run-history.md).

> **Built this session.** `app/pipeline.py`: `config_hash` / `cache_hash` (recursive content
> hash), `_reverse_reachable` (needed subgraph) + `descendants` (reset scope), `execute_to`
> (partial run with cache reuse; emits a `cached` on_node phase) and `plan` (dry-run
> reuse/run/blocked) — `run()` left intact. New store `app/workflow_state.py`
> (`load`/`save`/`set_node`/`reset`/`clear`, under `$FSKX_WORK_DIR/_workflow_state/`).
> `app/server.py`: `/api/pipeline/run` is now **cache-aware** (targets = all nodes; `force`
> ignores cache) and new routes `/execute-node`, `/reset-node`, `/plan`; the exec job persists
> the updated node-state + a Phase-A history record before announcing done; status exposes
> `ran`/`reused`. `app/templates/join.html`: hover **▶ Execute-up-to-here** / **↺ Reset** on
> each node, a **↻ Force re-run all** button, at-rest badges from the plan dry-run
> (clean ✓ / stale ● / blocked ⊘, refreshed on edit), a live **cached ♻** state, and a stable
> per-draft `workflow_id`. Tests: `tests/test_workflow_state.py` (11 checks) + a Phase-B block
> in `tests/test_pipeline.py` (cache reuse, config/upstream-change propagation, deterministic
> reuse, plan, descendants, cache_hash sensitivity); plus a live Flask integration driving the
> **real** `execute_to`/`plan` (engine seams stubbed): state persists across requests, only
> stale nodes re-run, reset clears downstream.
>
> **Post-test fixes (user feedback):** (1) reset did nothing on a loaded workflow — the canvas
> stayed in "run mode" so badge refreshes were suppressed; `loadRecord`/`resetNode` now clear
> `canvasRunMode`. (2) the **Execute (▶) button is now disabled on an up-to-date node** (plan =
> reuse), with a "reset to re-run" tooltip, instead of silently reusing. (3) run history no
> longer auto-saves — it is a manual **💾 Save current state** snapshot (see
> [phase-A](phase-A-run-history.md)). **Still to do live:** click-through in a real browser
> against Docker-backed runs.

## What already exists (the substrate)

- Each node run persists to `_results/<base>/<run_id>/` with `outputs.json` (the **on-disk port
  cache**) + `run_meta.json`. `engine.run_dir(fskx, run_id)` resolves a stored run folder.
- `pipeline.run` already, per node: gathers incoming edges, reads each source's
  `outputs.json` (`_source_param_data`), builds an injection (`build_injection`), and records a
  per-edge `value_hash = _hash(inj["rhs"])` in provenance.
- Phase A persists a history record linking `node → {fskx, run_id}` per execution.

The only thing missing for incremental execution: a node's source output may come from a
**previously cached run**, not one produced in the same invocation — plus a way to decide when
a cache is still valid.

## Design

### 1. Cache identity = a recursive content hash
A node's cached output is valid iff nothing that feeds it changed. Define, per node:

```
config_hash(n) = sha1( model_id · scenario · json(sorted params) · json(sorted shared-for-n) )
edge_input_hash(e) = _hash(inj["rhs"])          # already computed for provenance —
                                                 # encodes upstream value + transform
cache_hash(n) = sha1( config_hash(n) · sorted("{e.target.param}={edge_input_hash(e)}") )
```

Because `edge_input_hash` is a hash of the *injected literal* (which is derived from the
upstream node's `outputs.json` + the edge transform), `cache_hash` is a **recursive content
hash**: change a node's config, its wiring, or any upstream output, and its hash — and every
downstream hash — changes. This makes dirty-propagation *fall out of the cache mechanism*
rather than needing separate bookkeeping (the rigorous edge cases + model-version inclusion are
[phase E](workflow-engine.md), but the core is here). It is also content-based, so a
deterministic upstream re-run that yields an identical output legitimately keeps downstream
caches valid.

**Reuse rule.** Node `n` is *clean* (reuse, don't run) iff a recorded state entry exists with
`cache_hash` equal to the freshly computed one, its run was `ok`, and its run folder +
`outputs.json` still exist. Otherwise `n` is *stale* and must run.

### 2. Partial-execution engine — `pipeline.execute_to(...)`
New function alongside (not replacing) `run`:

```
execute_to(pipeline, targets, state, execute_fn=, load_ctx=, on_node=)
  -> {ok, ran:[...], reused:[...], results:{nid:{outdir,run_id,ok}}, state, provenance, failed_node?}
```

- Compute the **needed subgraph** = `targets` ∪ all their transitive ancestors (a reverse-edge
  walk), intersected with `topo_order` for scheduling.
- Walk needed nodes in topo order. For each: build injections from already-resolved upstream
  results (a reused upstream contributes `engine.run_dir(fskx, run_id)` as its source dir);
  compute `cache_hash`; if clean → **reuse** (`results[nid]` points at the cached run, emit
  `cached`); else **run** `execute_fn` and update `state[nid]`.
- A node never re-runs unless its hash changed — so "execute node X" runs the minimal set.
- Reuses the existing injection / provenance / `on_node` machinery unchanged; `on_node` gains a
  `cached` phase alongside `start`/`done`/`failed`.

`run()` stays as-is for back-compat and is trivially expressible as
`execute_to(pipeline, all_sink_nodes, state={})` — but we keep both so existing callers/tests
don't move.

### 3. Node-state store — `app/workflow_state.py` (new)
Per-workflow execution state, persisted to `$FSKX_WORK_DIR/_workflow_state/<workflow_id>.json`:

```
{ "n1": {"run_id": "...", "cache_hash": "...", "ok": true, "executed_at": "..."}, ... }
```

- `workflow_id` = the saved-pipeline id, or a client-supplied draft id for unsaved drafts.
- API: `load(wid)`, `save(wid, state)`, `set_node(...)`, `reset(wid, node_ids)`, `clear(wid)`.
- Stdlib-only, unit-testable (mirrors `pipeline_runs` / `pipeline_store`).
- **Boundary with phase C:** B keeps this a *side file* (an execution cache). C promotes it into
  the saved workflow document + the `.fskxp` export. Keeping it separate now means C is a clean,
  meaningful step and B doesn't touch the pipeline-definition format or archive.

### 4. Reset & configure
- **Reset(node)** = remove that node **and all downstream** from the state map (a reverse of
  the reachability walk). It does **not** delete `_results` artifacts — Phase A history still
  references them; reset only forgets the "current" association, so the next execute recomputes
  and re-runs. (Deleting artifacts would break history links — explicitly avoided.)
- **Configure** = edit a node's params/scenario (the per-node form already exists). Because that
  changes `config_hash`, the node + everything downstream become stale automatically on the next
  execute — no extra wiring.

### 5. Plan / dry-run (honest staleness preview)
`/api/pipeline/plan` runs the *planning half* of `execute_to` without executing: bottom-up over
the needed subgraph using only already-cached upstream outputs, returning per node
`will_reuse | will_run | blocked`. Drives a pre-execute badge on the canvas ("this click will
run nodes 2 & 4, reuse 1 & 3") so the user sees what will happen. (Conservative: an unresolved
stale upstream marks dependents `will_run`.)

### 6. Server routes
- `POST /api/pipeline/execute-node` — body `{pipeline, workflow_id, targets:[nid]}`; runs
  `execute_to` on a background job (reuses the job registry + `/api/pipeline/status/<job>`
  polling; `cached` nodes report instantly). Persists a Phase-A history record as today.
- `POST /api/pipeline/reset-node` — `{workflow_id, node}` → resets node + downstream.
- `GET  /api/pipeline/node-state/<workflow_id>` — current state map for canvas badges.
- `POST /api/pipeline/plan` — the dry-run preview above.

### 7. Canvas UI (`join.html`)
- **Per-node actions** (decided: **hover buttons on the node box**): a *▶ Execute up-to-here*
  and a *↺ Reset* icon appear on the node header on hover/select, beside the existing *✕*.
- **Persistent state badges** reusing the phase-#3a renderer: executed (green, links to its
  cached results), stale/needs-run, running, cached-this-pass (a subtle "reused" marker so
  skipped work is visible), failed (links to log). Canvas fetches node-state (+ a `plan`
  dry-run) on load and after a config edit.
- **"Run" button** (decided: **cache-aware**): the whole-graph Run executes only stale nodes
  (reusing valid caches — the content hash guarantees correctness); a separate **"Force re-run
  all"** ignores caches.

## Scope boundary (kept out of B on purpose)
- **Parallel execution** of independent branches → phase **D** (B schedules sequentially).
- **State saved *into* the workflow definition + export archive** → phase **C** (B uses a side
  file).
- **Rigorous staleness UX + model-version-in-hash + transform edge cases** → phase **E** (B has
  the content-hash core + a conservative dry-run).

## Invariants (don't break)
- **`run()` and its tests stay green** — `execute_to` is additive and shares helpers; the
  [HANDOFF §2](HANDOFF.md) invariants (file boundary, parameter identity, injection ordering,
  table-driven dataTypes) are untouched.
- **References, not copies** — reuse points at existing `_results` runs; reset never deletes
  artifacts (history stays intact).
- **Content-hash correctness over speed** — when in doubt (missing cache folder, unresolved
  upstream), treat as stale and run; never reuse a cache that might be wrong.
- **Best-effort persistence** — a failure to read/write the state file degrades to "all stale"
  (re-run), never a crash.

## Tests — `tests/test_workflow_state.py` + extend `tests/test_pipeline.py`
No Docker (stubbed `execute_fn`/`load_ctx`, as the existing pipeline tests already do):
- `cache_hash` changes on config edit, wiring change, and upstream-output change; stable
  otherwise.
- `execute_to` runs only the stale subset; reuses clean upstream; re-runs downstream when an
  upstream output changes; identical deterministic upstream keeps downstream cached.
- "execute node X" runs X's ancestors only (not unrelated branches).
- reset(node) clears node + downstream (not upstream), and does not delete `_results`.
- state store round-trips; tolerant of a missing/corrupt file (→ all stale).
- plan/dry-run reports the same run/reuse split `execute_to` then performs.

## Exit criteria
- Clicking *Execute* on a node runs only that node and its stale ancestors, reusing valid
  caches; the canvas shows which were run vs reused.
- Editing a node's config, then executing a downstream node, re-runs the changed node and its
  dependents but nothing else.
- *Reset* on a node marks it + downstream for re-run without touching upstream or deleting
  stored results.
- `run()` and all existing suites stay green; new tests pass without Docker.
