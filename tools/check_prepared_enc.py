#!/usr/bin/env python3
"""P1 acceptance gate for the durable prepared ENC cell store.

The gate proves that one real Puget Sound cell survives the round trip
through :mod:`gweatherrouting.rendering.encprepared` unchanged.  The same
cell is read twice -- once raw, straight from GDAL, and once through the
store's own artifact -- and the two are compared on the same viewport, with
the same palette, at the same device pixel ratio.

What is compared is content, not two different scenes:

* the compilation scale and the **exact** coverage geometry;
* the layer, feature, geometry, required-field and sounding-Z inventory;
* the drawn pixels, byte for byte, and the ink they carry.

The warm path is proved in a **fresh process**.  The parent builds the
artifact in a disposable sandbox; a child process then reloads it from
disk with the source opener disabled and counted.  The child must not open
the source at all, must be a different process, and must reproduce the
parent's pixel, inventory and coverage hashes.

The fixture is the approved snapshot, verified before any real cell is
touched.  The gate never writes outside its own sandbox: the snapshot, the
manifest and the user's settings are read, never changed.

The spans reported here are wall-clock costs of a mechanism -- cold
preparation, disk reload and cairo drawing, measured separately.  They are
never presentation FPS and never input-to-present, and they are never
thresholded by this tool.

The gate is a CPU content and mechanism check: no window is opened, no
scene graph is created and no presentation is measured.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: The approved fixture.  Nothing else is accepted.
EXPECTED_SNAPSHOT_SHA = (
    "410b88ada69dd5a22fc18765a0f26b016e9d15df22bcfeaa954b8b4b62da2346"
)

#: The cell the gate builds, reloads and compares.
TARGET_CELL = "US5SEAMG"

#: The fallback palette, used when the fixture carries none of its own.
DEFAULT_PALETTE = "cm93"

#: The device pixel ratio of the composition.
FACTOR = 2

#: The camera: the same Puget Sound view the existing ENC gates use.
TARGET_LAT = 48.1125
TARGET_LON = -122.7375
TARGET_ZOOM = 14.236
TARGET_WIDTH = 710
TARGET_HEIGHT = 743
TARGET_DENSITY = "very_dense"

#: The trial's declared caps (docs/enc-retained-architecture.md, P1).
CELL_BYTE_LIMIT = 64 * 1024**2
DISK_BYTE_LIMIT = 256 * 1024**2

#: A channel value beyond this counts as ink.
INK_EPSILON = 0.5

#: The fresh-process reload may not take longer than this.
CHILD_TIMEOUT_S = 60

#: Every check the gate reports.  A missing check is a failure.
CHECK_NAMES = (
    "fixture_verified",
    "cell_selected",
    "raw_reference_drawn",
    "prepared_chart_built",
    "cold_source_opened_once",
    "native_scale_preserved",
    "coverage_preserved",
    "inventory_preserved",
    "pixels_match",
    "render_nonblank",
    "cache_disk_accounted",
    "warm_child_succeeded",
    "warm_child_fresh_process",
    "warm_zero_source_opens",
    "warm_output_matches",
)

#: The checks the warm child reports about itself.
CHILD_CHECK_NAMES = (
    "prepared_chart_loaded",
    "render_nonblank",
    "zero_source_opens",
    "pixel_matches_expected",
    "inventory_matches_expected",
    "coverage_matches_expected",
)

#: The checks the parent reports about the warm child.
WARM_CHECK_NAMES = (
    "warm_child_succeeded",
    "warm_child_fresh_process",
    "warm_zero_source_opens",
    "warm_output_matches",
)

NOTES = (
    "Wall-clock costs of a mechanism: cold preparation, disk reload and "
    "cairo drawing. Never presentation FPS, never input-to-present.",
    "The fixture is the approved snapshot; the sandbox is disposable and the "
    "user's settings, manifest and charts are read, never written.",
    "The warm child reloads the artifact from disk with the source opener "
    "disabled; hashing the source bytes for its identity is not a source "
    "open, and no source decoding happens in the child.",
    "CPU content and mechanism only: no window, no scene graph, no "
    "presentation measurement.",
)


class GateError(Exception):
    """A condition the gate refuses to pass."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _is_digest(value):
    """Whether the value is a sha256 hex digest."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)


def _is_count(value):
    """Whether the value is a plain, non-negative integer."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number(value):
    """Whether the value is a plain number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load_module(name, path):
    """Load a tool module by path, registered in ``sys.modules``.

    The registration is not cosmetic: a module that declares dataclasses
    resolves its annotations through ``sys.modules``, and a module that is
    not registered there fails while its own class body runs.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------
def new_report():
    """A report with every check present and false."""
    return {
        "gate": "enc-prepared-cell",
        "tool": "tools/check_prepared_enc.py",
        "checks": {name: False for name in CHECK_NAMES},
        "snapshot_sha256": "",
        "cell": {},
        "identity": {},
        "render": {},
        "timings_s": {},
        "bytes": {},
        "child": {},
        "differences": {},
        "errors": [],
        "problems": [],
        "complete": False,
        "verdict": "fail",
        "process": {"pid": os.getpid()},
        "notes": list(NOTES),
    }


