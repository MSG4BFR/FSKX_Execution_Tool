# Phase 1 — Serialization Layer 🚧

**Goal.** Make every model run actually *emit* the Phase 0 format, and provide the
reader/renderer the join engine will use. After this phase a single run produces a typed
`outputs.json` (+ sidecars) that any consumer can read.

**Scope note (revised).** The bundle serializes **all declared parameters**
(`INPUT`/`CONSTANT`/`OUTPUT`), each tagged with its classification and `unit`, not just
OUTPUT — a join may source a shared *input* as well as a result (see INTERCHANGE_SPEC §2).
Units travel with values for warn-on-mismatch + explicit conversion (INTERCHANGE_SPEC §5.1).

## Scope

- **`app/interchange.py`** — the Python side, three concerns, all table-driven:
  - *writer*: `encode_value(value, dataType)` → typed `data` dict (+ sidecar files);
    `write_bundle(params, outdir, ...)`.
  - *reader*: `read_bundle(path)` / `read_value(data, base_dir)` → native Python value
    (dataframe-OBJECT → `pandas.DataFrame`).
  - *renderer*: `render_rhs(data, language, base_dir)` → an R/Python source literal for
    injection. Used by the engine, **not** inside the wrappers.
- **`app/interchange.R`** — the R-side *writer* only (the R wrapper runs in Rscript and can't
  call Python). Mirrors `encode_value` + `write_bundle`. The reader/renderer stay Python-only
  because injection always happens in the Python orchestration layer.
- **Engine run-plan extension** — `engine.write_run_plan` records the model's declared
  parameters (`id`, `dataType`, `classification`, `unit`, `name`) via
  `serializable_param_specs` into `_run_plan.json`, so the wrappers know *what* to serialize
  without re-parsing `metaData.json`.
- **Wrapper wiring** — after a successful run, `run_python_model.py` / `run_r_model.R` look up
  each declared parameter id in the model namespace and emit `outputs.json` via the
  interchange writer (missing OUTPUTs warn; missing INPUT/CONSTANT are simply skipped).
  Best-effort and non-fatal: a serialization failure becomes a warning, never a failed run
  (consistent with the existing visualization policy).

## Design notes

- **Writer lives in two languages, reader/renderer in one.** Emitting must happen inside each
  model's env (Python or R); injecting always happens in the engine, which targets the *next*
  model's language by emitting a literal string — so the renderer needs no R runtime.
- **Why the run-plan carries output specs.** Keeps `metaData.json` parsing in one place
  (engine), mirrors how `_run_plan.json` already hands resolved script names to the wrappers.
- **`results.json` is untouched** — it still serves the UI/chat. `outputs.json` is the new
  machine contract.

## Files

| File | Change |
|---|---|
| `app/interchange.py` | new — writer/reader/renderer |
| `app/interchange.R` | new — R writer |
| `app/engine.py` | `write_run_plan` records OUTPUT param specs; `_sync_runners` copies `interchange.R` |
| `app/run_python_model.py` | emit `outputs.json` after run |
| `app/run_r_model.R` | source `interchange.R`; emit `outputs.json` |
| `tests/test_interchange.py` | new — round-trip + schema-validation tests |

## Exit criteria

- A Python run writes a schema-valid `outputs.json` with every declared parameter
  (INPUT/CONSTANT/OUTPUT) typed correctly and tagged with classification + unit (incl. a
  dataframe OBJECT → CSV sidecar).
- `read_bundle` reconstructs native values; `render_rhs` emits valid R and Python literals.
- Round-trip tests pass; all modules `py_compile`. R emission validated on a Docker machine
  (R unavailable in the dev sandbox — see DEVELOPER.md §7).
