# FSKX Model-Interchange Format — Specification v1.0.0

The contract for passing a model run's **OUTPUT** values into another model's **INPUT**
values (model joining/chaining), across languages and language versions (R, Python 3,
Python 2). It is the load-bearing decision the rest of the joining feature depends on:
everything else (the pipeline DAG, the UI, value injection) is built on top of this format.

Companion files:

- `interchange-schema.json` — the machine-readable JSON Schema (draft-07).
- `parameters-schema.json` — the upstream RAKIP Parameter-JSON-Exchange-Format this extends.

## 1. Design rationale

A joined pipeline runs each model in its **own isolated environment** (micromamba env or
container), exactly as today. Values therefore never cross as in-process objects — they
cross as **files on the shared work volume**. That single fact is what makes cross-language
and cross-version interop fall out for free: the boundary is already a file boundary, so an
R-produced value and a Python-2 consumer never share a runtime.

JSON is the carrier because it is universal, dependency-free, human-inspectable and
diffable, and already used throughout the tool. Language-specific binary formats (pickle,
RDS, feather) are rejected as the *primary* carrier — pickle is Python-version-locked and
unsafe, RDS is R-only. Large or binary payloads are **spilled to sidecar files** (CSV /
Parquet / raw) referenced by relative path from the JSON, mirroring how the wrappers already
write DataFrames to CSV.

The format is a **superset of `parameters-schema.json`**: it keeps `parameters[].metadata`,
`parameters[].data` and `parameters[].modelId`, and makes the previously-unspecified `data`
object concrete and typed.

## 2. Bundle structure

```json
{
  "formatVersion": "1.0.0",
  "generatorLanguage": "R",
  "generatedBy": { "tool": "FSKX Runner", "modelId": "<uuid>", "runId": "20260623_101530" },
  "parameters": [
    {
      "metadata": { "id": "response", "classification": "OUTPUT",
                    "dataType": "VECTOROFNUMBERS", "unit": "[Probability]" },
      "modelId": "<uuid>",
      "provenance": { "runId": "20260623_101530", "sourceVariable": "response" },
      "data": { "dataType": "VECTOROFNUMBERS", "encoding": "inline", "value": [0.01, 0.04, 0.12] }
    }
  ]
}
```

A model run emits **one bundle** (`outputs.json`), written into the run folder
(`_results/<base>/<run_id>/`) next to any sidecars. The existing `results.json` is unchanged
and still serves the UI/chat; `outputs.json` is the typed, machine-consumable contract.

**Scope: all classifications, not just OUTPUT.** The bundle records every declared parameter
that resolves to a value — `INPUT`, `CONSTANT` and `OUTPUT` — each tagged with its
`metadata.classification`. A join may legitimately source a model's *input* as well as its
result: on the "dream" slide most wiring is shared **inputs** (e.g. `niter`, `nburnin`,
`consumption`) fanned out to several models, not outputs. This is a bounded set (it comes
from `metaData.json`, never the full namespace), so capturing inputs/constants is cheap and
also strengthens provenance. (INPUT/CONSTANT values are captured post-run; a model that
*reassigns* an input in place will record the mutated value — rare, and noted as a caveat.)

## 3. The `data` encoding, per `dataType`

`encoding` is `"inline"` (value carried in JSON) or `"ref"` (value in a sidecar). A reader
dispatches on `dataType` — handling is **table-driven**, never a chain of special cases, so
new types extend the table rather than branching the code.

| dataType | inline `value` | ref (sidecar) | notes |
|---|---|---|---|
| `NUMBER` / `DOUBLE` | JSON number | — | non-finite → `value:null` + `special:"NaN"\|"Infinity"\|"-Infinity"` |
| `INTEGER` | JSON integer | — | distinct from DOUBLE so R can emit `42L` |
| `BOOLEAN` | JSON bool | — | |
| `STRING` | JSON string (**raw, unescaped**) | — | escaping is a *materialization* concern (§4), not a transit one |
| `DATE` | ISO-8601 string | — | `"2026-06-23"` or full date-time |
| `VECTOROFNUMBERS` | array of numbers | `csv` (single column) | non-finite elements → `null` + `specials: {"<idx>": token}`; spill to `ref` when large |
| `MATRIXOFNUMBERS` | 2D array (rows) + `shape:[r,c]` | `csv` / `parquet` + `shape` | inline only below a size threshold; otherwise `ref` |
| `FILE` | — | `raw` (+ `mediaType`) | always `ref`; the materialized value is the file **path string** (FSKX treats FILE as a STRING of the filename) |
| `OBJECT` | JSON object | `csv` / `parquet` (dataframe) · `json` (other) | **dataframe-first** — see §3.1 |

