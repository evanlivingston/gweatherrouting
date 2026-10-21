# ENC retained architecture — P2: Continuous interaction with chart preview

**Decision:** port OpenCPN's cached-texture zoom preview into Python/Qt, not embed its C++ engine. User-directed, 2026-10-21. **Runtime status: design only.**

This is the ENC implementation detail linked from the
[canonical roadmap](rendering-performance-plan.md#enc-retained-architecture-port).
It builds on P1 (durable prepared cells) which is implemented and validated.

## Problem

On zoom or forecast-time change, the app explicitly withdraws the retained chart
image (`gribviewport.py:1204-1213`, `chart_withdrawn_on_camera_change` returns
True) and waits for the 500 ms settle window plus a new Cairo composition
(~1-2 s) before showing chart detail again. During that window the chart area is
blank — the user sees nothing where the chart was.

This directly contradicts the user's requirement: *"first draws to be fast and
later redraws effectively instant"* and *"ENC charts disappear whenever zooming
in or out. take a long time to load."*

## Design

Adopt OpenCPN's cached-texture preview pattern (source:
`gui/src/gl_chart_canvas.cpp:4766-4877`, `4913-4954`): keep the previous chart
texture visible during interaction, scale it to cover the new view, and replace
it with full-detail composition when ready.

### What changes

**1. Do not withdraw the chart on zoom/time change**

`gribviewport.py:1827-1834` currently clears the chart source (`QUrl("")`)
whenever zoom or time changes. Instead, keep the previous chart texture
published and mark it as a *preview*: geographically aligned, bounded, clearly
distinguished from current detail.

**2. Scale the preview texture during interaction**

`ChartLayer.qml` gains a `preview` property and a `previewScale` property.
When `preview` is true, the chart Image item is scaled (via QML `scale` or an
enlarged rect) so it covers the new camera view. The previous chart's content is
geographically aligned at its old scale; scaling stretches it to cover more
ground. Labels and symbols scale temporarily — acceptable for a preview, not
acceptable in the final portrayal.

**3. Replace preview with full-detail on settle**

When the 500 ms settle timer fires and a new composition completes,
`_publish_chart` replaces the preview with the full-detail chart, marked as
*current*. The preview is withdrawn at that point.

**4. Pan keeps current behavior**

Pan does not withdraw the chart (already works via `_place_chart`). The retained
composition is shifted to the new camera position. No preview needed for pan.

**5. Labels and symbols**

P2.1 keeps the single-pass Cairo composition: labels scale with the preview.
P2.2 (future) separates labels/symbols into a screen-space pass so they keep
final size during preview.

### What does NOT change

- The chart composition pipeline (Cairo, ENC overlay, isobars) is unchanged.
- The 500 ms settle window is unchanged: composition still happens only after
  the view settles.
- The chart is still composed at the device pixel ratio.
- Source selection, coverage, alpha, palette, layering are unchanged.
- No GPU renderer, no retained QSG geometry, no tessellator — those are P2.2/P3.

### QML contract (ChartLayer.qml)

```qml
Item {
    property alias source: image.source
    property bool ready: image.status === Image.Ready
    property bool preview: false   // new: the shown chart is a scaled preview
    property real previewScale: 1.0 // new: scale factor applied to the preview
    ...
    Image {
        id: image
        transform: Scale { origin: Item.Center; xScale: preview ? previewScale : 1.0; yScale: ... }
        // or: adjust width/height by previewScale and offset x/y
    }
}
```

### Python contract (gribviewport.py)

`chart_withdrawn_on_camera_change` is changed to never withdraw: it always
returns False. Instead, `set_camera` marks the chart as preview on zoom/time
change:

```python
def set_camera(self, camera):
    ...
    if self._chart_model is not None:
        previous_chart_camera = self._chart_camera
        if self._chart_camera != camera:
            self._chart_camera = camera
            self._chart_camera_changed = time.perf_counter()
        # P2: keep the previous chart visible as a preview on zoom/time change.
        # Only withdraw when the source itself changes (manifest, cells).
        self._chart_preview = self._chart_model._published is not None and (
            previous_chart_camera.zoom != camera.zoom
            or previous_chart_camera.stamp() != camera.stamp()
        )
        self._place_chart()
```

The chart model needs to track whether the published composition is preview or
current. `_publish_chart` sets `preview=False` when a new composition arrives.

### Acceptance

- No blank chart area during zoom: the previous chart texture stays visible.
- Preview is geographically aligned at its old scale; scaled to cover new view.
- Full-detail chart replaces preview when composition completes.
- Labels scale temporarily during preview; final labels are crisp after settle.
- Pan behavior unchanged (chart shifted, not scaled).
- 500 ms settle window unchanged.
- No change to chart composition pipeline, source selection, or portrayal.

## Staged delivery

| Slice | Vertical delivery | Exit evidence |
|---|---|---|
| P2.1 - Chart texture preview | Preview visible during zoom, full-detail on settle | Gate: zoom does not blank chart; preview scale bounded; replace-on-settle |
| P2.2 - Retained QSG geometry | Areas/strokes as QSG geometry nodes, camera transform without re-raster | Gate: camera motion causes zero Cairo composition; fallback tested |
| P2.3 - Separate labels/symbols | Screen-space labels keep final size during preview | Gate: label size constant during zoom preview |

P2.1 is the first runtime slice. It does not require P1's persistent cache (it
uses the existing Cairo composition), but P1's cache makes the replacement
composition faster.

## Acceptance and measurement

- Correctness: chart is never blank during zoom; preview covers the view;
  full-detail replaces preview on settle; no stale source reported as current.
- Mechanism: instrument chart withdrawals, preview duration, preview→current
  transitions, composition count during zoom/pan/return.
- Experience: exercise the actual app with chart and weather workers stalled;
  report blank-covered-area events (must be zero during zoom) and preview
  duration.
- Performance: retain canonical warm refresh-budget and p95 input-to-present
  <50 ms targets. Preview duration is not FPS; it is a mechanism measurement.

## Current bounded contract

P2.1 scope: QML preview scaling + Python preview tracking. Two free-model
read-only reviews; at most two documentation-validation rounds; zero native
runs, source changes outside gribviewport.py/ChartLayer.qml, baseline
measurements, commits, pushes, PRs or merges. Stop rather than broaden into
P2.2 provisionally.

**P2.1 decision:** continue to implementation, not provisionally to P2.2.
Before P2.1 code, record its exact QML/Python changes, fixture, gate command,
attempt/time budget and stop condition. At P2.1 exit decide whether the
remaining bottleneck supports continuing to retained geometry.