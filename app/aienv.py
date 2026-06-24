"""
AI-assisted environment building for complex models.

Some FSKX models need system software that the generic micromamba build can't provide
(JAGS, Stan, OpenBUGS, GDAL, compilers, OS libraries…). For those, we ask Claude to
write a Dockerfile tailored to the model, build a per-model image, and run the model
inside it as a sibling container that shares the work volume.

The image is keyed by the model's dependency hash, so once built it is reused across
app restarts (its existence is the cache).
"""

import base64
import json
import os
import platform
import shutil
import subprocess
import tempfile

import requests

import depresolve

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Default endpoint for a local OpenAI-compatible server (LM Studio, Ollama, llama.cpp …).
# NOTE: the app runs INSIDE a container, so `localhost` would point at the container, not
# the host where LM Studio listens. `host.docker.internal` reaches the host on Docker
# Desktop (macOS/Windows) and on Linux when the launcher adds the host-gateway mapping.
DEFAULT_LOCAL_URL = "http://host.docker.internal:1234/v1"


def provider_config(settings):
    """Normalize a settings-like dict into a provider config used by the LLM calls.

    Keys: provider ('anthropic'|'local'), api_key, model_id (Claude model),
    local_url (OpenAI-compatible base, …/v1), local_model (id served locally).
    """
    provider = (settings.get("provider") or "anthropic").strip().lower()
    if provider not in ("anthropic", "local"):
        provider = "anthropic"
    return {
        "provider": provider,
        "api_key": (settings.get("api_key") or "").strip(),
        "model_id": (settings.get("model_id") or "claude-sonnet-4-6").strip(),
        "local_url": (settings.get("local_url") or DEFAULT_LOCAL_URL).strip().rstrip("/"),
        "local_model": (settings.get("local_model") or "").strip(),
    }

# Contract the generated Dockerfile must satisfy. The image is run as:
#   docker run --rm -v fskx_work:/work <image> <INTERPRETER> /app/<runner> /work/<in> /work/<out>
SYSTEM_PROMPT = """\
You are an expert DevOps engineer. You write a single, correct, minimal Dockerfile that
provides the execution environment for ONE scientific model from an FSKX (food-safety)
archive. The model is run by a fixed wrapper script that will already be present in the
build context under ./app/ (run_python_model.py and run_r_model.R).

The resulting image will be invoked EXACTLY like this (the command is supplied at run
time, so do NOT hard-code it):
  - Python model: python /app/run_python_model.py /work/<dir> /work/<out>
  - R model:      Rscript /app/run_r_model.R /work/<dir> /work/<out>

Hard requirements for your Dockerfile:
  1. Choose an appropriate base image for the language and version.
  2. Install ALL needed system packages (e.g. jags, stan/cmdstan, openbugs, gdal, build
     tools) AND all language packages the model imports/uses.
  3. For R models you MUST ensure the 'jsonlite' package is installed (the wrapper uses it).
  4. The interpreter must be on PATH as `python` (Python) or `Rscript` (R).
  5. Include `COPY app/ /app/` so the wrapper scripts are available at /app.
  6. Do NOT set ENTRYPOINT or CMD — the run command is passed externally.
  7. Set `ENV MPLBACKEND=Agg` for Python models that plot.

ARCHITECTURE — read carefully:
  - The build host's architecture is stated in the context (TARGET ARCH).
  - Prefer NATIVE builds for that architecture. JAGS, Stan/cmdstan, GDAL and almost all
    R/Python packages have native arm64 and amd64 builds — use the normal base image and
    do NOT force a platform for them.
  - Some software needs a 32-bit (i386) toolchain. The i386 multilib packages
    (`libc6-dev-i386`, `gcc-multilib`) exist ONLY on amd64. So:
      * On an amd64 host, a normal amd64 base works and you may use multilib directly.
      * On an arm64 host, you MUST make the whole image amd64 to get those packages:
        start the Dockerfile with `FROM --platform=linux/amd64 <base>`. Under that amd64
        base, installing i386/multilib packages is correct. Docker runs it via emulation.
    NEVER add i386/multilib packages on a NATIVE arm64 base — they don't exist there.
  - Do NOT use wine or Windows .exe installers for Linux base images. Tools like OpenBUGS
    compile from source on Linux; use the source build, not a .exe.
  - Avoid dead download URLs. Prefer distribution packages or KNOWN-GOOD recipes given in
    the context; if you compile from source, use a URL proven to exist.

If the context contains KNOWN-GOOD BUILD HINTS, follow them closely — they are verified
recipes for this exact software. If it contains a PREVIOUS ATTEMPT and its BUILD ERROR,
return a corrected Dockerfile that fixes that specific error; do not repeat the failing
approach.

Output ONLY the Dockerfile content. No explanations, no markdown fences.
"""

