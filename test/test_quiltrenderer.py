"""Mixed ENC/raster ownership, worker isolation, cache and publication checks."""

import threading
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cairo
from osgeo import gdal, ogr

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.rendering.chartcollection import ChartCollection
from gweatherrouting.rendering.gdalrasterchart import (
    GDALRasterChart,
    GDALSingleRasterChart,
)
from gweatherrouting.rendering.overlayrenderer import RenderJob
from gweatherrouting.rendering.quiltrenderer import (
    ChartSource,
    QuiltRenderer,
    describe_charts,
    raster_source,
)
from test.map_helpers import MapCanvas, QueuedScheduler
from test.test_rastercharts import make_raster


class TestMixedQuilt(unittest.TestCase):
    def setUp(self):
        self.canvas = MapCanvas()
        self.viewport = MapViewport(self.canvas)
        self.native = self.canvas.get_scale() * 3779.527559
        self.renderer = QuiltRenderer()
        self.encs, self.rasters = {}, {}
        self.renderer._worker_chart = lambda path, scale, palette: self.encs[path]
        self.renderer._worker_raster = Mock(side_effect=lambda path: self.rasters[path])

    def rectangle(self, left, top, right, bottom):
        corners = [
            self.viewport.convert_screen_to_geographic(x, y).get_degrees()
            for x, y in (
                (left, top),
                (right, top),
                (right, bottom),
                (left, bottom),
                (left, top),
            )
        ]
        return ogr.CreateGeometryFromWkt(
            "POLYGON((" + ",".join(f"{lon} {lat}" for lat, lon in corners) + "))"
        )

    def source(self, kind, name, scale, region, color):
        def draw(viewport, cr):
            cr.set_source_rgb(*color)
            cr.paint()

        chart = SimpleNamespace(_coverage=[region], do_draw=Mock(side_effect=draw))
        (self.rasters if kind == "raster" else self.encs)[name] = chart
        return ChartSource(name, scale, (region.GetEnvelope(),), kind)

    def render(self, *sources):
        job = RenderJob((), MapViewport(self.canvas), sources, 1, "cm93")
        job.viewport.render_cancelled = job.cancel.is_set
        return self.renderer._render(job)

    @staticmethod
    def pixel(surface, x, y):
        surface.flush()
        offset = y * surface.get_stride() + x * 4
        return tuple(bytes(surface.get_data())[offset : offset + 4])

    def test_raster_and_enc_arbitrate_by_scale_not_import_order(self):
        enc = self.source(
            "enc", "coast", self.native * 8, self.rectangle(0, 0, 350, 300), (1, 0, 0)
        )
        raster = self.source(
            "raster", "harbor", self.native, self.rectangle(120, 0, 280, 300), (0, 1, 0)
        )
        first = self.render(enc, raster)
        self.assertEqual(self.pixel(first, 200, 150), (0, 255, 0, 255))
        self.assertEqual(self.pixel(first, 50, 150), (0, 0, 255, 255))
        self.assertEqual(self.pixel(first, 380, 150), (0, 0, 0, 0))
        second = self.render(raster, enc)
        self.assertEqual(bytes(first.get_data()), bytes(second.get_data()))
        # The same sources reverse ownership when the ENC is better scaled.
        self.canvas._zoom -= 3
        coarse = self.render(raster, enc)
        self.assertEqual(self.pixel(coarse, 200, 150), (0, 0, 255, 255))

    def test_enc_hole_uses_raster_and_uncovered_area_shows_base(self):
        full = self.rectangle(0, 0, 300, 300)
        hole = self.rectangle(160, 100, 240, 200)
        enc = self.source("enc", "enc", self.native, full.Difference(hole), (1, 0, 0))
        raster = self.source("raster", "raster", self.native / 8, hole, (0, 1, 0))
        detail = self.render(enc, raster)
        base = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
        cr = cairo.Context(base)
        cr.set_source_rgb(0, 0, 1)
        cr.paint()
        cr.set_source_surface(detail)
        cr.paint()
        self.assertEqual(self.pixel(base, 50, 150), (0, 0, 255, 255))
        self.assertEqual(self.pixel(base, 200, 150), (0, 255, 0, 255))
        self.assertEqual(self.pixel(base, 380, 150), (255, 0, 0, 255))

    def test_unknown_raster_never_displaces_even_unknown_enc(self):
        region = self.rectangle(0, 0, 400, 300)
        enc = self.source("enc", "z-enc", 0, region, (1, 0, 0))
        raster = self.source("raster", "a-raster", 0, region, (0, 1, 0))
        self.assertEqual(
            self.pixel(self.render(raster, enc), 200, 150), (0, 0, 255, 255)
        )
        self.assertEqual(self.pixel(self.render(raster), 200, 150), (0, 255, 0, 255))

    def test_unknown_raster_fills_behind_enc_without_coverage_metadata(self):
        region = self.rectangle(0, 0, 400, 300)
        enc = self.source("enc", "enc", 0, region, (1, 0, 0))
        self.encs["enc"]._coverage = []

        def draw(viewport, cr):
            cr.set_source_rgb(1, 0, 0)
            cr.rectangle(0, 0, 200, 300)
            cr.fill()

        self.encs["enc"].do_draw.side_effect = draw
        raster = self.source("raster", "raster", 0, region, (0, 1, 0))
        image = self.render(raster, enc)
        self.assertEqual(self.pixel(image, 100, 150), (0, 0, 255, 255))
        self.assertEqual(self.pixel(image, 300, 150), (0, 255, 0, 255))

    def test_outside_and_scale_hidden_rasters_are_not_decoded(self):
        outside = self.source(
            "raster",
            "outside",
            self.native,
            self.rectangle(600, 0, 700, 300),
            (0, 1, 0),
        )
        hidden = self.source(
            "raster",
            "hidden",
            self.native / 32,
            self.rectangle(0, 0, 400, 300),
            (0, 1, 0),
        )
        image = self.render(outside, hidden)
        self.renderer._worker_raster.assert_not_called()
        self.assertEqual(self.pixel(image, 200, 150), (0, 0, 0, 0))

    def test_fully_occluded_raster_is_not_decoded(self):
        enc = self.source(
            "enc", "enc", self.native, self.rectangle(-20, -20, 420, 320), (1, 0, 0)
        )
        raster = self.source(
            "raster",
            "raster",
            self.native * 8,
            self.rectangle(0, 0, 400, 300),
            (0, 1, 0),
        )
        self.render(raster, enc)
        self.renderer._worker_raster.assert_not_called()

    def test_failed_raster_leaves_coverage_for_enc_fallback(self):
        region = self.rectangle(0, 0, 400, 300)
        broken = ChartSource("missing", self.native, (region.GetEnvelope(),), "raster")
        enc = self.source("enc", "enc", self.native * 8, region, (1, 0, 0))
        with self.assertLogs("gweatherrouting", level="WARNING"):
            image = self.render(broken, enc)
        self.assertEqual(self.pixel(image, 200, 150), (0, 0, 255, 255))

    def test_cancellation_before_and_after_decode_discards_surface(self):
        raster = self.source(
            "raster", "raster", self.native, self.rectangle(0, 0, 400, 300), (0, 1, 0)
        )
        job = RenderJob((), self.viewport, (raster,), 1, "cm93")
        job.cancel.set()
        self.assertIsNone(self.renderer._render(job))
        self.renderer._worker_raster.assert_not_called()
        job.cancel.clear()

        def decode(path):
            job.cancel.set()
            return self.rasters[path]

        self.renderer._worker_raster.side_effect = decode
        self.assertIsNone(self.renderer._render(job))
        self.rasters["raster"].do_draw.assert_not_called()

    def test_production_raster_pixels_are_clipped_against_enc(self):
        with TemporaryDirectory() as directory:
            bbox = self.viewport.get_bbox()
            top, left = bbox[0].get_degrees()
            bottom, right = bbox[1].get_degrees()
            path = make_raster(
                directory + "/raster.tif",
                4326,
                (left, (right - left) / 50, 0, top, 0, (bottom - top) / 50),
            )
            dataset = gdal.Open(path, gdal.GA_Update)
            colors = gdal.ColorTable()
            colors.SetColorEntry(1, (0, 255, 0, 255))
            dataset.GetRasterBand(1).SetRasterColorTable(colors)
            dataset.GetRasterBand(1).Fill(1)
            dataset = None
            raster = raster_source(GDALSingleRasterChart.get_file_info(path))._replace(
                native_scale=self.native * 8
            )
            enc = self.source(
                "enc", "enc", self.native, self.rectangle(0, 0, 180, 300), (1, 0, 0)
            )
            self.renderer._worker_raster = QuiltRenderer._worker_raster.__get__(
                self.renderer
            )
            image = self.render(raster, enc)
            self.assertEqual(self.pixel(image, 100, 150), (0, 0, 255, 255))
            self.assertEqual(self.pixel(image, 300, 150), (0, 255, 0, 255))


