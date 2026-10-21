"""Retained QSG geometry for ENC chart area fills (P2.2.2).

Builds QSGGeometryNodes from prepared ENC cells in a fixed reference
coordinate system. Camera pan/zoom updates only the QSG transform matrix
(scale + translate) — zero vertex buffer upload on camera motion.

Limitations (documented):
- Hole polygons (inner rings) are not yet handled; outer rings only.
- Complex polygons with self-intersections may not triangulate correctly.
- Coverage clipping uses scissor rect (axis-aligned), not exact polygon clip.
"""

import logging
import math
from dataclasses import dataclass
from threading import Thread
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QRectF, Signal
from PySide6.QtGui import QColor, QTransform
from PySide6.QtQuick import (
    QQuickItem,
    QSGFlatColorMaterial,
    QSGGeometry,
    QSGGeometryNode,
    QSGNode,
    QSGTransformNode,
)

from gweatherrouting.core.mapviewport import MercatorProjection

if TYPE_CHECKING:
    from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart

logger = logging.getLogger("gweatherrouting")


# QSG vertex attributes for 2D points with color
_POINT2D_ATTRIBUTES = QSGGeometry.defaultAttributes_Point2D()


@dataclass(frozen=True)
class _Vertex:
    """A 2D vertex in world pixel coordinates at reference zoom."""

    x: float
    y: float


@dataclass(frozen=True)
class _Ring:
    """A polygon ring (outer or hole) as a sequence of vertices."""

    vertices: Tuple[_Vertex, ...]


@dataclass(frozen=True)
class _Polygon:
    """A polygon with outer ring and optional hole rings."""

    outer: _Ring
    holes: Tuple[_Ring, ...]


@dataclass(frozen=True)
class _CellGeometry:
    """Prepared geometry for one ENC cell.

    Vertices and coverage envelope are in world pixel coordinates at
    `reference_zoom`. Camera transforms are applied at render time via
    QSGTransformNode.
    """

    cell_path: str
    polygons: Tuple[_Polygon, ...]
    coverage_envelope: Tuple[
        float, float, float, float
    ]  # minx, maxx, miny, maxy (world pixels at reference_zoom)
    palette_colors: Dict[str, QColor]  # layer_name -> fill color
    reference_zoom: float
    reference_center_lat: float
    reference_center_lon: float
    reference_width: float
    reference_height: float


def _mercator_to_world_pixel(
    lat: float, lon: float, zoom: float
) -> Tuple[float, float]:
    """Convert geographic coordinates to world pixels at given zoom.

    World pixels are in the global Mercator coordinate system at the
    specified zoom level (not relative to any camera center).
    """
    n = 2.0**zoom
    tile_n = n * MercatorProjection.TILE_SIZE
    x = (lon + 180.0) / 360.0 * tile_n
    lat = max(-MercatorProjection.MAX_LAT, min(MercatorProjection.MAX_LAT, lat))
    lat_rad = math.radians(lat)
    y = (
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * tile_n
    )
    return (x, y)


def _world_pixel_to_scene(
    wx: float, wy: float, camera: MercatorProjection
) -> Tuple[float, float]:
    """Convert world pixel coordinates to scene coordinates for a camera."""
    cx = camera._lon_to_x(camera._center_lon)
    cy = camera._lat_to_y(camera._center_lat)
    w = camera.get_allocated_width()
    h = camera.get_allocated_height()
    sx = wx - cx + w / 2.0
    sy = wy - cy + h / 2.0
    return (sx, sy)


