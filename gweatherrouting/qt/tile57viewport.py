"""Opt-in Tile57 C-ABI chart renderer for the Qt viewport prototype.

This prototype consumes a Tile57 *baked library* (the output of ``tile57 bake``)
and a dynamically loadable build of ``libtile57``.  The upstream releases
currently ship a static archive, so callers must provide a shared build.  It
renders through Tile57's native composed-PNG API on the chart worker; it does
not yet upload Tile57 GPU scenes to Qt's scene graph.
"""

import ctypes
import ctypes.util
from dataclasses import dataclass
import hashlib
import io
import json
import os
import time
from pathlib import Path

import cairo


STATE_DISABLED = "disabled"
STATE_READY = "ready"


@dataclass(frozen=True)
class _Cell:
    path: str
    enabled: bool

    @property
    def name(self):
        return Path(self.path).stem.upper()


class Tile57Unavailable(RuntimeError):
    """Tile57 was selected but its library or baked charts are unavailable."""


class _Error(ctypes.Structure):
    _fields_ = [("status", ctypes.c_int), ("message", ctypes.c_char * 256)]


def _library_path():
    configured = os.environ.get("GWR_TILE57_LIBRARY")
    if configured:
        return configured
    return ctypes.util.find_library("tile57")


def _load_library(path=None):
    path = path or _library_path()
    if not path:
        raise Tile57Unavailable(
            "Tile57 selected, but no shared libtile57 was found; "
            "set GWR_TILE57_LIBRARY"
        )
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise Tile57Unavailable(f"Cannot load libtile57 at {path}: {exc}") from exc

    lib.tile57_chart_open.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_Error),
    ]
    lib.tile57_chart_open.restype = ctypes.c_int
    lib.tile57_chart_close.argtypes = [ctypes.c_void_p]
    lib.tile57_chart_close.restype = None
    lib.tile57_compose_open.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_Error),
    ]
    lib.tile57_compose_open.restype = ctypes.c_int
    lib.tile57_compose_close.argtypes = [ctypes.c_void_p]
    lib.tile57_compose_close.restype = None
    lib.tile57_compose_png.argtypes = [
        ctypes.c_void_p,
        ctypes.c_double,
        ctypes.c_double,
        ctypes.c_double,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(_Error),
    ]
    lib.tile57_compose_png.restype = ctypes.c_int
    lib.tile57_free.argtypes = [ctypes.c_void_p]
    lib.tile57_free.restype = None
    return lib


