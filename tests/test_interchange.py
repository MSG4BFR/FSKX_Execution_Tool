"""
Round-trip + schema-validation tests for the FSKX Model-Interchange format (Phase 1).

Run: python3 tests/test_interchange.py   (from the FSKX_Execution_Tool dir)

Covers every supported dataType through encode -> (schema-validate) -> read, and verifies
the renderer emits literals that are (a) valid Python that evaluates back to the value, and
(b) the expected R source. The R *writer* (interchange.R) needs an R runtime and is verified
on a Docker-enabled machine (see DEVELOPER.md §7); these tests exercise the Python side.
"""

import json
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
ROOT = os.path.dirname(os.path.dirname(HERE))  # workspace root (has interchange-schema.json)
sys.path.insert(0, APP)

import interchange as ic  # noqa: E402

try:
    import jsonschema
    _SCHEMA = json.load(open(os.path.join(ROOT, "interchange-schema.json")))
except Exception:  # noqa: BLE001
    jsonschema = None
    _SCHEMA = None

import pandas as pd  # noqa: E402

_failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def eq(a, b):
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    return a == b


def roundtrip(value, dtype, outdir, name):
    data = ic.encode_value(value, dtype, outdir=outdir, sidecar_basename=name)
    back = ic.read_value(data, outdir)
    return data, back


