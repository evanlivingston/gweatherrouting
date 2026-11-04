"""Focused tests for the opt-in Tile57 viewport prototype."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gweatherrouting.qt import tile57viewport


def test_tile57_missing_shared_library_is_explicit(monkeypatch):
    monkeypatch.delenv("GWR_TILE57_LIBRARY", raising=False)
    monkeypatch.setattr(tile57viewport.ctypes.util, "find_library", lambda _name: None)

    with pytest.raises(tile57viewport.Tile57Unavailable, match="GWR_TILE57_LIBRARY"):
        tile57viewport._load_library()


def test_tile57_missing_bake_directory_is_explicit(monkeypatch):
    monkeypatch.delenv("GWR_TILE57_BAKED_DIR", raising=False)

    with pytest.raises(tile57viewport.Tile57Unavailable, match="GWR_TILE57_BAKED_DIR"):
        tile57viewport.Tile57ViewportRenderer("/unused", library=object())


def test_tile57_force_visibility_is_session_only(monkeypatch, tmp_path):
    (tmp_path / "settings.json").write_text(
        '{"chartENCVisible": false, "vectorCharts": []}'
    )
    monkeypatch.setenv("GWR_TILE57_FORCE_ENC", "1")
    renderer = tile57viewport.Tile57ViewportRenderer(
        tmp_path, baked_dir=tmp_path, library=object()
    )

    surface, metadata = renderer.render(None)

    assert surface is None
    assert metadata["enabled"] is False  # No enabled cells exist.
    assert metadata["visibility_override"] is True


def test_tile57_archive_selection_follows_enabled_enc_cells(tmp_path):
    charts = tmp_path / "tiles"
    charts.mkdir()
    coarse = charts / "US4ABC01.pmtiles"
    harbor = charts / "US5ABC01.pmtiles"
    coarse.touch()
    harbor.touch()
    renderer = tile57viewport.Tile57ViewportRenderer.__new__(
        tile57viewport.Tile57ViewportRenderer
    )
    renderer._baked_dir = tmp_path

    selected = renderer._resolve_archives(
        (SimpleNamespace(name="US5ABC01"), SimpleNamespace(name="US4ABC01"))
    )

    assert selected == (harbor, coarse)


def test_tile57_rejects_missing_or_ambiguous_archive(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "US5ABC01.pmtiles").touch()
    (tmp_path / "b" / "US5ABC01.pmtiles").touch()
    renderer = tile57viewport.Tile57ViewportRenderer.__new__(
        tile57viewport.Tile57ViewportRenderer
    )
    renderer._baked_dir = Path(tmp_path)

    with pytest.raises(tile57viewport.Tile57Unavailable, match="found 2"):
        renderer._resolve_archives((SimpleNamespace(name="US5ABC01"),))
    with pytest.raises(tile57viewport.Tile57Unavailable, match="found 0"):
        renderer._resolve_archives((SimpleNamespace(name="US5ABC02"),))
