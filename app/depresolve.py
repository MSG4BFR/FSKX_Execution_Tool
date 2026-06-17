"""
Dependency resolution for FSKX models.

Each FSKX archive declares its language (R or Python, possibly a specific version)
in packages.json. The package list there is often incomplete, so we *also* scan the
model / visualization / simulation scripts for imports (Python) and library()/require()
calls (R) and merge the two sources.

A per-model micromamba environment is then created, keyed by a hash of
(language, version, sorted package set) so identical requirement sets are reused and
each distinct model only pays the build cost once. Environments live under
MAMBA_ROOT_PREFIX, which the Docker launcher puts on a persistent named volume.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# Module name -> PyPI / conda package name, for imports that differ from the
# installable package. Anything not listed is assumed to install under its own name.
PY_IMPORT_TO_PKG = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "Bio": "biopython",
    "OpenSSL": "pyOpenSSL",
    "dateutil": "python-dateutil",
    "descartes": "descartes",
    "geopandas": "geopandas",
    "networkx": "networkx",
}

# Packages best installed from conda-forge because they pull compiled system libs.
# Everything else goes through pip (honouring versions from packages.json).
CONDA_PREFERRED_PY = {
    "numpy", "scipy", "pandas", "matplotlib", "geopandas", "shapely", "fiona",
    "pyproj", "gdal", "rasterio", "pyogrio", "scikit-learn", "scikit-image",
    "netcdf4", "h5py", "cartopy", "rtree",
}

# Python standard-library modules we must never try to install.
PY_STDLIB = {
    "os", "sys", "re", "json", "math", "random", "time", "datetime", "itertools",
    "functools", "collections", "subprocess", "glob", "shutil", "io", "csv",
    "warnings", "logging", "pathlib", "typing", "copy", "pickle", "hashlib",
    "tempfile", "string", "statistics", "decimal", "fractions", "abc", "argparse",
    "operator", "traceback", "textwrap", "unittest", "threading", "multiprocessing",
    "__future__", "contextlib", "enum", "base64", "zipfile", "xml", "urllib",
}


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def detect_language_and_version(model_dir):
    """Return (language, version_or_None) from packages.json."""
    pkg_path = os.path.join(model_dir, "packages.json")
    lang_field = ""
    if os.path.exists(pkg_path):
        try:
            lang_field = json.load(open(pkg_path)).get("Language", "") or ""
        except (ValueError, OSError):
            lang_field = ""
    low = lang_field.lower()
    if low.startswith("python"):
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", lang_field)
        return "python", (m.group(1) if m else None)
    if low.startswith("r"):
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", lang_field)
        return "r", (m.group(1) if m else None)
    # Fall back to file presence.
    if os.path.exists(os.path.join(model_dir, "model.py")):
        return "python", None
    return "r", None


def declared_packages(model_dir):
    """Packages explicitly listed in packages.json -> {name: version_or_None}."""
    pkg_path = os.path.join(model_dir, "packages.json")
    out = {}
    if not os.path.exists(pkg_path):
        return out
    try:
        data = json.load(open(pkg_path))
    except (ValueError, OSError):
        return out
    for entry in data.get("PackageList", []) or []:
        name = (entry.get("Package") or "").strip()
        if name:
            out[name] = (entry.get("Version") or "").strip() or None
    return out


def _script_files(model_dir, exts):
    files = []
    for root, _dirs, names in os.walk(model_dir):
        for n in names:
            if os.path.splitext(n)[1].lower() in exts:
                files.append(os.path.join(root, n))
    return files


def scan_python_imports(model_dir):
    mods = set()
    # Capture the top-level package of both `import a.b.c [as x]` and
    # `from a.b import c` (dotted module paths included).
    pat = re.compile(
        r"^\s*(?:import\s+([a-zA-Z0-9_]+)(?:\.[a-zA-Z0-9_]+)*"
        r"|from\s+([a-zA-Z0-9_]+)(?:\.[a-zA-Z0-9_]+)*\s+import)",
        re.M,
    )
    for f in _script_files(model_dir, {".py"}):
        for m in pat.finditer(_read(f)):
            mods.add(m.group(1) or m.group(2))
    pkgs = set()
    for mod in mods:
        if mod in PY_STDLIB:
            continue
        pkgs.add(PY_IMPORT_TO_PKG.get(mod, mod))
    return pkgs


def scan_r_libraries(model_dir):
    pat = re.compile(r"(?:library|require)\(\s*['\"]?([A-Za-z0-9_.]+)", re.M)
    pkgs = set()
    base = {"base", "stats", "utils", "graphics", "grDevices", "methods", "datasets"}
    for f in _script_files(model_dir, {".r"}):
        for m in pat.finditer(_read(f)):
            if m.group(1) not in base:
                pkgs.add(m.group(1))
    return pkgs


def resolve(model_dir):
    """
    Produce a normalized requirement spec for the model.

    Returns dict:
      language: 'python' | 'r'
      version:  str | None
      packages: {name: version_or_None}   (merged declared + scanned)
    """
    lang, version = detect_language_and_version(model_dir)
    declared = declared_packages(model_dir)
    if lang == "python":
        scanned = scan_python_imports(model_dir)
    else:
        scanned = scan_r_libraries(model_dir)
    packages = dict(declared)
    for name in scanned:
        packages.setdefault(name, None)
    return {"language": lang, "version": version, "packages": packages}


def env_key(spec):
    """Stable short hash identifying an environment for a requirement spec."""
    items = sorted(f"{k}=={v}" if v else k for k, v in spec["packages"].items())
    blob = json.dumps(
        {"l": spec["language"], "v": spec["version"], "p": items},
        sort_keys=True,
    )
    h = hashlib.sha1(blob.encode()).hexdigest()[:12]
    return f"{spec['language']}_{h}"


# ---------------------------------------------------------------------------
# Environment creation via micromamba
# ---------------------------------------------------------------------------

def _run(cmd, log):
    log.write("\n$ " + " ".join(cmd) + "\n")
    log.flush()
    proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    return proc.returncode


def micromamba_available():
    """True if micromamba is on PATH (the isolated, per-model env path)."""
    return shutil.which("micromamba") is not None


def env_exists(name):
    root = os.environ.get("MAMBA_ROOT_PREFIX", "/opt/conda")
    return os.path.isdir(os.path.join(root, "envs", name))


def list_envs():
    """Names of all per-model micromamba envs under the mamba root (excludes base)."""
    root = os.environ.get("MAMBA_ROOT_PREFIX", "/opt/conda")
    envs_dir = os.path.join(root, "envs")
    if not os.path.isdir(envs_dir):
        return []
    return [n for n in sorted(os.listdir(envs_dir))
            if os.path.isdir(os.path.join(envs_dir, n))]


def remove_env(name):
    """Remove a micromamba env (and its directory). Returns True if gone afterward."""
    if not name:
        return False
    if not env_exists(name):
        return True  # already gone
    if micromamba_available():
        try:
            subprocess.run(_micromamba("env", "remove", "-y", "-n", name),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=300)
        except Exception:  # noqa: BLE001
            pass
    # Belt-and-braces: drop the directory if micromamba left anything behind.
    if env_exists(name):
        root = os.environ.get("MAMBA_ROOT_PREFIX", "/opt/conda")
        shutil.rmtree(os.path.join(root, "envs", name), ignore_errors=True)
    return not env_exists(name)


def create_env(spec, name, log):
    """Create the micromamba env. Returns True on success."""
    lang = spec["language"]
    if lang == "python":
        return _create_python_env(spec, name, log)
    return _create_r_env(spec, name, log)


def _micromamba(*args):
    return ["micromamba", *args]


def _create_python_env(spec, name, log):
    version = spec["version"]
    # Try the requested python version; fall back to a modern default if conda-forge
    # no longer ships it (e.g. very old 3.4/3.5).
    candidates = []
    if version:
        mm = ".".join(version.split(".")[:2])
        candidates.append(mm)
    candidates.append("3.11")
    created = False
    for ver in candidates:
        rc = _run(_micromamba("create", "-y", "-n", name, "-c", "conda-forge",
                              f"python={ver}", "pip"), log)
        if rc == 0:
            log.write(f"\n[env] created with python={ver}\n")
            created = True
            break
        log.write(f"\n[env] python={ver} unavailable, trying next candidate\n")
    if not created:
        return False

    pkgs = spec["packages"]
    conda_pkgs, pip_pkgs = [], []
    for nm, ver in pkgs.items():
        low = nm.lower()
        if low in CONDA_PREFERRED_PY and not ver:
            conda_pkgs.append(nm)
        else:
            pip_pkgs.append(f"{nm}=={ver}" if ver else nm)
    if conda_pkgs:
        rc = _run(_micromamba("install", "-y", "-n", name, "-c", "conda-forge",
                              *conda_pkgs), log)
        if rc != 0:
            # Fall back to pip for the conda set.
            pip_pkgs = [p for p in conda_pkgs] + pip_pkgs
    if pip_pkgs:
        rc = _run(_micromamba("run", "-n", name, "pip", "install", *pip_pkgs), log)
        if rc != 0:
            log.write("\n[env] WARNING: some pip packages failed to install\n")
    return True


def _create_r_env(spec, name, log):
    version = spec["version"]
    base_pkgs = ["r-base"] if not version else [f"r-base={'.'.join(version.split('.')[:2])}"]
    rc = _run(_micromamba("create", "-y", "-n", name, "-c", "conda-forge",
                          *base_pkgs, "r-jsonlite"), log)
    if rc != 0:
        rc = _run(_micromamba("create", "-y", "-n", name, "-c", "conda-forge",
                              "r-base", "r-jsonlite"), log)
        if rc != 0:
            return False

    cran_fallback = []
    for nm in spec["packages"]:
        conda_name = "r-" + nm.lower()
        rc = _run(_micromamba("install", "-y", "-n", name, "-c", "conda-forge",
                              conda_name), log)
        if rc != 0:
            cran_fallback.append(nm)
    if cran_fallback:
        r_expr = (
            'options(repos="https://cloud.r-project.org");'
            f'install.packages(c({",".join(repr(p) for p in cran_fallback)}))'
        )
        _run(_micromamba("run", "-n", name, "Rscript", "-e", r_expr), log)
    return True


def ensure_env(model_dir, logs_dir):
    """
    Resolve deps and ensure an env exists. Returns (env_name, spec, log_path).
    Idempotent: a cached env is reused without rebuilding.
    """
    spec = resolve(model_dir)
    name = env_key(spec)
    log_path = os.path.join(logs_dir, f"env_{name}.log")
    # No micromamba (e.g. native install / local test): caller runs the interpreter
    # directly from PATH. env_name is None to signal this.
    if not micromamba_available():
        return None, spec, log_path
    if env_exists(name):
        return name, spec, log_path
    os.makedirs(logs_dir, exist_ok=True)
    with open(log_path, "w") as log:
        log.write(f"Building environment '{name}'\nspec: {json.dumps(spec, indent=2)}\n")
        ok = create_env(spec, name, log)
        if not ok:
            raise RuntimeError(f"Failed to create environment for {model_dir}; see {log_path}")
    return name, spec, log_path


if __name__ == "__main__":
    # Debug helper: python depresolve.py <model_dir>
    print(json.dumps(resolve(sys.argv[1]), indent=2))
