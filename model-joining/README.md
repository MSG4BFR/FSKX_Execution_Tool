# Model Joining — Feature Roadmap

> **Continuing in a new session? Start with [HANDOFF.md](HANDOFF.md)** — full summary,
> invariants, how to run/test, file map, and the prioritized open-TODO list.

Chaining FSKX model simulations so the **OUTPUT** of one model feeds the **INPUT** of the
next ("the dream" slide: Single-Hit → Exposure → Population Risk). Models may be in
different languages and versions (R, Python 3, Python 2), so values cross between them as a
neutral, typed, on-disk format — never as shared in-process objects.

## Why this works with the existing tool

The architecture already has the seams: parameters are classified
(`INPUT`/`CONSTANT`/`OUTPUT` + `dataType`) in `metaData.json`; inputs are injected by
*appending override assignments* in `engine.write_param_script`; and every run is persisted
and addressable (`_results/<base>/<run_id>/`, `engine.list_runs`). Each model already runs
in its own isolated env/container, so the language boundary is already a file boundary — the
key reason cross-language/version interop is automatic.

## Phases

| Phase | Goal | Status |
|---|---|---|
| [0 — Interchange spec](phase-0-interchange-spec.md) | Define the typed, language-neutral value format | ✅ Done |
| [1 — Serialization layer](phase-1-serialization-layer.md) | Emit/read the format from R & Python runs | ✅ Done |
| [2 — Pipeline engine](phase-2-pipeline-engine.md) | Two-node join via value injection (API/CLI) | ✅ Done (core; live Docker/R run still to verify) |
| [3 — UI & DAG](phase-3-ui-and-dag.md) | N-node join builder, validation, provenance | ✅ Done (drag-and-drop node-graph canvas) |
| [4 — Packaging](phase-4-packaging.md) | Persist/share a pipeline; composite FSKX | ✅ Done (COMBINE/OMEX-conformant `.fskxp` w/ `manifest.xml` + `metadata.rdf`) |

> **Latest:** the edge-table builder grew into a full **drag-and-drop node-graph canvas** in
> `app/templates/join.html` — draggable boxes with input/output ports, drawn bezier edges
> (with a dotted "ghost" echo where a wire passes under a node), pan/zoom/fit/fullscreen, a
> live execution-step badge per node (derived topo order), reachability-based loop blocking,
> and connected-first collapsible ports. See [HANDOFF.md](HANDOFF.md) §3 and the open TODOs.

## Principles (carried through every phase)

- **No hardcoded language/version.** The only language-specific code is a table-driven
  value→source renderer, selected by the model's already-resolved language.
- **Reuse existing seams** (param overrides, run history, OMEX metadata) — no parallel
  machinery.
- **File boundary, not runtime boundary.** Values cross as JSON + sidecar files on the
  shared work volume.
- **Each phase is independently shippable and tested.**
