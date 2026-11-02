"""The saved viewport camera must survive startup data refreshes."""

from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QObject

from gweatherrouting.qt.window import PreviewWindow


class MockTrackSource(QObject):
    def __init__(self):
        super().__init__()
        self.clear = Mock()
        self.addItem = Mock()
        self.currentData = Mock()
        self.setEnabled = Mock()


def make_window(point_offsets):
    window = Mock()
    window.track_source = MockTrackSource()
    log = SimpleNamespace(
        source_tracks={},
        devices={},
        point_offsets=point_offsets,
        numeric_series=(),
        duration=0.0,
        points=[],
    )
    window.session = SimpleNamespace(
        source="log", log=log, loading=False, _generation=0, display_position=0.0
    )
    window.controller.connections.get_status.return_value = []
    window.controller.connections.polling_active = False
    return window


def test_initial_data_refresh_does_not_fit_or_switch_from_saved_view():
    window = make_window([])
    window.controller.points = [SimpleNamespace(lat=90, lon=-180)]
    window.data_key = None
    PreviewWindow._refresh_data(window)
    window.map.set_points.assert_called_once_with(
        window.controller.points, [], fit=False
    )
    window.log_map.set_points.assert_called_once_with(
        window.controller.points, [], fit=False
    )
    window.views.setCurrentWidget.assert_not_called()


def test_deliberate_source_switch_still_fits_track():
    window = make_window([0])
    log = window.session.log
    window.controller.points = [SimpleNamespace(lat=48.4, lon=-123.4)]
    window.data_key = ("live", 1)
    PreviewWindow._refresh_data(window)
    window.map.set_points.assert_called_once_with(
        window.controller.points, log.point_offsets, fit=True
    )
    window.views.setCurrentWidget.assert_called_once_with(window.logs_view)
