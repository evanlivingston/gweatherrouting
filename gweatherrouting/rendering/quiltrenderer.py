"""Scale-ranked ENC/raster quilting with exclusively worker-owned GDAL handles."""

import logging
from collections import OrderedDict
from typing import NamedTuple

import cairo
from osgeo import ogr

from gweatherrouting.core.enccharts import chart_scale_rank, chart_scale_visible
from gweatherrouting.core.mapviewport import MapPoint
from gweatherrouting.core.scheduling import schedule_idle

from .chartlayer import ChartLayer
from .gdalrasterchart import GDALRasterChart, GDALSingleRasterChart
from .gdalvectorchart import GDALVectorChart
from .overlayrenderer import OverlayRenderer
from .vectordrawer.s57chartdrawer import S57ChartDrawer

logger = logging.getLogger("gweatherrouting")


class ChartSource(NamedTuple):
    path: str
    native_scale: float
    envelopes: tuple
    kind: str = "enc"
    bbox: tuple = ()


def raster_source(entry):
    path, bbox, _width, _height, scale = entry
    corners = tuple(tuple(point) for point in bbox)
    lat_a, lon_a, *_ = corners[0]
    lat_b, lon_b, *_ = corners[1]
    envelope = (
        min(lon_a, lon_b),
        max(lon_a, lon_b),
        min(lat_a, lat_b),
        max(lat_a, lat_b),
    )
    return ChartSource(path, scale, (envelope,), "raster", corners)


def describe_charts(charts):
    """Snapshot file metadata on the UI thread; no GUI charts cross to workers."""
    base, detail = [], []
    for chart in charts:
        if isinstance(chart, GDALRasterChart):
            if chart.enabled:
                detail.extend(raster_source(entry) for entry in chart.metadata or ())
        elif isinstance(chart, GDALVectorChart) and isinstance(
            chart.drawer, S57ChartDrawer
        ):
            if chart.enabled:
                detail.append(
                    ChartSource(
                        chart.path,
                        chart._native_scale,
                        tuple(chart._coverage_envelopes),
                    )
                )
        else:
            base.append(chart)
    return base, tuple(detail)


def coverage_path(cr, viewport, geometry):
    """Append polygon rings to a Cairo clip, including islands and holes."""
    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbPolygon:
        for index in range(geometry.GetGeometryCount()):
            ring = geometry.GetGeometryRef(index)
            for point_index, (lon, lat, *_) in enumerate(ring.GetPoints()):
                x, y = viewport.convert_geographic_to_screen(MapPoint(lat, lon))
                if point_index == 0:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
            cr.close_path()
    elif kind in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        for index in range(geometry.GetGeometryCount()):
            coverage_path(cr, viewport, geometry.GetGeometryRef(index))


