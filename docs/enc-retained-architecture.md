# ENC retained architecture — OpenCPN-inspired port

**Decision:** port the architecture into Python/Qt, not embed OpenCPN's C++
engine. User-directed, 2026-10-21. **Runtime status: not implemented.**
This is implementation detail for the
[canonical roadmap](rendering-performance-plan.md#enc-retained-architecture-port),
not a replacement weather program or a second issue tracker.

## Decision and scope

Adopt durable prepared charts, resident geometry, coverage planning, direct GPU
portrayal, separate screen-space annotations and continuous camera interaction.
Use Qt Quick's existing scene graph/Metal host, not a second OpenGL context or
wxWidgets. Retain Cairo as reference and recovery path.

This revises the earlier rejection of transformed ENC previews. Geographically
aligned, bounded previews may remain visible during interaction, explicitly
distinguished from current chart detail. Final scale selection, coverage,
symbols and labels must refresh. Scaled bitmaps are transitional, not the
completed retained-vector port.

Preserve supported chart detail, soundings, visibility, source ranking, exact
coverage, alpha/order, palettes and switches. Pixel-for-pixel legacy matching is
not required; missing features or reduced detail are not acceptable speedups.
This does not promise full S-52/OpenCPN feature parity. OpenCPN's chart-choice
policies and raster-versus-ENC arbitration are not imported without a separate
behavioral decision. Keep existing world/raster/CHS/MBTiles/marine/navigation
layers working and ordered; do not subsume the other quilting workstream.

No routing/sampling changes, automatic downloads, user-data/settings mutation,
GTK migration, CPU fallback removal or world-base fix (#84) are included.

## Source evidence

Reference: [OpenCPN commit
02cf53340029c291b2903a5eeaa5a6b9443f9d21](https://github.com/OpenCPN/OpenCPN/tree/02cf53340029c291b2903a5eeaa5a6b9443f9d21).
Three free-model agents investigated source; the primary agent checked these
mechanisms. Source findings are not runtime benchmarks.

| Mechanism | Pinned source | Our adaptation |
| --- | --- | --- |
| Persistent preparation | `gui/src/s57chart.cpp:2794-2816` builds/loads SENC; `gui/src/o_senc.cpp:2234-2316` serializes tessellated area primitives | Own versioned prepared-cell format, not binary SENC compatibility |
| Retained geometry | `gui/src/s57chart.cpp:1377-1397` creates a static line VBO once; `libs/s52plib/src/s52plib.cpp:9555-9587` uploads/reuses area buffers, with a temporary-buffer option | Immutable arrays and retained QSG geometry with Qt thread ownership |
| Coverage clipping | `gui/src/s57chart.cpp:1571-1678` intersects update/chart regions and clips GL drawing | Preserve exact current ownership, not rectangular clipping alone |
| Zoom preview | `gui/src/gl_chart_canvas.cpp:4766-4877`, `4913-4954`, `5585-5597` transform cached textures in an accelerated pinch path and commit final zoom | Bounded geographic preview while new detail prepares |
| Annotation refresh | `gui/src/s57chart.cpp:1587-1606` refreshes lookup rules/text caches/point bounds | Separate screen-space layout and portrayal from geometry identity |

Do not cite the disabled combined line/area VBO branch as enabled behavior.
An empty `FastPan` function does not establish all panning behavior. The full
software/input/backend/quilt-cache matrix was not audited. No measured OpenCPN
FPS/latency comparison is claimed. No code is copied; direct code/asset reuse
requires separate licensing/provenance review.

## Base and integration boundary

`feature/enc-architecture-port` starts at integrated fork `dev`, `98f1787`,
including PRs #82/#83 weather residency/lookahead. The behind local `dev`
checkout has unrelated changes and was not modified.

The user subsequently authorized merging PR #81 before runtime port work.
PR #81 merged into `dev` at `386f5a1`, preserving its rollout, all-points
behavior, pan retention and view-tolerance cache together with #82/#83 weather
residency. Integration passed 104 targeted tests and the 13-check app rollout
gate; interaction performance remains unaccepted. Update this branch from that
merged base before attaching the prepared source. Do not expand #81 to include
the port or launch another app.

Reuse these boundaries:

- `qt/viewportenc.py`: manifest/catalog and per-cell coverage reporting.
- `rendering/encrenderer.py`: authoritative scale ranking/visibility and exact
  coverage union/intersection/difference; factor planning only under tests.
- `rendering/vectordrawer/s57chartdrawer.py`: supported extraction, portrayal
  order and soundings. View-dependent simplification is not durable identity.
  Portrayal currently mutates some features: adapt before sharing immutable data.
- `qt/gribviewportchart.py`: split static ENC from forecast-dependent isobars.
- `qt/gribviewport.py`, `qt/gpuscene.py`: shared camera/projection, input and QSG
  ownership patterns; do not invent another camera engine.

PR #81's isolated ~2.4 s cold/~1.04 s repeat composition is not interactive
latency or a baseline on this newer weather branch. The user rejected its app
experience despite functional gate passes.

## Target contracts

```text
source revision -> worker-private decode -> durable prepared cells + index
camera demand -> authoritative coverage planner -> resident Qt Quick geometry
                                               + screen-space labels/symbols
continuous camera transform -> app layer composition + honest coverage status
```

### Durable chart preparation

- Store canonical coordinates/topology, stable layer/FID/part IDs, attributes,
  sounding Z, source iteration order, compilation scale, bounds, exact coverage
  with holes, and a camera-independent spatial index. No OGR objects cross threads.
- Begin at full detail. Derived tessellation/stroke data is versioned. The
  completed warm camera path must not decode or run `SimplifyPreserveTopology`.
  A later finite LOD ladder requires topology preservation and the existing
  half-pixel geometric error bound at every admitted scale.
- Proposed container: standard-library SQLite metadata/index plus validated
  WKB/typed arrays; no pickle/executable deserialization or optional extension
  assumption. P1 must settle the portable index, schema and bounds validation.
- Identity includes ordered base/update content digests, decoder/options/version,
  projection and schema. Stats can accelerate validation, not replace persisted
  identity. Preserve GDAL update semantics; check source revision before/after
  building and reject publication if it changes.
- Transactional disposable cache writes, never source writes. Reject corrupt,
  incompatible or oversized data and rebuild. Deduplicate builds across maps;
  specify disk quota, staging space, active-reader protection and failed-write
  recovery before enabling application cache writes.

### Planning, drawing and lifetime

- Camera transforms immediately: no GUI file I/O, feature collection or full
  raster redraw. Visible chart demand must not wait for the 500 ms settle or
  weather-idle gates. Bound preparation so weather/interaction remain responsive.
- Select contributing cells from cached exact coverage before opening/uploading
  geometry; preserve scale ranking and overlap ownership. Reuse completed cell
  work across camera-only supersession. At most two preparations in flight;
  visible gaps outrank speculation; wrong content generations cannot publish.
- Use Metal-compatible QSG geometry/materials, shared projection and local
  origins for harbor precision. Audit polygon holes, coastlines, antimeridian,
  world copies, alpha, clipping, stroke widths/joins/caps and dashes explicitly.
- Labels/symbols use geographic anchors with logical-pixel sizes and actual DPR.
  Preserve sounding values/format/visibility/anchors/screen suppression before
  changing declutter policy. Label layout may follow camera changes, not cell
  decoding/tessellation. Preview labels may scale temporarily, not finally.
- Pure ENC identity excludes forecast time. Isobars/weather retain independent
  revisions; scrubbing must not flush static charts or show old contours as current.
- Distinguish current/preview/loading/missing/disabled/unavailable. Disable or
  source removal immediately withdraws affected content. Preview covers only
  retained geography; do not repeat/stretch it into newly exposed areas.
- Share the existing session resource ledger across both maps, accounting CPU
  geometry, GPU buffers/atlases, preview images and in-flight copies once. Reserve
  before allocation; evict speculation before visible content. If visible demand
  cannot fit, recover explicitly. Scene-graph thread creates/destroys GPU resources;
  durable CPU/disk data can survive context recreation.

## Delivery slices and exit gates

Link executing slices to existing #51/#69 work; #70 remains validation-only.
Issue creation/reopening and PR publication need separate authorization. Execute
one slice at a time; decide continue/narrow/pivot/stop at each gate.

| Slice | Vertical delivery | Exit evidence |
| --- | --- | --- |
| P0: decision (current) | Source-grounded design, preview revision and integration boundaries | Documentation validation; no runtime claim |
| P1: durable prepared cell | Real cell builds, closes, reloads in a fresh process and renders through a Cairo adapter without source reopen for drawing | Attributes/Z/topology/order/coverage and selection preserved; base/update/schema/corruption/concurrent-write invalidation; bounded CPU/disk use; separate cold-build/disk-warm/memory-warm costs |
| P2: retained geometry | Areas with holes, strokes and exact multi-cell ownership in native QSG | App-host Metal rendering, harbor/DPR/projection checks; resident camera changes cause zero decode/tessellation/unchanged-buffer uploads; explicit fallback |
| P3: full portrayal/interaction | Separate labels/symbols, preview states and weather-independent chart demand | Supported feature inventory, final glyph sizes and soundings; pan/zoom/reversal/recovery under stalled workers; no blanking of retained covered areas or stale content reported current |
| P4: app integration/hardening | Main/log maps, layers/switches/recovery; reconcile #81 with #82/#83 | Actual combined app workload, cold/warm revisits, no forecast, all wind modes, time/source/style/DPR changes, shared eviction, context loss/shutdown, memory plateau/platform matrix |

P1 is not the complete port. Reopening sources is allowed for build/invalidation.
Its transitional Cairo adapter may still use camera-specific preparation: do
not claim it fulfills P2/P3 or removes the warm redraw cost.

## Measurement and acceptance

Extend `tools/gate_runner.py`/ENC gates. P1+ commands must be registered before
experiments; they do not yet exist. Test source/style invalidation separately
from valid camera previews; do not simply weaken stale-image-hiding checks.

- Correctness: feature/provenance and exact coverage identities, holes/alpha/order,
  readable supported annotations and final density. Readback is fidelity, not timing.
- Mechanism: source opens, decoded features, simplified/tessellated vertices,
  uploads/bytes, duplicate/canceled work, invalidation. Zero repeated preparation
  is necessary for warm residency, not proof of FPS.
- Experience: real app/input with deliberately stalled chart/weather workers;
  count blank-covered-area events and preview duration. Report cold/new-scale/
  new-region/missing-detail completion separately so previews cannot hide delays.
- Performance: preserve canonical warm refresh-budget and p95 input-to-present
  <50 ms targets. Use a defensible endpoint or mark presentation unmeasured.
  Cold-build/load/settling/recovery limits need workload-based ratification.
- Resources: accounted plateau, eviction/reload and two-map sharing including
  buffers/atlases; accounted payload is not RSS/VRAM.
- Verify approved snapshot
  `410b88ada69dd5a22fc18765a0f26b016e9d15df22bcfeaa954b8b4b62da2346`.
  It is not identical to the damaged older fixture. Freeze its manifest time;
  alter sandbox switches only, never live settings.

## Current bounded contract

The completed P0 touched this spec, the ADR pointer, canonical checkpoint and doc index only.
Two free-model read-only boundary reviews; at most two documentation validation
rounds; zero native runs, source changes, baseline measurements, commits, pushes,
PRs or merges. Stop rather than expand into a triangulation/backend experiment.

**Now: P1, user-authorized after merging existing work.** Nothing is declared
shipped by this design. Do not provisionally start P2.

### P1 implementation contract

- Scope: worker-private persistent prepared source plus an explicitly requested
  Cairo adapter and one real-cell build/reload/render gate. No default app
  switch, GUI-thread chart preparation, tessellator or GPU rewrite.
- Format: versioned SQLite metadata/layer/feature tables, canonical WKB geometry
  and JSON attributes with field definitions; preserve layer/feature order,
  FIDs, nulls, lists, sounding Z, metadata and coverage. Numeric envelope columns
  provide a portable spatial-filter index without an RTree dependency. Rebuild
  worker-local OGR Memory layers for the existing drawer; GDAL source decoding
  is absent from a warm prepared load. The Cairo adapter may still simplify
  per view; that cost is reported, not claimed solved.
- Identity: SHA256 of ordered `.000`/applicable numbered update components,
  GDAL version/S57 options and schema. Validate before and after building;
  publish atomically. Ignore invalid artifacts and rebuild, without falling
  back silently to outdated chart content.
- Trial limits: 64 MiB serialized feature payload per cell and 256 MiB retained
  disk cache, including a bounded staging allowance. These are conservative
  experimental caps, not measured working-set/RSS limits. No unbounded whole-
  catalogue preload or automatic source-tree writes. Active worker resources
  are explicit and closed. This opt-in trial does not add a production cache
  outside the shared session ledger.
- Workload: approved snapshot `410b88ad…da2346`, verified before real-cell use;
  begin with one Puget cell already used by existing gates. Compare raw and
  prepared rendering at identical viewport/DPR/palette, source and coverage.
  Synthetic fixtures supplement source updates/corruption/ordering/bounds tests.
- Gate: register `enc-prepared-cell` in `tools/gates.py`, invoked through
  `python tools/gate_runner.py --gate enc-prepared-cell --snapshot <verified>
  --output <dir>`. It must fail when build/reload/content/coverage parity or
  zero source opens on warm drawing fails. Timings are cold preparation,
  disk reload and Cairo drawing separately, not FPS.
- Budget: two implementation/review iterations and two targeted validation
  rounds; at most two real-cell gate attempts, 180 s each. Integration validation
  for merging #81 is separate. No full suite or unaffected baseline remeasurement.
  Delegate implementation/audit to free models and validation to test-runner.
- Stop if exact supported attributes/topology cannot round-trip, GDAL update
  semantics cannot be preserved, the cell exceeds the declared caps, or the
  adapter requires changes to shared weather sampling/routing. Report blockers
  instead of broadening into P2 or weakening correctness criteria.