# Curated, verified build recipes injected into the prompt when the relevant software is
# detected in the model. Keyed by lowercase trigger substrings found in scripts/packages.
KNOWN_RECIPES = [
    {
        "triggers": ["openbugs", "r2openbugs", "brugs"],
        "hint": (
            "OpenBUGS (verified recipe): it compiles from source on Linux — do NOT use "
            "wine or a .exe. It needs a 32-bit toolchain, so on arm64 hosts start with "
            "`FROM --platform=linux/amd64 rocker/r-ver:4.5` (or rocker/tidyverse:4.5). "
            "Install system deps: `curl cmake jags wget unzip libx11-dev "
            "libglu1-mesa-dev libfreetype6-dev libxml2-dev libssl-dev "
            "libcurl4-openssl-dev libc6-dev-i386 gcc-multilib`. Then:\n"
            "  RUN wget https://webbugs.psychstat.org/wiki/Download/OpenBUGS-3.2.3.tar.gz "
            "&& tar -xzf OpenBUGS-3.2.3.tar.gz && cd OpenBUGS-3.2.3 && ./configure && "
            "make && make install && cd .. && rm -rf OpenBUGS-3.2.3*\n"
            "Install the R interface with remotes: "
            "`R -e \"install.packages('remotes')\" -e "
            "\"remotes::install_version('R2OpenBUGS', version='3.2-3.2.1')\"`. "
            "Also install jsonlite. The webbugs.psychstat.org URL is known to work; the "
            "openbugs.net URLs are dead."
        ),
    },
    {
        "triggers": ["rjags", "jags"],
        "hint": ("JAGS: install the distro package `jags` (native on arm64 and amd64), "
                 "then the R package with `install.packages('rjags')`. No platform "
                 "override needed."),
    },
    {
        "triggers": ["rstan", "cmdstanr", "brms", "stan"],
        "hint": ("Stan: prefer a base with a C++ toolchain (`build-essential`). For "
                 "rstan/brms use `install.packages(...)`; for cmdstanr also install "
                 "CmdStan. Native on arm64 and amd64 — no platform override needed."),
    },
]


def matched_recipes(model_dir):
    """Return verified build hints whose triggers appear in the model's files."""
    blob = ""
    for root, _d, names in os.walk(model_dir):
        for n in names:
            if os.path.splitext(n)[1].lower() in (".r", ".py", ".json", ".txt"):
                blob += _read_clip(os.path.join(root, n), 6000).lower() + "\n"
    hints = []
    for rec in KNOWN_RECIPES:
        if any(t in blob for t in rec["triggers"]):
            hints.append(rec["hint"])
    return hints


def host_arch():
    """Docker-style architecture string for the build host (arm64 / amd64)."""
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    return m or "amd64"


# ---------------------------------------------------------------------------
# Docker availability
# ---------------------------------------------------------------------------