def _triangle_fan(outer_ring: _Ring) -> List[Tuple[_Vertex, _Vertex, _Vertex]]:
    """Triangulate a simple polygon (outer ring only) using triangle fan from centroid.

    This is a pragmatic P2.2.1 approach. It works for convex and many concave
    polygons but fails for self-intersecting or complex shapes. Hole rings
    are ignored (documented limitation).
    """
    if len(outer_ring.vertices) < 3:
        return []

    # Compute centroid
    cx = sum(v.x for v in outer_ring.vertices) / len(outer_ring.vertices)
    cy = sum(v.y for v in outer_ring.vertices) / len(outer_ring.vertices)
    centroid = _Vertex(cx, cy)

    triangles = []
    verts = outer_ring.vertices
    for i in range(len(verts) - 1):
        triangles.append((centroid, verts[i], verts[i + 1]))
    return triangles


def _build_polygons_from_ogr(geometry, reference_zoom: float) -> Tuple[_Polygon, ...]:
    """Extract polygons from OGR geometry, converting to world pixels at reference zoom.

    Only processes Polygon and MultiPolygon types. Returns outer rings only;
    inner rings (holes) are collected but not yet used for triangulation.
    """
    polygons: List[_Polygon] = []
    geom_type = geometry.GetGeometryType()

    def process_polygon(poly_geom):
        rings: List[_Ring] = []
        for i in range(poly_geom.GetGeometryCount()):
            ring_geom = poly_geom.GetGeometryRef(i)
            if ring_geom is None:
                continue
            points = ring_geom.GetPoints()
            if len(points) < 3:
                continue
            vertices = tuple(
                _Vertex(*_mercator_to_world_pixel(p[1], p[0], reference_zoom))
                for p in points
            )
            rings.append(_Ring(vertices))
        if rings:
            # First ring is outer, rest are holes (P2.2.1: holes stored but not triangulated)
            polygons.append(_Polygon(rings[0], tuple(rings[1:])))

    if geom_type in (3, 6):  # wkbPolygon, wkbMultiPolygon (flattened)
        if geom_type == 3:
            process_polygon(geometry)
        else:
            for i in range(geometry.GetGeometryCount()):
                sub = geometry.GetGeometryRef(i)
                if sub is not None:
                    process_polygon(sub)

    return tuple(polygons)


def _prepare_cell_geometry(
    cell_path: str,
    palette: str,
    camera: MercatorProjection,
    cache_dir: str,
    cancel: Optional[callable] = None,
) -> Optional[_CellGeometry]:
    """Build QSG-ready geometry for one ENC cell from its prepared artifact.

    Runs on the worker thread. Uses PreparedCellStore to get the GDALVectorChart
    on an OGR Memory dataset, then iterates area layers and tessellates.

    Geometry is built at the camera's current zoom as the reference zoom.
    Vertices are stored in world pixel coordinates at that reference zoom.
    """
    # Use the camera's zoom as the reference zoom for this geometry build
    reference_zoom = camera._zoom
    reference_center_lat = camera._center_lat
    reference_center_lon = camera._center_lon
    reference_width = camera.get_allocated_width()
    reference_height = camera.get_allocated_height()

    chart = _open_prepared_chart(cell_path, palette, cache_dir, cancel)
    if chart is None:
        return None

    try:
        vector_file = chart.vector_file
        if vector_file is None:
            return None

        from gweatherrouting.rendering.style import Style

        palette_obj = Style.chart_palettes[palette]
        drawer = chart.drawer

        polygons, palette_colors = _collect_area_polygons(
            vector_file, reference_zoom, drawer, palette_obj, cancel
        )

        coverage_env = _compute_coverage_envelope(
            chart, reference_zoom, reference_center_lat, reference_center_lon
        )

        return _CellGeometry(
            cell_path=cell_path,
            polygons=tuple(polygons),
            coverage_envelope=coverage_env,
            palette_colors=palette_colors,
            reference_zoom=reference_zoom,
            reference_center_lat=reference_center_lat,
            reference_center_lon=reference_center_lon,
            reference_width=reference_width,
            reference_height=reference_height,
        )
    finally:
        _release_chart_vector_file(chart)


