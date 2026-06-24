# Phase A — Workflow run history (snapshot-on-execute) ✅

**Status.** ✅ Done — **live-verified**. First phase of the
[stateful workflow engine](workflow-engine.md) arc.

> **Revised in Phase B (per user feedback):** auto-saving a record on *every* execution created
> clutter, so history is now a **manual "💾 Save current state"** action. A saved record carries
> the per-node run references *and* the raw node-state (cache hashes), so **loading it restores
> the working state** (`/api/pipeline/restore-state`) — executed nodes show as clean and caching
> keeps functioning — rather than only a visual replay. Legacy auto-saved records (which lack
> `node_state`) still load via the original visual replay. See
> `pipeline_runs.build_record_from_state` + `/api/pipeline-runs/save`.

> **Built this session.** New store `app/pipeline_runs.py` (`build_record` / `save_run` /
> `load_run` / `list_runs` / `delete_run` / `new_run_id`, records under
> `$FSKX_WORK_DIR/_pipeline_runs/`). `app/server.py`: `_pipeline_job` now persists a record
> **before** marking the job done (so a finished run is always already reloadable — no race),
> the run route accepts `{pipeline, name, pipeline_id}` (legacy bare body still works) and
> returns `pipeline_run_id`, status exposes it, plus routes `GET /api/pipeline-runs`,
> `GET /api/pipeline-runs/<id>`, `POST /api/pipeline-runs/<id>/delete`. `app/templates/join.html`:
> a "Past runs (history)" bar; `refreshRuns` / `loadPastRun` / `loadRunRecord` /
> `deletePastRun`; `pollStatus` refactored to share `renderRunList` / `renderProvenance`, which
> the replay path reuses with `computeRunStates` + `applyRunState` (phase #3a) so a reloaded run
> renders identically to a live one. Tests: `tests/test_pipeline_runs.py` (12 checks) +
> a Flask test-client integration pass (wrapped/legacy bodies, persist-before-done, list/load/
> delete, references-not-copies). **Live-verified:** runs are saved on execution and past runs
> reload + replay on the canvas in the browser against Docker-backed runs.

**Goal.** Persist every pipeline execution as a lightweight, addressable record so past runs
can be listed and **reloaded onto the canvas** — replaying the finished status (node colours +
provenance + links to each node's stored results). This ships the user-requested "load past
pipeline runs" feature *and* establishes the node→run persistence layer that phases B–C build
on.

**Chosen UX (agreed).** History lives on the **Join page**; clicking a past run replays it on
the canvas (vs. a separate read-only page). Every execution is recorded (success *and*
failure — failures are often the most useful to revisit).

## What already exists (reuse, don't fork)

- Per-node runs persist at `_results/<base>/<run_id>/` with `run_meta.json` (incl. `injected`),
  listable via `engine.list_runs`, viewable at `/runs/<fskx>/<run_id>`.
- A pipeline execution builds an in-memory job (`JOBS[job_id]`) and `pipeline.run` returns
  `{ok, order, results:{nid:exec}, provenance, failed_node}` — but this is **never persisted**;
  it dies with the job and on restart.
- The canvas already renders run state (phase #3a: `applyRunState` / `computeRunStates`) —
  reuse it verbatim to replay a *finished* run (`finished:true`).

## Design

### New store — `app/pipeline_runs.py`
Mirrors `pipeline_store.py`: stdlib-only, no Flask/engine import, unit-testable in isolation.
Records at `$FSKX_WORK_DIR/_pipeline_runs/<run_id>.json`, parallel to `_pipelines/` and
`_results/`. API: `save_run(record)`, `list_runs()`, `load_run(run_id)`, `delete_run(run_id)`,
plus a `safe_id` traversal guard (as in `pipeline_store.safe_id`).

### Record schema (references, never copies of artifacts)
```json
{
  "run_id": "20260624_141530_ab12",        // timestamp + short uuid (collision-safe within a second)
  "created": "2026-06-24T14:15:30",
  "name": "My join pipeline",               // saved pipeline name, else "Unsaved draft"
  "pipeline_id": "abc123 | null",           // link to the saved definition, if any
  "ok": true,
  "stage": "run",                           // or "validate" when it never started
  "failed_node": null,
  "errors": [],
  "order": ["n1", "n2", "n3"],              // topo order
  "pipeline": { "nodes": [/*incl. x/y*/], "edges": [], "shared": [] },
  "nodes": {
    "n1": {"fskx": "A.fskx", "run_id": "20260624_141500", "ok": true, "phase": "done"},
    "n2": {"fskx": "B.fskx", "run_id": "20260624_141512", "ok": true, "phase": "done"}
  },
  "provenance": [
    {"target": {"node":"n2","param":"dose"}, "source": {"node":"n1","param":"response"},
     "source_run": "20260624_141500", "transform": null, "value_hash": "…"}
  ]
}
```
- Embeds the **wiring snapshot** (`pipeline`) so a reload is faithful even if the saved
  definition is later changed or deleted.
- `nodes[nid].run_id` points at each node's existing `_results` run → the reload links straight
  to `/runs/<fskx>/<run_id>`. **No plots/files are copied.**
- Stores per-node *outcome* (`done` / `failed`) + `failed_node` only; the UI derives
  `blocked` / `skipped` via the existing `computeRunStates`, so a snapshot and a live run
  render identically.

### Write hook — `server.py` `_pipeline_job`
- Generate the pipeline `run_id` at **job start** (timestamp + short uuid) and expose it in the
  status payload, so a just-finished run is immediately addressable.
- After `pipeline.run` returns `rec`, assemble the record from the submitted pipeline `p` +
  `rec` (per-node `run_id`s come from `rec["results"][nid]["run_id"]`) and call
  `pipeline_runs.save_run(...)`.
- **Best-effort:** a persistence failure must never fail the job (mirrors
  `engine.write_run_meta`).

### Server routes (mirror the saved-pipeline routes)
- `GET  /api/pipeline-runs` → summaries (`run_id`, `created`, `name`, `ok`, `failed_node`,
  `n_nodes`), newest first.
- `GET  /api/pipeline-runs/<run_id>` → the full record.
- `POST /api/pipeline-runs/<run_id>/delete`.

### UI — `app/templates/join.html`
A **"Past runs"** bar near "Saved pipelines". Each entry shows `name · time · ✓/✕ · N nodes`.
Clicking a run:
1. fetches the record;
2. loads `record.pipeline` into the canvas via the existing `loadRecord` path (nodes/edges/
   positions restore; ports re-fetch);
3. sets `runState` from `record.nodes` + `failed_node` + `provenance` with `finished:true`,
   then calls `applyRunState()` (reuses phase #3a — no new rendering code);
4. opens the Run section with the provenance table and per-node "view results / view log"
   links.

Read-only replay: editing the wiring after loading starts a new draft (as today). Delete via
an ✕ with confirm.

### Tests — `tests/test_pipeline_runs.py`
Mirrors `test_pipeline_store.py`, no Docker: save→list→load→delete round-trip; the record
references node `run_id`s without copying artifacts; `safe_id` traversal guard; newest-first
ordering; tolerant of missing optional fields (back-compat).

## Invariants (don't break)

- **No change to `pipeline.py`.** The run engine and the [HANDOFF §2](HANDOFF.md) invariants
  stay intact; persistence is a server-side wrapper + the new store module.
- **References, not copies.** Node artifacts remain owned by `_results/`; the record only
  points at them — cleanup / compare / chat keep working, no double storage.
- **Snapshot the wiring** so reload is faithful if the saved definition changes or is deleted.
- **Best-effort persistence** — never fail a run because history couldn't be written.

## Groundwork for B–C

The `nodes: {nid: {fskx, run_id, …}}` association is exactly what **Phase C** promotes from an
immutable snapshot into *current, mutable, saved-with-workflow* node state. Phase A delivers
value now and de-risks that storage model.

## Exit criteria

- Running a pipeline auto-creates a history record (success or failure).
- The Join page lists past runs newest-first; loading one restores the wiring and replays the
  final node states + provenance, with working links to each node's stored results / log.
- Records survive a server restart and **reference** (don't duplicate) node artifacts.
- `tests/test_pipeline_runs.py` passes without Docker.