def write_report(output, report):
    """Write the report where the runner expects it."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def evaluate_report(report):  # noqa: C901 - one fail-closed verdict
    """The gate's own verdict, read from the report it is given.

    The verdict is fail-closed.  Every check has to be present and exactly
    true -- a truthy string is not a passed check -- and the evidence those
    checks stand on has to be in the report as well: the pixel, coverage
    and inventory digests, the ink, the accounted bytes, the child's own
    checks, the child's source-open count and the child's process id.  A
    report that carries the flags but not the evidence does not pass.
    """
    problems = []
    checks = report.get("checks")
    if not isinstance(checks, dict):
        return False, ["the report carries no checks"]
    for name in CHECK_NAMES:
        if name not in checks:
            problems.append("missing check: %s" % name)
        elif checks[name] is not True:
            problems.append("failed check: %s" % name)
    unknown = sorted(set(checks) - set(CHECK_NAMES))
    if unknown:
        problems.append("unknown checks: %s" % ", ".join(unknown))

    identity = report.get("identity") or {}
    cold_opens = identity.get("source_opens_during_preparation")
    if not _is_count(cold_opens) or cold_opens != 1:
        problems.append("cold preparation did not open the source exactly once")
    raw = identity.get("raw") or {}
    prepared = identity.get("prepared") or {}

    render = report.get("render") or {}
    if not _is_count(render.get("ink_pixels")) or render["ink_pixels"] <= 0:
        problems.append("the render is blank")
    if not _is_digest(render.get("pixel_sha256")):
        problems.append("the render carries no pixel digest")

    for name, block in (("raw", raw), ("prepared", prepared)):
        for key in ("pixel_sha256", "coverage_sha256", "inventory_sha256"):
            if not _is_digest(block.get(key)):
                problems.append("the %s rendering carries no %s" % (name, key))
    for key in ("pixel_sha256", "coverage_sha256", "inventory_sha256"):
        if raw.get(key) != prepared.get(key):
            problems.append("the raw and prepared %s differ" % key)
    if (
        not _is_count(raw.get("coverage_geometry_count"))
        or not raw["coverage_geometry_count"] > 0
    ):
        problems.append("the cell has no coverage")
    if not _is_count(raw.get("feature_count")) or not raw["feature_count"] > 0:
        problems.append("the cell has no features")
    native_scale = prepared.get("native_scale")
    if not _is_number(native_scale) or native_scale <= 0:
        problems.append("the prepared chart has no compilation scale")

    accounted = report.get("bytes") or {}
    disk = accounted.get("cache_disk_bytes")
    if not _is_count(disk) or not 0 < disk <= DISK_BYTE_LIMIT:
        problems.append("the cache disk use is not accounted: %r" % (disk,))
    payload = accounted.get("cell_payload_bytes")
    if not _is_count(payload) or not 0 < payload <= CELL_BYTE_LIMIT:
        problems.append("the cell payload is not accounted: %r" % (payload,))

    problems.extend(_child_problems(report, prepared))

    for error in report.get("errors") or []:
        problems.append("error: %s" % error)
    return (not problems), problems


def _child_problems(report, prepared):  # noqa: C901 - one child's whole story
    """What the report says about the fresh-process reload."""
    problems = []
    child = report.get("child") or {}
    if child.get("timed_out"):
        problems.append("the warm child timed out")
    elif "returncode" in child and child.get("returncode") != 0:
        problems.append("the warm child exited %r" % (child.get("returncode"),))
    elif "returncode" not in child:
        problems.append("the warm child did not run")
    if child.get("error"):
        problems.append("the warm child: %s" % child["error"])
    child_report = child.get("report")
    if not isinstance(child_report, dict):
        problems.append("the warm child wrote no report")
        return problems
    child_checks = child_report.get("checks")
    if not isinstance(child_checks, dict):
        problems.append("the warm child reported no checks")
    else:
        for name in CHILD_CHECK_NAMES:
            if name not in child_checks:
                problems.append("the warm child is missing check: %s" % name)
            elif child_checks[name] is not True:
                problems.append("the warm child failed check: %s" % name)
    if child_report.get("complete") is not True:
        problems.append("the warm child did not complete")
    count = child_report.get("source_open_count")
    if not _is_count(count):
        problems.append("the warm child reported no source-open count")
    elif count != 0:
        problems.append("the warm child opened the source %s time(s)" % count)
    pid = child_report.get("pid")
    parent = (report.get("process") or {}).get("pid")
    if not _is_count(pid) or not pid > 0:
        problems.append("the warm child reported no process id")
    elif _is_count(parent) and pid == parent:
        problems.append("the warm child ran in the parent's process")
    for key in ("pixel_sha256", "coverage_sha256", "inventory_sha256"):
        if child_report.get(key) != prepared.get(key):
            problems.append("the warm child's %s differs" % key)
    if child_report.get("native_scale") != prepared.get("native_scale"):
        problems.append("the warm child's native scale differs")
    if child_report.get("ink_pixels") != prepared.get("ink_pixels"):
        problems.append("the warm child's ink differs")
    for error in child_report.get("errors") or []:
        problems.append("the warm child reported: %s" % error)
    return problems


def apply_observations(report, observations):
    """Set the checks the observations support.

    Every check is derived from a value the engine actually measured: a
    missing observation is a failed check, never a passed one.
    """
    raw = observations.get("raw") or {}
    prepared = observations.get("prepared") or {}
    cell = observations.get("cell") or {}
    checks = report["checks"]
    checks["cell_selected"] = cell.get("found") is True
    checks["raw_reference_drawn"] = _has_ink(raw) and _is_digest(
        raw.get("pixel_sha256")
    )
    checks["prepared_chart_built"] = prepared.get("built") is True
    checks["cold_source_opened_once"] = (
        _is_count(observations.get("source_opens"))
        and observations["source_opens"] == 1
    )
    raw_scale = raw.get("native_scale")
    checks["native_scale_preserved"] = (
        _is_number(raw_scale)
        and raw_scale > 0
        and raw_scale == prepared.get("native_scale")
    )
    checks["coverage_preserved"] = (
        _is_digest(raw.get("coverage_sha256"))
        and raw.get("coverage_sha256") == prepared.get("coverage_sha256")
        and _is_count(raw.get("coverage_geometry_count"))
        and raw["coverage_geometry_count"] > 0
    )
    checks["inventory_preserved"] = (
        _is_digest(raw.get("inventory_sha256"))
        and raw.get("inventory_sha256") == prepared.get("inventory_sha256")
        and _is_count(raw.get("feature_count"))
        and raw["feature_count"] > 0
    )
    checks["pixels_match"] = _is_digest(raw.get("pixel_sha256")) and raw.get(
        "pixel_sha256"
    ) == prepared.get("pixel_sha256")
    checks["render_nonblank"] = _has_ink(raw) and _has_ink(prepared)
    disk = observations.get("cache_disk_bytes")
    payload = observations.get("cell_payload_bytes")
    checks["cache_disk_accounted"] = (
        _is_count(disk)
        and 0 < disk <= DISK_BYTE_LIMIT
        and _is_count(payload)
        and 0 < payload <= CELL_BYTE_LIMIT
    )
    report["render"] = {
        "ink_pixels": prepared.get("ink_pixels", 0),
        "raw_ink_pixels": raw.get("ink_pixels", 0),
        "pixel_sha256": prepared.get("pixel_sha256", ""),
        "raw_pixel_sha256": raw.get("pixel_sha256", ""),
        "surface": prepared.get("surface", {}),
    }
    report["bytes"] = {
        "cache_disk_bytes": disk,
        "cache_disk_limit_bytes": DISK_BYTE_LIMIT,
        "cell_payload_bytes": payload,
        "cell_payload_limit_bytes": CELL_BYTE_LIMIT,
    }
    return checks


def _has_ink(block):
    """Whether a rendering carries ink."""
    ink = (block or {}).get("ink_pixels")
    return _is_count(ink) and ink > 0


def compare_child(child, expected, parent_pid):  # noqa: C901 - one child's verdict
    """The parent's verdict on the fresh-process reload.

    ``child`` is the whole child outcome: the process' exit status, whether
    it timed out and the report it wrote.  The child has to have succeeded,
    in another process, without opening the source, and it has to have
    reproduced the parent's hashes.  A child that says it completed is not
    believed on its word: its own checks have to be there and true, and
    the numbers it reports have to agree with them.
    """
    checks = {name: False for name in WARM_CHECK_NAMES}
    if not isinstance(child, dict):
        return checks
    report = child.get("report")
    if not isinstance(report, dict):
        return checks
    if child.get("timed_out"):
        return checks
    if child.get("returncode") != 0:
        return checks
    if child.get("error"):
        return checks
    if report.get("errors"):
        return checks
    child_checks = report.get("checks")
    if not isinstance(child_checks, dict):
        return checks
    if any(child_checks.get(name) is not True for name in CHILD_CHECK_NAMES):
        return checks
    if report.get("complete") is not True:
        return checks
    checks["warm_child_succeeded"] = True
    pid = report.get("pid")
    if _is_count(pid) and pid > 0 and pid != int(parent_pid):
        checks["warm_child_fresh_process"] = True
    count = report.get("source_open_count")
    if _is_count(count) and count == 0:
        checks["warm_zero_source_opens"] = True
    if isinstance(expected, dict) and expected:
        scale = expected.get("native_scale")
        if (
            report.get("pixel_sha256") == expected.get("pixel_sha256")
            and report.get("ink_pixels") == expected.get("ink_pixels")
            and report.get("inventory_sha256") == expected.get("inventory_sha256")
            and report.get("coverage_sha256") == expected.get("coverage_sha256")
            and _is_number(scale)
            and scale > 0
            and report.get("native_scale") == scale
        ):
            checks["warm_output_matches"] = True
    return checks


def evaluate_child_report(report):
    """The child's own verdict: every check present and true, no error."""
    problems = []
    checks = report.get("checks")
    if not isinstance(checks, dict):
        return False, ["the child report carries no checks"]
    for name in CHILD_CHECK_NAMES:
        if name not in checks:
            problems.append("missing check: %s" % name)
        elif checks[name] is not True:
            problems.append("failed check: %s" % name)
    for error in report.get("errors") or []:
        problems.append("error: %s" % error)
    return (not problems), problems


