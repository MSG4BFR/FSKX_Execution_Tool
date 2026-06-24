# Phase 0 — Interchange Spec ✅

**Goal.** Define the language-neutral, typed serialization format for parameter values — the
load-bearing contract everything else builds on.

## Deliverables

- [`INTERCHANGE_SPEC.md`](../INTERCHANGE_SPEC.md) — human-readable contract: per-`dataType`
  encoding, the value→RHS renderer table for R/Python, join coercion table, provenance.
- [`interchange-schema.json`](../interchange-schema.json) — draft-07 JSON Schema; a superset
  of the RAKIP `parameters-schema.json` with a concrete, typed `data` object.

## Key decisions

- **JSON envelope, sidecars for bulk.** A run emits `outputs.json` scoped to declared OUTPUT
  params; large/binary values spill to CSV/Parquet/raw files referenced by relative path.
- **Table-driven `data` encoding** per `dataType` (`inline` vs `ref`), with explicit
  NaN/Inf handling (JSON has neither: `value:null` + `special`/`specials`).
- **OBJECT is dataframe-first.** Tabular objects (`data.frame`/`DataFrame`) serialize to a
  CSV/Parquet sidecar with a `columns` type map and round-trip *natively*
  (`read.csv` ↔ `pd.read_csv`); non-tabular JSON-serializable objects fall back to JSON;
  anything else is flagged `partial` with a warning.

## Verification done

Sample bundle validates against the schema; a round-trip reconstructs an Inf scalar, a
NaN-containing vector, and a dataframe-OBJECT into native values, and emits correct R **and**
Python literals. R execution path to be confirmed on a Docker-enabled machine.

## Exit criteria (met)

A value of any supported `dataType` can be described unambiguously in the format, and a
reader in either language can reconstruct it without sharing a runtime.