**Non-finite numbers.** JSON has no NaN/Inf. A scalar encodes `value:null` + `special`. A
vector/matrix encodes the element as `null` and records the flat (row-major) index in
`specials`. Readers reconstruct the language's native NaN/Inf.

**Size threshold.** Inline vs ref is a writer policy (suggested default: spill vectors
> 1000 elements and matrices > 10 000 cells to a sidecar). Readers must handle both
regardless of the threshold used.

### 3.1 OBJECT — dataframe-first policy

In practice the overwhelmingly common scientific OBJECT is a **table with named columns**
(an R `data.frame` / `data.table` / `tibble`, a Python `pandas.DataFrame`). It also happens
to be the one object type that round-trips *natively* across both languages, because
`read.csv()` yields a `data.frame` and `pd.read_csv()` yields a `DataFrame` with no glue
code. So OBJECT serialization is attempted in this order:

1. **Is it tabular (dataframe-like)?** → write a `csv` (or `parquet`) sidecar with a header
   row, `encoding:"ref"`, and an optional `columns` map preserving each column's logical
   type (e.g. `{"region":"STRING","flow":"DOUBLE"}`) so dtypes survive the boundary
   (R `character`/`factor` vs Python `str`, numeric vs integer). `shape:[rows,cols]`.
   This is the happy path and the materialized value is a native dataframe in both languages.
2. **Else, is it JSON-serializable?** (a plain list/dict/named list) → inline `json` value,
   or a `json` sidecar if large. Materialized via `jsonlite::fromJSON` / `json.loads`.