def _open_prepared_chart(
    cell_path: str, palette: str, cache_dir: str, cancel: Optional[callable]
) -> Optional["GDALVectorChart"]:
    """Open a prepared chart from the cell store."""
    from gweatherrouting.rendering.encprepared import PreparedCellStore
    from gweatherrouting.rendering.style import Style

    if palette not in Style.chart_palettes:
        logger.warning("Unknown palette %r for cell %s", palette, cell_path)
        return None

    try:
        store = PreparedCellStore(cache_dir)
        return store.open_chart(cell_path, palette, cancel=cancel)
    except Exception as exc:
        logger.warning("Failed to open prepared chart %s: %s", cell_path, exc)
        return None


def _collect_area_polygons(
    vector_file,
    reference_zoom: float,
    drawer,
    palette_obj,
    cancel: Optional[callable],
) -> Tuple[List[_Polygon], Dict[str, QColor]]:
    """Collect area polygons from vector file layers."""
    area_layers = {
        "LNDARE",
        "DEPARE",
        "DRGARE",
        "BUAARE",
        "BUISGL",
        "LAKARE",
        "RIVERS",
        "ACHARE",
        "ANCBRT",
        "RESARE",
        "CTNARE",
    }

    polygons: List[_Polygon] = []
    palette_colors: Dict[str, QColor] = {}

    for layer_index in range(vector_file.GetLayerCount()):
        if cancel and cancel():
            return [], {}
        layer = vector_file.GetLayerByIndex(layer_index)
        name = layer.GetName()
        if name not in area_layers:
            continue

        layer.ResetReading()
        for feature in layer:
            if cancel and cancel():
                return [], {}
            geom = feature.GetGeometryRef()
            if geom is None:
                continue
            cell_polygons = _build_polygons_from_ogr(geom, reference_zoom)
            polygons.extend(cell_polygons)

            # Get fill color for this layer from palette
            if name not in palette_colors:
                fill_style, _ = drawer._get_area_styles(name, {}, palette_obj)
                if fill_style:
                    r, g, b, a = fill_style.color
                    palette_colors[name] = QColor(r * 255, g * 255, b * 255, a * 255)

    return polygons, palette_colors


def _compute_coverage_envelope(
    chart,
    reference_zoom: float,
    reference_center_lat: float,
    reference_center_lon: float,
) -> Tuple[float, float, float, float]:
    """Compute coverage envelope in world pixel coordinates at reference zoom."""
    coverage_env = (0, 0, 0, 0)
    if chart._coverage:
        env = chart._coverage[0].GetEnvelope()
        # Convert to world pixels at reference zoom
        minx, miny = _mercator_to_world_pixel(env[2], env[0], reference_zoom)
        maxx, maxy = _mercator_to_world_pixel(env[3], env[1], reference_zoom)
        coverage_env = (minx, maxx, miny, maxy)
    return coverage_env


def _release_chart_vector_file(chart) -> None:
    """Release the chart's vector file dataset."""
    if hasattr(chart, "vector_file") and chart.vector_file:
        from gweatherrouting.rendering.encprepared import _release_dataset

        _release_dataset(chart.vector_file)


