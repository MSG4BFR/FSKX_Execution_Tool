"""
Client for the RAKIP / FSKX public model repository.

Lets the app list the online catalogue and download a model's .fskx into the local
models folder at runtime, so new models can be added without restarting.
"""

import os
import re
import time

import requests

BASE = ("https://fskx-api-gateway-service.risk-ai-cloud.com"
        "/model-lookup-service/public/models")
LIST_PARAMS = ("?hide_creators=true&hide_description=true&hide_products=true"
               "&hide_modelClass=true&hide_software=true&hide_hazards=true")

MODELS_DIR = os.environ.get("FSKX_MODELS_DIR", "/models")


def list_remote(timeout=30):
    """Return the catalogue as a list of {id, name, repository_name, upload_date}."""
    r = requests.get(BASE + LIST_PARAMS, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    out = []
    for m in data:
        out.append({
            "id": m.get("id"),
            "name": (m.get("name") or "").strip(),
            "repository_name": m.get("repository_name", ""),
            "upload_date": m.get("upload_date", ""),
        })
    # Newest first.
    out.sort(key=lambda m: m["upload_date"], reverse=True)
    return out


def safe_filename(name, model_id):
    """A filesystem-safe .fskx filename derived from the model name (+id suffix)."""
    base = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip().replace(" ", "_")
    base = re.sub(r"_+", "_", base)[:80].strip("_") or "model"
    short = (model_id or "")[:8]
    return f"{base}__{short}.fskx"


def downloaded_filename(model_id):
    """Local .fskx filename for this model id if it has been downloaded, else None."""
    short = (model_id or "")[:8]
    if not short or not os.path.isdir(MODELS_DIR):
        return None
    for n in sorted(os.listdir(MODELS_DIR)):
        if n.lower().endswith(".fskx") and f"__{short}.fskx" in n:
            return n
    return None


def is_downloaded(model_id):
    """True if a file for this model id already exists locally."""
    return downloaded_filename(model_id) is not None


def download(model_id, name, timeout=300, progress=None):
    """
    Download a model's .fskx into MODELS_DIR. Returns the saved filename.
    Verifies the payload is a ZIP/OMEX archive (FSKX magic bytes 'PK').
    """
    if progress:
        progress(f"Downloading “{name}”…")
    url = f"{BASE}/{model_id}/files/fskx_file.fskx"
    r = requests.get(url, timeout=timeout, stream=True)
    r.raise_for_status()

    os.makedirs(MODELS_DIR, exist_ok=True)
    fname = safe_filename(name, model_id)
    tmp = os.path.join(MODELS_DIR, fname + ".part")
    first = b""
    with open(tmp, "wb") as fh:
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            if not first:
                first = chunk[:4]
            fh.write(chunk)
    if not first.startswith(b"PK"):
        os.remove(tmp)
        raise ValueError("Downloaded file is not a valid FSKX/ZIP archive.")
    final = os.path.join(MODELS_DIR, fname)
    os.replace(tmp, final)
    if progress:
        progress("Download complete.")
    return fname
