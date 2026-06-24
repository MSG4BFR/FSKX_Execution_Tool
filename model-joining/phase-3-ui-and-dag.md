# Phase 3 — UI & DAG ✅

**Status.** Done. A full **drag-and-drop node-graph canvas** in `templates/join.html` on top
of `server.py` routes (`/join`, `/api/model-params/<fskx>`,
`/api/pipeline/validate|run|status`), the `on_node` live hook in `pipeline.run`, and
`engine.model_param_specs`. The `validate`/`run` API and the pipeline JSON shape are unchanged
— the canvas is pure front-end on top of the same endpoints.

Edge-first builder (first cut):
- **Per-node parameter entry** — each node expands to an editable parameter form (defaults
  from the model's scenario); inputs driven by an edge/shared constant render **locked**
  ("🔒 bound — from #1.K ×2") instead of disappearing. `/api/model-params` returns the
  editable `fields` with defaults.
- **Used-value transparency** — `engine.execute` records the injected values
  (`run_meta.injected = {id: rhs}`), surfaced on the run page as "Joined parameters (from
  upstream models)" so a joined run shows e.g. `K = 4`, not just the scenario name.
- **Config persistence** — the builder autosaves to `localStorage` (now incl. node positions
  and view zoom/pan); leaving and returning to `/join` restores the whole configuration.

Node-graph canvas (current, dependency-free — no library):
- **Draggable node boxes** with blue **input** ports (left) and violet **output** ports
  (right); two-line model name + hover title. Drag from *any* port onto an input to wire an
  edge (inputs can be sources too, matching the dream slide's grey wires). Click a wire to edit
  its transform / delete.
- **Bezier edges with a dotted "ghost"** drawn on a top overlay only where a wire passes under
  a node box — including live while a new connection is being dragged — so an occluded wire
  stays traceable without doubling the line in open space.
- **Live execution-step badge** per node (the circle chip) from a client-side topological sort
  mirroring `pipeline.topo_order`; the raw `n#` id is internal only, so the visible number
  always reflects run order. Positions are purely cosmetic — drag freely; the step re-derives.
- **Loops blocked at creation** by a reachability check (canvas *and* the table editor), with a
  toast explaining why — never by id ordering. (`topo_order` is the backstop.)
- **Collapsible ports** — a column shows at most `PORT_CAP` (6) ports; connected ports are
  always rendered, the rest hide behind "+ N more…", so a wired port never detaches.
- **Pan / zoom / Fit / fullscreen** — background-drag pan, wheel + button zoom, Fit-to-view,
  and a maximize toggle; Auto-layout arranges by dependency depth then fits.
- The old row-based **table editor + parameter forms** are preserved under a collapsible
  "Advanced editor" — both bind to the same `nodes`/`edges` arrays as the canvas.
- **Two-view live sync** — the canvas edge panel and the table are two views of the same edge.
  Transform-value inputs (scale / offset / expression) edit via `edgeRaw`, which deliberately
  avoids a full re-render so the focused input keeps its cursor. Each such input carries
  `data-ei`/`data-ek` tags and `edgeRaw` pushes the new value into the matching *sibling* input
  (skipping the active one), so editing a transform on the canvas reflects in the table
  immediately and vice-versa — no save/refresh needed. (Fixed a bug where the table lagged
  until the next save or "Add connection".)

Remaining for later: scenario selection per node (TODO #2), and the live/animated run-status
overlay on the canvas (HANDOFF TODO).

**Goal.** Let a non-developer build and run a multi-node join in the browser, echoing the
node-graph on the "dream" slide.

## Scope

- **Join builder page** — pick models as nodes; for each edge, choose a source port and a
  target INPUT from dropdowns populated by each model's own `metaData.json` (filtered by
  `dataType` compatibility so only valid wirings are offered). Ports are labelled with the
  human `name` + unit, and addressed by `(node-instance, paramId)` so a `t` in one model never
  collides with a `t` in another (see Phase 2 — Parameter identity). Live validation surfaces
  warnings (unit/coercion) and errors (type/cycle) before running.

- **Bound input fields — locked, not hidden.** On a node's run form, a bound input renders as
  a **read-only "bound" field** showing its source (`← node · param`, plus any transform such
  as `h→s ×3600`) and the resolved value once upstream has run ("from upstream" before). This
  keeps the model's full parameter set visible and the data flow auditable — the opposite of
  silently dropping the field. An **"unbind / override for this run"** control is the escape
  hatch. Unbound inputs behave exactly as today.
- **N-node DAG execution** — generalize the Phase 2 two-node path to an arbitrary DAG with
  topological scheduling; show per-node status as the pipeline runs (reuse the background-job
  registry and polling already in `server.py`).
- **Provenance view** — for a finished pipeline run, show which source run/value fed each
  input, linking back to the existing run-history/compare pages.

## Design notes

- **Start simple.** A first cut can be a form (source run → map outputs to B's inputs) before
  a full drag-and-drop graph editor; the graph editor is pure front-end on top of the same
  validate/run API.
- **Reuse, don't fork.** Node runs go through the same `engine.execute`; the page reuses the
  existing status polling, result rendering, and "talk to your model" chat partial.

## Exit criteria

- A user wires the three-model example in the UI, runs it, and sees results + provenance with
  no code.
- Invalid wirings are un-selectable or clearly flagged before run.
