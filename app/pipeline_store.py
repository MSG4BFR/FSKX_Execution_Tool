"""
pipeline_store.py — durable, named storage for model-joining pipelines (Phase 4).

A saved pipeline is a small JSON record on the persistent work volume:

    {"id", "name", "created", "modified", "pipeline": {<the join definition>}}

stored at ``$FSKX_WORK_DIR/_pipelines/<id>.json`` — the same volume that already survives
restarts (next to ``_results``), so saved joins persist with no database.

A pipeline can also be EXPORTED as a self-contained ``.fskxp`` archive and re-IMPORTED
elsewhere — the sharing path. The archive is a **COMBINE/OMEX-conformant** ZIP: it carries an
``manifest.xml`` (``omexManifest``) registering every entry with its COMBINE format URI, an
``metadata.rdf`` with archive-level metadata, the pipeline record (``pipeline.json``, flagged
``master`` — the app-private wiring), and every member ``.fskx`` (themselves OMEX archives).
A legacy ``manifest.json`` is still written for human inspection and older readers. Other FSKX
/ COMBINE tools can therefore at least enumerate the archive contents through the standard
manifest; the join wiring itself stays in ``pipeline.json`` (no standard SED-ML construct
expresses cross-model parameter joins — see model-joining/phase-4 for that follow-up).

Import is tolerant: it reads ``pipeline.json`` (the master) and restores ``models/*.fskx``,
working for both the new OMEX archives and the older plain zips.

Pure stdlib; no Flask/engine import, so it is unit-testable in isolation.
"""

import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from xml.sax.saxutils import escape, quoteattr

WORK_DIR = os.environ.get("FSKX_WORK_DIR", "/tmp/fskx_work")
MODELS_DIR = os.environ.get("FSKX_MODELS_DIR", "/models")
PIPELINES_DIR = os.path.join(WORK_DIR, "_pipelines")

ARCHIVE_EXT = ".fskxp"
ARCHIVE_FORMAT = "fskx-pipeline"
ARCHIVE_VERSION = "1.1.0"

# COMBINE/OMEX format URIs (mirroring the convention real .fskx archives use).
OMEX_FMT = "http://identifiers.org/combine.specifications/omex"
OMEX_MANIFEST_FMT = "http://identifiers.org/combine.specifications/omex-manifest"
OMEX_METADATA_FMT = "http://identifiers.org/combine.specifications/omex-metadata"
JSON_FMT = "https://www.iana.org/assignments/media-types/application/json"

MANIFEST_XML = "manifest.xml"
METADATA_RDF = "metadata.rdf"
PIPELINE_JSON = "pipeline.json"


def _omex_format(location):
    """COMBINE format URI for an archive entry, by extension."""
    low = location.lower()
    if low.endswith(".fskx"):
        return OMEX_FMT
    if low.endswith(".rdf"):
        return OMEX_METADATA_FMT
    if low == "./" + MANIFEST_XML:
        return OMEX_MANIFEST_FMT
    if low.endswith(".json"):
        return JSON_FMT
    return "https://www.iana.org/assignments/media-types/application/octet-stream"