def key_format_ok(api_key):
    """Cheap, offline plausibility check for an Anthropic key.

    Catches the most common foot-gun: copying .env.example to .env and leaving the
    `sk-ant-...` placeholder in place. That placeholder is a non-empty (truthy) string,
    so a bare `if api_key:` happily treats it as configured and the first real API call
    then 401s. Reject empty values, the literal placeholder (any `...`/`…`), and anything
    not shaped like a real key.
    """
    key = (api_key or "").strip()
    if not key or "..." in key or "…" in key:
        return False
    return key.startswith("sk-ant-") and len(key) >= 30


def verify_api_key(api_key, model_id="claude-sonnet-4-6", timeout=20):
    """Check that the key can actually reach the Anthropic API.

    Returns (ok: bool, detail: str). Does the offline format check first, then a minimal
    Messages call (max_tokens=1) so we learn the real auth/connectivity state instead of
    only guessing from the string. Intended to run once at startup and whenever the key
    changes — never on a hot path.
    """
    key = (api_key or "").strip()
    if not key:
        return False, "No API key set."
    if not key_format_ok(key):
        return False, ("The key looks like the placeholder from .env.example — replace "
                       "sk-ant-... with your real key (it should start with sk-ant- and "
                       "contain no '...').")
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {"model": model_id, "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}]}
    try:
        r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"Could not reach the Anthropic API: {exc}"
    if r.status_code == 200:
        return True, "Key verified — Claude is reachable."
    if r.status_code in (401, 403):
        return False, ("The API key was rejected by Anthropic "
                       f"(HTTP {r.status_code} authentication_error). Check the key.")
    if r.status_code == 404:
        return False, (f"The model '{model_id}' is not accessible with this key "
                       "(HTTP 404). Check the model id under Settings.")
    return False, f"Anthropic API error {r.status_code}: {r.text[:200]}"


def verify_local(local_url, local_model, timeout=20):
    """Check that a local OpenAI-compatible endpoint is reachable and has a model.

    Returns (ok, detail). Tries GET {url}/models first (cheap, lists loaded models);
    falls back to a tiny chat completion if that route isn't served.
    """
    url = (local_url or "").strip().rstrip("/")
    if not url:
        return False, "No local endpoint URL set."
    headers = {"Content-Type": "application/json", "Authorization": "Bearer lm-studio"}
    try:
        r = requests.get(url + "/models", headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return False, (f"Could not reach the local endpoint at {url} ({exc}). If the app "
                       "runs in Docker, use http://host.docker.internal:<port>/v1, not "
                       "localhost, and make sure LM Studio's server is started.")
    if r.status_code == 200:
        ids = []
        try:
            ids = [m.get("id", "") for m in (r.json().get("data") or [])]
        except ValueError:
            pass
        if local_model and ids and local_model not in ids:
            return True, (f"Endpoint reachable, but '{local_model}' isn't in the loaded "
                          f"models ({', '.join(ids) or 'none'}). Load it in LM Studio or "
                          "fix the model id.")
        return True, f"Local endpoint reachable at {url}."
    return False, f"Local endpoint returned HTTP {r.status_code}: {r.text[:200]}"


def verify_provider(cfg, timeout=20):
    """Verify whichever provider `cfg` selects. Returns (ok, detail)."""
    if cfg["provider"] == "local":
        return verify_local(cfg["local_url"], cfg["local_model"], timeout=timeout)
    return verify_api_key(cfg["api_key"], cfg["model_id"], timeout=timeout)


def provider_usable(cfg):
    """Offline gate for AI features given a provider config.

    Local: a non-empty endpoint URL is enough to try. Anthropic: a plausibly-shaped key.
    (A confirmed verification failure is handled separately by the caller.)
    """
    if cfg["provider"] == "local":
        return bool(cfg["local_url"])
    return key_format_ok(cfg["api_key"])


# ---------------------------------------------------------------------------
# Provider-agnostic chat completion
# ---------------------------------------------------------------------------

def _complete_anthropic(cfg, system, messages, max_tokens, timeout):
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {"model": cfg["model_id"], "max_tokens": max_tokens,
            "system": system, "messages": messages}
    r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"Anthropic API error {r.status_code}: {r.text[:500]}")
    data = r.json()
    return "".join(block.get("text", "") for block in data.get("content", [])
                   if block.get("type") == "text").strip()


def _flatten_content(content):
    """Reduce Anthropic-style content (str or list of blocks) to plain text for the
    OpenAI-compatible API, which doesn't accept document/image blocks here."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n\n".join(parts)


def _complete_openai(cfg, system, messages, max_tokens, timeout):
    url = cfg["local_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer lm-studio"}
    oai_messages = [{"role": "system", "content": system}]
    for m in messages:
        oai_messages.append({"role": m["role"],
                             "content": _flatten_content(m.get("content"))})
    body = {"model": cfg["local_model"], "max_tokens": max_tokens,
            "temperature": 0.2, "messages": oai_messages}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach the local model at {url}: {exc}. Check that LM Studio's "
            "server is running and the endpoint URL in Settings/.env is correct "
            "(inside Docker use host.docker.internal, not localhost).")
    if r.status_code != 200:
        raise RuntimeError(f"Local model error {r.status_code}: {r.text[:500]}")
    data = r.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Unexpected response from local model: {str(data)[:300]}")


def complete(cfg, system, messages, max_tokens, timeout=180):
    """Provider-agnostic completion. `messages` uses Anthropic-style content (str or
    text blocks); non-text blocks are dropped for the local provider."""
    if cfg["provider"] == "local":
        return _complete_openai(cfg, system, messages, max_tokens, timeout)
    return _complete_anthropic(cfg, system, messages, max_tokens, timeout)


def docker_available():
    """True if the docker CLI is present and can reach a daemon."""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def image_tag(spec):
    return "fskx-model-" + depresolve.env_key(spec)


def image_exists(tag):
    try:
        r = subprocess.run(["docker", "image", "inspect", tag],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def list_model_images():
    """Repository names of all per-model AI images (the fskx-model-* tags)."""
    if not docker_available():
        return []
    try:
        r = subprocess.run(["docker", "images", "--format", "{{.Repository}}"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    return sorted({ln.strip() for ln in (r.stdout or "").splitlines()
                   if ln.strip().startswith("fskx-model-")})


def remove_image(tag):
    """Remove a per-model image. Returns True if it's gone afterward."""
    if not docker_available():
        return False
    try:
        subprocess.run(["docker", "image", "rm", "-f", tag],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)
    except Exception:  # noqa: BLE001
        pass
    return not image_exists(tag)


# ---------------------------------------------------------------------------
# Context gathering + prompt
# ---------------------------------------------------------------------------

def _read_clip(path, limit=8000):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            txt = fh.read()
        return txt if len(txt) <= limit else txt[:limit] + "\n…(truncated)…"
    except OSError:
        return ""


def gather_context(model_dir, error_log="", prev_dockerfile=""):
    """Assemble the user-message context describing the model + the failure."""
    spec = depresolve.resolve(model_dir)
    parts = []
    arch = host_arch()
    parts.append(f"TARGET ARCH: {arch}  (the Docker host is {arch}; build native for "
                 "this arch unless the model needs x86-only software like OpenBUGS, in "
                 "which case use `FROM --platform=linux/amd64`).")
    parts.append(f"LANGUAGE: {spec['language']}  VERSION: {spec['version'] or 'unspecified'}")
    parts.append("DETECTED PACKAGES (declared + scanned from scripts): "
                 + json.dumps(spec["packages"]))

    pkg_json = os.path.join(model_dir, "packages.json")
    if os.path.exists(pkg_json):
        parts.append("packages.json:\n" + _read_clip(pkg_json, 2000))

    readme = os.path.join(model_dir, "README.txt")
    if os.path.exists(readme):
        parts.append("README.txt (excerpt):\n" + _read_clip(readme, 1500))

    for fn in ("model.r", "model.R", "model.py"):
        p = os.path.join(model_dir, fn)
        if os.path.exists(p):
            parts.append(f"{fn}:\n" + _read_clip(p, 9000))
            break
    for fn in ("visualization.r", "visualization.R", "visualization.py"):
        p = os.path.join(model_dir, fn)
        if os.path.exists(p):
            parts.append(f"{fn}:\n" + _read_clip(p, 3000))
            break

    hints = matched_recipes(model_dir)
    if hints:
        parts.append("KNOWN-GOOD BUILD HINTS (verified — follow these closely):\n"
                     + "\n\n".join(f"- {h}" for h in hints))

    if prev_dockerfile:
        parts.append("PREVIOUS ATTEMPT (Dockerfile that FAILED — fix it, don't repeat "
                     "the failing step):\n" + prev_dockerfile[-4000:])

    if error_log:
        parts.append("BUILD/RUN ERROR from the previous attempt — your Dockerfile must "
                     "fix this specific failure:\n" + error_log[-4000:])

    parts.append("Write the corrected Dockerfile now."
                 if prev_dockerfile else "Write the Dockerfile now.")
    return spec, "\n\n".join(parts)


def generate_dockerfile(model_dir, cfg, error_log="", prev_dockerfile="",
                        max_tokens=2500):
    """Generate a Dockerfile via the selected provider. Returns (spec, dockerfile_text)."""
    spec, user_msg = gather_context(model_dir, error_log, prev_dockerfile)
    text = complete(cfg, SYSTEM_PROMPT,
                    [{"role": "user", "content": user_msg}], max_tokens)
    return spec, sanitize_dockerfile(text)


def sanitize_dockerfile(text):
    """Strip markdown fences, remove ENTRYPOINT/CMD, ensure the runner COPY exists."""
    lines = text.splitlines()
    # Drop leading/trailing ``` fences.
    cleaned = []
    for ln in lines:
        if ln.strip().startswith("```"):
            continue
        # Neutralize ENTRYPOINT/CMD so our run command is used verbatim.
        if ln.strip().upper().startswith(("ENTRYPOINT", "CMD")):
            cleaned.append("# (removed) " + ln)
            continue
        cleaned.append(ln)
    body = "\n".join(cleaned).strip()
    if "COPY app" not in body and "COPY ./app" not in body:
        body += "\n\n# Ensure the model-runner wrappers are available.\nCOPY app/ /app/\n"
    return body + "\n"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_image(dockerfile_text, tag, log_path, progress=None):
    """
    Build a per-model image from the given Dockerfile. The build context contains the
    wrapper scripts under app/ so the Dockerfile's `COPY app/ /app/` works. Returns True
    on success; the full build log is written to log_path.
    """
    if progress:
        progress("Building model image (this can take several minutes)…")
    ctx = tempfile.mkdtemp(prefix="fskx_build_")
    try:
        app_ctx = os.path.join(ctx, "app")
        os.makedirs(app_ctx, exist_ok=True)
        for fn in ("run_python_model.py", "run_r_model.R"):
            shutil.copy2(os.path.join(APP_DIR, fn), os.path.join(app_ctx, fn))
        with open(os.path.join(ctx, "Dockerfile"), "w") as fh:
            fh.write(dockerfile_text)

        with open(log_path, "w") as log:
            log.write("=== Dockerfile ===\n" + dockerfile_text + "\n=== build ===\n")
            log.flush()
            proc = subprocess.run(
                ["docker", "build", "-t", tag, "."],
                cwd=ctx, stdout=log, stderr=subprocess.STDOUT,
            )
        return proc.returncode == 0
    finally:
        shutil.rmtree(ctx, ignore_errors=True)


# ---------------------------------------------------------------------------
# "Talk to your model" — a grounded chat over the model archive + its results
# ---------------------------------------------------------------------------

CHAT_SYSTEM_PROMPT = """\
You are a knowledgeable assistant helping a food-safety researcher understand one FSKX
predictive model and its simulation results. Answer using ONLY the MODEL CONTEXT and
SIMULATION RESULTS provided below (plus the attached paper PDF, if any). If the answer
isn't in the provided material, say so plainly instead of guessing. Be concise and
precise, use the model's own parameter names and units, and when you discuss results refer
to specific runs by their run id / timestamp where useful.

===== MODEL CONTEXT =====
{model_context}

===== SIMULATION RESULTS =====
{results_context}
"""

# Anthropic accepts PDFs up to ~32 MB / 100 pages; skip clearly oversized files.
MAX_PDF_BYTES = 24 * 1024 * 1024


def _find_paper_pdf(model_dir):
    """Largest .pdf in the archive — almost always the source paper. None if none/oversized."""
    best, best_size = None, -1
    for root, _d, names in os.walk(model_dir):
        for n in names:
            if n.lower().endswith(".pdf"):
                p = os.path.join(root, n)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size > best_size:
                    best, best_size = p, size
    if best and 0 < best_size <= MAX_PDF_BYTES:
        return best
    return None


def _csv_sample(path, max_lines=8):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            out = []
            for i, ln in enumerate(fh):
                if i >= max_lines:
                    out.append("…(more rows)…")
                    break
                out.append(ln.rstrip("\n"))
        return "\n".join(out)
    except OSError:
        return ""


def _gather_model_context(model_dir):
    """Readable text context describing the model itself (metadata, readme, scripts)."""
    import omex
    spec = depresolve.resolve(model_dir)
    parts = [f"Language: {spec['language']}  Version: {spec['version'] or 'unspecified'}",
             "Packages (declared + scanned): " + json.dumps(spec["packages"])]

    meta = os.path.join(model_dir, "metaData.json")
    if os.path.exists(meta):
        parts.append("metaData.json:\n" + _read_clip(meta, 6000))

    # README — declared role first, then common filenames.
    readme_path = None
    try:
        scripts = omex.resolve_scripts(model_dir, spec["language"])
    except Exception:  # noqa: BLE001
        scripts = {}
    for cand in ("README.txt", "README.md", "readme.txt", "README"):
        p = os.path.join(model_dir, cand)
        if os.path.exists(p):
            readme_path = p
            break
    if readme_path:
        parts.append(os.path.basename(readme_path) + ":\n" + _read_clip(readme_path, 3000))

    for role, limit in (("model", 9000), ("visualization", 3000)):
        fn = scripts.get(role)
        if fn:
            p = os.path.join(model_dir, fn)
            if os.path.exists(p):
                parts.append(f"{role} script ({fn}):\n" + _read_clip(p, limit))

    # Scenario / parameter scripts, if present as a folder.
    sim_dir = os.path.join(model_dir, "simulations")
    if os.path.isdir(sim_dir):
        names = sorted(os.listdir(sim_dir))[:4]
        for n in names:
            p = os.path.join(sim_dir, n)
            if os.path.isfile(p):
                parts.append(f"simulations/{n}:\n" + _read_clip(p, 1500))

    return "\n\n".join(parts)


def _gather_results_context(fskx_name, run_ids=None, per_run_limit=4000, total_limit=40000):
    """
    Digest of stored runs: params, results.json, CSV samples, warnings.

    `run_ids` optionally restricts the digest to specific runs (e.g. the run being
    viewed, or the runs selected for comparison). When None/empty, all runs are used.
    A leading scope note tells the model exactly what it is and isn't seeing.
    """
    import engine
    runs = engine.list_runs(fskx_name)
    if run_ids:
        wanted = set(run_ids)
        runs = [r for r in runs if r["run_id"] in wanted]
    if not runs:
        if run_ids:
            return ("The run(s) selected on this page could not be found among the "
                    "stored results.")
        return "No simulation runs are stored for this model yet."
    scope_note = (f"(Scope: the {len(runs)} run(s) selected on this page — the user is "
                  "asking specifically about these.)" if run_ids
                  else f"(Scope: all {len(runs)} stored run(s) for this model.)")
    blocks, total = [scope_note], 0
    for r in runs:
        if total >= total_limit:
            blocks.append("…(further runs omitted to stay within size limits)…")
            break
        lines = [f"Run {r['run_id']}  (ok={r['ok']}, scenario={r.get('scenario')})"]
        if r.get("params"):
            lines.append("parameters: " + json.dumps(r["params"]))
        if r.get("warnings"):
            lines.append(f"visualization warnings: {len(r['warnings'])}")
        rj = engine.run_file_path(fskx_name, r["run_id"], "results.json")
        if rj:
            txt = _read_clip(rj, per_run_limit)
            lines.append("results.json:\n" + txt)
        for f in r.get("files", []):
            if f.lower().endswith((".csv", ".tsv")):
                p = engine.run_file_path(fskx_name, r["run_id"], f)
                if p:
                    lines.append(f"{f} (sample):\n" + _csv_sample(p))
        block = "\n".join(lines)
        if len(block) > per_run_limit * 2:
            block = block[:per_run_limit * 2] + "\n…(truncated)…"
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks)


