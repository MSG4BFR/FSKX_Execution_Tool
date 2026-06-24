"""
interchange.py — the FSKX Model-Interchange format, v1.0.0 (Python side).

See ../INTERCHANGE_SPEC.md and ../interchange-schema.json. Three concerns, all
table-driven so new dataTypes extend a dict rather than branch the code:

  * WRITER   ``encode_value`` turns a native Python value into a typed ``data`` dict
             (spilling large/binary payloads to sidecar files); ``write_bundle`` writes a
             whole ``outputs.json``. Runs INSIDE a Python model environment.
  * READER   ``read_value`` / ``read_bundle`` reconstruct native Python values. Used by the
             join engine / tests.
  * RENDERER ``render_rhs`` turns a typed ``data`` dict into an R or Python source LITERAL
             for injection as a parameter override into the NEXT model. Engine-only — it
             emits a string, so it needs no R runtime and assumes no Python version.

Pure module: only stdlib + an OPTIONAL pandas (used for dataframe OBJECTs; everything has a
pandas-free fallback so the base app environment need not ship it).
"""

import csv
import json
import math
import os

FORMAT_VERSION = "1.0.0"

# Writer policy: spill above these sizes to a sidecar file (readers handle both regardless).
VECTOR_INLINE_MAX = 1000
MATRIX_INLINE_MAX = 10000  # cells

_SPECIAL_TO_FLOAT = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}

try:  # optional; only needed for dataframe OBJECTs
    import pandas as _pd
except Exception:  # noqa: BLE001
    _pd = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _norm(dt):
    return (dt or "").upper()


def _finite_or_special(x):
    """(json_number, None) for finite x; (None, token) for NaN/Inf."""
    f = float(x)
    if math.isnan(f):
        return None, "NaN"
    if math.isinf(f):
        return None, "Infinity" if f > 0 else "-Infinity"
    return f, None


def _safe_name(s):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(s)) or "value"


def _as_dataframe(value):
    """Return a pandas DataFrame if value is tabular (DataFrame, or a dict of equal-length
    columns), else None. Capability-based, not class-name based."""
    if _pd is not None and isinstance(value, _pd.DataFrame):
        return value
    if isinstance(value, dict) and value and _pd is not None:
        cols = list(value.values())
        if all(isinstance(c, (list, tuple)) for c in cols) and \
                len({len(c) for c in cols}) == 1:
            return _pd.DataFrame(value)
    return None


def _col_type(series):
    """Infer an FSK-ML dataType for a dataframe column."""
    try:
        import pandas.api.types as pat
        if pat.is_bool_dtype(series):
            return "BOOLEAN"
        if pat.is_integer_dtype(series):
            return "INTEGER"
        if pat.is_float_dtype(series) or pat.is_numeric_dtype(series):
            return "DOUBLE"
    except Exception:  # noqa: BLE001
        pass
    return "STRING"


# ---------------------------------------------------------------------------
# WRITER
# ---------------------------------------------------------------------------

def _enc_scalar_number(value, dt, outdir, base):
    if dt == "INTEGER":
        try:
            return {"dataType": "INTEGER", "encoding": "inline", "value": int(value)}
        except (TypeError, ValueError):
            pass
    v, sp = _finite_or_special(value)
    out = {"dataType": dt or "DOUBLE", "encoding": "inline", "value": v}
    if sp:
        out["special"] = sp
    return out


def _enc_bool(value, dt, outdir, base):
    return {"dataType": "BOOLEAN", "encoding": "inline", "value": bool(value)}


def _enc_string(value, dt, outdir, base):
    return {"dataType": "STRING", "encoding": "inline", "value": str(value)}


def _enc_date(value, dt, outdir, base):
    iso = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return {"dataType": "DATE", "encoding": "inline", "value": iso}


