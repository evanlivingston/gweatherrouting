# Chart quilting plan (Expedition-style), Qt first

Status: Q1 merged in fork PR #41. Q2 implemented locally on
`feature/chart-quilting-q2`, pending review. Q3/Q4 remain separate slices.

## Goal

Match the Expedition model: one detail chart "owns" each screen area — the
best-scaled chart whose coverage contains it — and everything else draws only
where that coverage ends. The GSHHS/fallback world base stays underneath as
land-fill, so holes fall through to the small-scale world view. A new
`chartQuilting` setting (default **on**) restores today's stack behavior when
off.

## Current state (findings)

1. **ENC is already quilted.** `rendering/encrenderer.py:56-95` sorts ENC cells
   by `chart_scale_rank`, applies the `chart_scale_visible` display-level
   window, and does coverage union/difference with a `coverage_path` clip.
   The plan generalizes this engine; it does not replace it.
2. **Rasters have no scale and lose every selection.** `detailed_chart_at`
   ranks charts via `getattr(c, "_native_scale", 0)`; `GDALRasterChart` never
   defines it, so every raster ranks as scale-0 and never wins best-chart
   queries (`chartcollection.py:159-187`). Adding raster metadata fixes both
   paint and selection.
3. **No raster-to-ENC arbitration.** The ENC quilt runs as a separate layer
   after base charts (`chartcollection.py:253-271`), so an ENC always paints
   over a harbor KAP/BSB regardless of which is better scaled. The core of
   this plan is putting rasters and ENCs into one scale-ranked quilt.
4. **Debug rectangles on raster renders.** `gdalrasterchart.py:310-336`
   strokes a red box around every raster file bbox on every bake; the
   `continue` that would skip it is commented out. Removed on this branch
   (commit below) before any quilt work.
5. **CM93 and seamarks are left out of the quilt in v1.** CM93 selects its
   own 8 scale levels internally (a per-viewport mini-quilt); seamarks are a
   zoom-gated overlay (hidden past 300 m/px). Both keep today's path. A
   follow-up may quilt seamarks via viewport-bbox coverage so they fill holes
   only, letting us delete the "enable any chart → disable seamarks" hack
   (`chartcollection.py:152-155`).
6. **Rendered overlay frames can be stranded by the base-image cache.**
   Confirmed on `dev` (2026-10-16, debug panel): the ENC quilt renders a
   correct, full-viewport surface, but the base image is cached by
   `_base_key() = (size, ratio, palette, wind_style, marine_key)` with **no
   overlay-frame state**. Sequence: view change → base bakes while the
   overlay worker is still rendering (bake = base-only) → frame publishes and
   calls `canvas.queue_draw()` → `_draw_reusable_layer` sees an unchanged key
   and reuses the stale base-only image → `_base_image` is never re-run, so
   the frame is never composited. The ENC only appears once something else
   forces a re-bake (zoom/palette/resize/pan-past-margin) that happens to
   catch a ready frame — which is why the fallback to base map is
   intermittent. Instrumented repro: frame `ready=True` after publish, but
   the next no-view-change repaint re-baked `False`. Q2 must make a published
   frame reach the screen (invalidate the base cache on publish, or composite
   overlays at paint time rather than baking them into the base) — otherwise
   even a correct quilt stays invisible.

## Architecture

### New `rendering/quiltrenderer.py` (absorbs `ENCOverlayRenderer`)

- Input: heterogeneous chart descriptors snapshotted on the GUI thread:
  ENC cells `(path, native_scale, coverage_envelopes)` and raster files
  `(path, per-file bbox, derived native scale)`.
- Worker holds private GDAL handles in an LRU (extend the ENC 8-entry pattern
  to raster datasets); raster tile loading moves out of
  `GDALRasterChart.load_raster`'s ad-hoc threads into the renderer.
- Same algorithm as today's ENC quilt: best `chart_scale_rank` first,
  `chart_scale_visible` culling, coverage difference, clip-and-draw.
- Charts with unknown coverage (GSHHS, country fallback) do not participate;
  they stay in the base layer underneath, producing the Expedition
  world-through-holes look.

### Raster scale metadata (`rendering/gdalrasterchart.py`)

- Prefer the header scale when GDAL/BSB metadata exposes it
  (`SCALE`/`CSCALE`); otherwise derive
  `native_scale ≈ ground_m_per_px × 3779.5` (the same 96 dpi convention as
  `chart_scale_visible`), using the geotransform at file center latitude.
- Per-file bbox doubles as quilt coverage; `metadata` tuples gain a scale
  field with backward-compatible index access.
- Applied at import (`on_register`) and lazily for pre-existing imports.
- **Rasters with no derivable scale render as always-visible, lowest
  priority**: they fill over the world base but never beat an ENC.

### `ChartCollection.do_draw`

Build one descriptor list; draw order becomes: base (GSHHS worker path,
unchanged) → CHS tiles → unified detail quilt (ENC + raster, scale-ranked
together). GTK keeps its synchronous fallbacks but shares the core; only the
Qt UI gets a settings checkbox in this round.

