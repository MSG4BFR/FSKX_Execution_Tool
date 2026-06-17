# FSKX Runner — Developer Guide

Internal documentation: architecture, design decisions, the pitfalls we already hit and
resolved, and the ones likely to surface as the tool is extended.

---

## 1. What this is

A generic, local execution environment for FSKX models. An FSKX archive is a ZIP/OMEX
container holding a predictive model (R or Python), its parameter scenarios, a
visualization script, and metadata (`metaData.json`, `packages.json`, `sim.sedml`,
manifest, etc.). FSK-Lab's execution semantics are: run the **parameter script**, then
the **model script**, then the **visualization script** in one shared session.

The tool reproduces those semantics, wraps them in a web UI, and resolves each model's
dependencies automatically. It is deliberately model-agnostic: nothing about any specific
model is baked into the image. The two original examples (a Yersinia Monte-Carlo R model
and an egg-supply-chain Python model) are just test cases.

### Design goals

- **Generic.** Works for any R or Python FSKX model, with differing language *versions* and
  dependency sets, not just the bundled examples.
- **Self-service.** A non-developer picks a model, edits parameters, runs, downloads
  results — all in the browser.
- **Reproducible-ish isolation.** Each model gets its own environment, cached so the cost
  is paid once.
- **Extensible.** Online-repository download and AI-assisted environment building for the
  hard cases.

---

## 2. Architecture

```
run.sh / run.bat                     host launcher: docker build + docker run
        │  mounts: models(rw), fskx_envs, fskx_work, /var/run/docker.sock
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ App container (mambaorg/micromamba base, runs as root)                    │
│                                                                           │
│  server.py    Flask UI + JSON API + background-job registry               │
│  engine.py    extract archive, parse params, pick backend, run, collect   │
│  depresolve.py  language/version + package resolution, micromamba envs    │
│  repo.py      RAKIP public-API client (list + download)                   │
│  aienv.py     Claude → Dockerfile, per-model image build, recipes         │
│  state.py     persistent per-model status (executed history)              │
│  run_python_model.py / run_r_model.R   the in-environment wrappers        │
│                                                                           │
│  Execution backends (chosen per model):                                   │
│   1. docker  → AI-built per-model image, run as SIBLING container         │
│   2. micromamba → per-model env under /opt/conda/envs (the default)       │
│   3. direct  → interpreter on PATH (local/native, no micromamba)          │
└─────────────────────────────────────────────────────────────────────────┘
        │ (backend 1 only) docker build / docker run via mounted socket
        ▼
   Sibling model container  ── shares the fskx_work volume at /work ──┐
        runs <interp> /app/run_*_model.* /work/<model> /work/<out>    │
        results written back onto fskx_work, read by the app ◄────────┘
```

### Execution flow (`engine.execute`)

1. **Extract** the `.fskx` into `/work/<model>` (the `fskx_work` named volume).
2. **Resolve** language/version + packages (`depresolve.resolve`).
3. **Build the parameter script**: the chosen scenario file verbatim, then user overrides
   appended (later assignment wins), written as `params.py` / `params.R`.
4. **Choose backend** (`engine.choose_backend`): AI image if one exists for this dep hash;
   else build/reuse the micromamba env; else direct.
5. **Run** the wrapper in that backend. The wrapper sources params → model → visualization
   in one namespace, captures plots, serializes results, copies output files.
6. **Collect** plots, `results.json`, `status.json`, and any model-written files; return
   them to the UI. On success, record the model's **executed** flag (`state.mark_executed`).

### Backend selection cache

The per-model environment is keyed by `depresolve.env_key(spec)` = hash of
(language, version, sorted packages). The **existence of the artifact is the cache**: a
micromamba env directory under `/opt/conda/envs/<key>`, or a Docker image tagged
`fskx-model-<key>`. This survives app restarts with no registry file. AI images take
precedence over micromamba envs for the same hash.

### Unified model list + status badges

The home page (`server.index`) shows **one list** of all models: the online catalogue
(`repo.list_remote`) merged with local files, downloaded ones matched to their catalogue
entry by the `__<id8>.fskx` suffix (`repo.downloaded_filename`). A not-downloaded card
posts straight to the download flow on click; downloaded/bundled cards link to the run
form. There is no separate repository page anymore (`/repository` just redirects).

Each downloaded model carries three badges — **downloaded**, **runnable**, **executed** —
produced by `engine.model_status`:

- **runnable** is derived **live** from whether the artifact actually exists (its image or
  its env), *not* from a stored flag. Because the artifact is keyed by the dependency
  fingerprint (`env_key`), it is **shared by every model with identical deps**. Deriving
  the badge live means removing a shared image/env is reflected in *all* sharing models for
  free — no per-model bookkeeping, no stale "runnable" badge.
