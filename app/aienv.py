"""
AI-assisted environment building for complex models.

Some FSKX models need system software that the generic micromamba build can't provide
(JAGS, Stan, OpenBUGS, GDAL, compilers, OS libraries…). For those, we ask Claude to
write a Dockerfile tailored to the model, build a per-model image, and run the model
inside it as a sibling container that shares the work volume.

The image is keyed by the model's dependency hash, so once built it is reused across
app restarts (its existence is the cache).
"""

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


def generate_dockerfile(model_dir, api_key, model_id, error_log="", prev_dockerfile="",
                        max_tokens=2500):
    """Call Claude to generate a Dockerfile. Returns (spec, dockerfile_text)."""
    spec, user_msg = gather_context(model_dir, error_log, prev_dockerfile)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"Anthropic API error {r.status_code}: {r.text[:500]}")
    data = r.json()
    text = "".join(block.get("text", "") for block in data.get("content", [])
                   if block.get("type") == "text").strip()
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
