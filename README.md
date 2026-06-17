# FSKX Runner

Run food-safety predictive models (FSKX archives) on your own computer through a simple
web page — pick a model, adjust its parameters, and get the result plot and data files
back. No coding required.

## What you need

1. **Docker Desktop**, installed and running.
   Download it from https://www.docker.com/products/docker-desktop/ . After installing,
   start it once and wait until the whale icon says Docker is running.

2. **The `fskx-runner` folder** (this folder).
   By default the tool serves — and saves repository downloads into — a dedicated
   **`fskx_models`** folder inside `fskx-runner` (created automatically). Put your own
   `.fskx` files there, or point the tool at any folder (see below).

3. **An internet connection** the first time you run each model (to download its software)
   and whenever you download models from the online repository.

That's it for normal use. A Claude API key is only needed for the optional
"AI environment" feature (see below).

## How to start it

**macOS / Linux**

```bash
cd fskx-runner
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

**Add a model.** Use the filter box to find a model, then just click any one that isn't
downloaded yet — it's fetched from the RAKIP / FSKX public catalogue into your folder and
opens ready to run. Use **⟳ Rescan** if you added `.fskx` files to the folder manually.

**Free up space.** Each model's page has a **Remove environment** button that deletes its
built environment, AI image, and stored run results (the model file stays; it just rebuilds
next run). **🧹 Clean all caches** on the home page does this for every model at once.

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

- **Models:** the `fskx_models` folder inside `fskx-runner` by default (or whatever
  `MODELS_DIR` points to) — read-write, so repository downloads land here.
- **Results:** kept inside the tool between runs and offered as downloads on the result
  page. Save anything you want to keep to your own location — they're deleted when you
  remove a model's environment or clean all caches.

## Troubleshooting

- **"Docker is not installed or not running"** — install/start Docker Desktop and retry.
- **A model's first run is slow** — that's the one-time environment build; it's cached
  afterward.
- **AI features are greyed out** — set your API key in Settings, and make sure you started
  the tool with the provided launcher (it grants the Docker access the feature needs).
- **Something looks wrong with a result** — open the "Execution log" on the result page;
  it usually says what happened.