- **executed** is the one genuinely per-model fact (different models can share deps but have
  different scripts), persisted in `state.py` on a successful run. It is reported True
  **only while the environment is present**, so removing a shared artifact also drops the
  executed badge for every sharing model. `index` fetches the existing-image set once
  (`engine.existing_model_images`) and reuses it for every card to avoid a docker call per
  model; `model_status` reuses the dir `model_info` already extracted.

### FSKX/OMEX format handling (the standard, not filename guessing)

An FSKX archive is an OMEX/COMBINE ZIP. Two sidecars are authoritative and `omex.py` reads
them instead of assuming conventional filenames:

- **`metadata.rdf`** declares each file's ROLE:
  `<rdf:Description rdf:about="/path"><dc:type>modelScript|visualizationScript|readme</dc:type>`.
  So the model/viz scripts can have **any** name; `omex.resolve_scripts` maps roles → files
  (RDF → SED-ML `<model source>` → `model.<ext>` convention → single-script heuristic).

- **`sim.sedml`** holds the SIMULATION DATA: one `<model id="…">` per scenario, each with a
  `<listOfChanges>` of `<changeAttribute target="p" newValue="…"/>`. This is the source of
  truth for scenarios and parameters; the `simulations/` folder (when present) is only a
  fallback. Values are read as XML **attributes**, so entity escaping is undone for us — a
  string param stored as `newValue="&quot;Eggs&quot;"` comes back as `"Eggs"`, identical to
  the materialised script (no manual unescaping, no double-quoting).

`engine` wires this in: `scenario_names` / `scenario_assignments` (SED-ML first),
`build_param_fields(assignments, metadata)`, and `write_run_plan` (the resolved param/model/
viz names handed to the wrapper via `_run_plan.json`). **Fail-fast**: if no model script
resolves or no scenario/parameter source exists, `execute` returns a clear "not runnable as
a standard FSK-ML model" result and `model_info` sets `error` (the model page hides Run).
`manifest.xml` validation is intentionally not done (yet).

Output capture and display follow the same "don't assume the root folder" principle: the
wrappers capture new/modified files **recursively** (preserving subpaths), and
`classify_outputs` walks the result tree, showing every image (png/svg/jpg/gif/webp/bmp/pdf,
device captures first) and listing the rest as downloads. Result URLs encode each path
segment but keep the slashes; the `result_file` traversal guard still applies.

---

## 3. Key components

- **`depresolve.py`** — The dependency brain. Reads `packages.json` for language/version,
  **and scans the scripts** for `import`/`library()` calls because `packages.json` is
  frequently incomplete (the egg model declares zero packages but needs five). Maps import
  names → install names (`descartes`, `cv2`→`opencv-python`, etc.), filters stdlib, and
  creates micromamba envs (conda-forge for compiled/geo packages, pip for the rest; R
  packages via `r-<name>` with a CRAN fallback).

- **`engine.py`** — Orchestration: extraction, parameter generation, backend selection,
  run dispatch, output collection/classification (`classify_outputs`), the AI generate/build
  helpers, live status (`model_status`), and cache cleanup (`cleanup_model` / `cleanup_all`).
  Script/scenario resolution and parameters now come from `omex.py` (see §2.x).

- **`omex.py`** — FSKX/OMEX introspection. Parses `metadata.rdf` for file ROLES
  (`modelScript` / `visualizationScript` / `readme`) and `sim.sedml` for SIMULATION DATA
  (one `<model id>` per scenario, `changeAttribute` parameter values). `resolve_scripts`
  picks the model/viz scripts by declared role (RDF → SED-ML `source` → `model.<ext>`
  convention → single-script heuristic). Everything is best-effort and side-effect free; the
  caller (engine) applies the fail-fast policy. See §2.x.

- **`state.py`** — Tiny persistent store for the per-model **executed** flag, written as
  JSON on the `fskx_work` volume (so it survives restarts like the env/image cache). Keyed
  by `.fskx` filename. Holds nothing sensitive. (Runnable status is *not* stored here — it
  is derived live; see §2.)

- **`run_python_model.py` / `run_r_model.R`** — The in-environment wrappers, identical in
  spirit across backends. They read `_run_plan.json` for the actual script names (so scripts
  need not be `model.<ext>`), reproduce FSK-Lab semantics, treat the visualization step as
  **best-effort** (a partial plot is still saved), serialize the model's namespace (capping
  large objects), and **recursively** capture files created or modified during the run
  (models may write into subfolders). The R wrapper autoprints the visualization (so a bare
  trailing `ggplot(...)` actually draws); the Python wrapper installs a small matplotlib
  colorbar compat shim. For docker-backed runs these wrappers execute from `/work/_runner/`
  on the shared volume (synced each run by `engine._sync_runners`), not from a copy baked
  into the per-model image — so wrapper changes apply without rebuilding that image.