class TestQuiltLifecycle(unittest.TestCase):
    def setUp(self):
        self.renderer = QuiltRenderer()
        self.renderer._start_next = Mock()
        self.canvas = MapCanvas()
        self.cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300))
        self.source = ChartSource("raster", 10000, ((-123, -122, 47, 48),), "raster")

    def test_scale_and_coverage_changes_cancel_old_demand(self):
        self.renderer.draw(self.canvas, self.cr, (self.source,), "cm93")
        first = self.renderer._states[self.canvas].requested
        changed = self.source._replace(native_scale=20000)
        self.renderer.draw(self.canvas, self.cr, (changed,), "cm93")
        self.assertTrue(first.cancel.is_set())
        second = self.renderer._states[self.canvas].requested
        changed = changed._replace(envelopes=((-124, -122, 47, 48),))
        self.renderer.draw(self.canvas, self.cr, (changed,), "cm93")
        self.assertTrue(second.cancel.is_set())
        self.assertEqual(len(self.renderer._pending), 1)

    def test_published_frame_reuses_and_obsolete_completion_does_not_advance_revision(
        self,
    ):
        self.renderer.draw(self.canvas, self.cr, (self.source,), "cm93")
        job = self.renderer._states[self.canvas].requested
        image = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, job.viewport.width, job.viewport.height
        )
        self.renderer._completed(self.canvas, job, image, None)
        revision = self.renderer.frame_revision(self.canvas)
        self.assertGreater(revision, 0)
        starts = self.renderer._start_next.call_count
        self.renderer.draw(self.canvas, self.cr, (self.source,), "cm93")
        self.assertEqual(self.renderer._start_next.call_count, starts)
        self.renderer.draw(self.canvas, self.cr, (self.source,), "dark")
        self.renderer._completed(self.canvas, job, image, None)
        self.assertEqual(self.renderer.frame_revision(self.canvas), revision)
        self.renderer.forget(self.canvas)
        self.assertEqual(self.renderer.frame_revision(self.canvas), 0)

    def test_raster_lru_limits_entries_bytes_and_refreshes_recency(self):
        self.renderer.RASTER_CACHE_ENTRIES = 2
        self.renderer.RASTER_CACHE_BYTES = 32

        def chart(path):
            return SimpleNamespace(
                surface=cairo.ImageSurface(cairo.FORMAT_ARGB32, 2, 2)
            )

        with patch(
            "gweatherrouting.rendering.quiltrenderer.GDALSingleRasterChart",
            side_effect=chart,
        ) as load:
            first = self.renderer._worker_raster("a")
            self.renderer._worker_raster("b")
            self.assertIs(self.renderer._worker_raster("a"), first)
            self.renderer._worker_raster("c")
            self.assertEqual(list(self.renderer._rasters), ["a", "c"])
            self.assertEqual(load.call_count, 3)
            self.assertEqual(self.renderer._raster_bytes, 32)
            self.renderer.RASTER_CACHE_BYTES = 8
            self.renderer._worker_raster("oversize")
            self.assertNotIn("oversize", self.renderer._rasters)

    def test_raster_byte_limit_evicts_before_entry_limit(self):
        self.renderer.RASTER_CACHE_ENTRIES = 8
        self.renderer.RASTER_CACHE_BYTES = 16
        chart = SimpleNamespace(surface=cairo.ImageSurface(cairo.FORMAT_ARGB32, 2, 2))
        with patch(
            "gweatherrouting.rendering.quiltrenderer.GDALSingleRasterChart",
            return_value=chart,
        ):
            self.renderer._worker_raster("a")
            self.renderer._worker_raster("b")
        self.assertEqual(list(self.renderer._rasters), ["b"])
        self.assertEqual(self.renderer._raster_bytes, 16)

    def test_adopting_a_prefetched_frame_advances_publication_revision(self):
        self.renderer.draw(self.canvas, self.cr, (self.source,), "cm93")
        state = self.renderer._states[self.canvas]
        job = state.requested
        image = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, job.viewport.width, job.viewport.height
        )
        self.renderer._completed(self.canvas, job, image, None)
        revision = self.renderer.frame_revision(self.canvas)
        prepared = RenderJob(
            job.key, job.viewport, job.charts, 1, "cm93", speculative=True
        )
        state.prefetched = [(prepared, image)]
        self.renderer.draw(self.canvas, self.cr, (self.source,), "cm93")
        self.assertGreater(self.renderer.frame_revision(self.canvas), revision)
        self.assertIs(state.frame[0], prepared)

    def test_snapshot_uses_per_file_scale_and_copies_mutable_metadata(self):
        chart = GDALRasterChart(
            "/charts",
            None,
            metadata=[
                ("a", [[48, -123], [47, -122]], 2, 2, 5000),
                ("b", [[48, -123], [47, -122]], 2, 2, 50000),
            ],
        )
        base, sources = describe_charts([chart])
        self.assertFalse(base)
        self.assertEqual([source.native_scale for source in sources], [5000, 50000])
        chart.metadata[0][1][0][0] = 0
        self.assertEqual(sources[0].bbox[0][0], 48)
        chart.enabled = False
        self.assertEqual(describe_charts([chart]), ([], ()))

    def test_collection_decodes_raster_only_on_background_worker(self):
        canvas = MapCanvas()
        scheduler = QueuedScheduler()
        manager = ChartCollection(Mock(), schedule=scheduler)
        manager._palette = "cm93"
        manager._chs_renderer.draw = Mock()
        chart = GDALRasterChart(
            "/charts", None, metadata=[("private", [[48, -123], [47, -122]], 2, 2, 0)]
        )
        chart.do_draw = Mock(side_effect=AssertionError("GUI raster loader invoked"))
        manager.charts = [chart]
        started, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.addCleanup(release.set)
        self.addCleanup(manager.forget_map, canvas)
        threads = []

        def decode(path):
            threads.append(threading.get_ident())
            started.set()
            release.wait(2)
            finished.set()
            return SimpleNamespace(do_draw=lambda viewport, cr: None)

        manager._quilt_renderer._worker_raster = decode
        manager.do_draw(canvas, self.cr)
        self.assertTrue(started.wait(1))
        self.assertNotEqual(threads[0], threading.get_ident())
        chart.do_draw.assert_not_called()
        release.set()
        self.assertTrue(finished.wait(1))
