# ENC retained architecture — P2.2: Retained QSG geometry

**Decision:** keep chart geometry as resident QSG geometry nodes so camera moves
don't re-rasterize through Cairo. User-directed, 2026-10-21. **Runtime status:
P2.2.1 and P2.2.2 implemented; the chart layer's own display defect found and
fixed; P2.2.3 not started.**

Builds on P1 (durable prepared cells, validated) and P2.1 (chart texture
preview, validated). P2.2 is the step that removes the Cairo composition cost
from camera motion.

## Problem

P2.1 keeps the chart visible during zoom, but the replacement composition still
goes through Cairo: feature collection, `SimplifyPreserveTopology`, full-frame
rasterization, texture upload. That's ~1-2 s. P2.2 removes it: camera pans and
zooms update QSG node geometry/transforms; no feature collection, no Cairo,
no texture upload.

## Design

### What changes

**1. Prepared cells → QSG geometry**

P1's `PreparedCellStore` rebuilds worker-local OGR Memory layers from the
artifact. P2.2 reads those layers and builds QSG geometry nodes:
- Areas (filled polygons with holes) → triangle strips, even-odd fill
- Lines (strokes with joins/caps) → expanded line geometry
- Points/symbols → quads or text items
- Soundings → text items at fixed glyph size

Geometry is kept in worker-local memory; only the QSG node's vertex buffers
cross to the scene-graph thread.

**2. Camera transform, not re-rasterization**

Pan/zoom update the QSG node's transform (or recompute vertex positions from
the Mercator projection). No `SimplifyPreserveTopology`, no `cairo.Context`,
no `ImageSurface`. The previous frame's geometry is reused; only the
projection changes.

**3. Coverage clipping**

Each cell's coverage region is a clip path (QSG `clip` or scissor). Cells draw
only where their coverage applies, preserving the quilt ownership rules:
best-scale cell first, others fill where coverage ends.

**4. Labels/symbols (P2.3, not P2.2)**

P2.2 keeps labels as part of the geometry pass (Cairo-style text rendering into
the same QSG node or a separate text QSG item). P2.3 separates them into a
screen-space pass with fixed glyph sizes.

### QML/Scene contract

`ViewportChartScene.qml` currently loads `ViewportChartScene.qml` with a
`ChartLayer` item showing one `Image`. P2.2 adds a second item type:
`ChartGeometry` (`QSGGeometryNode`) that renders prepared cell geometry.
The scene holds both:
- `ImageItem` — the preview (P2.1) or full-detail Cairo texture
- `ChartGeometryItem` — resident geometry, shown when ready, hidden during
  preview-only zoom

On camera motion:
1. Preview texture scales immediately (P2.1)
2. QSG geometry nodes update their transform/vertices for the new camera
3. When geometry is ready, it replaces the preview image

### Python contract (gribviewport.py)

`ViewportChartLayer` gains a `prepare_geometry(camera)` worker method that:
- Loads prepared cells from `PreparedCellStore` (P1, no source reopen)
- Builds QSG-ready geometry (vertex buffers, indices, clip paths)
- Publishes to the GUI thread via generation-keyed payload

The widget's `set_camera` updates the geometry transform immediately, without
waiting for the settle timer.

### Staged delivery

| Slice | Vertical delivery | Exit evidence |
|---|---|---|
| P2.2.1 - Area fills | Polygon areas with holes as QSG triangle strips, even-odd fill, coverage clip | Gate: areas render correctly at multiple scales; holes respected; camera pan causes zero Cairo composition |
| P2.2.2 - Strokes | Line strokes with joins/caps, boundary lines | Gate: stroke rendering matches Cairo reference |
| P2.2.3 - Points/soundings | Point symbols and sounding text items | Gate: sounding values match; symbol placement correct |

P2.2.1 is the first runtime slice.

## Acceptance and measurement

- Correctness: area fill matches Cairo reference (byte-identical or within
  half-pixel tolerance); holes respected; coverage ownership preserved
- Mechanism: instrument feature collections, simplifications, Cairo
  compositions, texture uploads during camera motion — all should be zero for
  resident cells
- Experience: pan/zoom causes no chart blanking; geometry updates follow
  camera immediately
- Performance: camera motion composition time → near-zero (QSG node update,
  not Cairo rasterization). Not FPS; mechanism timing only.