def build_manifest_xml(locations, master=None):
    """Render a COMBINE ``omexManifest`` for the given archive-relative ``locations``
    (each like ``./pipeline.json``). The archive root and the manifest itself are always
    included. ``master`` (one of ``locations``) is flagged ``master="true"``."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<omexManifest xmlns="http://identifiers.org/combine.specifications/omex-manifest">',
             '  <content location="." format=%s />' % quoteattr(OMEX_FMT),
             '  <content location="./%s" format=%s />' % (MANIFEST_XML, quoteattr(OMEX_MANIFEST_FMT))]
    for loc in locations:
        attrs = 'location=%s format=%s' % (quoteattr(loc), quoteattr(_omex_format(loc)))
        if master and loc == master:
            attrs += ' master="true"'
        lines.append('  <content %s />' % attrs)
    lines.append('</omexManifest>')
    return "\n".join(lines) + "\n"


def build_metadata_rdf(record):
    """Render an OMEX ``metadata.rdf`` carrying archive-level metadata (title, created) and
    a ``dc:type`` role for the pipeline record."""
    name = record.get("name", "")
    created = record.get("created", "") or _now()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        ' xmlns:dcterms="http://purl.org/dc/terms/">\n'
        '  <rdf:Description rdf:about=".">\n'
        '    <dcterms:conformsTo>%s %s</dcterms:conformsTo>\n'
        '    <dcterms:title>%s</dcterms:title>\n'
        '    <dcterms:created>%s</dcterms:created>\n'
        '  </rdf:Description>\n'
        '  <rdf:Description rdf:about="/%s">\n'
        '    <dc:type xmlns:dc="http://purl.org/dc/elements/1.1/">fskxPipeline</dc:type>\n'
        '  </rdf:Description>\n'
        '</rdf:RDF>\n'
        % (escape(ARCHIVE_FORMAT), escape(ARCHIVE_VERSION),
           escape(name), escape(created), PIPELINE_JSON)
    )


def _ensure_dir():
    os.makedirs(PIPELINES_DIR, exist_ok=True)


def safe_id(pid):
    """A stored id must be a bare token (guards the file path against traversal)."""
    return bool(pid) and "/" not in pid and "\\" not in pid and ".." not in pid


def _path(pid):
    return os.path.join(PIPELINES_DIR, pid + ".json")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def save(pipeline, name, pid=None):
    """Create or update a named pipeline. Returns the stored record. Updating preserves the
    original ``created`` timestamp."""
    _ensure_dir()
    created = _now()
    if pid and safe_id(pid) and os.path.exists(_path(pid)):
        existing = load(pid) or {}
        created = existing.get("created", created)
    else:
        pid = uuid.uuid4().hex[:12]
    record = {"id": pid, "name": (name or "Untitled pipeline").strip(),
              "created": created, "modified": _now(), "pipeline": pipeline}
    with open(_path(pid), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    return record


def load(pid):
    if not safe_id(pid):
        return None
    p = _path(pid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def list_all():
    """Summaries of all saved pipelines, newest-modified first."""
    _ensure_dir()
    out = []
    for fn in os.listdir(PIPELINES_DIR):
        if not fn.endswith(".json"):
            continue
        rec = load(fn[:-5])
        if not rec:
            continue
        p = rec.get("pipeline", {})
        out.append({"id": rec["id"], "name": rec["name"],
                    "created": rec.get("created"), "modified": rec.get("modified"),
                    "n_nodes": len(p.get("nodes", [])), "n_edges": len(p.get("edges", []))})
    out.sort(key=lambda r: r.get("modified", ""), reverse=True)
    return out


def delete(pid):
    if not safe_id(pid):
        return False
    p = _path(pid)
    if os.path.exists(p):
        try:
            os.remove(p)
            return True
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# Composite archive (export / import) — sharing a pipeline + its member models
# ---------------------------------------------------------------------------

def member_models(record):
    return sorted({n["fskx"] for n in record.get("pipeline", {}).get("nodes", []) if n.get("fskx")})


def build_archive(pid, models_dir=None):
    """Write a COMBINE/OMEX-conformant ``.fskxp`` archive for a saved pipeline. Returns
    (tmp_path, name) or None. Embeds every member ``.fskx`` that exists in ``models_dir`` so
    the archive is self-contained, and registers everything in an ``omexManifest``."""
    record = load(pid)
    if not record:
        return None
    models_dir = models_dir or MODELS_DIR
    fd, tmp = tempfile.mkstemp(suffix=ARCHIVE_EXT)
    os.close(fd)
    members = member_models(record)
    missing = []
    embedded = []  # archive-relative locations of members actually written
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(PIPELINE_JSON, json.dumps(record, indent=2))
        for f in members:
            src = os.path.join(models_dir, f)
            if os.path.exists(src):
                z.write(src, "models/" + f)
                embedded.append("./models/" + f)
            else:
                missing.append(f)
        # OMEX manifest: master pipeline + each embedded member + the legacy json manifest.
        locations = ["./" + PIPELINE_JSON] + embedded + ["./manifest.json", "./" + METADATA_RDF]
        z.writestr(MANIFEST_XML, build_manifest_xml(locations, master="./" + PIPELINE_JSON))
        z.writestr(METADATA_RDF, build_metadata_rdf(record))
        # Legacy human-readable manifest (also registered above), kept for older readers.
        z.writestr("manifest.json", json.dumps({
            "format": ARCHIVE_FORMAT, "version": ARCHIVE_VERSION,
            "name": record["name"], "models": members,
            "missing_models": missing}, indent=2))
    return tmp, record["name"]


def _master_location(z):
    """The archive-relative path of the master content from manifest.xml, or None.
    Best-effort: a missing/malformed manifest just yields None (caller falls back)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(z.read(MANIFEST_XML))
    except (KeyError, ET.ParseError, OSError):
        return None
    for c in root.iter("{http://identifiers.org/combine.specifications/omex-manifest}content"):
        if (c.get("master") or "").lower() == "true":
            loc = c.get("location") or ""
            return loc.lstrip("./") or None
    return None


def import_archive(path, models_dir=None):
    """Read a ``.fskxp`` archive: restore any member ``.fskx`` not already present, then save
    the pipeline as a NEW record (fresh id). Returns the saved record. Reads the OMEX
    ``manifest.xml`` to locate the master pipeline record when present, falling back to
    ``pipeline.json`` for older plain-zip archives. Guards zip entry paths against traversal."""
    models_dir = models_dir or MODELS_DIR
    os.makedirs(models_dir, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        master = _master_location(z)
        names = set(z.namelist())
        if not (master and master in names):
            master = PIPELINE_JSON
        record = json.loads(z.read(master).decode("utf-8"))
        for n in z.namelist():
            if not (n.startswith("models/") and n.endswith(".fskx")):
                continue
            base = os.path.basename(n)
            if not base or base != n[len("models/"):]:  # reject nested / traversal
                continue
            target = os.path.join(models_dir, base)
            if not os.path.exists(target):
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    return save(record.get("pipeline", {}), record.get("name", "Imported pipeline"))