# ---------------------------------------------------------------------------
# the fixture harness
# ---------------------------------------------------------------------------
_FIXTURE = None


def _fixture():
    """The startup rendering harness: the snapshot's verifier and sandbox."""
    global _FIXTURE
    if _FIXTURE is None:
        _FIXTURE = _load_module(
            "startup_fixture", ROOT / "tools" / "benchmark_startup_rendering.py"
        )
    return _FIXTURE


# ---------------------------------------------------------------------------
# the drawing helpers (the Cairo adapter)
# ---------------------------------------------------------------------------
def build_viewport(camera_spec, factor):
    """The gate's own viewport: the application's camera, margin and scale.

    The same construction the existing ENC gate uses: the viewport's
    camera, the application's chart camera at the device pixel ratio and
    the application's own margin around the composition.  ``density`` and
    ``over_land`` are the camera's own fields.
    """
    from datetime import datetime

    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.qt.gribviewport import ViewportCamera
    from gweatherrouting.qt.gribviewportchart import ChartCamera
    from gweatherrouting.rendering.overlayrenderer import MARGIN

    camera = ViewportCamera(
        center_lat=float(camera_spec["center_lat"]),
        center_lon=float(camera_spec["center_lon"]),
        zoom=float(camera_spec["zoom"]),
        width=int(camera_spec["width"]),
        height=int(camera_spec["height"]),
        time=datetime.fromisoformat(str(camera_spec["time"])),
        density=str(camera_spec["density"]),
        over_land=bool(camera_spec["over_land"]),
    )
    canvas = ChartCamera(camera, factor)
    viewport = MapViewport(canvas, MARGIN, lambda: None)
    viewport.render_cancelled = lambda: False
    return viewport