class QuiltRenderer(OverlayRenderer):
    RASTER_CACHE_ENTRIES = 8
    RASTER_CACHE_BYTES = 128 * 1024 * 1024

    def __init__(self, schedule=schedule_idle):
        super().__init__(schedule)
        self._charts = OrderedDict()
        self._rasters = OrderedDict()
        self._raster_bytes = 0

    def _content_key(self, charts):
        # Scale/coverage edits and chart kind matter, not just the file path.
        return tuple(ChartSource(*source) for source in charts)

    def _render(self, job):
        viewport = job.viewport
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            viewport.width * job.factor,
            viewport.height * job.factor,
        )
        surface.set_device_scale(job.factor, job.factor)
        cr = cairo.Context(surface)
        p1, p2 = viewport.get_bbox()
        bounding = ogr.CreateGeometryFromWkt(
            ChartLayer.get_bounding_wkt_of_coords(*p1.get_degrees(), *p2.get_degrees())
        )
        min_lon, max_lon, min_lat, max_lat = bounding.GetEnvelope()
        scale = viewport.get_scale()
        sources = sorted(
            (ChartSource(*source) for source in job.charts),
            key=lambda source: (
                chart_scale_rank(source.native_scale, scale),
                source.kind != "enc",
                source.path,
            ),
        )
        covered = None
        unknown_coverage_drawn = False
        for source in sources:
            if job.cancel.is_set():
                return None
            if not chart_scale_visible(source.native_scale, scale):
                continue
            if source.envelopes and not any(
                a <= max_lon and b >= min_lon and c <= max_lat and d >= min_lat
                for a, b, c, d in source.envelopes
            ):
                continue
            try:
                chart = None
                region = None
                if source.kind == "raster":
                    a, b, c, d = source.envelopes[0]
                    region = ogr.CreateGeometryFromWkt(
                        ChartLayer.get_bounding_wkt_of_coords(c, a, d, b)
                    )
                else:
                    chart = self._worker_chart(
                        source.path, source.native_scale, job.palette
                    )
                    for geometry in chart._coverage:
                        region = (
                            geometry.Clone()
                            if region is None
                            else region.Union(geometry)
                        )
                known_coverage = region is not None
                region = (
                    region.Intersection(bounding)
                    if known_coverage
                    else bounding.Clone()
                )
                if covered is not None:
                    region = region.Difference(covered)
                if region.IsEmpty():
                    continue
                if chart is None:
                    chart = self._worker_raster(source.path)
                if job.cancel.is_set():
                    return None
                cr.save()
                try:
                    cr.new_path()
                    coverage_path(cr, viewport, region)
                    cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
                    cr.clip()
                    if unknown_coverage_drawn:
                        # An ENC without M_COVR cannot claim a geometric region.
                        # Preserve its painted pixels while filling transparent
                        # gaps behind it. Grouping preserves each chart's own
                        # normal internal OVER portrayal order.
                        cr.push_group()
                        try:
                            chart.do_draw(viewport, cr)
                        finally:
                            pixels = cr.pop_group()
                        cr.set_source(pixels)
                        cr.set_operator(cairo.OPERATOR_DEST_OVER)
                        cr.paint()
                    else:
                        chart.do_draw(viewport, cr)
                finally:
                    cr.restore()
                if known_coverage:
                    covered = region if covered is None else covered.Union(region)
                else:
                    unknown_coverage_drawn = True
            except Exception as error:
                # A missing/bad file must not strand all the other chart sources.
                logger.warning("Unable to quilt chart %s: %s", source.path, error)
        surface.flush()
        return None if job.cancel.is_set() else surface

    def _worker_chart(self, path, native_scale, palette):
        chart = self._charts.get(path)
        if chart is None:
            chart = GDALVectorChart.__new__(GDALVectorChart)
            chart.vector_file = ogr.Open(path)
            if chart.vector_file is None:
                raise ValueError(f"Cannot open chart {path}")
            chart._coverage = GDALVectorChart.read_coverage(chart.vector_file)
            chart.drawer = S57ChartDrawer.__new__(S57ChartDrawer)
            self._charts[path] = chart
            if len(self._charts) > 8:
                self._charts.popitem(last=False)
        self._charts.move_to_end(path)
        chart._native_scale = native_scale
        chart.drawer.palette = palette
        return chart

    def _worker_raster(self, path):
        chart = self._rasters.get(path)
        if chart is not None:
            self._rasters.move_to_end(path)
            return chart
        chart = GDALSingleRasterChart(path)
        size = chart.surface.get_stride() * chart.surface.get_height()
        # Oversized charts may render, but are never retained in the LRU.
        if size <= self.RASTER_CACHE_BYTES:
            self._rasters[path] = chart
            self._raster_bytes += size
            while (
                len(self._rasters) > self.RASTER_CACHE_ENTRIES
                or self._raster_bytes > self.RASTER_CACHE_BYTES
            ):
                _, old = self._rasters.popitem(last=False)
                self._raster_bytes -= (
                    old.surface.get_stride() * old.surface.get_height()
                )
        return chart