- **`repo.py`** — RAKIP public API client. Lists the catalogue
  (`/public/models?hide_*`), downloads `/{id}/files/fskx_file.fskx`, verifies ZIP magic
  bytes, and saves under a sanitized `name__<id8>.fskx`.

- **`aienv.py`** — The AI path: gathers model context (language, deps, scripts, host arch,
  matched recipes, previous attempt + error), prompts Claude for a Dockerfile, sanitizes
  it (strip fences, neutralize ENTRYPOINT/CMD, ensure the wrapper `COPY`), builds a
  per-model image, and runs it as a sibling container.

- **`server.py`** — Flask routes + a small in-memory job registry (runs, downloads, AI
  generate/build, and cleanup all run in background threads and are polled by the page JS).
  In-memory settings (API key/model), `LAST_ERROR` and `LAST_DOCKERFILE` per model for AI
  iteration. The unified home page lives here (`index` + `_local_entry`); `cleanup_model` /
  `cleanup_all` routes drive the per-model "Remove environment" and global "Clean all
  caches" actions.

---

## 4. Pitfalls already encountered and resolved

These are real issues from building the tool. Keep them in mind before "simplifying".

1. **`packages.json` is unreliable.** It often lists no/incomplete packages and an
   unusable language version (`"Python 3.4.8"`). We merge it with an import scan and treat
   the version as best-effort with a modern fallback. Don't trust it alone.

2. **Import-scan regex missed dotted modules.** `from descartes.patch import …` wasn't
   matched by the first regex, so `descartes` wasn't installed → ImportError. The regex now
   handles dotted paths (`from a.b import`, `import a.b.c`). Watch this when adding mappings.

3. **Old model code vs. modern libraries.** The egg model's `visualization.py` uses a
   `matplotlib.colorbar` API that raises on modern matplotlib. Fix: visualization is
   **non-fatal** — the already-drawn figure is saved and the error becomes a warning. Don't
   make the visualization step fatal.

4. **`results.json` ballooned to megabytes.** Serializing the full namespace dumped huge
   dicts/DataFrames. We cap JSON values (~4 KB) and write DataFrames to CSV instead.

5. **Output capture missed overwritten files.** The R model overwrites a CSV that already
   ships *inside* the archive, so "new files only" detection missed it. We snapshot mtimes
   and capture files that are new **or modified**. (Earlier we tried wall-clock time, which
   was fragile around extraction timing — mtime snapshot is the robust version.)

6. **R serializer leaked runner internals.** `ls()` returned the wrapper's own variables.
   We snapshot the environment before sourcing the model and serialize only model-created
   objects.

7. **Filename casing.** Archives vary (`model.r` vs `model.R`). Both wrappers resolve
   script names case-insensitively.

8. **Docker named-volume ownership.** `/work` and `/opt/conda/envs` are created
   **root-owned** by Docker; the micromamba image runs as `mambauser` → `PermissionError`.
   Resolution: **run the app container as root** (`USER root`). For a local single-user
   tool this is the simplest robust choice and also lets the app write root-owned volumes
   from a previous failed run without manual cleanup.

9. **Docker CLI install needs root.** Copying the static `docker` binary into
   `/usr/local/bin` failed as `mambauser`. Needs `USER root` *before* that step.

10. **Cross-architecture (Apple Silicon).** The AI's first OpenBUGS Dockerfile used
    `wine32`/i386 on an `arm64` base — those packages don't exist there. Two-part fix:
    (a) tell the AI the host arch and that i386/multilib is amd64-only, so on arm64 it must
    use `FROM --platform=linux/amd64`; (b) **OpenBUGS is not a Windows .exe** — it compiles
    from source on Linux. We encode the verified recipe (working `webbugs.psychstat.org`
    URL, `libc6-dev-i386 gcc-multilib`, `R2OpenBUGS` via `remotes::install_version`) in
    `aienv.KNOWN_RECIPES`.

11. **AI build failures need a feedback loop.** We store the failed Dockerfile and build
    log per model (`LAST_DOCKERFILE`, `LAST_ERROR`); the next "Generate" includes them so
    Claude iterates on the specific error instead of repeating it.