class Tile57ViewportRenderer:
    """Render enabled ENC cells through Tile57 on the viewport's chart worker.

    Set ``GWR_ENC_RENDERER=tile57`` to opt in and
    ``GWR_TILE57_BAKED_DIR`` to the directory produced by ``tile57 bake``.
    ``GWR_TILE57_FORCE_ENC=1`` temporarily overrides the chart-ENC visibility
    switch for this experiment without changing the user's settings file. The
    ordinary renderer remains the default.
    """

    def __init__(self, data_dir, baked_dir=None, library=None):
        self._data_dir = Path(data_dir) if data_dir is not None else None
        baked_dir = baked_dir or os.environ.get("GWR_TILE57_BAKED_DIR")
        if not baked_dir:
            raise Tile57Unavailable(
                "Tile57 baked chart directory is not configured; "
                "set GWR_TILE57_BAKED_DIR"
            )
        self._baked_dir = Path(baked_dir)
        self._lib = library or _load_library()
        self._charts = ()
        self._compose = None
        self._signature = None

    def render(self, viewport, factor=1):
        """Return a Cairo chart surface and source metadata for this view."""
        class_enabled, enc_cells = self._read_manifest()
        force_enabled = os.environ.get("GWR_TILE57_FORCE_ENC") == "1"
        enc_enabled = class_enabled or force_enabled
        metadata = {
            "enabled": enc_enabled,
            "state": STATE_DISABLED,
            "reason": "",
            "error": "",
            "source_ids": (),
            "disabled_ids": tuple(cell.name for cell in enc_cells if not cell.enabled),
            "selected": (),
            "coverage": (),
            "coverage_status": "not reported by Tile57's PNG API",
            "failed": (),
            "factor": factor,
            "generation": "",
            "bytes": 0,
            "backend": "tile57-c-api-png",
            "palette": "Tile57 default day palette",
            "visibility_override": force_enabled and not class_enabled,
        }
        if not enc_enabled:
            return None, self._finish_metadata(metadata)
        sources = tuple(cell for cell in enc_cells if cell.enabled)
        if not sources:
            metadata["enabled"] = False
            metadata["state"] = STATE_DISABLED
            metadata["reason"] = "No ENC chart is enabled"
            return None, self._finish_metadata(metadata)

        if not self._baked_dir.is_dir():
            raise Tile57Unavailable(
                "Tile57 baked chart directory is missing; set GWR_TILE57_BAKED_DIR"
            )
        archives = self._resolve_archives(sources)
        self._adopt_archives(archives)
        metadata["source_ids"] = tuple(source.name for source in sources)
        metadata["generation"] = hashlib.sha256(
            repr(
                tuple(
                    (cell.path, cell.enabled, self._file_version(cell.path))
                    for cell in enc_cells
                )
            ).encode("utf-8")
        ).hexdigest()
        width = int(viewport.width * factor)
        height = int(viewport.height * factor)
        output = ctypes.POINTER(ctypes.c_uint8)()
        output_len = ctypes.c_size_t()
        error = _Error()
        started = time.perf_counter()
        status = self._lib.tile57_compose_png(
            self._compose,
            float(viewport._center_lon),
            float(viewport._center_lat),
            float(viewport._zoom),
            width,
            height,
            None,
            ctypes.byref(output),
            ctypes.byref(output_len),
            ctypes.byref(error),
        )
        metadata["native_render_ms"] = (time.perf_counter() - started) * 1000.0
        if status != 0:
            raise Tile57Unavailable(
                error.message.decode("utf-8", errors="replace")
                or f"Tile57 render failed with status {status}"
            )
        try:
            png = ctypes.string_at(output, output_len.value)
        finally:
            if output:
                self._lib.tile57_free(output)
        decode_started = time.perf_counter()
        surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
        metadata["png_decode_ms"] = (time.perf_counter() - decode_started) * 1000.0
        surface.set_device_scale(factor, factor)
        surface.flush()
        metadata.update(
            state=STATE_READY,
        )
        return surface, self._finish_metadata(metadata)

    @staticmethod
    def _finish_metadata(metadata):
        metadata["bytes"] = len(
            json.dumps(metadata, sort_keys=True, default=str).encode("utf-8")
        )
        return metadata

    def _read_manifest(self):
        """Read only this source's settings; never import app controllers."""
        if self._data_dir is None:
            raise Tile57Unavailable("Application data directory is unavailable")
        path = self._data_dir / "settings.json"
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            raise Tile57Unavailable(
                f"Cannot read chart settings {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise Tile57Unavailable(f"Unexpected chart settings in {path}")
        cells = []
        for entry in raw.get("vectorCharts") or ():
            if not isinstance(entry, dict):
                continue
            chart_path = entry.get("path")
            if isinstance(chart_path, str) and ".000" in chart_path:
                cells.append(_Cell(chart_path, bool(entry.get("enabled", True))))
        return bool(raw.get("chartENCVisible", True)), tuple(cells)

    @staticmethod
    def _file_version(path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def release(self):
        """Release native handles on the chart worker that owns them."""
        self._close_composition()

    def _resolve_archives(self, sources):
        candidates = {}
        for path in self._baked_dir.rglob("*.pmtiles"):
            candidates.setdefault(path.stem.upper(), []).append(path)
        resolved = []
        for source in sources:
            matches = candidates.get(source.name, ())
            if len(matches) != 1:
                raise Tile57Unavailable(
                    f"Expected one baked Tile57 archive for {source.name}; "
                    f"found {len(matches)}"
                )
            resolved.append(matches[0])
        return tuple(resolved)

    def _adopt_archives(self, archives):
        signature = tuple(
            (str(path), path.stat().st_size, path.stat().st_mtime_ns)
            for path in archives
        )
        if signature == self._signature:
            return
        self._close_composition()
        opened = []
        error = _Error()
        try:
            for path in archives:
                chart = ctypes.c_void_p()
                status = self._lib.tile57_chart_open(
                    os.fsencode(path), ctypes.byref(chart), ctypes.byref(error)
                )
                if status != 0:
                    raise Tile57Unavailable(
                        error.message.decode("utf-8", errors="replace")
                        or f"Tile57 could not open {path} (status {status})"
                    )
                opened.append(chart)
            chart_array = (ctypes.c_void_p * len(opened))(
                *(chart.value for chart in opened)
            )
            compose = ctypes.c_void_p()
            status = self._lib.tile57_compose_open(
                chart_array,
                len(opened),
                ctypes.byref(compose),
                ctypes.byref(error),
            )
            if status != 0:
                raise Tile57Unavailable(
                    error.message.decode("utf-8", errors="replace")
                    or f"Tile57 chart composition failed (status {status})"
                )
        except Exception:
            for chart in reversed(opened):
                self._lib.tile57_chart_close(chart)
            raise
        self._charts = tuple(opened)
        self._compose = compose
        self._signature = signature

    def _close_composition(self):
        if self._compose:
            self._lib.tile57_compose_close(self._compose)
            self._compose = None
        for chart in reversed(self._charts):
            self._lib.tile57_chart_close(chart)
        self._charts = ()
        self._signature = None
