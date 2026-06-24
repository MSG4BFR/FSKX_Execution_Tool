# Stateful Workflow Engine — Roadmap (the next arc)

Phases 0–4 (see [README.md](README.md)) turned the tool into a small DAG **runner**: wire
models, run the whole graph top-to-bottom, see results. The next arc turns it into a **live,
stateful no-code workflow tool** — KNIME-like by deliberate, pragmatic subset, *not* a KNIME
clone. This doc is the decision record and the A–E roadmap; each phase gets its own
`phase-<X>-*.md` like before, starting with [phase-A](phase-A-run-history.md).

## Decision record (this session)

- **Backbone = live stateful workflow, not immutable run-history.** The workflow holds the
  *current* per-node execution state; execute / reset / configure mutate it; saving persists
  it. Re-executing overwrites. **History is a snapshot layer on top** (Phase A), not the main
  mechanism. (We considered making immutable run-history the primary model — the original
  "load past runs" request — and decided against it; history is the cheap groundwork, the
  stateful workflow is the goal.)
- **Why the codebase is ready:** the **file boundary** from phases 0–1 means every node
  already serialises its output to `outputs.json` in its run folder — that *is* an on-disk
  cached port output, and downstream consumption already reads it. Incremental and parallel
  execution are *data-safe* because each node already runs in its own env / container.

## The missing primitive (shared by every feature)

**Persistent per-node execution state with output caching.** Today `pipeline.json` is pure
*wiring* — it has no notion of "node n2 is executed; its output lives here." All three
KNIME-style features the user asked for reduce to adding that one primitive:

1. **Execute / reset / configure a node, auto-running upstream** — needs a per-node "executed"
   state + a cached output to plan partial runs against. *Reset* must invalidate downstream.
2. **Store each node's state and save the workflow** — the persistence layer the other two
   stand on; turns `pipeline.json` from wiring into a stateful document.
3. **Execute independent branches in parallel** — a readiness scheduler over the same DAG;
   the most self-contained of the three (data-safe already via per-node isolation).

## Phases

| Phase | Goal | Status |
|---|---|---|
| [A — Run history / snapshot](phase-A-run-history.md) | Persist every execution as a *referencing* record; list + reload onto the canvas (replays finished status) | ✅ Done (live-verified) |
| [B — Per-node caching + "execute up to here"](phase-B-node-caching.md) | Partial execution: run one node, auto-running only stale upstream; reuse caches | ✅ Done (live-verified) |
| C — Persistent live node state | Promote the node→run associations to *mutable*, saved-with-workflow state (the stateful document) | 🔭 Future |
| D — Parallel independent branches | Readiness scheduler + bounded worker pool; concurrent per-node status | 🔭 Future |
| E — Dirty / staleness propagation | Content-hash invalidation; downstream dirtying on config / upstream / model-version change | 🔭 Future |

A is the storage groundwork; **B is the heart of the KNIME feel**; C makes it durable across
sessions; D adds throughput; E makes it trustworthy.

## The hard problem (carried into B–E)

**Staleness / dirty propagation is what makes a workflow tool trustworthy** — and it is where
most of the real engineering lives. A node's cache is invalid when *any* of these change: its
own config (params / scenario), its incoming wiring, any upstream output, or the model
version. Clean approach: a per-node content hash `hash(config + upstream out-hashes + model
id)` compared against the cache's recorded hash — mismatch ⇒ dirty, and dirty **propagates
downstream**. The per-edge `value_hash`es already in provenance supply most of the raw
material. Get this disciplined *before* trusting cache reuse, or the tool will silently run on
stale data.

## Principles (carried through every phase)

- **Don't rip out the one-shot Run.** Incremental and parallel execution layer *on top of* the
  existing `pipeline.run` path; the simple "run the whole graph" button stays.
- **References, not copies.** Node artifacts stay owned by `_results/`; workflow and history
  records only *point* at them — no double storage, and existing compare / chat / cleanup keep
  working.
- **No change to the run-engine invariants** ([HANDOFF §2](HANDOFF.md)): file boundary,
  parameter identity `(node, paramId)`, injection-wins-by-ordering, table-driven dataTypes.
- **Each phase independently shippable + tested** behind the stubbed seams — unit tests need
  no Docker.
