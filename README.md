# FSKX Runner

Run food-safety predictive models (FSKX archives) on your own computer through a simple
web page — pick a model, adjust its parameters, and get the result plot and data files
back. No coding required.

## What you need

1. **Docker Desktop**, installed and running.
   Download it from https://www.docker.com/products/docker-desktop/ . After installing,
   start it once and wait until the whale icon says Docker is running.

2. **This folder** (the FSKX Execution Tool).
   By default the tool serves — and saves repository downloads into — a dedicated
   **`fskx_models`** folder inside this directory (created automatically). Put your own
   `.fskx` files there, or point the tool at any folder (see below).

3. **An internet connection** the first time you run each model (to download its software)
   and whenever you download models from the online repository.

That's it for normal use. A Claude API key is only needed for the optional AI features —
the **AI environment** builder and the **Talk to your model** chat (see below).

## How to start it

**macOS / Linux**

```bash
./run.sh
```

**Windows**

Double-click `run.bat` (or run it from a command prompt).

The first launch builds the tool's Docker image, which takes a minute or two. After that
your browser opens automatically at http://localhost:8000 . To stop the tool, press
`Ctrl+C` in the terminal (macOS/Linux) or close the window (Windows).

To use a different models folder or port, either pass them on the command line:

```bash
./run.sh /path/to/your/models 8000
```

or set them persistently in `.env` (next to `run.sh`) — this is the tool's config file:

```
MODELS_DIR=/path/to/your/models
PORT=8000
```

## Using it

**Run a model.** The home page lists every model — both the ones in your folder and those
in the online repository — with small status tags: **downloaded**, **runnable** (its
software environment is built), and **executed** (it has completed a run). Click a
downloaded model, adjust the input parameters (each field shows its name, unit, and
default), optionally pick a different scenario, and click **Run model**. The first run of a
given model also builds its software environment — this can take a few minutes and needs
internet; later runs of the same model are fast. When it finishes you'll see the plot(s)
in the page and download links for the result files (CSVs, the R workspace, etc.).

**Browse and compare past runs.** Every run is kept (until you remove that model's
environment), so you can come back to it later. On a model's page click **🕘 Run history**
in the header to see all its past runs, each tagged with its scenario, parameters and
status. Click any run to reopen its result page, or tick two or more and **Compare** them
side by side — plots in a column per run, a table highlighting the parameters that differ,
and each run's data files.

**Talk to your model.** Below the parameters on a model's page is a chat box. Ask Claude
about the model — its purpose, parameters, equations, assumptions, or the bundled paper and
documentation — and about its simulation results. Follow-up questions keep their context.
The chat is grounded in what's actually in front of you: on a model's page it considers all
stored runs, on a single run's page (and on the result page shown right after a run) just
that run, and on a comparison page just the runs you selected. This needs a Claude API key
(Settings); without one the chat is greyed out.

**Join models into a workflow.** Open **🔗 Join models** from the home page to chain several
models so one model's output feeds another's input — even across languages (R ↔ Python). Add
models as boxes on the canvas, drag them to arrange, then draw a connection by dragging from a
model's output port (right) or input port (left) onto another model's input port; the numbered
badge on each box shows the order it will run in, which the tool works out from the connections.
You can add an optional unit transform on a connection, feed one shared constant to several
models, then **Validate** and **Run** the whole chain. While it runs, each box lights up live
(running → done, or red on failure, with downstream boxes shown as blocked) and you can open any
node's results.

The workflow remembers what each node has already computed, so it works incrementally — like a
small no-code workflow tool:

- **Run** only re-executes what actually changed; everything still valid is reused from cache
  (reused nodes show a **♻**). Use **↻ Force re-run all** to ignore the cache and run everything
  fresh.
- Hover a box for two actions: **▶ Execute up to here** runs just that node and any out-of-date
  nodes feeding it (it's greyed out when the node is already up to date), and **↺ Reset** marks
  that node and everything downstream as needing a re-run. Each box carries an at-rest badge —
  green ✓ up-to-date, amber ● stale, ⊘ can't load.
- Edit a node's parameters (or a connection) and the affected nodes automatically become stale,
  so the next run recomputes exactly those and nothing else.

Pipelines (the wiring) can be **saved**, reloaded, and exported/imported as a single `.fskxp`
file to share. Separately, **💾 Save current state** snapshots which nodes have run and with
which results; reload a saved state later to pick up exactly where you left off. (Loops — a model
that ultimately feeds back into itself — aren't supported yet.)

**Stop the tool.** Click **⏻ Quit** in the header on the home page to shut the server down
cleanly — this also stops and removes its Docker container, so you don't have to find the
terminal window or force-stop anything in Docker Desktop. (Closing the terminal window with
its ✕ does **not** reliably stop the container on Windows; use Quit, or press `Ctrl+C` in
the terminal.)

**Add a model.** Use the filter box to find a model, then just click any one that isn't
downloaded yet — it's fetched from the RAKIP / FSKX public catalogue into your folder and
opens ready to run. Use **⟳ Rescan** if you added `.fskx` files to the folder manually.

**Free up space.** The header on each model's page has a **🗑 Remove environment** button
that deletes its built environment, AI image, and stored run results (the model file stays;
it just rebuilds next run). **🧹 Clean all caches** on the home page does this for every
model at once. Note that removing a model's environment also deletes its stored run history.

**Models with complex software (advanced, optional).** A few models need extra system
software (JAGS, Stan, OpenBUGS, GDAL…). If a model won't run, its result page offers
**Fix with AI**, and every model page has an **AI environment** button. This uses Claude
to write a tailored Docker setup for that model. To use it:

1. Open **⚙ Settings** and paste your Anthropic API key (get one at
   https://console.anthropic.com ). A key entered here lasts only for the session. To avoid
   re-entering it each launch, copy `.env.example` to `.env` (next to `run.sh`) and set
   `ANTHROPIC_API_KEY=sk-ant-…`; the launcher loads it automatically. Keep `.env` private —
   it's never committed or built into the image.
2. On the model's **AI environment** page, click **Generate Dockerfile with AI**, review
   the result, and click **Build image & use it**.
3. Run the model as usual — it will now use the AI-built environment.

If a build fails, read the log and click Generate again; it learns from the error and
tries a corrected version.

## Where your files live

- **Models:** the `fskx_models` folder inside this directory by default (or whatever
  `MODELS_DIR` points to) — read-write, so repository downloads land here.
- **Results:** kept inside the tool between runs (and across restarts) and browsable any
  time via **Run history** on the model's page — open a single run or compare several. They
  are offered as downloads on the result page; save anything you want to keep to your own
  location, as they're deleted when you remove a model's environment or clean all caches.

## Troubleshooting

- **"Docker is not installed or not running"** — install/start Docker Desktop and retry.
- **A model's first run is slow** — that's the one-time environment build; it's cached
  afterward.
- **AI features are greyed out** — set your API key in Settings, and make sure you started
  the tool with the provided launcher (it grants the Docker access the feature needs). The
  tool checks your key at startup; if you copied `.env.example` to `.env` but left the
  `sk-ant-…` placeholder in place, or the key is rejected, Settings shows the reason.
- **Something looks wrong with a result** — open the "Execution log" on the result page;
  it usually says what happened.