def raw_chart(source, palette):
    """The cell as the application's own worker reads it.

    The reference's drawer is created here, explicitly, exactly as the
    application's ENC worker creates it.  That is the reference's business:
    the prepared chart has to bring its own drawer, or the gate refuses it.
    """
    from osgeo import ogr

    from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
    from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

    dataset = ogr.Open(source)
    if dataset is None:
        raise GateError("cannot open %s" % source)
    chart = GDALVectorChart.__new__(GDALVectorChart)
    chart.path = str(source)
    chart.ctype = "vector"
    chart.enabled = True
    chart.metadata = None
    chart.settings_manager = None
    chart.vector_file = dataset
    chart._coverage = []
    chart._native_scale = 0
    chart._load_enc_metadata()
    chart._coverage_envelopes = tuple(
        geometry.GetEnvelope() for geometry in chart._coverage
    )
    drawer = S57ChartDrawer.__new__(S57ChartDrawer)
    drawer.palette = palette
    drawer.symbolProvider = None
    chart.drawer = drawer
    return chart


def require_drawer(chart, palette):
    """The prepared chart's own drawer, or a refusal.

    A prepared chart has to arrive ready to draw.  The gate does not
    supply a drawer for it: a missing or unusable adapter is a defect of
    the store, and hiding it behind a substituted drawer would compare a
    drawing the store never made.
    """
    drawer = getattr(chart, "drawer", None)
    if drawer is None:
        raise GateError("the prepared chart carries no drawer")
    if not callable(getattr(drawer, "draw", None)):
        raise GateError("the prepared chart's drawer cannot draw")
    if not callable(getattr(chart, "do_draw", None)):
        raise GateError("the prepared chart cannot be drawn")
    bound = getattr(drawer, "palette", None)
    if bound != palette:
        raise GateError(
            "the prepared chart's drawer carries the palette %r, not %r"
            % (bound, palette)
        )
    return drawer, "prepared"


def draw_chart(chart, viewport, palette, factor):
    """Draw one chart, clipped by its own exact coverage.

    The raw chart and the prepared chart go through this one helper, so
    the only difference between the two renderings is the data.  The
    application's own rules apply to both: the compilation scale decides
    whether the cell is drawn at all, the clip is the cell's own exact
    coverage and the drawing itself is the chart's own ``do_draw``.
    """
    import cairo
    from osgeo import ogr

    from gweatherrouting.core.enccharts import chart_scale_visible
    from gweatherrouting.rendering.chartlayer import ChartLayer
    from gweatherrouting.rendering.encrenderer import coverage_path

    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32, viewport.width * factor, viewport.height * factor
    )
    surface.set_device_scale(factor, factor)
    cr = cairo.Context(surface)
    native_scale = getattr(chart, "_native_scale", 0)
    if not chart_scale_visible(native_scale, viewport.get_scale()):
        surface.flush()
        return surface
    p1, p2 = viewport.get_bbox()
    bounding = ogr.CreateGeometryFromWkt(
        ChartLayer.get_bounding_wkt_of_coords(*p1.get_degrees(), *p2.get_degrees())
    )
    region = None
    for geometry in getattr(chart, "_coverage", ()) or ():
        region = geometry.Clone() if region is None else region.Union(geometry)
    if region is not None:
        region = region.Intersection(bounding)
    if region is not None and not region.IsEmpty():
        cr.save()
        cr.new_path()
        coverage_path(cr, viewport, region)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.clip()
        chart.do_draw(viewport, cr)
        cr.restore()
    surface.flush()
    return surface


def surface_array(surface):
    """A cairo surface as tightly packed BGRA bytes."""
    import numpy as np

    surface.flush()
    stride = surface.get_stride()
    height = surface.get_height()
    width = surface.get_width()
    data = np.frombuffer(bytes(surface.get_data()), np.uint8)
    return data.reshape(height, stride)[:, : width * 4].reshape(height, width, 4).copy()


def camera_array(surface, factor):
    """The published view: the camera's own rectangle of the composition.

    The viewport is the camera plus the application's margin; the margin
    is not part of the view the camera asked for, so it is cropped away
    before the two renderings are compared.
    """
    from gweatherrouting.rendering.overlayrenderer import MARGIN

    array = surface_array(surface)
    offset = MARGIN * factor
    height = array.shape[0] - 2 * offset
    width = array.shape[1] - 2 * offset
    if height <= 0 or width <= 0:
        return array
    return array[offset : offset + height, offset : offset + width]


def ink_pixels(array, epsilon=INK_EPSILON):
    """The pixels that carry ink."""
    return int((array.astype("int16") > epsilon).any(axis=2).sum())


def pixel_digest(array):
    """The exact bytes of a rendered surface."""
    return hashlib.sha256(array.tobytes()).hexdigest()


# ---------------------------------------------------------------------------
# the content identities
# ---------------------------------------------------------------------------
def required_fields():
    """The fields the application's own ENC code reads."""
    from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

    fields = {
        name: tuple(values) for name, values in S57ChartDrawer.ATTRIBUTE_FIELDS.items()
    }
    fields.setdefault("DSID", ("DSPM_CSCL",))
    fields.setdefault("M_COVR", ("CATCOV",))
    return fields


def coverage_identity(geometries):
    """A cell's exact coverage: each geometry's own bytes and its shape."""
    digest = hashlib.sha256()
    rows = []
    for geometry in geometries or ():
        wkb = bytes(geometry.ExportToWkb())
        digest.update(len(wkb).to_bytes(8, "big"))
        digest.update(wkb)
        rows.append(
            {
                "wkb_sha256": hashlib.sha256(wkb).hexdigest(),
                "wkb_bytes": len(wkb),
                "envelope": [round(value, 9) for value in geometry.GetEnvelope()],
                "area": round(geometry.GetArea(), 6),
            }
        )
    return digest.hexdigest(), rows


