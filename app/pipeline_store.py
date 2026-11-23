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
import stat
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
# Per-node run artifacts live at  RESULTS_ROOT/<base>/<run_id>/  (mirrors engine.RESULTS_ROOT).
# A "portable" export bundles the run folders referenced by the saved node-state so the archive
# can be opened on another machine with results viewable and nodes clean — no re-run.
RESULTS_ROOT = os.path.join(WORK_DIR, "_results")

ARCHIVE_EXT = ".fskxp"
ARCHIVE_FORMAT = "fskx-pipeline"
ARCHIVE_VERSION = "1.1.0"

# Archive limits. Export and import share them, so an archive this tool writes can always be
# read back by the same tool (a larger one fails at export with a clear message).
MAX_ARCHIVE_ENTRIES = 4096
MAX_ARCHIVE_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PIPELINE_JSON_BYTES = 1024 * 1024
_GENERATED_ENTRIES = 3          # manifest.xml, metadata.rdf, manifest.json
_GENERATED_BYTES = 1024 * 1024  # budget reserved for those three while exporting
_LIMIT_MESSAGE = ("Pipeline archive exceeds the size limits (max {} files, {} MiB in total, "
                  "{} MiB per file); export fewer results".format(
                      MAX_ARCHIVE_ENTRIES, MAX_ARCHIVE_TOTAL_BYTES // 2**20,
                      MAX_ARCHIVE_ENTRY_BYTES // 2**20))

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


def save(pipeline, name, pid=None, node_state=None):
    """Create or update a named pipeline. Returns the stored record. Updating preserves the
    original ``created`` timestamp.

    ``node_state`` (Phase C — persistent live node state) is the per-node execution map
    (``{nid: {run_id, cache_hash, ok, executed_at}}``) snapshotted *with* the workflow, so
    saving a pipeline also saves its execution state and an exported ``.fskxp`` carries the
    cached-run identity. It is stored at the record top level, parallel to ``pipeline`` — the
    wiring object handed to ``pipeline.validate`` / ``pipeline.run`` is untouched. When
    ``node_state is None`` the existing record's state is preserved on update (a metadata-only
    save never wipes execution state); a brand-new record defaults to ``{}``. It only
    **references** ``_results`` runs by id — no artifacts are copied (HANDOFF §2)."""
    _ensure_dir()
    created = _now()
    existing = {}
    if pid and safe_id(pid) and os.path.exists(_path(pid)):
        existing = load(pid) or {}
        created = existing.get("created", created)
    else:
        pid = uuid.uuid4().hex[:12]
    if node_state is None:
        node_state = existing.get("node_state", {}) or {}
    record = {"id": pid, "name": (name or "Untitled pipeline").strip(),
              "created": created, "modified": _now(), "pipeline": pipeline,
              "node_state": node_state}
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
    members = {n["fskx"] for n in record.get("pipeline", {}).get("nodes", []) if n.get("fskx")}
    for name in members:
        if (not isinstance(name, str) or not 5 < len(name) <= 255 or not name.lower().endswith('.fskx')
                or any(c in name for c in '/\\:\x00') or any(ord(c) < 32 for c in name)):
            raise ValueError("Pipeline model references must be bare FSKX filenames")
    return sorted(members)


def _contained(root, path):
    root = os.path.realpath(root)
    if os.path.commonpath([root, os.path.realpath(path)]) != root or os.path.islink(path):
        raise ValueError("Pipeline archive path escapes its configured directory")
    return path


def _node_runs(record):
    """The ``(base, run_id)`` pairs referenced by a record's saved node-state, deduped. ``base``
    is the member model's folder name (``splitext(fskx)[0]``), matching ``engine`` layout."""
    fskx_of = {n["id"]: n.get("fskx")
               for n in record.get("pipeline", {}).get("nodes", []) if n.get("fskx")}
    seen, out = set(), []
    for nid, s in (record.get("node_state") or {}).items():
        rid, fskx = s.get("run_id"), fskx_of.get(nid)
        if not (rid and fskx):
            continue
        if not pipeline_safe_run_id(rid):
            raise ValueError("Invalid pipeline result reference")
        base = os.path.splitext(fskx)[0]
        if (base, rid) in seen:
            continue
        seen.add((base, rid))
        out.append((base, rid))
    return out


def pipeline_safe_run_id(rid):
    return isinstance(rid, str) and len(rid) <= 128 and safe_id(rid) and ':' not in rid


def build_archive(pid, models_dir=None, include_results=False, results_root=None):
    """Write a COMBINE/OMEX-conformant ``.fskxp`` archive for a saved pipeline. Returns
    (tmp_path, name) or None. Embeds every member ``.fskx`` that exists in ``models_dir`` so
    the archive is self-contained, and registers everything in an ``omexManifest``.

    When ``include_results`` is set (the *portable* export), the run folders referenced by the
    saved node-state are copied under ``results/<base>/<run_id>/`` too, so the archive opens on
    another machine with results viewable and the nodes clean (the content hash recomputes to a
    match and the run folders now exist) — no re-run needed. This trades a larger archive for
    portability; the lean default carries only the definition + state references."""
    record = load(pid)
    if not record:
        return None
    models_dir = models_dir or MODELS_DIR
    results_root = results_root or RESULTS_ROOT
    members = member_models(record)  # validate references before creating any file
    pipeline_blob = json.dumps(record, indent=2)
    if len(pipeline_blob.encode("utf-8")) > MAX_PIPELINE_JSON_BYTES:
        raise ValueError("Pipeline definition exceeds 1 MiB")
    totals = {"entries": 1 + _GENERATED_ENTRIES, "bytes": len(pipeline_blob) + _GENERATED_BYTES}

    def account(path):
        """Refuse early (before copying) anything the importer would later reject."""
        size = os.path.getsize(path)
        totals["entries"] += 1
        totals["bytes"] += size
        if (totals["entries"] > MAX_ARCHIVE_ENTRIES or size > MAX_ARCHIVE_ENTRY_BYTES
                or totals["bytes"] > MAX_ARCHIVE_TOTAL_BYTES):
            raise ValueError(_LIMIT_MESSAGE)

    fd, tmp = tempfile.mkstemp(suffix=ARCHIVE_EXT)
    os.close(fd)
    missing = []
    embedded = []       # archive-relative locations of members actually written
    result_locs = []    # archive-relative locations of bundled result files
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(PIPELINE_JSON, pipeline_blob)
            for f in members:
                src = _contained(models_dir, os.path.join(models_dir, f))
                if os.path.exists(src):
                    account(src)
                    z.write(src, "models/" + f)
                    embedded.append("./models/" + f)
                else:
                    missing.append(f)
            if include_results:
                for base, rid in _node_runs(record):
                    run_path = _contained(results_root, os.path.join(results_root, base, rid))
                    if not os.path.isdir(run_path):
                        continue
                    for dirpath, _dirs, fnames in os.walk(run_path):
                        for fn in fnames:
                            ap = _contained(results_root, os.path.join(dirpath, fn))
                            rel = os.path.relpath(ap, results_root).replace(os.sep, "/")
                            arc = "results/" + rel
                            account(ap)
                            z.write(ap, arc)
                            result_locs.append("./" + arc)
            # OMEX manifest: master pipeline + each embedded member + bundled results + json manifest.
            locations = (["./" + PIPELINE_JSON] + embedded + result_locs
                         + ["./manifest.json", "./" + METADATA_RDF])
            z.writestr(MANIFEST_XML, build_manifest_xml(locations, master="./" + PIPELINE_JSON))
            z.writestr(METADATA_RDF, build_metadata_rdf(record))
            # Legacy human-readable manifest (also registered above), kept for older readers.
            z.writestr("manifest.json", json.dumps({
                "format": ARCHIVE_FORMAT, "version": ARCHIVE_VERSION,
                "name": record["name"], "models": members,
                "missing_models": missing,
                "with_results": bool(include_results and result_locs),
                "result_files": len(result_locs)}, indent=2))
        # Exact check on what was really written (the running totals above are an early abort).
        with zipfile.ZipFile(tmp) as z:
            sizes = [e.file_size for e in z.infolist()]
        if (len(sizes) > MAX_ARCHIVE_ENTRIES or sum(sizes) > MAX_ARCHIVE_TOTAL_BYTES
                or any(n > MAX_ARCHIVE_ENTRY_BYTES for n in sizes)):
            raise ValueError(_LIMIT_MESSAGE)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
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


def _safe_rel(prefix, name):
    """The path under ``prefix`` for a zip entry, or None if it escapes (traversal/absolute)."""
    rel = name[len(prefix):].replace('\\', '/')
    if not rel or rel.endswith("/"):
        return None
    if ':' in rel or '\x00' in rel or '..' in rel.split('/'):
        return None
    norm = os.path.normpath(rel)
    if os.path.isabs(norm) or norm.startswith(".." + os.sep) or norm == "..":
        return None
    return norm


class ArchiveConflictError(ValueError):
    """An opt-in rejecting import found different local model/result contents."""

    def __init__(self, paths):
        self.paths = sorted(set(paths))
        summary = ", ".join(self.paths[:5])
        if len(self.paths) > 5:
            summary += " (and {} more)".format(len(self.paths) - 5)
        super().__init__("Import rejected: existing local files conflict with the archive: "
                         + summary + ". Use another destination or resolve the conflicts first.")


def _import_targets(archive, models_dir, results_root):
    """Use the same entry selection and containment rules for preflight and copying."""
    for entry in archive.infolist():
        if entry.is_dir():
            continue
        name = entry.filename
        if name.startswith("models/") and name.endswith(".fskx"):
            base = os.path.basename(name)
            if base and base == name[len("models/"):]:
                yield entry, _contained(models_dir, os.path.join(models_dir, base))
        elif name.startswith("results/"):
            relative = _safe_rel("results/", name)
            if relative:
                yield entry, _contained(results_root, os.path.join(results_root, relative))


def _same_contents(archive, entry, target):
    if not os.path.isfile(target) or os.path.getsize(target) != entry.file_size:
        return False
    with archive.open(entry) as incoming, open(target, "rb") as local:
        while True:
            block = incoming.read(64 * 1024)
            if block != local.read(64 * 1024):
                return False
            if not block:
                return True


def _check_import_conflicts(archive, targets):
    conflicts, destinations = [], set()
    for entry, target in targets:
        key = os.path.normcase(os.path.realpath(target))
        if key in destinations:
            raise ValueError("Multiple archive entries resolve to the same import target: "
                             + entry.filename)
        destinations.add(key)
        if os.path.exists(target):
            if not _same_contents(archive, entry, target):
                conflicts.append(entry.filename)
        else:
            # A file in place of a required parent directory also blocks this import.
            parent = os.path.dirname(target)
            while parent and not os.path.exists(parent):
                ancestor = os.path.dirname(parent)
                if ancestor == parent:
                    break
                parent = ancestor
            if parent and not os.path.isdir(parent):
                conflicts.append(entry.filename)
    if conflicts:
        raise ArchiveConflictError(conflicts)


def import_archive(path, models_dir=None, results_root=None, *, conflict_policy="keep_local"):
    """Read a ``.fskxp`` archive: restore any member ``.fskx`` not already present, then save
    the pipeline as a NEW record (fresh id). Returns the saved record. Reads the OMEX
    ``manifest.xml`` to locate the master pipeline record when present, falling back to
    ``pipeline.json`` for older plain-zip archives. Guards zip entry paths against traversal.

    If the archive carries bundled run artifacts (``results/<base>/<run_id>/…`` — a *portable*
    export), they are restored into ``results_root`` so the saved node-state references resolve:
    the nodes recompute to *clean* and their results are viewable without re-running. Existing
    local files are never clobbered. ``conflict_policy="keep_local"`` (the default)
    skips existing files even if their contents differ and still saves the new pipeline.
    Opt in with ``conflict_policy="reject"`` to compare existing model/result files
    byte-for-byte before copying anything. Identical files are accepted; differences
    raise ``ArchiveConflictError`` with archive-relative ``paths``, without restoring
    files or saving a pipeline. This preflight is not a transaction or a filesystem
    lock: later I/O failures and concurrent changes are not rolled back."""
    if not isinstance(conflict_policy, str) or conflict_policy not in ("keep_local", "reject"):
        raise ValueError("conflict_policy must be 'keep_local' or 'reject'")
    models_dir = models_dir or MODELS_DIR
    results_root = results_root or RESULTS_ROOT
    with zipfile.ZipFile(path) as z:
        entries = z.infolist()
        if (len(entries) > MAX_ARCHIVE_ENTRIES or len({e.filename for e in entries}) != len(entries)
                or sum(e.file_size for e in entries) > MAX_ARCHIVE_TOTAL_BYTES
                or any(e.file_size > MAX_ARCHIVE_ENTRY_BYTES or stat.S_ISLNK(e.external_attr >> 16)
                       for e in entries)):
            raise ValueError("Pipeline archive exceeds limits or contains duplicate/link entries")
        master = _master_location(z)
        names = set(z.namelist())
        if not (master and master in names):
            master = PIPELINE_JSON
        if z.getinfo(master).file_size > MAX_PIPELINE_JSON_BYTES:
            raise ValueError("Pipeline definition exceeds 1 MiB")
        record = json.loads(z.read(master).decode("utf-8"))
        member_models(record)  # Validate references before restoring any files.
        # Resolve and contain every target first, in BOTH modes: a bad path then fails the whole
        # import before anything is restored (a lazy generator failed halfway, leaving files).
        targets = list(_import_targets(z, models_dir, results_root))
        if conflict_policy == "reject":
            _check_import_conflicts(z, targets)
        os.makedirs(models_dir, exist_ok=True)
        for entry, target in targets:
            if os.path.exists(target):       # never clobber a local model or result
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(entry) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    # Carry the embedded node-state (Phase C) into the new record. With a portable archive the
    # restored run folders make these references resolve → nodes show clean. Without results,
    # the missing runs degrade safely to "stale" and re-run (HANDOFF §2 "when in doubt, stale").
    return save(record.get("pipeline", {}), record.get("name", "Imported pipeline"),
                node_state=record.get("node_state"))