def _enc_vector(value, dt, outdir, base):
    seq = list(value)
    if outdir and len(seq) > VECTOR_INLINE_MAX:
        path = base + ".csv"
        with open(os.path.join(outdir, path), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for x in seq:
                w.writerow([x])
        return {"dataType": "VECTOROFNUMBERS", "encoding": "ref", "shape": [len(seq)],
                "ref": {"path": path, "format": "csv", "mediaType": "text/csv",
                        "header": False}}
    vals, specials = [], {}
    for i, x in enumerate(seq):
        v, sp = _finite_or_special(x)
        vals.append(v)
        if sp:
            specials[str(i)] = sp
    out = {"dataType": "VECTOROFNUMBERS", "encoding": "inline", "value": vals,
           "shape": [len(seq)]}
    if specials:
        out["specials"] = specials
    return out


def _to_rows(value):
    if _pd is not None and isinstance(value, _pd.DataFrame):
        return value.values.tolist()
    rows = []
    for r in value:
        rows.append(list(r) if isinstance(r, (list, tuple)) else [r])
    return rows


def _enc_matrix(value, dt, outdir, base):
    rows = _to_rows(value)
    nrows = len(rows)
    ncols = len(rows[0]) if rows else 0
    if outdir and nrows * ncols > MATRIX_INLINE_MAX:
        path = base + ".csv"
        with open(os.path.join(outdir, path), "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        return {"dataType": "MATRIXOFNUMBERS", "encoding": "ref", "shape": [nrows, ncols],
                "ref": {"path": path, "format": "csv", "mediaType": "text/csv",
                        "header": False}}
    out_rows, specials, idx = [], {}, 0
    for r in rows:
        rr = []
        for x in r:
            v, sp = _finite_or_special(x)
            rr.append(v)
            if sp:
                specials[str(idx)] = sp
            idx += 1
        out_rows.append(rr)
    out = {"dataType": "MATRIXOFNUMBERS", "encoding": "inline", "value": out_rows,
           "shape": [nrows, ncols]}
    if specials:
        out["specials"] = specials
    return out


def _enc_file(value, dt, outdir, base):
    p = str(value)
    return {"dataType": "FILE", "encoding": "ref",
            "ref": {"path": os.path.basename(p), "format": "raw",
                    "mediaType": "application/octet-stream"}}


def _enc_object(value, dt, outdir, base):
    # 1. dataframe-first.
    df = _as_dataframe(value)
    if df is not None and outdir is not None:
        path = base + ".csv"
        df.to_csv(os.path.join(outdir, path), index=False)
        cols = {str(c): _col_type(df[c]) for c in df.columns}
        return {"dataType": "OBJECT", "encoding": "ref", "shape": list(df.shape),
                "columns": cols,
                "ref": {"path": path, "format": "csv", "mediaType": "text/csv",
                        "header": True}}
    # 2. JSON-serializable?
    try:
        json.dumps(value)
        return {"dataType": "OBJECT", "encoding": "inline", "value": value}
    except (TypeError, ValueError):
        pass
    # 3. best-effort partial.
    extracted = {}
    for attr in getattr(value, "__dict__", {}) or {}:
        try:
            json.dumps(getattr(value, attr))
            extracted[attr] = getattr(value, attr)
        except (TypeError, ValueError):
            extracted[attr] = repr(getattr(value, attr))
    return {"dataType": "OBJECT", "encoding": "inline", "value": extracted,
            "partial": True}


_ENCODERS = {
    "NUMBER": _enc_scalar_number, "DOUBLE": _enc_scalar_number, "INTEGER": _enc_scalar_number,
    "BOOLEAN": _enc_bool, "STRING": _enc_string, "DATE": _enc_date,
    "VECTOROFNUMBERS": _enc_vector, "MATRIXOFNUMBERS": _enc_matrix,
    "FILE": _enc_file, "OBJECT": _enc_object,
}


def encode_value(value, data_type, outdir=None, sidecar_basename=None):
    """Encode a native Python value into a typed ``data`` dict. ``outdir`` (when given) is
    where sidecar files are written; without it, everything is forced inline."""
    dt = _norm(data_type)
    base = _safe_name(sidecar_basename or "value")
    handler = _ENCODERS.get(dt, _enc_object)
    return handler(value, dt, outdir, base)


def write_bundle(parameters, outdir, generator_language="Python", generated_by=None):
    """Write ``outputs.json`` for a run. ``parameters`` is a list of dicts with at least
    ``id`` and ``value`` (and ideally ``dataType``). Returns (path, warnings)."""
    items, warnings = [], []
    model_id = (generated_by or {}).get("modelId", "")
    for p in parameters:
        pid = p["id"]
        try:
            data = encode_value(p.get("value"), p.get("dataType"),
                                outdir=outdir, sidecar_basename=pid)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"could not serialize output '{pid}': {exc}")
            continue
        if data.get("partial"):
            warnings.append(f"output '{pid}' is a non-tabular OBJECT; serialized partially")
        meta = {"id": pid, "dataType": _norm(p.get("dataType")) or data["dataType"]}
        for k in ("name", "unit", "classification", "description"):
            if p.get(k) is not None:
                meta[k] = p[k]
        items.append({"metadata": meta, "modelId": model_id, "data": data})
    bundle = {"formatVersion": FORMAT_VERSION,
              "generatorLanguage": generator_language, "parameters": items}
    if generated_by:
        bundle["generatedBy"] = generated_by
    path = os.path.join(outdir, "outputs.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
    return path, warnings


# ---------------------------------------------------------------------------
# READER
# ---------------------------------------------------------------------------

def _special(token):
    return _SPECIAL_TO_FLOAT.get(token, float("nan"))


def _apply_specials(value, specials, dt):
    if not specials:
        return value
    if dt == "VECTOROFNUMBERS":
        out = list(value)
        for i, tok in specials.items():
            out[int(i)] = _special(tok)
        return out
    # matrix: flat row-major index
    flat = [x for row in value for x in row]
    for i, tok in specials.items():
        flat[int(i)] = _special(tok)
    out, k = [], 0
    for row in value:
        out.append(flat[k:k + len(row)])
        k += len(row)
    return out


def _read_ref(data, base_dir):
    ref = data["ref"]
    path = os.path.join(base_dir or "", ref["path"])
    dt = _norm(data["dataType"])
    fmt = ref.get("format")
    if fmt == "raw" or dt == "FILE":
        return path  # FILE: the value is the path itself
    if fmt == "json":
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    # csv-backed vector / matrix / dataframe
    if dt == "OBJECT":
        if _pd is not None:
            return _pd.read_csv(path)
        with open(path, encoding="utf-8") as fh:  # pandas-free fallback: list of dicts
            return list(csv.DictReader(fh))
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    nums = [[_maybe_num(c) for c in r] for r in rows]
    if dt == "VECTOROFNUMBERS":
        return [r[0] for r in nums if r]
    return nums


def _maybe_num(s):
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def read_value(data, base_dir=None):
    """Reconstruct a native Python value from a typed ``data`` dict."""
    dt = _norm(data["dataType"])
    if data.get("encoding") == "ref":
        return _read_ref(data, base_dir)
    v = data.get("value")
    if dt in ("NUMBER", "DOUBLE", "INTEGER") and v is None and data.get("special"):
        return _special(data["special"])
    if dt in ("VECTOROFNUMBERS", "MATRIXOFNUMBERS"):
        return _apply_specials(v, data.get("specials"), dt)
    return v


def read_bundle(path):
    """Read an ``outputs.json`` into ``{param_id: native_value}``."""
    with open(path, encoding="utf-8") as fh:
        bundle = json.load(fh)
    base = os.path.dirname(path)
    return {p["metadata"]["id"]: read_value(p["data"], base)
            for p in bundle.get("parameters", [])}


# ---------------------------------------------------------------------------
# RENDERER (typed value -> R / Python source literal)
# ---------------------------------------------------------------------------

def _is_r(language):
    return str(language).lower().startswith("r")


def _num_lit(v, special, is_r):
    if v is None and special:
        if is_r:
            return {"NaN": "NaN", "Infinity": "Inf", "-Infinity": "-Inf"}[special]
        return {"NaN": "float('nan')", "Infinity": "float('inf')",
                "-Infinity": "float('-inf')"}[special]
    return repr(v)


def _str_lit(s):
    # Double-quoted with backslash escapes — valid in both R and Python (matches
    # engine.render_value), so STRING escaping is identical across languages.
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _embed_json_lit(value, is_r):
    payload = json.dumps(value)
    # Wrap in a single-quoted literal; escape backslashes then single quotes.
    esc = payload.replace("\\", "\\\\").replace("'", "\\'")
    # Self-contained: don't assume the target model imported jsonlite / json. R's jsonlite::
    # is explicitly namespaced; Python uses __import__ so the literal is a single expression
    # that needs no prior import statement in the model's parameter script.
    return (f"jsonlite::fromJSON('{esc}')" if is_r
            else f"__import__('json').loads('{esc}')")


def render_rhs(data, language, path_override=None):
    """Render a typed ``data`` dict as a source-code literal for ``language`` ('R'/'Python').
    ``path_override`` replaces the sidecar path in the emitted read call (the engine sets it
    to where the sidecar will live in the target work dir)."""
    is_r = _is_r(language)
    dt = _norm(data["dataType"])

    if data.get("encoding") == "ref":
        path = path_override or data["ref"]["path"]
        # Self-contained literals: R uses base read.csv; Python uses __import__('pandas')
        # so nothing assumes the model already imported pandas as `pd`.
        if dt == "FILE":
            return _str_lit(path)
        if dt == "OBJECT":  # dataframe
            return (f'read.csv("{path}")' if is_r
                    else f'__import__("pandas").read_csv("{path}")')
        if dt == "VECTOROFNUMBERS":
            return (f'read.csv("{path}", header=FALSE)[[1]]' if is_r
                    else f'__import__("pandas").read_csv("{path}", header=None).iloc[:, 0].tolist()')
        return (f'as.matrix(read.csv("{path}", header=FALSE))' if is_r
                else f'__import__("pandas").read_csv("{path}", header=None).values')

    v = data.get("value")
    if dt in ("NUMBER", "DOUBLE"):
        return _num_lit(v, data.get("special"), is_r)
    if dt == "INTEGER":
        return f"{int(v)}L" if is_r else str(int(v))
    if dt == "BOOLEAN":
        return ("TRUE" if v else "FALSE") if is_r else ("True" if v else "False")
    if dt in ("STRING", "DATE"):
        return _str_lit(v)
    if dt == "VECTOROFNUMBERS":
        specials = data.get("specials", {})
        els = []
        for i, x in enumerate(v):
            els.append(_num_lit(x, specials.get(str(i)), is_r) if x is None
                       else repr(x))
        return ("c(" + ", ".join(els) + ")") if is_r else ("[" + ", ".join(els) + "]")
    if dt == "MATRIXOFNUMBERS":
        specials = data.get("specials", {})
        flat, idx = [], 0
        for row in v:
            for x in row:
                flat.append(_num_lit(x, specials.get(str(idx)), is_r) if x is None
                            else repr(x))
                idx += 1
        nrows = len(v)
        if is_r:
            return f"matrix(c({', '.join(flat)}), nrow={nrows}, byrow=TRUE)"
        return "[" + ", ".join("[" + ", ".join(
            (_num_lit(x, specials.get(str(r * len(v[0]) + c)), is_r) if x is None else repr(x))
            for c, x in enumerate(row)) + "]" for r, row in enumerate(v)) + "]"
    # OBJECT inline (and any fallback): embed JSON and parse in-language.
    return _embed_json_lit(v, is_r)