class ChartGeometryItem(QQuickItem):
    """QQuickItem that renders one ENC cell's area geometry as QSGGeometryNode.

    Geometry is built once in world pixel coordinates at a reference zoom.
    Camera pan/zoom updates only the QSGTransformNode matrix (scale + translate).
    Zero vertex buffer upload on camera motion.
    """

    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)
        self._cell_geometry: Optional[_CellGeometry] = None
        self._camera: Optional[MercatorProjection] = None
        self._geometry_dirty = True
        self._transform_dirty = False
        self._transform_node: Optional[QSGTransformNode] = None
        self._geometry_node: Optional[QSGGeometryNode] = None
        self._geometry: Optional[QSGGeometry] = None
        self._material: Optional[QSGFlatColorMaterial] = None
        # Reference camera parameters (from cell geometry at build time)
        self._ref_zoom: float = 0.0
        self._ref_center_lat: float = 0.0
        self._ref_center_lon: float = 0.0
        self._ref_width: float = 0.0
        self._ref_height: float = 0.0

    def set_cell_geometry(self, cell_geometry: _CellGeometry):
        """Set the prepared cell geometry (from worker thread)."""
        self._cell_geometry = cell_geometry
        # Store reference parameters for transform computation
        self._ref_zoom = cell_geometry.reference_zoom
        self._ref_center_lat = cell_geometry.reference_center_lat
        self._ref_center_lon = cell_geometry.reference_center_lon
        self._ref_width = cell_geometry.reference_width
        self._ref_height = cell_geometry.reference_height
        self._geometry_dirty = True
        self._transform_dirty = True  # Transform needs update for new reference
        self.update()

    def set_camera(self, camera: MercatorProjection):
        """Update camera for transform (called on GUI thread)."""
        self._camera = camera
        if self._cell_geometry is not None and self._transform_node is not None:
            # Compute and apply transform; mark only transform dirty
            self._update_transform_node()
            self._transform_dirty = True
            self._geometry_dirty = False  # Explicitly NOT geometry dirty
            self.update()

    def _update_transform_node(self):
        """Compute and apply transform matrix for current camera vs reference."""
        if self._transform_node is None or self._camera is None:
            return

        cell_geom = self._cell_geometry
        if cell_geom is None:
            return

        # Current camera parameters
        cur_zoom = self._camera._zoom
        cur_center_lat = self._camera._center_lat
        cur_center_lon = self._camera._center_lon
        cur_width = self._camera.get_allocated_width()
        cur_height = self._camera.get_allocated_height()

        # Scale factor: 2^(cur_zoom - ref_zoom)
        scale = 2.0 ** (cur_zoom - self._ref_zoom)

        # Current camera center in world pixels at current zoom
        cur_cx = (
            (cur_center_lon + 180.0)
            / 360.0
            * (2.0**cur_zoom)
            * MercatorProjection.TILE_SIZE
        )
        cur_lat_rad = math.radians(
            max(
                -MercatorProjection.MAX_LAT,
                min(MercatorProjection.MAX_LAT, cur_center_lat),
            )
        )
        cur_cy = (
            (
                1.0
                - math.log(math.tan(cur_lat_rad) + 1.0 / math.cos(cur_lat_rad))
                / math.pi
            )
            / 2.0
            * (2.0**cur_zoom)
            * MercatorProjection.TILE_SIZE
        )

        # Transform: world pixels at ref_zoom -> screen pixels at current camera
        # Vertex at ref_zoom: x_ref
        # 1. Scale to cur_zoom world pixels: x_ref * scale
        # 2. Convert to screen: x_ref * scale - cur_cx + cur_width/2
        # Transform matrix: x' = scale * x + tx, where tx = -cur_cx + cur_width/2

        tx = -cur_cx + cur_width / 2.0
        ty = -cur_cy + cur_height / 2.0

        transform = QTransform()
        transform.scale(scale, scale)
        transform.translate(tx, ty)  # Translation in screen space (after scaling)

        self._transform_node.setMatrix(transform)

    def updatePaintNode(self, old_node: Optional[QSGNode], _):  # noqa: N802
        if self._cell_geometry is None:
            return None

        try:
            # Root node is a transform node; geometry node is its child
            transform_node = old_node
            geometry_node = None

            if transform_node is None:
                transform_node = QSGTransformNode()
                geometry_node = QSGGeometryNode()
                geometry_node.setFlag(QSGNode.Flag.OwnsGeometry)
                geometry_node.setFlag(QSGNode.Flag.OwnsMaterial)
                transform_node.appendChildNode(geometry_node)
            else:
                # Find the geometry child node
                geometry_node = transform_node.firstChild()
                if geometry_node is None or not isinstance(
                    geometry_node, QSGGeometryNode
                ):
                    geometry_node = QSGGeometryNode()
                    geometry_node.setFlag(QSGNode.Flag.OwnsGeometry)
                    geometry_node.setFlag(QSGNode.Flag.OwnsMaterial)
                    transform_node.appendChildNode(geometry_node)

            self._transform_node = transform_node
            self._geometry_node = geometry_node

            # Rebuild geometry only if geometry is dirty (cell set change or coverage change)
            if self._geometry_dirty:
                self._rebuild_geometry(geometry_node)
                self._geometry_dirty = False
                self._transform_dirty = (
                    True  # Transform needs update after geometry rebuild
                )

            # Update transform if dirty
            if self._transform_dirty and self._camera is not None:
                self._update_transform_node()
                self._transform_dirty = False

            # Apply coverage scissor rect on geometry node (in world pixel coords at ref zoom)
            # The scissor rect is in the geometry node's coordinate space (reference world pixels)
            if self._cell_geometry.coverage_envelope != (0, 0, 0, 0):
                minx, maxx, miny, maxy = self._cell_geometry.coverage_envelope
                geometry_node.setClipRect(QRectF(minx, miny, maxx - minx, maxy - miny))

            return transform_node
        except Exception as exc:
            self.failed.emit(str(exc))
            return None

    def _rebuild_geometry(self, node: QSGGeometryNode):
        """Build QSGGeometry from cell polygons (triangle fan triangulation).

        Vertices are in world pixel coordinates at reference zoom.
        """
        cell_geom = self._cell_geometry
        if cell_geom is None:
            return

        # Collect all triangles from all polygons
        all_triangles: List[Tuple[_Vertex, _Vertex, _Vertex]] = []
        polygon_colors: List[QColor] = []

        for poly in cell_geom.polygons:
            triangles = _triangle_fan(poly.outer)
            all_triangles.extend(triangles)
            # Use the first layer's color for this polygon (simplified)
            color = QColor(200, 200, 200, 255)  # fallback
            for layer_name, layer_color in cell_geom.palette_colors.items():
                color = layer_color
                break
            polygon_colors.extend([color] * len(triangles))

        if not all_triangles:
            return

        # Build geometry
        vertex_count = len(all_triangles) * 3
        if self._geometry is None or self._geometry.vertexCount() != vertex_count:
            self._geometry = QSGGeometry(_POINT2D_ATTRIBUTES, vertex_count)
            self._geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
            self._geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
            node.setGeometry(self._geometry)

        if self._material is None:
            self._material = QSGFlatColorMaterial()
            node.setMaterial(self._material)

        # Write vertices (in world pixel coordinates at reference zoom)
        geometry = node.geometry()
        geometry.allocate(vertex_count)
        vertex_data = geometry.vertexDataAsPoint2D()

        for i, (v1, v2, v3) in enumerate(all_triangles):
            base = i * 3
            vertex_data[base].set(v1.x, v1.y)
            vertex_data[base + 1].set(v2.x, v2.y)
            vertex_data[base + 2].set(v3.x, v3.y)

        node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)

        # Use first color for whole node (simplified; per-polygon colors need separate nodes)
        if polygon_colors:
            self._material.setColor(polygon_colors[0])