def z_signature(geometry):
    """The geometry's own Z values, in order: the soundings' depths."""
    if geometry is None:
        return ""
    values = []
    _collect_z(geometry, values)
    if not values:
        return ""
    return hashlib.sha256(
        repr([round(value, 6) for value in values]).encode()
    ).hexdigest()


def _collect_z(geometry, values):
    from osgeo import ogr

    if geometry is None:
        return
    count = geometry.GetGeometryCount()
    if count:
        for index in range(count):
            _collect_z(geometry.GetGeometryRef(index), values)
        return
    if ogr.GT_Flatten(geometry.GetGeometryType()) == ogr.wkbPoint:
        point = geometry.GetPoint()
        if len(point) > 2:
            values.append(point[2])
        return
    for index in range(geometry.GetPointCount()):
        point = geometry.GetPoint(index)
        if len(point) > 2:
            values.append(point[2])


def effective_geometry_type(layer):
    """The layer's own geometry type, or the type its features really have."""
    from osgeo import ogr

    declared = ogr.GT_Flatten(layer.GetLayerDefn().GetGeomType())
    layer.ResetReading()
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is not None:
            return ogr.GT_Flatten(geometry.GetGeometryType())
    layer.ResetReading()
    return declared


def chart_inventory(chart):
    """The cell's own feature inventory.

    Layers in the dataset's order, features in their own order, each
    feature's FID, its geometry bytes, its sounding Z and the fields the
    application's own ENC code reads.
    """
    rows = []
    dataset = chart.vector_file
    wanted = required_fields()
    for index in range(dataset.GetLayerCount()):
        layer = dataset.GetLayerByIndex(index)
        name = layer.GetName()
        definition = layer.GetLayerDefn()
        field_names = tuple(
            definition.GetFieldDefn(position).GetName()
            for position in range(definition.GetFieldCount())
        )
        # The drawing leaves a spatial filter on the layer it read; the
        # inventory is the cell's own, not the view's.
        try:
            layer.SetSpatialFilter(None)
        except Exception:
            layer.ResetReading()
        geometry_type = effective_geometry_type(layer)
        layer.ResetReading()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            wkb = bytes(geometry.ExportToWkb()) if geometry is not None else b""
            values = []
            for field_name in wanted.get(name, ()):
                position = definition.GetFieldIndex(field_name)
                if position < 0:
                    values.append([field_name, None, "absent"])
                    continue
                value = feature.GetField(position)
                values.append(
                    [
                        field_name,
                        None if value is None else feature.GetFieldAsString(position),
                        "null" if value is None else "value",
                    ]
                )
            rows.append(
                {
                    "layer": name,
                    "layer_index": index,
                    "field_names": list(field_names),
                    "geometry_type": int(geometry_type),
                    "fid": feature.GetFID(),
                    "geometry_sha256": hashlib.sha256(wkb).hexdigest(),
                    "z_signature": z_signature(geometry),
                    "values": values,
                }
            )
    return rows


def inventory_digest(rows):
    """The inventory's own identity."""
    payload = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def inventory_differences(raw_rows, prepared_rows):
    """Where the two inventories differ, in the order they differ."""
    differences = []
    if len(raw_rows) != len(prepared_rows):
        differences.append(
            "feature count: %d != %d" % (len(raw_rows), len(prepared_rows))
        )
    for index, (left, right) in enumerate(zip(raw_rows, prepared_rows)):
        if left == right:
            continue
        differences.append(
            {
                "index": index,
                "layer": left.get("layer"),
                "fid": left.get("fid"),
                "raw": {
                    key: left.get(key)
                    for key in (
                        "geometry_sha256",
                        "z_signature",
                        "values",
                        "field_names",
                    )
                },
                "prepared": {
                    key: right.get(key)
                    for key in (
                        "geometry_sha256",
                        "z_signature",
                        "values",
                        "field_names",
                    )
                },
            }
        )
        if len(differences) >= 5:
            differences.append("further differences were not listed")
            break
    return differences


def coverage_differences(raw_rows, prepared_rows):
    """Where the two coverages differ."""
    differences = []
    if len(raw_rows) != len(prepared_rows):
        differences.append(
            "coverage geometry count: %d != %d" % (len(raw_rows), len(prepared_rows))
        )
    for index, (left, right) in enumerate(zip(raw_rows, prepared_rows)):
        if left == right:
            continue
        differences.append({"index": index, "raw": left, "prepared": right})
        if len(differences) >= 5:
            differences.append("further differences were not listed")
            break
    return differences


# ---------------------------------------------------------------------------
# the source open guards
# ---------------------------------------------------------------------------
class SourceOpenCounter:
    """Counts the source opens a preparation made."""

    def __init__(self):
        self.count = 0

    def opener(self, *args, **kwargs):
        from osgeo import ogr

        self.count += 1
        return ogr.Open(*args, **kwargs)


class SourceOpenGuard:
    """Refuses and counts every source open inside the warm child."""

    def __init__(self):
        self.count = 0
        self._saved = []

    def install(self):
        from osgeo import gdal, ogr

        for module, name in ((ogr, "Open"), (ogr, "OpenEx"), (gdal, "Open")):
            if not hasattr(module, name):
                continue
            original = getattr(module, name)
            self._saved.append((module, name, original))
            setattr(module, name, self._refuse)
        return self

    def restore(self):
        for module, name, original in self._saved:
            setattr(module, name, original)
        self._saved = []

    def _refuse(self, *args, **kwargs):
        self.count += 1
        raise RuntimeError(
            "the warm child must not open the source; the artifact is on disk"
        )

    def opener(self, *args, **kwargs):
        """The opener handed to the store: it never opens anything."""
        self.count += 1
        raise RuntimeError(
            "the warm child must not open the source; the artifact is on disk"
        )


