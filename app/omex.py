"""
FSKX / OMEX archive introspection.

An FSKX archive is an OMEX (COMBINE) ZIP. Two of its sidecar files tell us how to run
the model in a standard-compliant way, instead of guessing from filenames:

  * ``metadata.rdf`` declares the ROLE of each file — which script is the model, which is
    the visualization, which is the readme — via
    ``<rdf:Description rdf:about="/path"><dc:type>modelScript</dc:type></rdf:Description>``.
    Script files can therefore have arbitrary names; the role mapping is authoritative.

  * ``sim.sedml`` (SED-ML) holds the SIMULATION DATA: one ``<model id="…">`` per scenario,
    each with a ``<listOfChanges>`` of ``<changeAttribute target="p" newValue="…"/>`` — the
    parameter values. This is the standard's source of truth for parameters; the
    ``simulations/`` folder (when present) is just these changes materialised as a script.

This module reads both with the stdlib XML parser. Crucially, parameter values are read as
XML *attributes*, so XML entity escaping is undone for us: a string parameter stored as
``newValue="&quot;Eggs&quot;"`` is returned as the Python string ``"Eggs"`` (quotes
intact), exactly matching the materialised script — no manual unescaping, no double-quoting.

Everything here is best-effort and side-effect free: missing or malformed files yield empty
results rather than raising, so callers can apply their own fail-fast policy.
"""

import os
import re
import xml.etree.ElementTree as ET

RDF_NS = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
SEDML_NS = "{http://sed-ml.org/}"

# dc:type value (lowercased) -> our role key
_ROLE_TYPES = {
    "modelscript": "model",
    "visualizationscript": "visualization",
    "readme": "readme",
}


# ---------------------------------------------------------------------------
# Small filesystem helpers (archives vary in casing and may nest scripts)
# ---------------------------------------------------------------------------

def _strip_path(p):
    """Normalise an archive-relative reference: drop leading './' or '/'."""
    if not p:
        return None
    p = re.sub(r"^\.?/+", "", p.strip())
    return p or None


def _find_ci(model_dir, name):
    """Top-level case-insensitive lookup of a file by name. Returns abs path or None."""
    target = name.lower()
    try:
        for n in os.listdir(model_dir):
            if n.lower() == target:
                return os.path.join(model_dir, n)
    except OSError:
        pass
    return None


def _resolve_existing(model_dir, relpath):
    """
    Resolve an archive-relative script path to the actual file, tolerating case and
    nesting differences. Returns the path RELATIVE to model_dir (posix), or None.
    """
    if not relpath:
        return None
    direct = os.path.join(model_dir, relpath)
    if os.path.isfile(direct):
        return relpath.replace("\\", "/")
    # Fall back to a case-insensitive search for the basename anywhere in the tree.
    base = os.path.basename(relpath).lower()
    for root, _dirs, files in os.walk(model_dir):
        for f in files:
            if f.lower() == base:
                rel = os.path.relpath(os.path.join(root, f), model_dir)
                return rel.replace("\\", "/")
    return None


# ---------------------------------------------------------------------------
# metadata.rdf — file roles
# ---------------------------------------------------------------------------

def script_roles(model_dir):
    """
    Map declared roles to archive-relative paths from metadata.rdf:
    ``{'model': ..., 'visualization': ..., 'readme': ...}`` (only keys that are declared).
    Empty dict if there is no readable RDF.
    """
    path = _find_ci(model_dir, "metadata.rdf")
    roles = {}
    if not path:
        return roles
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return roles
    for desc in root.iter(RDF_NS + "Description"):
        about = desc.get(RDF_NS + "about")
        if not about or about == ".":
            continue
        t = desc.find(DC_NS + "type")
        if t is None or not (t.text or "").strip():
            continue
        role = _ROLE_TYPES.get(t.text.strip().lower())
        if role:
            roles[role] = _strip_path(about)
    return roles


# ---------------------------------------------------------------------------
# sim.sedml — scenarios and parameter values
# ---------------------------------------------------------------------------

def sedml_scenarios(model_dir):
    """
    Ordered scenarios from sim.sedml. Each is
    ``{'id': str, 'source': model-script-ref|None, 'changes': [(name, raw_value), ...]}``.
    ``raw_value`` is the value as a target-language RHS expression, with XML entities
    already decoded (e.g. quoted strings come back quoted). Empty list if no SED-ML.
    """
    path = _find_ci(model_dir, "sim.sedml")
    if not path:
        return []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    out = []
    for model in root.iter(SEDML_NS + "model"):
        changes = []
        for ch in model.iter(SEDML_NS + "changeAttribute"):
            tgt = ch.get("target")
            val = ch.get("newValue")
            if tgt is not None and val is not None:
                changes.append((tgt, val))
        out.append({
            "id": (model.get("id") or model.get("name") or "").strip(),
            "source": _strip_path(model.get("source")),
            "changes": changes,
        })
    return out


def sedml_model_source(model_dir):
    """The model-script reference from the first SED-ML model that declares a source."""
    for s in sedml_scenarios(model_dir):
        if s["source"]:
            return s["source"]
    return None


# ---------------------------------------------------------------------------
# Combined script resolution
# ---------------------------------------------------------------------------

def _heuristic_model(model_dir, ext, exclude):
    """Last resort: if exactly one top-level script (excluding viz/params) exists, use it."""
    exclude = {e for e in (exclude or []) if e}
    cands = []
    try:
        for n in os.listdir(model_dir):
            if not n.lower().endswith(ext):
                continue
            low = n.lower()
            if low.startswith(("visualization", "param")) or n in exclude:
                continue
            if os.path.isfile(os.path.join(model_dir, n)):
                cands.append(n)
    except OSError:
        pass
    return cands[0] if len(cands) == 1 else None


def resolve_scripts(model_dir, language):
    """
    Resolve the script roles for a model, in priority order:
      1. metadata.rdf declarations (authoritative),
      2. the SED-ML ``<model source=…>`` (for the model script),
      3. the ``model.<ext>`` / ``visualization.<ext>`` filename convention,
      4. a single-script heuristic (model only).

    Returns ``{'model': rel|None, 'visualization': rel|None, 'readme': rel|None}`` with
    paths relative to ``model_dir``. ``model`` is None only when nothing resolves to an
    existing file — the caller should treat that as a non-runnable archive.
    """
    ext = ".py" if language == "python" else ".r"
    roles = script_roles(model_dir)

    model_ref = roles.get("model") or sedml_model_source(model_dir) or ("model" + ext)
    viz_ref = roles.get("visualization") or ("visualization" + ext)

    model = _resolve_existing(model_dir, model_ref)
    viz = _resolve_existing(model_dir, viz_ref)
    if not model:
        model = _resolve_existing(model_dir, _heuristic_model(model_dir, ext, [viz_ref]))

    readme = _resolve_existing(model_dir, roles.get("readme")) if roles.get("readme") else \
        _resolve_existing(model_dir, "README.txt")

    return {"model": model, "visualization": viz, "readme": readme}