class ChartGeometryScene(QObject):
    """Manages ChartGeometryItems for all visible ENC cells (MapScene pattern).

    Holds geometry items per cell, updates transforms on camera change.
    Built from PreparedCellStore on worker thread; GUI thread owns QSG nodes.
    """

    geometry_ready = Signal(object)  # emits dict of cell_path -> _CellGeometry

    def __init__(self, parent_item=None, cache_dir: str = "/tmp/enc_cache"):
        super().__init__(parent_item)
        self._parent_item = parent_item
        self._items: Dict[str, ChartGeometryItem] = {}
        self._camera: Optional[MercatorProjection] = None
        self._palette: str = "cm93"
        self._cache_dir: str = cache_dir
        self._cell_paths: Tuple[str, ...] = ()
        self._worker_thread: Optional["_GeometryWorker"] = None

    def set_palette(self, palette: str):
        self._palette = palette

    def set_camera(self, camera: MercatorProjection):
        """Update camera for all items (GUI thread)."""
        self._camera = camera
        for item in self._items.values():
            item.set_camera(camera)

    def set_cell_paths(self, cell_paths: Sequence[str]):
        """Set the visible cell paths and request geometry build."""
        self._cell_paths = tuple(cell_paths)
        self._rebuild_geometry()

    def _rebuild_geometry(self):
        """Start worker thread to build geometry for all cells."""
        if self._worker_thread is not None and self._worker_thread.is_running():
            self._worker_thread.cancel()
            self._worker_thread.wait()

        self._worker_thread = _GeometryWorker(
            cell_paths=self._cell_paths,
            palette=self._palette,
            camera=self._camera,
            cache_dir=self._cache_dir,
            on_ready=self._on_geometry_ready,
            parent_item=self._parent_item,
        )
        self._worker_thread.start()

    def _on_geometry_ready(self, geometries: Dict[str, _CellGeometry]):
        """Callback from worker thread with built geometry."""
        # Update existing items or create new ones
        for cell_path, geom in geometries.items():
            if cell_path in self._items:
                item = self._items[cell_path]
            else:
                item = ChartGeometryItem()
                if self._parent_item is not None:
                    item.setParentItem(self._parent_item)
                item.failed.connect(
                    lambda msg: logger.error("Chart geometry error: %s", msg)
                )
                self._items[cell_path] = item
            item.set_cell_geometry(geom)
            if self._camera:
                item.set_camera(self._camera)

        # Remove items for cells no longer visible
        for cell_path in list(self._items.keys()):
            if cell_path not in geometries:
                item = self._items.pop(cell_path)
                item.setParentItem(None)
                item.deleteLater()

    def clear(self):
        """Clear all geometry items."""
        if self._worker_thread is not None:
            self._worker_thread.cancel()
            self._worker_thread.wait()
            self._worker_thread = None
        for item in self._items.values():
            item.setParentItem(None)
            item.deleteLater()
        self._items.clear()


