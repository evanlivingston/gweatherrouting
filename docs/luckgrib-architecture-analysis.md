# LuckGrib implementation analysis — architectural techniques (hypotheses)

**Status:** architecture hypothesis source, not implementation source, not a benchmark.
**Scope:** read-only static analysis of a locally installed LuckGrib build and its
reconstructed pseudocode. Findings describe *techniques* that may be independently
implemented. No recovered code is copied, and no timing claim about LuckGrib is made.

**Evidence boundary (per #51):** decompiler output is evidence with limitations, not
implementation source. Nothing in this document establishes that LuckGrib is faster;
it establishes only *how* LuckGrib is structured, which we may choose to mirror.

## Artifacts inspected

| Role | Path | Identity |
| --- | --- | --- |
| Application binary | `/Applications/LuckGrib.app/Contents/MacOS/LuckGrib` | Mach-O universal x86_64/arm64, SHA-256 `9c47e09b511f737cd222f85c3a35fb3b77d03eb8c6c3856b5e32cc4955c1578a` |
| Render pseudocode | `/private/tmp/LuckGrib-render-pseudocode.txt` | 2,433 lines, reconstructed pseudocode |
| Field pseudocode | `/private/tmp/LuckGrib-field-pseudocode.txt` | 1,759 lines, reconstructed pseudocode |
| Ghidra projects | `/private/tmp/luckgrib-ghidra/`, `/private/tmp/luckgrib-fields-fast/`, `/private/tmp/luckgrib-grib-ghidra/` | **Empty** — 0-byte `.gpr` files, empty `.rep` idata. No decompiled functions recovered. |

**Coverage limitation:** all findings below come from the two pseudocode files and
binary strings. The Ghidra projects yielded no functions, so no symbol-level,
address-qualified, or call-graph verification was possible. Findings are labeled
accordingly. Absence of evidence is not evidence of absence.

## Techniques observed

### T1 — Async, chunked, task-group data generation
- **Evidence:** artifact lead + reconstructed pseudocode. `GridFieldAlg_generate` creates a
  `cwmThreadTaskGroup` and passes a `GribGenerateContext` / `GribAlgorithmData`.
  `ScalarImageFieldAlg::generate` submits chunked work via `cwmThreadTaskGroup::addWork`.
- **No** synchronous full-file `fread` observed.
- **No** GDAL or eccodes import observed; strings reference a proprietary `cwmGrib` framework.

### T2 — Tiled/pyramidal field storage
- **Evidence:** reconstructed pseudocode. `ScalarImageFieldData` (derived from
  `GribAlgorithmData`) is the core structure; `ScalarImage_generateValueTextureRows` writes
  `ushort` values into `param_4 + 0x338` in row form; `ScalarImage_generateTextureGeometry`
  maps field geometry to texture coordinates.
- Values are 16-bit. Whether a full mip/pyramid hierarchy exists was **not** established.

### T3 — Retained Metal scene graph with dirty flags
- **Evidence:** reconstructed pseudocode + binary strings. `GrDrawableTrianglesImpl_metal::drawItem`
  binds fragment textures at indices `0x11`/`0x12` via `setFragmentTexture_atIndex_`.
  `GrDrawableScene` holds `preDrawLevels`/`postDrawLevels` with per-level dirty flags.
  `GrProgram_metal::useColorTable` / `GrProgramAspects::isColorIndex` show retained shader
  and color-table state. `GrDrawableScene::drawIfModified` only regenerates when dirty.

### T4 — Producer/consumer thread pool; main thread never blocks on generation
- **Evidence:** reconstructed pseudocode. `GrDrawableScene::generateFrame` submits a
  `cwmThreadTaskGroup` to `cwmThreadPool::submit`. `cwmThreadTaskGroup::addCompletion`
  handles completion. No mutex/lock primitives were observed in the excerpts.

### T5 — Animation-state time switching
- **Evidence:** reconstructed pseudocode. `GrDrawableScene::submitGenerateTasks` checks a
  frame-count parameter and calls `GrSceneAnimation::setTime`, measuring duration via
  `addGenerateDuration`.
- **No** evidence of pre-decoded time stacks. Fast switching appears to come from
  retained textures plus parallel chunk generation, not from precomputing all timesteps.

### T6 — CPU-side bilinear interpolation
- **Evidence:** reconstructed pseudocode. `ScalarImage_generateValueTextureRows` computes a
  linear scaling with clamping (`0xfffe < iVar12` → `0xffff`). A bitmask (`param_9 & 1`)
  selects 1- vs 2-channel output, suggesting density-dependent sampling.
- **No** bicubic kernel observed. LuckGrib does **not** appear to do spatial interpolation
  on the GPU. This corrects a common assumption: mirroring LuckGrib does *not* mean
  adopting GPU spatial interpolation.

### T7 — Manual, scoped memory management
- **Evidence:** reconstructed pseudocode. `__Znwm` / `__ZdlPv` appear paired with chunk-task
  object creation and destruction.
- **No** LRU, eviction, or unified residency budget was observed in the excerpts.

### T8 — Framework-managed projection
- **Evidence:** artifact lead (binary strings). `cwmBaseMap_visibleProjRegionModified`,
  `setBaseMapLandHeightTexture`. No `mercator`/`utm`/`lambert` strings found.
- Projection handling lives in a framework that was not decompiled, so its implementation
  is **unknown**.

## What LuckGrib does *not* appear to do (within inspected coverage)
- GPU spatial interpolation / compute shaders (T6).
- Pre-decoded time stacks (T5).
- Unified GPU residency budgeting with eviction (T7).
- Streaming or partial decode of GRIB files (T1 is chunked *generation*, not partial read).

## Relevance to our target architecture

The gap analysis in [`grib-architecture-analysis.md`](grib-architecture-analysis.md) lists
seven structural differences. Mapping them against these techniques:

| Our gap | LuckGrib technique that would close it | Adoption judgment |
| --- | --- | --- |
| CPU spatial interpolation | T6 — CPU bilinear | **Do not mirror.** This is a known deficiency on both sides; a GPU-sampling target is still defensible, but it is *our* improvement, not a LuckGrib parity claim. |
| Serialized pipeline | T4 — producer/consumer pool | Mirror the shape; implement independently. |
| No GPU residency | T3 + T2 — retained textures, tiled storage | Mirror the shape; add the eviction budget T7 lacks. |
| Time-scrub restage | T5 — animation-state switching | Mirror the shape; T5 is the closest analogue to our M-scrub milestone. |
| CPU decode | T1 — task-group chunked generation | Partial: mirrors async structure, not GPU decode. |
| Cache-key mismatch | — | No LuckGrib analogue identified; treat as our own work. |

**Honest conclusion:** LuckGrib's smoothness is explained by *architecture shape*
(async task groups, retained resources, dirty-flag scene graph, main-thread isolation) —
not by exotic GPU compute. Roughly 4 of our 7 gaps have a direct LuckGrib analogue;
the remaining 3 (GPU spatial interpolation, unified GPU budget, cache coherence) are
places where we can exceed the reference rather than merely match it.

## Reproduction

Analysis was static and read-only. Re-running it requires the pseudocode exports and the
application binary above. The Ghidra projects are empty; populating them is a separate,
unbounded task that this analysis does not depend on.