## Current bounded contract

P2.2.1 scope: area fills as QSG geometry. Two free-model read-only reviews;
at most two documentation-validation rounds; zero native runs, source changes
outside gribviewport.py/gpuscene.py/new QSG geometry module, baseline
measurements, commits, pushes, PRs or merges. Stop rather than broaden into
P2.2.2 provisionally.

**P2.2.1 decision:** continue to implementation, not provisionally to P2.2.2.
Before code, record exact QSG geometry format, clip approach, fixture, gate
command, attempt/time budget and stop condition.
## Runtime status — chart layer display defect found and fixed

### What was wrong

The chart never reached the screen, and the viewport reported "Chart loading…"
for ever. Three separate faults, each masking the next:

1. `ViewportChartScene.qml` declared a bare `Item` where the `ChartLayer`
   component belongs. The viewport sets the published image (`source`), the
   chart's own loading state (`chartLoading`) and the image placement on that
   object; a bare `Item` has none of them, so `setProperty` failed silently
   and the composition went nowhere.
2. The scene carried its own status rectangle bound to `root.chartLoading` —
   a property the scene root never declared. The binding was `undefined`, so
   the rectangle stayed visible for ever: the "Chart loading…" badge was not a
   state at all, it was a permanent decoration.
3. `ChartLayer.qml` could not be instantiated even where it was used: its
   preview transform was an object literal inside a conditional expression
   (not valid QML). The working copy's replacement used `enabled`, which
   `Scale` does not have. Commit `6c135ae` had worked around the resulting
   "Type ChartLayer unavailable" by downgrading the scene to a bare `Item`,
   which is what turned the compile error into a permanently blank chart.

Two further faults sat behind those: the viewport set `chartPreview` and
`chartPreviewScale`, but the component's contract is `preview` and
`previewScale`, so the preview never activated; and `ViewportChartLayer.is_busy`
ignored a queued request, so the viewport could cancel a composition in the
window between queueing it and the worker picking it up.

### The fix

- `ChartLayer.qml`: the preview transform scales the image while a preview is
  shown and is the identity otherwise. No object literal in an expression, no
  property `Scale` does not have.
- `ViewportChartScene.qml`: the chart and environment layers are `ChartLayer`
  instances again; the scene's duplicate status rectangle is gone, so the
  chart's loading state belongs to the chart layer and only to it.
- `gribviewport.py`: the preview is set by the names the component has.
- `gribviewportchart.py`: a queued request counts as busy.

### Evidence

- Gate `enc-viewport-functional`: **PASS 13/13** plus the functional check
  "all requested chart views completed", 49.3 s process wall time, snapshot
  `410b88ad…da2346` (approved), `python tools/gate_runner.py --gate
  enc-viewport-functional --snapshot <verified> --output <dir>`. Endpoint is
  the recorded render-callback proxy: not presentation FPS, not input-to-
  present, not scanout. Raw artifacts in `enc-viewport-fix-3/` under the
  approved OpenCode temporary directory.
- The stale-chart check was **revised**, not weakened: P2.1 changed the
  behaviour on purpose (the retained composition stays on screen during a
  zoom or a forecast-time change, scaled and marked as a preview), so the
  check now accepts withdrawal *or* a marked preview, and additionally
  requires the settled chart to be current rather than a preview. The
  recorded behaviour distinguishes the two paths: `stale_withdrawn=False`,
  `stale_shown_as_preview=True`, `settled_source=image://chart/10`,
  `settled_preview=False`, superseded generations 8 and 9 rejected.
- Targeted validation: 141 passed, 1 pre-existing failure
  (`test_the_application_never_imports_the_viewport`, unrelated to this work),
  changed-file Black and flake8 clean, `git diff --check` clean.
- Direct readback in a headless viewport: the chart layer holds
  `source=image://chart/1`, `ready=True`, `chartLoading=False`,
  `chart state=ready`; the rendered frame contains the chart (land, water and
  coastal detail) and the badge rectangle is gone.

### Decision

**Revise and continue.** The chart is displayed and the ENC viewport gate
passes. The remaining ladder step is P2.2.3 (points and soundings as
retained geometry); it is not started. The P1 audit findings B4 and M1–M10
and issue #84 (invisible world base map) are still open, and #84 still
blocks any default-path switch.