# ---------------------------------------------------------------------------
# the engine: the gate's contact with GDAL, the store and the sandbox
# ---------------------------------------------------------------------------
class RealEngine:
    """Builds, reads and compares the real cell."""

    def verify_snapshot(self, snapshot):
        """The snapshot's own manifest, verified against its own files."""
        return _fixture().verify_snapshot(Path(snapshot))

    def prepare_sandbox(self, snapshot, root):
        """A disposable copy of the snapshot's data; the original is untouched."""
        return _fixture().prepare_sandbox(Path(snapshot), Path(root))

    def measure(self, data_dir, cache_dir, forecast_time):
        """Build the prepared cell and compare it with the raw one."""
        from gweatherrouting.qt.gribviewport import saved_chart_palette
        from gweatherrouting.qt.viewportenc import read_manifest
        from gweatherrouting.rendering.encprepared import PreparedCellStore

        manifest = read_manifest(data_dir)
        if manifest.missing:
            raise GateError("the sandbox has no settings file: %s" % manifest.error)
        if manifest.error:
            raise GateError("the manifest could not be read: %s" % manifest.error)
        source = self._select_cell(manifest)
        palette = saved_chart_palette(data_dir) or DEFAULT_PALETTE
        camera_spec = {
            "center_lat": TARGET_LAT,
            "center_lon": TARGET_LON,
            "zoom": TARGET_ZOOM,
            "width": TARGET_WIDTH,
            "height": TARGET_HEIGHT,
            "time": str(forecast_time),
            "density": TARGET_DENSITY,
            "over_land": False,
        }
        viewport = build_viewport(camera_spec, FACTOR)

        reference = raw_chart(source, palette)
        raw_began = time.perf_counter()
        raw_surface = draw_chart(reference, viewport, palette, FACTOR)
        raw_draw_s = time.perf_counter() - raw_began
        raw_array = camera_array(raw_surface, FACTOR)
        raw_coverage_digest, raw_coverage_rows = coverage_identity(
            getattr(reference, "_coverage", ())
        )
        raw_rows = chart_inventory(reference)
        raw = {
            "native_scale": getattr(reference, "_native_scale", None),
            "pixel_sha256": pixel_digest(raw_array),
            "ink_pixels": ink_pixels(raw_array),
            "coverage_sha256": raw_coverage_digest,
            "coverage_geometry_count": len(raw_coverage_rows),
            "coverage": raw_coverage_rows,
            "inventory_sha256": inventory_digest(raw_rows),
            "feature_count": len(raw_rows),
            "layer_names": sorted({row["layer"] for row in raw_rows}),
            "surface": {
                "width": int(raw_array.shape[1]),
                "height": int(raw_array.shape[0]),
            },
        }

        store = PreparedCellStore(
            cache_dir=str(cache_dir),
            cell_byte_limit=CELL_BYTE_LIMIT,
            disk_byte_limit=DISK_BYTE_LIMIT,
        )
        counter = SourceOpenCounter()
        prepared_chart = None
        try:
            build_began = time.perf_counter()
            prepared_chart = store.open_chart(
                str(source), palette, cancel=None, opener=counter.opener
            )
            cold_prepare_s = time.perf_counter() - build_began
            drawer, drawer_origin = require_drawer(prepared_chart, palette)
            prepared_began = time.perf_counter()
            prepared_surface = draw_chart(prepared_chart, viewport, palette, FACTOR)
            prepared_draw_s = time.perf_counter() - prepared_began
            prepared_array = camera_array(prepared_surface, FACTOR)
            prepared_coverage_digest, prepared_coverage_rows = coverage_identity(
                getattr(prepared_chart, "_coverage", ())
            )
            prepared_rows = chart_inventory(prepared_chart)
            prepared = {
                "built": True,
                "native_scale": getattr(prepared_chart, "_native_scale", None),
                "pixel_sha256": pixel_digest(prepared_array),
                "ink_pixels": ink_pixels(prepared_array),
                "coverage_sha256": prepared_coverage_digest,
                "coverage_geometry_count": len(prepared_coverage_rows),
                "coverage": prepared_coverage_rows,
                "inventory_sha256": inventory_digest(prepared_rows),
                "feature_count": len(prepared_rows),
                "layer_names": sorted({row["layer"] for row in prepared_rows}),
                "surface": {
                    "width": int(prepared_array.shape[1]),
                    "height": int(prepared_array.shape[0]),
                },
            }
            payload_bytes = int(
                getattr(prepared_chart, "prepared_payload_bytes", 0) or 0
            )
            stats = store.stats()
        finally:
            _close(prepared_chart)
            _release(reference)

        differences = []
        if raw["native_scale"] != prepared["native_scale"]:
            differences.append(
                "native scale: %r != %r"
                % (raw["native_scale"], prepared["native_scale"])
            )
        if not raw["coverage_geometry_count"]:
            differences.append("the cell has no coverage")
        if raw["coverage_sha256"] != prepared["coverage_sha256"]:
            differences.append("the exact coverage bytes differ")
        if raw["inventory_sha256"] != prepared["inventory_sha256"]:
            differences.append("the feature inventory differs")
        if raw["pixel_sha256"] != prepared["pixel_sha256"]:
            differences.append("the drawn pixels differ")
        return {
            "source": str(source),
            "cell": {
                "found": True,
                "name": TARGET_CELL,
                "path": str(source),
                "enabled": True,
                "class_enabled": bool(manifest.class_enabled),
            },
            "camera": camera_spec,
            "factor": FACTOR,
            "palette": palette,
            "drawer_origin": drawer_origin,
            "raw": raw,
            "prepared": prepared,
            "differences": differences,
            "inventory_differences": inventory_differences(raw_rows, prepared_rows),
            "coverage_differences": coverage_differences(
                raw_coverage_rows, prepared_coverage_rows
            ),
            "timings_s": {
                "cold_prepare": round(cold_prepare_s, 3),
                "raw_draw": round(raw_draw_s, 3),
                "prepared_draw": round(prepared_draw_s, 3),
            },
            "cache_disk_bytes": directory_bytes(cache_dir),
            "cell_payload_bytes": payload_bytes,
            "cache_stats": stats,
            "source_opens": counter.count,
            "drawer": type(drawer).__name__,
        }

    @staticmethod
    def _select_cell(manifest):
        """The target cell, as the manifest's own enabled records name it."""
        from gweatherrouting.qt.viewportenc import enc_cell_name

        for path, _enabled in manifest.enabled:
            if enc_cell_name(path) != TARGET_CELL:
                continue
            if not Path(path).is_file():
                raise GateError("%s is enabled but not installed" % TARGET_CELL)
            return path
        raise GateError("%s is not among the manifest's enabled cells" % TARGET_CELL)