3. **Else** → emit the parameter with `partial:true`, a best-effort JSON of what could be
   extracted (e.g. an S4/environment's public fields), and **record a warning** on the run.
   The join may still proceed for *other* edges; an edge that depends on an unserializable
   OBJECT fails validation with a clear message rather than producing a silently-wrong value.

A writer decides "tabular" by capability, not class name: R via `is.data.frame(x)` (and
coercible `data.table`/`tibble`, which subclass it); Python via `isinstance(x, pd.DataFrame)`.
This keeps the rule language-neutral and avoids hardcoding library-specific types.

## 4. Renderer contract: typed value → source-code literal

Injecting a joined value reuses the **existing override mechanism** in
`engine.write_param_script` (base scenario assignments, then appended overrides — last wins).
The one new language-specific primitive is rendering a `typedValue` into a right-hand-side
**literal** for the target language. This is the only place language branches, and it is
selected by the model's already-resolved `language` (from `depresolve`) — **no Python
version is ever assumed; no language is hardcoded** outside this table.

| dataType | R RHS | Python RHS |
|---|---|---|
| NUMBER/DOUBLE | `3.14` · NaN→`NaN` · Inf→`Inf`/`-Inf` | `3.14` · NaN→`float('nan')` · Inf→`float('inf')`/`float('-inf')` |
| INTEGER | `42L` | `42` |
| BOOLEAN | `TRUE` / `FALSE` | `True` / `False` |
| STRING | quoted + escaped: `"a\"b"` | quoted + escaped: `"a\"b"` |
| DATE | `"2026-06-23"` (string; `as.Date(...)` optional) | `"2026-06-23"` |
| VECTOROFNUMBERS (inline) | `c(1, 2, 3)` | `[1, 2, 3]` |
| MATRIXOFNUMBERS (inline) | `matrix(c(...), nrow=r, byrow=TRUE)` | `[[...], [...]]` (plain nested list; model converts) |
| VECTOR/MATRIX (ref csv) | `as.matrix(read.csv("p", header=FALSE))` | `pd.read_csv("p", header=None).values` |
| FILE (ref) | `"p"` (path string) | `"p"` (path string) |
| OBJECT — dataframe (ref csv) | `read.csv("p")` → native `data.frame` | `pd.read_csv("p")` → native `DataFrame` |
| OBJECT — other (inline/json) | `jsonlite::fromJSON('<json>')` | `json.loads('<json>')` |

Notes:

- **STRING escaping happens here, at materialization** — matching `engine.render_value`,
  which already re-quotes string params. In transit (§3) the string is raw.
- **Python read literals are emitted self-contained** as `__import__('pandas').read_csv(...)`
  and `__import__('json').loads(...)` (shown abbreviated as `pd.read_csv` / `json.loads`
  above for readability), so an injected value assumes **nothing** about what the target
  model already imported. R's `jsonlite::` and base `read.csv` are self-contained already.
- **OBJECT** is materialized by embedding its JSON and parsing it in-language
  (`jsonlite::fromJSON` / `json.loads`) rather than hand-building nested literals — robust
  and dependency-light (`jsonlite` is already a wrapper dependency; `json` is Python stdlib).
- **Ref-backed tabular reads** assume `pandas` in Python (already special-cased in the
  Python wrapper) and base `read.csv` in R. A stdlib-`csv` fallback is acceptable where
  pandas is absent.

## 5. Join validation: dataType compatibility

When wiring `(modelA.outputId) → (modelB.inputId)`, the target input's declared `dataType`
is checked against the source output's. Exact match passes silently. The coercion table
below passes **with a warning**; anything else is a hard error before execution.

| source → target | result |
|---|---|
| NUMBER ↔ INTEGER ↔ DOUBLE | OK |
| NUMBER/INTEGER/DOUBLE → VECTOROFNUMBERS | OK (length-1 vector) |
| VECTOROFNUMBERS (length 1) → NUMBER/DOUBLE | warn |
| MATRIXOFNUMBERS (1×n or n×1) → VECTOROFNUMBERS | warn |
| VECTOROFNUMBERS → MATRIXOFNUMBERS | error (ambiguous shape) |
| any other cross-type | error |

**Cycles** are rejected: a pipeline must be a DAG.

## 5.1 Units & conversion

Every parameter carries its `metadata.unit` (the value from `metaData.json`, e.g. `"h"`,
`"CFU/g"`, `"log10"`, or `"[]"`). Units travel with the value so a join can reason about
them — but the policy is deliberately conservative:

- **No silent auto-conversion.** FSKX units are free text, not a controlled or reliably
  machine-parseable vocabulary, and in a risk-assessment context a silently-applied wrong
  factor is dangerous. The format therefore never converts on its own.
- **Warn on mismatch.** When an edge wires `source.unit` ≠ `target.unit` (and both are
  non-empty / not `"[]"`/`"none"`), validation surfaces a **warning** naming both units, so
  the modeller notices the hours-vs-seconds problem rather than getting a wrong result.
- **Explicit, auditable conversion per edge.** A join edge may declare an optional
  `transform` applied while transferring the value:
  - `{"scale": s, "offset": o}` — an **affine** map `x' = x*s + o`, applied **numerically**
    and element-wise to numbers/vectors/matrices (language-neutral; note temperature needs
    the `offset`, e.g. °C→K is `+273.15`). Example h→s: `{"scale": 3600}`.
  - `{"expression": "<expr>"}` — an escape hatch substituting the rendered literal for the
    `{value}` token (which arrives already parenthesized) in a target-language expression,
    e.g. `"{value} * 3600"`, for conversions an affine can't express.
  A transform is opt-in and recorded in the pipeline's provenance, so every conversion is
  visible and reproducible. The `transform` lives on the **join edge** (pipeline definition,
  Phase 2), not in the value bundle — the bundle describes a value *as produced*, including
  its unit; the pipeline decides how to adapt it on the way in.
- **Later:** a small known-units table (h/min/s/day, °C/K/°F, CFU↔log10CFU, …) may *suggest*
  a `transform` for a flagged mismatch, applied only on user confirmation. Full unit algebra
  (UCUM/pint) is out of scope while FSKX units remain free text.

## 6. Provenance & reproducibility

Each bundle records `generatedBy.modelId` + `runId`; each parameter may record its own
`provenance.runId` and `sourceVariable`. A pipeline run should additionally stamp, per edge,
the source `runId` and a hash of the materialized value, so a joined result is traceable to
the exact upstream values that produced it.

## 7. What this spec deliberately leaves open (v1 scope)

- No automatic unit conversion (warn only).
- `OBJECT` is dataframe-first (§3.1): tabular objects round-trip as native dataframes;
  non-tabular but JSON-serializable objects fall back to JSON; anything else is flagged
  `partial` with a warning.
- Parquet is an *optional* sidecar format for large tables; CSV is the floor everyone reads.
- Packaging a whole pipeline as a composite FSKX-of-FSKX is a later phase; v1 persists the
  join definition as a separate `pipeline.json`.