def _extract_pdf_text(path, limit=20000):
    """Best-effort plain-text extraction from a PDF, for providers that can't take a
    native PDF document block (the local OpenAI-compatible path). Returns "" if no
    extractor is available or it fails — the chat then relies on the other context."""
    try:
        import pypdf
    except ImportError:
        return ""
    try:
        reader = pypdf.PdfReader(path)
        out, total = [], 0
        for page in reader.pages:
            txt = page.extract_text() or ""
            out.append(txt)
            total += len(txt)
            if total >= limit:
                break
        text = "\n".join(out).strip()
        return text[:limit] + "\n…(truncated)…" if len(text) > limit else text
    except Exception:  # noqa: BLE001
        return ""


def chat_about_model(model_dir, fskx_name, messages, cfg, run_ids=None,
                     max_tokens=1500):
    """
    Answer a (possibly multi-turn) conversation about a model and its results.

    `messages` is the full browser-side conversation as a list of
    {"role": "user"|"assistant", "content": "<text>"} — resent each turn so follow-up
    questions keep their context. The model archive context and a digest of ALL stored
    runs go into the system prompt. The paper PDF (if any) is attached as a native
    document block for Claude, or extracted to text and folded into the system prompt for
    a local model. Returns the assistant's reply text.
    """
    model_context = _gather_model_context(model_dir)
    pdf_path = _find_paper_pdf(model_dir)

    # For local models, fold the paper's extracted text into the model context (no native
    # PDF support). For Claude, the PDF rides along as a document block (below).
    if pdf_path and cfg["provider"] == "local":
        paper_text = _extract_pdf_text(pdf_path)
        if paper_text:
            model_context += "\n\nSOURCE PAPER (extracted text):\n" + paper_text

    system = CHAT_SYSTEM_PROMPT.format(
        model_context=model_context,
        results_context=_gather_results_context(fskx_name, run_ids=run_ids),
    )

    api_messages = []
    for m in messages:
        role = m.get("role")
        text = (m.get("content") or "").strip()
        if role in ("user", "assistant") and text:
            api_messages.append({"role": role, "content": text})
    if not api_messages:
        raise RuntimeError("No message to send.")

    # Claude only: attach the paper PDF to the first user turn as a native document block
    # (resent each request; acceptable here). `complete` drops non-text blocks for local.
    if pdf_path and cfg["provider"] != "local":
        for am in api_messages:
            if am["role"] == "user":
                try:
                    data = base64.b64encode(open(pdf_path, "rb").read()).decode("ascii")
                    am["content"] = [
                        {"type": "document",
                         "source": {"type": "base64", "media_type": "application/pdf",
                                    "data": data}},
                        {"type": "text", "text": am["content"]},
                    ]
                except OSError:
                    pass
                break

    return complete(cfg, system, api_messages, max_tokens)