12. **Long operations can't block a request.** Env builds and image builds take minutes.
    All such work runs in background threads with a job registry the page polls; nothing
    relies on a single long HTTP request.

13. **Sibling container volume sharing.** The AI image runs via the host daemon (mounted
    socket), so its `-v` mounts resolve on the **host**. We mount the same `fskx_work`
    **named volume** (`FSKX_WORK_VOLUME`) into the sibling at `/work`, so absolute `/work/…`
    paths are valid in both containers. This only works because `FSKX_WORK_DIR=/work` is
    exactly the volume mount point.

14. **R `[[` on an atomic vector errors on a missing name.** The R wrapper's file-capture
    compares each file's mtime against a pre-run snapshot (`.before_mtimes`, a *named POSIXct
    vector*). `list[[absent]]` returns NULL, but `atomic_vector[[absent_name]]` raises
    `subscript out of bounds`. A model that writes a **brand-new** output file (vs. one that
    only overwrites an existing input, the case we first tested) hit this and the whole run
    failed. Fix: check `nf %in% names(.before_mtimes)` before indexing; an absent name means
    a new file → keep it. Mirror of the Python wrapper, where `dict.get` already returns None.

15. **R ggplot built but never drawn.** `source(echo=FALSE)` defaults `print.eval=FALSE`, so
    a visualization ending on a bare `ggplot(...)` object (the common FSK-Lab pattern) was
    built — firing e.g. the `size`→`linewidth` warning — but never printed, leaving the PNG
    device empty ("null device 1"). Fix: source the visualization with `print.eval=TRUE`
    (autoprint, like the REPL/Rscript). Base-graphics scripts draw as side effects and were
    unaffected; the model script still runs without autoprint.

16. **Old matplotlib API in viz scripts.** `fig.colorbar(sm)` on a free-standing
    `ScalarMappable` raised "Unable to determine Axes…" on modern matplotlib (old matplotlib
    stole from the current axes). The Python wrapper installs a non-invasive shim that
    defaults `ax` to the figure's current axes only on that otherwise-failing path.

17. **Outputs aren't always at the root.** Models may write results/plots into subfolders
    (e.g. a `visualizations/` dir, or `runs/<ts>/…`). The wrappers snapshot/capture
    **recursively** (preserving relative paths), and `classify_outputs` walks the tree, so
    nested images display and nested data files download. Result URLs encode segments but
    keep slashes so `<path:name>` serves them.

18. **The wrapper was frozen into the per-model image.** AI images `COPY app/ /app/`, so a
    docker-backed run used the wrapper baked in at build time — wrapper fixes silently
    didn't apply until the image was rebuilt (a real "why no nested plots?" trap). Fix: the
    docker backend now runs the wrapper from `/work/_runner/…` on the shared volume, synced
    each run (`engine._sync_runners`); the image's `/app` copy is just a fallback. (The app
    image `fskx-runner` is itself rebuilt every `run.sh` launch, so engine/template/wrapper
    changes land on restart; only per-model images are cached.)

## 5. Known limitations & future pitfalls

- **Security: the Docker socket.** Mounting `/var/run/docker.sock` gives the app container
  full control of the host Docker daemon — acceptable for a local single-user tool, but do
  not expose this server on a network. Future hardening: a rootless/remote Docker endpoint,
  or a build-only proxy.

- **Running the app as root.** Same single-user assumption. Revisit before any multi-user
  or hosted deployment.

- **No request authentication.** The server binds `0.0.0.0:8000` with no auth. Fine on
  `localhost`; never expose as-is.

- **Emulated amd64 images are slow.** OpenBUGS et al. run under QEMU emulation on Apple
  Silicon. Functional but slow; the image cache makes it a one-time build cost. A native
  arm64 path (e.g. JAGS instead of OpenBUGS) is faster where the science allows it.

- **AI environments cost money and vary.** Each generation is a Claude API call paid by the
  user's key. Output is not guaranteed correct on the first try — the recipe table + the
  feedback loop mitigate this. Grow `KNOWN_RECIPES` as new system-software models appear
  (INLA, NIMBLE, GDAL/geo, sf, rstan/cmdstan variants, …).