### Settings and Qt UI

- `chartQuilting` bool in `core/settings.py`, default `True`.
- Display-tab checkbox in `qt/settingsdialog.py` (the `smoothZoom` /
  `canadianCharts` pattern) with an Expedition-style tooltip: "Quilt charts —
  fill screen areas from the next scale-out chart where the best chart has no
  coverage. Off leaves gaps empty."
- Off = today's list-order stack, no coverage subtraction (slow machines,
  inspecting RNC edge detail).
- Cache/signature keys (`_base_key`, collection surface signatures) updated to
  include the flag.

### Selection consistency

`detailed_chart_at` / `chart_suggestion_at` need no changes; they improve
automatically once rasters carry `_native_scale`.

## Slices (each a reviewable PR off `dev`)

- **Q1 — Metadata + selection.** Raster native scale + per-file coverage;
  unit tests for scale derivation (synthetic geotransforms) and for
  best-chart selection preferring a harbor KAP at harbor zoom.
- **Q2 — Generic quilt renderer.** Absorb the ENC quilt; add raster
  descriptors and the worker-side raster LRU; unify ENC and raster ordering.
  **Also fix the frame-stranding bug (finding 6):** a published overlay frame
  must reach the screen — invalidate the base cache on publish (or composite
  overlays at paint time) so a static view shows charts once the worker
  finishes. Tests: synthetic ENC-vs-raster scene asserting the best-scaled
  chart wins each region and holes fall through to base; a test that a frame
  published after the bake is composited on the next repaint (no view change);
  cancellation/reuse tests mirror the existing overlay tests.
- **Q3 — Setting.** `chartQuilting` toggle, Qt checkbox, cache keys; test that
  the off-path matches the current stack output.
- **Q4 — Validation and docs.** Bake-time benchmark against the saved startup
  snapshot (`rendering-baseline-2026-10-15.md`) with a raster+ENC scene;
  targeted tests + `black --check` per fork rules; update this doc with any
  divergences discovered during implementation.

## Decisions

- `chartQuilting` defaults **on**.
- Scale-less raster files: always visible, lowest priority (never beat an ENC).
- Red debug bbox rectangles: removed outright (no env-flag replacement).

## Q2 implementation notes

- `QuiltRenderer` generalizes the existing ENC union/difference/clip algorithm.
  `encrenderer` remains a compatibility import. The collection snapshots frozen
  per-file descriptors, moves rasters out of the synchronous base list, and
  paints base → CHS → mixed ENC/raster quilt. CM93 and seamarks retain their
  existing paths. Raster source selection now uses each file's scale window,
  not the layer's most-detailed representative scale. The inspector mirrors
  that per-file visibility rule.
- Equally ranked ENC/raster sources prefer ENC, then path for deterministic
  ties. Unknown-scale rasters remain visible as last-priority hole fillers,
  even behind unknown-scale ENCs. Raster coverage is the existing rectangular
  footprint, not an alpha/no-data mask; raster placement/decoding is unchanged.
  For ENCs without coverage metadata, fallback sources are grouped and painted
  behind existing pixels so they cannot erase higher-priority ENC features.
  The existing layer-level best-at-center query remains an approximation for
  directories containing multiple scales; paint ownership is per file.
- A single overlay worker owns private ENC and raster handles. ENC retention
  remains eight entries; decoded rasters retain at most eight entries and
  128 MiB of Cairo pixel buffers. Oversized rasters render without retention.
  These are retention limits, not peak-memory limits: the existing full-raster
  decode can temporarily exceed them. Cancellation is checked between sources
  and after decoding; a GDAL read already in progress is not interruptible.
  Failed sources do not claim coverage or prevent other sources from rendering.
- **Finding 6 recheck:** on the Q2 base, `TrackMap.queue_draw()` already clears
  the base cache when stationary. Thus the earlier static-cache diagnosis is
  not sufficient by itself to explain every fallback on current `dev`.
  During gestures the notification intentionally preserves the cached image.
  Q2 makes accepted chart/base-worker frame generations explicit in the Qt
  composite key, including prefetched-frame adoption. Tests assert that a late
  published frame reaches the next composite with no view change, both with
  normal invalidation and with the cached image retained during a gesture.
- No scale, resolution or geometry simplification formulas change in Q2.
  Mixed-source ordering and raster chart-level scale culling are intentional
  behavior changes. The off/legacy-stack switch belongs to Q3; performance
  benchmarking and real-chart acceptance remain Q4 (no speedup claim here).

Q2 local validation: 284 targeted tests and 42 subtests passed (mixed quilting,
raster metadata, chart selection/rendering, overlay scheduling/prefetch, CHS,
weather overlays, Qt base charts and preview). Changed Python files pass
`black --check`; `git diff --check` is clean. Tests include a paletted GeoTIFF
through the production raster loader; no real KAP or interactive mixed-library
acceptance has been performed.