def _close(chart):
    """Release a prepared chart's own resources."""
    close = getattr(chart, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _release(chart):
    """Release the reference's GDAL handle."""
    try:
        chart.vector_file = None
    except Exception:
        pass


def directory_bytes(path):
    """The accounted bytes of a directory's files."""
    total = 0
    root = Path(path)
    if not root.is_dir():
        return 0
    for entry in sorted(root.rglob("*")):
        if entry.is_file():
            total += entry.stat().st_size
    return total


# ---------------------------------------------------------------------------
# the fresh-process reload
# ---------------------------------------------------------------------------
def child_config(observations):
    """The child's own instructions, written into the sandbox."""
    prepared = observations.get("prepared") or {}
    return {
        "source": observations.get("source"),
        "cache_dir": observations.get("cache_dir"),
        "palette": observations.get("palette"),
        "factor": observations.get("factor"),
        "camera": observations.get("camera"),
        "expected": {
            "pixel_sha256": prepared.get("pixel_sha256"),
            "ink_pixels": prepared.get("ink_pixels"),
            "inventory_sha256": prepared.get("inventory_sha256"),
            "coverage_sha256": prepared.get("coverage_sha256"),
            "native_scale": prepared.get("native_scale"),
        },
    }


def run_child(config_path, output_path, timeout_s=CHILD_TIMEOUT_S):
    """Reload the artifact in a fresh process and report what happened."""
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--warm-child",
        "--config",
        str(config_path),
        "--output",
        str(output_path),
    ]
    outcome = {
        "command": command,
        "returncode": None,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "report": None,
        "error": "",
    }
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        outcome["timed_out"] = True
        outcome["error"] = "the warm child exceeded %s s" % timeout_s
        outcome["stdout"] = _as_text(stdout)
        outcome["stderr"] = _as_text(stderr)
        return outcome
    outcome["returncode"] = process.returncode
    outcome["stdout"] = stdout or ""
    outcome["stderr"] = stderr or ""
    try:
        outcome["report"] = json.loads(Path(output_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        outcome["error"] = "the warm child wrote no readable report: %s" % exc
    if process.returncode != 0 and not outcome["error"]:
        outcome["error"] = "the warm child exited %d" % process.returncode
    return outcome


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def child_run(config_path, output_path):
    """The warm child's own run: load from disk, draw, report."""
    report = {
        "role": "warm-child",
        "pid": os.getpid(),
        "checks": {name: False for name in CHILD_CHECK_NAMES},
        "source_open_count": 0,
        "errors": [],
        "problems": [],
        "complete": False,
    }
    guard = SourceOpenGuard()
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        observations = child_measure(config, guard)
        report.update(observations)
        expected = config.get("expected") or {}
        report["checks"]["prepared_chart_loaded"] = (
            observations.get("prepared_chart_loaded") is True
        )
        report["checks"]["render_nonblank"] = (
            int(observations.get("ink_pixels") or 0) > 0
        )
        report["checks"]["zero_source_opens"] = guard.count == 0
        report["checks"]["pixel_matches_expected"] = observations.get(
            "pixel_sha256"
        ) == expected.get("pixel_sha256") and observations.get(
            "ink_pixels"
        ) == expected.get(
            "ink_pixels"
        )
        report["checks"]["inventory_matches_expected"] = observations.get(
            "inventory_sha256"
        ) == expected.get("inventory_sha256")
        report["checks"]["coverage_matches_expected"] = observations.get(
            "coverage_sha256"
        ) == expected.get("coverage_sha256") and observations.get(
            "native_scale"
        ) == expected.get(
            "native_scale"
        )
    except Exception as exc:  # the report must still say what happened
        report["errors"].append("%s: %s" % (type(exc).__name__, exc))
    finally:
        guard.restore()
    report["source_open_count"] = guard.count
    ok, problems = evaluate_child_report(report)
    report["complete"] = ok
    report["problems"] = problems
    write_report(output_path, report)
    return 0 if ok else 1


def child_measure(config, guard):
    """Load the artifact from disk and draw it, without opening the source."""
    from gweatherrouting.rendering.encprepared import PreparedCellStore

    source = str(config["source"])
    cache_dir = str(config["cache_dir"])
    palette = str(config.get("palette") or DEFAULT_PALETTE)
    factor = int(config.get("factor") or FACTOR)
    viewport = build_viewport(config["camera"], factor)
    guard.install()
    store = PreparedCellStore(
        cache_dir=cache_dir,
        cell_byte_limit=CELL_BYTE_LIMIT,
        disk_byte_limit=DISK_BYTE_LIMIT,
    )
    chart = None
    try:
        began = time.perf_counter()
        chart = store.open_chart(source, palette, cancel=None, opener=guard.opener)
        reload_s = time.perf_counter() - began
        require_drawer(chart, palette)
        draw_began = time.perf_counter()
        surface = draw_chart(chart, viewport, palette, factor)
        draw_s = time.perf_counter() - draw_began
        array = camera_array(surface, factor)
        coverage_digest, coverage_rows = coverage_identity(
            getattr(chart, "_coverage", ())
        )
        rows = chart_inventory(chart)
        return {
            "prepared_chart_loaded": True,
            "native_scale": getattr(chart, "_native_scale", None),
            "pixel_sha256": pixel_digest(array),
            "ink_pixels": ink_pixels(array),
            "coverage_sha256": coverage_digest,
            "coverage_geometry_count": len(coverage_rows),
            "coverage": coverage_rows,
            "inventory_sha256": inventory_digest(rows),
            "feature_count": len(rows),
            "layer_names": sorted({row["layer"] for row in rows}),
            "cell_payload_bytes": int(getattr(chart, "prepared_payload_bytes", 0) or 0),
            "cache_disk_bytes": directory_bytes(cache_dir),
            "timings_s": {
                "disk_reload": round(reload_s, 3),
                "draw": round(draw_s, 3),
            },
        }
    finally:
        _close(chart)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def enable_sandbox_switches(data_dir):
    """The sandbox copy states what the gate needs; the user's file is untouched.

    The ENC class switch and the barb density are the switches the existing
    ENC gate sets in its own sandbox copy.  Only the disposable copy is
    written, and only these two switches.
    """
    path = Path(data_dir) / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(settings, dict):
        return ""
    settings["chartENCVisible"] = True
    settings["gribBarbDensity"] = "very_dense"
    try:
        path.write_text(json.dumps(settings), encoding="utf-8")
    except OSError:
        return ""
    return (
        "Sandbox copy: chartENCVisible and gribBarbDensity set for the "
        "gate; the user's own settings are untouched."
    )


def run(snapshot, output, engine=None, child_runner=None):
    """Run the gate and write its report.

    The report is the verdict and it is always written: the gate runner
    reads the report, so a failed check has to stay readable instead of
    turning into a missing file.
    """
    report = new_report()
    engine = engine if engine is not None else RealEngine()
    child_runner = child_runner if child_runner is not None else run_child
    started = time.perf_counter()
    sandbox_root = Path(tempfile.mkdtemp(prefix="enc-prepared-"))
    try:
        manifest = engine.verify_snapshot(snapshot) or {}
        report["snapshot_sha256"] = str(manifest.get("content_sha256", ""))
        report["checks"]["fixture_verified"] = (
            report["snapshot_sha256"] == EXPECTED_SNAPSHOT_SHA
        )
        if not report["checks"]["fixture_verified"]:
            raise GateError(
                "the snapshot's content digest differs from the approved fixture"
            )
        forecast_time = manifest.get("forecast_time")
        if not forecast_time:
            raise GateError("the snapshot manifest carries no forecast time")
        data_dir = engine.prepare_sandbox(snapshot, sandbox_root)
        note = enable_sandbox_switches(data_dir)
        if note:
            report["notes"].append(note)
        cache_dir = sandbox_root / "prepared-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        observations = engine.measure(data_dir, cache_dir, forecast_time)
        observations["cache_dir"] = str(cache_dir)
        apply_observations(report, observations)
        report["cell"] = observations.get("cell") or {}
        report["identity"] = {
            "camera": observations.get("camera"),
            "factor": observations.get("factor"),
            "palette": observations.get("palette"),
            "drawer": observations.get("drawer"),
            "drawer_origin": observations.get("drawer_origin"),
            "source_opens_during_preparation": observations.get("source_opens"),
            "cache_stats": observations.get("cache_stats"),
            "raw": _public(observations.get("raw") or {}),
            "prepared": _public(observations.get("prepared") or {}),
        }
        report["differences"] = {
            "summary": observations.get("differences") or [],
            "inventory": observations.get("inventory_differences") or [],
            "coverage": observations.get("coverage_differences") or [],
        }
        timings = dict(observations.get("timings_s") or {})
        config_path = sandbox_root / "child-config.json"
        config_path.write_text(
            json.dumps(child_config(observations), indent=2, default=str),
            encoding="utf-8",
        )
        child = child_runner(
            config_path, sandbox_root / "child-report.json", CHILD_TIMEOUT_S
        )
        report["child"] = child
        child_report = child.get("report") if isinstance(child, dict) else None
        if isinstance(child_report, dict):
            timings["disk_reload"] = (child_report.get("timings_s") or {}).get(
                "disk_reload"
            )
            timings["warm_draw"] = (child_report.get("timings_s") or {}).get("draw")
        report["timings_s"].update(timings)
        report["checks"].update(
            compare_child(child, observations.get("prepared") or {}, os.getpid())
        )
    except Exception as exc:  # the report must still say what happened
        import traceback

        report["errors"].append(
            "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc())
        )
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)
    ok, problems = evaluate_report(report)
    report["complete"] = ok
    report["verdict"] = "pass" if ok else "fail"
    report["problems"] = problems
    report["timings_s"]["total"] = round(time.perf_counter() - started, 3)
    write_report(output, report)
    print(
        "enc-prepared-cell: %s (%d problem(s))"
        % ("PASS" if ok else "FAIL", len(problems))
    )
    return 0


def _public(observation):
    """The part of an observation that belongs in the report."""
    return {
        key: value for key, value in observation.items() if key not in ("coverage",)
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, help="the approved snapshot")
    parser.add_argument("--output", required=True, type=Path, help="the report path")
    parser.add_argument(
        "--warm-child",
        action="store_true",
        help="reload the prepared artifact from disk in this process",
    )
    parser.add_argument("--config", type=Path, help="the warm child's instructions")
    args = parser.parse_args(argv)
    if args.warm_child:
        if args.config is None:
            parser.error("--warm-child needs --config")
        return child_run(args.config, args.output)
    if args.snapshot is None:
        parser.error("--snapshot is required")
    return run(args.snapshot, args.output)


if __name__ == "__main__":
    sys.exit(main())