class _GeometryWorker:
    """Worker thread for building cell geometry from prepared cells."""

    def __init__(
        self,
        cell_paths: Sequence[str],
        palette: str,
        camera: Optional[MercatorProjection],
        cache_dir: str,
        on_ready: callable,
        parent_item=None,
    ):
        self._cell_paths = cell_paths
        self._palette = palette
        self._camera = camera
        self._cache_dir = cache_dir
        self._on_ready = on_ready
        self._parent_item = parent_item
        self._cancelled = False
        self._thread: Optional[Thread] = None

    def cancel(self):
        self._cancelled = True

    def start(self):
        """Start the worker thread."""
        self._thread = Thread(target=self._run, name="chart-geometry", daemon=True)
        self._thread.start()

    def wait(self):
        """Wait for the worker thread to finish."""
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def is_running(self) -> bool:
        """Check if the worker thread is running."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        """Run geometry building (called from thread)."""
        if self._camera is None:
            self._invoke_ready({})
            return

        geometries: Dict[str, _CellGeometry] = {}
        for cell_path in self._cell_paths:
            if self._cancelled:
                break
            geom = _prepare_cell_geometry(
                cell_path,
                self._palette,
                self._camera,
                self._cache_dir,
                cancel=lambda: self._cancelled,
            )
            if geom is not None:
                geometries[cell_path] = geom

        # Invoke callback on GUI thread
        self._invoke_ready(geometries)

    def _invoke_ready(self, geometries: Dict[str, _CellGeometry]):
        """Invoke the ready callback on the GUI thread."""
        from PySide6.QtCore import QTimer

        if self._parent_item is not None:
            QTimer.singleShot(0, lambda: self._on_ready(geometries))
        else:
            # Fallback: call directly (should not happen in normal operation)
            self._on_ready(geometries)