def main():
    tmp = tempfile.mkdtemp()

    # --- scalars ---
    for val, dt in [(3.14, "DOUBLE"), (42, "INTEGER"), (True, "BOOLEAN"),
                    ("hello \"world\"", "STRING")]:
        d, b = roundtrip(val, dt, tmp, "s")
        check(f"scalar {dt} round-trip", eq(b, val))

    d, b = roundtrip(float("inf"), "DOUBLE", tmp, "inf")
    check("scalar Inf round-trip", math.isinf(b) and b > 0 and d.get("special") == "Infinity")
    d, b = roundtrip(float("nan"), "DOUBLE", tmp, "nan")
    check("scalar NaN round-trip", math.isnan(b) and d.get("special") == "NaN")

    # --- vector with embedded NaN ---
    vec = [0.01, float("nan"), 0.12]
    d, b = roundtrip(vec, "VECTOROFNUMBERS", tmp, "vec")
    check("vector NaN round-trip", eq(b, vec) and d["specials"] == {"1": "NaN"})

    # --- matrix with embedded Inf ---
    mat = [[1.0, 2.0], [float("inf"), 4.0]]
    d, b = roundtrip(mat, "MATRIXOFNUMBERS", tmp, "mat")
    check("matrix Inf round-trip", eq(b, mat) and d["shape"] == [2, 2]
          and d["specials"] == {"2": "Infinity"})

    # --- large vector spills to a csv sidecar ---
    big = list(range(ic.VECTOR_INLINE_MAX + 5))
    d, b = roundtrip(big, "VECTOROFNUMBERS", tmp, "big")
    check("large vector spills to ref", d["encoding"] == "ref"
          and os.path.exists(os.path.join(tmp, d["ref"]["path"])) and eq([float(x) for x in b], [float(x) for x in big]))

    # --- OBJECT: dataframe-first -> csv sidecar -> native DataFrame ---
    df = pd.DataFrame({"region": ["Berlin", "Bayern"], "flow": [1200.5, 3400.0]})
    d, b = roundtrip(df, "OBJECT", tmp, "tbl")
    check("OBJECT dataframe -> ref csv", d["encoding"] == "ref"
          and d["columns"] == {"region": "STRING", "flow": "DOUBLE"})
    check("OBJECT dataframe -> native DataFrame", isinstance(b, pd.DataFrame)
          and list(b.columns) == ["region", "flow"] and b.shape == (2, 2))

    # --- OBJECT: non-tabular but JSON-serializable -> inline ---
    obj = {"alpha": 1, "beta": [1, 2, 3]}
    d, b = roundtrip(obj, "OBJECT", tmp, "obj")
    check("OBJECT json inline round-trip", d["encoding"] == "inline" and b == obj)

    # --- FILE -> ref raw, value is the path ---
    open(os.path.join(tmp, "out.csv"), "w").close()
    d, b = roundtrip("out.csv", "FILE", tmp, "f")
    check("FILE -> ref path", d["encoding"] == "ref" and b.endswith("out.csv"))

    # --- write_bundle validates against the schema, carrying unit + classification ---
    params = [
        {"id": "incubation", "dataType": "DOUBLE", "unit": "h", "classification": "INPUT",
         "value": 2.5},
        {"id": "emu", "dataType": "DOUBLE", "unit": "log10", "classification": "OUTPUT",
         "value": float("inf")},
        {"id": "response", "dataType": "VECTOROFNUMBERS", "classification": "OUTPUT",
         "value": vec},
        {"id": "outputTable", "dataType": "OBJECT", "classification": "OUTPUT", "value": df},
    ]
    path, warns = ic.write_bundle(params, tmp, "Python",
                                  {"tool": "FSKX Runner", "modelId": "m1", "runId": "r1"})
    bundle = json.load(open(path))
    by_id = {p["metadata"]["id"]: p["metadata"] for p in bundle["parameters"]}
    check("bundle carries unit + classification (incl. an INPUT source)",
          by_id["incubation"]["unit"] == "h"
          and by_id["incubation"]["classification"] == "INPUT"
          and by_id["emu"]["classification"] == "OUTPUT")
    if jsonschema:
        try:
            jsonschema.validate(bundle, _SCHEMA)
            check("bundle validates against interchange-schema.json", True)
        except jsonschema.ValidationError as e:  # noqa: BLE001
            check("bundle validates against interchange-schema.json: " + e.message, False)
    else:
        print("  skip  schema validation (jsonschema not installed)")

    # --- renderer: Python literals must eval back to the value ---
    ns = {"math": math}  # deliberately NO pd/json: literals must be self-contained
    cases = [
        (ic.encode_value(3.14, "DOUBLE"), 3.14),
        (ic.encode_value(42, "INTEGER"), 42),
        (ic.encode_value(True, "BOOLEAN"), True),
        (ic.encode_value("a\"b", "STRING"), "a\"b"),
        (ic.encode_value([0.01, float("nan"), 0.12], "VECTOROFNUMBERS"), None),
        (ic.encode_value([[1.0, 2.0], [3.0, 4.0]], "MATRIXOFNUMBERS"), [[1.0, 2.0], [3.0, 4.0]]),
        (ic.encode_value({"a": 1}, "OBJECT"), {"a": 1}),
    ]
    all_ok = True
    for data, expected in cases:
        lit = ic.render_rhs(data, "Python")
        try:
            got = eval(lit, ns)  # noqa: S307 (trusted, generated literal)
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  FAIL  python literal did not eval: {lit!r} ({exc})")
            continue
        if expected is not None and not eq(got, expected):
            all_ok = False
            print(f"  FAIL  python literal {lit!r} -> {got!r} != {expected!r}")
    check("renderer: Python literals eval back to values", all_ok)

    # special floats render correctly
    check("renderer: Inf -> Python",
          ic.render_rhs(ic.encode_value(float("inf"), "DOUBLE"), "Python") == "float('inf')")
    check("renderer: NaN in vector -> Python evals to nan",
          math.isnan(eval(ic.render_rhs(
              ic.encode_value([1.0, float("nan")], "VECTOROFNUMBERS"), "Python"), ns)[1]))

    # --- renderer: R literals are the expected source ---
    check("renderer: vector -> R c(...)",
          ic.render_rhs(ic.encode_value([1.0, 2.0], "VECTOROFNUMBERS"), "R") == "c(1.0, 2.0)")
    check("renderer: INTEGER -> R 42L",
          ic.render_rhs(ic.encode_value(42, "INTEGER"), "R") == "42L")
    check("renderer: Inf -> R Inf",
          ic.render_rhs(ic.encode_value(float("inf"), "DOUBLE"), "R") == "Inf")
    check("renderer: matrix -> R matrix(...)",
          ic.render_rhs(ic.encode_value([[1.0, 2.0], [3.0, 4.0]], "MATRIXOFNUMBERS"), "R")
          == "matrix(c(1.0, 2.0, 3.0, 4.0), nrow=2, byrow=TRUE)")
    dref = ic.encode_value(df, "OBJECT", outdir=tmp, sidecar_basename="t2")
    check("renderer: dataframe ref -> R read.csv / Py pandas",
          ic.render_rhs(dref, "R", path_override="t2.csv") == 'read.csv("t2.csv")'
          and ic.render_rhs(dref, "Python", path_override="t2.csv")
          == '__import__("pandas").read_csv("t2.csv")')
    # the self-contained Python dataframe literal actually evaluates (sidecar exists in tmp)
    _df_back = eval(ic.render_rhs(dref, "Python", path_override=os.path.join(tmp, "t2.csv")), ns)
    check("renderer: Py dataframe literal evals to a DataFrame",
          isinstance(_df_back, pd.DataFrame) and list(_df_back.columns) == ["region", "flow"])

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