- **Model version pinning is approximate.** We can't reliably install EOL Python (3.4) or
  arbitrary historical R versions from conda-forge; we fall back to modern versions. Most
  scientific code runs fine, but API drift (pitfall #3) can bite. The AI/Docker path is the
  escape hatch for models that genuinely need an exact old stack.

- **In-memory state.** Jobs, settings, and AI iteration history live in process memory and
  reset on restart. What persists: the dependency-hash/image cache, and the per-model
  **executed** flag (`state.py`, on the `fskx_work` volume). The app still never writes the
  API key to disk — for convenience the launcher loads a local `.env` (next to `run.sh`;
  `ANTHROPIC_API_KEY=…`, `API_KEY` accepted as an alias) and passes it in via `-e`, so the
  key lives only in that user-managed file (git/Docker-ignored, see `.env.example`) and the
  process environment, prefilling `SETTINGS`. Settings-UI edits still override per session.

- **Run results are kept until explicitly cleaned.** Each run writes a timestamped folder
  under `/work/_results/<model>/` on the `fskx_work` volume (never the host models folder).
  Nothing prunes them automatically *except* the cleanup actions: `cleanup_model` deletes
  that model's results (and image + env), `cleanup_all` deletes the whole `_results` tree.
  No retention cap and no "save to host folder" option yet — both are natural next steps.

- **Parameter parsing is line-based.** `parse_assignments` handles single-line
  `name <- value` / `name = value`. Multi-line or computed parameter assignments in a
  scenario file won't become editable form fields (they're still applied verbatim because
  the whole scenario file is written before the overrides). If a model needs multi-line
  params editable, extend the parser.

- **String parameters.** STRING params are detected and re-quoted on submit. Enumerated
  string choices (e.g. the egg model's fixed `actor` list) are not auto-detected into a
  dropdown — they remain free-text. A per-model enum hint could improve UX.

- **Repository assumptions.** `repo.py` hard-codes the RAKIP gateway URL and the
  `fskx_file.fskx` download path, and dedupes by an 8-char id suffix in the filename. If the
  API shape changes, update `repo.py` (and re-check the `is_downloaded` heuristic).

- **Dockerfile sanitization is heuristic.** We strip fences and neutralize ENTRYPOINT/CMD,
  and append the wrapper `COPY` if missing. A multi-stage Dockerfile from the AI could
  defeat the appended `COPY` (wrong stage). The system prompt asks for single-stage; if
  multi-stage becomes necessary, make the `COPY`-injection stage-aware.

- **`results.json` for huge R objects.** We cap by length/structure heuristically; very
  large atomic vectors could still be sizeable. Tighten if needed.

---

## 6. Extending the tool

- **Add a verified build recipe.** Append to `aienv.KNOWN_RECIPES` a dict with `triggers`
  (lowercase substrings found in the model's scripts/packages) and a `hint` string. It is
  injected verbatim as a "KNOWN-GOOD BUILD HINT" when matched. This is the highest-leverage
  way to make new system-software models "just work".

- **Add an import→package mapping.** Edit `depresolve.PY_IMPORT_TO_PKG` (and
  `CONDA_PREFERRED_PY` if it needs compiled/system libs).

- **Change the default Claude model.** `SETTINGS["model_id"]` default in `server.py`
  (currently `claude-sonnet-4-6`), overridable in the Settings UI or via
  `FSKX_CLAUDE_MODEL`.

- **Relevant env vars:** `FSKX_MODELS_DIR`, `FSKX_WORK_DIR`, `FSKX_ENV_LOGS`,
  `FSKX_WORK_VOLUME`, `PORT`, `ANTHROPIC_API_KEY`, `FSKX_CLAUDE_MODEL`, `MAMBA_ROOT_PREFIX`.

- **Launcher config (`.env`).** `.env` next to `run.sh` is the single host-side config file,
  loaded by the launcher. Inside the container `FSKX_MODELS_DIR=/models` is fixed (the mount
  point); the configurable thing is the **host** folder mounted there, set via `MODELS_DIR`
  (CLI arg > `.env` > default `fskx-runner/fskx_models`, created if missing). `PORT`,
  `ANTHROPIC_API_KEY`/`API_KEY` and `FSKX_CLAUDE_MODEL` are resolved the same way and passed
  in with `-e`. The default models folder changed from the script's parent to the dedicated
  `fskx_models/` (git/Docker-ignored).

---

## 7. Testing notes

- The Python path is validated end-to-end (egg model, parameter overrides → distinct
  correct maps, output capture, web flow).
- The R path was validated by static review plus the shared resolver/parameter logic; the R
  interpreter itself runs only in Docker (conda-forge/`r-mc2d` are blocked in the dev
  sandbox). Validate R runs on a real machine.
- The Docker-socket build/run and the live Claude API call cannot be exercised without
  Docker + network; verify those on the user's machine. The OpenBUGS recipe was confirmed
  against a known-good production Dockerfile.
- Quick checks worth keeping: `python -m py_compile` on all modules; `depresolve.resolve`
  on a model; `aienv.gather_context` to inspect the assembled prompt; `aienv.matched_recipes`
  for recipe triggering.
