"""Durable prepared ENC cells (P1 of the retained ENC architecture port).

A cell is decoded once into a versioned SQLite artifact and is then rebuilt as
worker-local OGR memory layers.  A warm chart reads the artifact and never
re-opens the GDAL source, while the artifact identity still tracks the source
bytes so a changed cell is rebuilt instead of served stale.

Scope is the P1 slice only:

* one cell, one artifact, one Cairo adapter (``GDALVectorChart`` plus an
  ``S57ChartDrawer`` bound to a palette);
* attributes, field definitions, nulls, unset fields, lists, sounding Z,
  geometry topology, layer/feature/FID order and exact coverage are
  preserved;
* the trial limits are 64 MiB of accounted feature payload per cell and
  256 MiB of retained disk cache, staging included.

The adapter still simplifies geometry per view.  That cost is reported, not
claimed solved: retained geometry and GPU residency are P2/P3 work.

Public API
-----------

``PreparedCellStore(cache_dir, cell_byte_limit=64MiB, disk_byte_limit=256MiB)``
    Owns the cache directory, the declared limits and the cross-process lock.

``store.open_chart(path, palette, cancel=None, opener=None)``
    Returns a ``GDALVectorChart`` instance whose ``vector_file`` is an OGR
    in-memory dataset rebuilt from the prepared artifact and whose ``drawer`` is
    an ``S57ChartDrawer`` bound to ``palette``.  The chart renders immediately:
    there is no drawer callback to attach and no GUI object is constructed.

    ``opener`` receives the chart path and returns a GDAL dataset.  It is
    called on the cold path only: a warm load never decodes the source.  The
    opener owns the dataset it returns; the store does not release it.

``PreparedCellError`` and its subclasses
    ``PreparedCellLimitError``, ``PreparedCellCanceledError``,
    ``PreparedCellSourceChangedError`` and ``PreparedCellCorruptError``
    report why a preparation failed: a limit that was exceeded, a caller
    that cancelled, a source that changed and an artifact that has to be
    rebuilt.
"""

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from osgeo import gdal, ogr, osr

from gweatherrouting.rendering.chartlayer import ChartLayer
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.style import Style
from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

try:  # pragma: no cover - Windows has no flock(2).
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - the Windows byte-range lock.
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

try:  # pragma: no cover - resource is Unix only.
    import resource
except ImportError:  # pragma: no cover
    resource = None

logger = logging.getLogger("gweatherrouting")

#: Version of the on-disk artifact layout.  A change invalidates every artifact.
SCHEMA_VERSION = 1
#: Marker stored in every artifact so foreign files are never misread.
FORMAT_NAME = "enc-prepared-cell"

DEFAULT_CELL_BYTE_LIMIT = 64 * 1024 * 1024
DEFAULT_DISK_BYTE_LIMIT = 256 * 1024 * 1024

#: Ceiling for one feature's serialized payload (geometry plus attributes).
MAX_FEATURE_PAYLOAD = 16 * 1024 * 1024
#: Ceiling for one attribute value.
MAX_ATTRIBUTE_BYTES = 4 * 1024 * 1024
#: Ceiling for one layer's field definition metadata.
MAX_FIELD_DEFS_BYTES = 1024 * 1024
#: Ceiling for the SQLite page cache used while staging.
SQLITE_CACHE_PAGES = 4096
#: The page size of a staged artifact.
SQLITE_PAGE_SIZE = 4096

_ARTIFACT_SUFFIX = ".sqlite"
_ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}" + re.escape(_ARTIFACT_SUFFIX) + r"$")
_LOCK_NAME = ".prepared-cell.lock"
_STAGING_PREFIX = ".staging-"

#: The on-disk layout of an artifact.
_ARTIFACT_SCHEMA = """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE layers (
        layer_id INTEGER PRIMARY KEY,
        ord INTEGER NOT NULL,
        name TEXT NOT NULL,
        geom_type INTEGER NOT NULL,
        geom_fields INTEGER NOT NULL,
        field_defs TEXT NOT NULL,
        srs_wkt TEXT NOT NULL,
        feature_count INTEGER NOT NULL,
        fids_preserved INTEGER NOT NULL
    );
    CREATE TABLE features (
        feature_id INTEGER PRIMARY KEY,
        layer_id INTEGER NOT NULL,
        ord INTEGER NOT NULL,
        fid INTEGER,
        wkb BLOB,
        attrs TEXT NOT NULL,
        minx REAL,
        miny REAL,
        maxx REAL,
        maxy REAL
    );
    CREATE INDEX features_layer_idx ON features (layer_id, ord);
    CREATE INDEX features_bbox_idx ON features (layer_id, minx, maxx, miny, maxy);
"""


class PreparedCellError(Exception):
    """Base class for prepared-cell failures."""


class PreparedCellLimitError(PreparedCellError):
    """A declared payload, row or disk limit was exceeded."""


class PreparedCellCanceledError(PreparedCellError):
    """The caller cancelled preparation."""


class PreparedCellSourceChangedError(PreparedCellError):
    """The source changed while the cell was being prepared."""


class PreparedCellCorruptError(PreparedCellError):
    """A prepared artifact failed validation and has to be rebuilt."""


#: The names the errors carried before they were renamed.  They are public
#: API, so a caller that catches them keeps working.
PreparedCellCanceled = PreparedCellCanceledError
PreparedCellSourceChanged = PreparedCellSourceChangedError


# ---------------------------------------------------------------------------
# Source identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceComponent:
    """One file that takes part in the identity of a cell."""

    name: str
    number: int
    size: int
    digest: str


@dataclass(frozen=True)
class SourceIdentity:
    """Identity of a cell source: content, decoder and update semantics."""

    path: str
    digest: str
    components: Tuple[SourceComponent, ...]
    applied_updates: Tuple[int, ...]
    gaps: Tuple[int, ...]
    decoder: Dict[str, object]


def _feed(digest, label: str, value: bytes) -> None:
    """Add one length-prefixed, labelled chunk to a running digest."""
    digest.update(label.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(len(value)).encode("ascii"))
    digest.update(b"\x00")
    digest.update(value)
    digest.update(b"\x00")


def _file_digest(path: str) -> Tuple[int, str]:
    """Return (size, sha256) of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _decoder_signature() -> Dict[str, object]:
    """Describe the decoder that produced (or will produce) the content.

    The GDAL release and every S57 related configuration option change how a
    cell decodes, so both belong to the identity of a prepared cell.
    """
    options: Dict[str, str] = {}
    try:
        configured = gdal.GetConfigOptions() or {}
    except Exception:  # pragma: no cover - defensive, bindings vary.
        configured = {}
    for name, value in configured.items():
        upper = name.upper()
        if upper.startswith("S57_") or upper.startswith("OGR_S57"):
            options[upper] = str(value)
    options.setdefault(
        "OGR_S57_OPTIONS", gdal.GetConfigOption("OGR_S57_OPTIONS", "") or ""
    )
    return {
        "gdal_release": gdal.VersionInfo("RELEASE_NAME") or "",
        "gdal_version_num": gdal.VersionInfo("VERSION_NUM") or "",
        "options": options,
    }


def _component_numbers(path: str) -> Tuple[str, str, Optional[int], int]:
    """Split a chart path into (directory, stem, base number, suffix width).

    The suffix width is the width of the *written* suffix, not the width of
    its number: a base chart called ``chart.000`` has the number 0 and the
    width 3, so its update siblings are ``chart.001``, ``chart.002`` and so
    on.  Deriving the width from ``str(number)`` would look for one-digit
    siblings only and would silently ignore every update.
    """
    directory = os.path.dirname(path) or "."
    filename = os.path.basename(path)
    stem, extension = os.path.splitext(filename)
    suffix = extension[1:] if extension.startswith(".") else ""
    if suffix.isdigit():
        return directory, stem, int(suffix), len(suffix)
    return directory, stem, None, 0


def _collect_components(
    path: str,
) -> Tuple[List[SourceComponent], Tuple[int, ...], Tuple[int, ...]]:
    """Collect the base chart and its numbered update siblings.

    ``applied_updates`` lists the contiguous update numbers a decoder applies
    after the base; ``gaps`` lists update files that exist but sit behind a
    missing number.  Both take part in the identity, so a filled gap, a new
    update or a deleted update always invalidates the artifact.
    """
    directory, stem, base_number, width = _component_numbers(path)
    if base_number is None:
        size, digest = _file_digest(path)
        return [SourceComponent(os.path.basename(path), 0, size, digest)], (), ()

    numbers: Dict[int, str] = {}
    try:
        entries = os.listdir(directory)
    except OSError as error:
        raise PreparedCellError(
            "cannot list chart directory %s: %s" % (directory, error)
        )
    prefix = stem + "."
    for entry in entries:
        if not entry.startswith(prefix):
            continue
        tail = entry[len(prefix) :]
        if len(tail) != width or not tail.isdigit():
            continue
        numbers[int(tail)] = os.path.join(directory, entry)
    numbers[base_number] = path

    components: List[SourceComponent] = []
    for number in sorted(numbers):
        full = numbers[number]
        size, digest = _file_digest(full)
        components.append(SourceComponent(os.path.basename(full), number, size, digest))

    applied: List[int] = []
    candidate = base_number + 1
    while candidate in numbers:
        applied.append(candidate)
        candidate += 1
    gaps = tuple(sorted(set(numbers) - set(applied) - {base_number}))
    return components, tuple(applied), gaps


def source_identity(path: str) -> SourceIdentity:
    """Compute the identity of a chart source.

    The identity covers the ordered content digests of the base chart and every
    numbered update sibling, the GDAL release, the S57 configuration options
    and the artifact schema.  The palette is deliberately absent: a palette
    changes how a cell is drawn, never what it contains.
    """
    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(resolved):
        raise PreparedCellError("chart source not found: %s" % resolved)
    components, applied, gaps = _collect_components(resolved)
    decoder = _decoder_signature()

    digest = hashlib.sha256()
    _feed(digest, "format", FORMAT_NAME.encode("utf-8"))
    _feed(digest, "schema", str(SCHEMA_VERSION).encode("ascii"))
    _feed(digest, "decoder", json.dumps(decoder, sort_keys=True).encode("utf-8"))
    for component in components:
        _feed(digest, "component", component.name.encode("utf-8"))
        _feed(digest, "number", str(component.number).encode("ascii"))
        _feed(digest, "size", str(component.size).encode("ascii"))
        _feed(digest, "digest", component.digest.encode("ascii"))
    _feed(
        digest, "applied", ",".join(str(number) for number in applied).encode("ascii")
    )
    _feed(digest, "gaps", ",".join(str(number) for number in gaps).encode("ascii"))
    return SourceIdentity(
        path=resolved,
        digest=digest.hexdigest(),
        components=tuple(components),
        applied_updates=applied,
        gaps=gaps,
        decoder=decoder,
    )


# ---------------------------------------------------------------------------
# Field and value handling
# ---------------------------------------------------------------------------

#: The field types a prepared cell can rebuild.  They are exactly the types
#: the OGR in-memory driver can recreate: a type the memory layer cannot hold
#: would silently change the chart, so it is rejected at build time instead.
_SUPPORTED_FIELD_TYPES = frozenset(
    {
        ogr.OFTInteger,
        ogr.OFTInteger64,
        ogr.OFTIntegerList,
        ogr.OFTInteger64List,
        ogr.OFTReal,
        ogr.OFTRealList,
        ogr.OFTString,
        ogr.OFTStringList,
        ogr.OFTBinary,
        ogr.OFTDate,
        ogr.OFTTime,
        ogr.OFTDateTime,
    }
)

_FIELD_TYPE_NAMES = {
    ogr.OFTInteger: "Integer",
    ogr.OFTInteger64: "Integer64",
    ogr.OFTIntegerList: "IntegerList",
    ogr.OFTInteger64List: "Integer64List",
    ogr.OFTReal: "Real",
    ogr.OFTRealList: "RealList",
    ogr.OFTString: "String",
    ogr.OFTStringList: "StringList",
    ogr.OFTBinary: "Binary",
    ogr.OFTDate: "Date",
    ogr.OFTTime: "Time",
    ogr.OFTDateTime: "DateTime",
}


def _field_type_name(field_type: int) -> str:
    """Human readable name of an OGR field type."""
    return _FIELD_TYPE_NAMES.get(field_type, "Type(%d)" % field_type)


def _as_int(value: Any) -> int:
    """An integer payload."""
    return int(value)


def _as_int_list(values: Any) -> List[int]:
    """A list of integer payloads."""
    return [int(item) for item in values]


def _as_float(value: Any) -> float:
    """A double payload, restored to its exact binary value."""
    return float.fromhex(value)


def _as_float_list(values: Any) -> List[float]:
    """A list of double payloads, restored to their exact binary values."""
    return [float.fromhex(item) for item in values]


def _as_text(value: Any) -> str:
    """A text payload."""
    return str(value)


def _as_text_list(values: Any) -> List[str]:
    """A list of text payloads."""
    return [str(item) for item in values]


def _date_payload(feature, index: int) -> Tuple[str, List[Any]]:
    """The text and the parts of a date, a time or a date-time field."""
    parts = feature.GetFieldAsDateTime(index) or ()
    return (
        feature.GetFieldAsString(index),
        [value for value in parts],
    )


def _encode_binary(feature, index: int) -> List[str]:
    """Capture a binary field."""
    raw = bytes(feature.GetFieldAsBinary(index) or b"")
    return [base64.b64encode(raw).decode("ascii")]


def _encode_integer_list(feature, index: int) -> List[List[int]]:
    """Capture an integer list field."""
    return [[int(item) for item in feature.GetFieldAsIntegerList(index) or []]]


def _encode_integer64_list(feature, index: int) -> List[List[int]]:
    """Capture a wide integer list field."""
    return [[int(item) for item in feature.GetFieldAsInteger64List(index) or []]]


def _encode_real_list(feature, index: int) -> List[List[str]]:
    """Capture a double list field, keeping the exact binary values."""
    return [[float.hex(value) for value in feature.GetFieldAsDoubleList(index) or []]]


def _encode_string_list(feature, index: int) -> List[List[str]]:
    """Capture a text list field."""
    return [[str(item) for item in feature.GetFieldAsStringList(index) or []]]


def _restore_integer(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore an integer field."""
    feature.SetField(index, _as_int(encoded[1]))


def _restore_integer64(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore a wide integer field."""
    feature.SetFieldInteger64(index, _as_int(encoded[1]))


def _restore_real(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore a double field, keeping its exact binary value."""
    feature.SetField(index, _as_float(encoded[1]))


def _restore_string(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore a text field."""
    feature.SetField(index, _as_text(encoded[1]))


def _restore_binary(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore a binary field."""
    feature.SetFieldBinary(index, base64.b64decode(encoded[1]))


def _restore_date(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore a date, a time or a date-time field."""
    feature.SetField(index, _as_text(encoded[1]))


def _restore_integer_list(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore an integer list field."""
    feature.SetFieldIntegerList(index, _as_int_list(encoded[1]))


def _restore_integer64_list(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore a wide integer list field."""
    feature.SetFieldInteger64List(index, _as_int_list(encoded[1]))


def _restore_real_list(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore a double list field, keeping the exact binary values."""
    feature.SetFieldDoubleList(index, _as_float_list(encoded[1]))


def _restore_string_list(feature, index: int, encoded: Sequence[Any]) -> None:
    """Restore a text list field."""
    feature.SetFieldStringList(index, _as_text_list(encoded[1]))


@dataclass(frozen=True)
class _FieldCodec:
    """How one OGR field type is encoded, restored and read back.

    A codec carries the tag written into the artifact, the encoder that
    captures a source value, the decoder that turns a tagged payload back
    into the value a rebuilt feature must read, the reader that fetches a
    rebuilt value and the setter that restores a tagged value.
    """

    tag: str
    encode: Callable[[Any, int], List[Any]]
    decode: Callable[[Sequence[Any]], Any]
    get: Callable[[Any, int], Any]
    restore: Callable[[Any, int, Sequence[Any]], None]


#: The codecs of the OGR field types a prepared cell supports.  One codec
#: serves one type, so the encoder, the reader and the setter of a type
#: always agree and a value can never be captured with one type and rebuilt
#: with another.
_CODECS: Dict[str, _FieldCodec] = {
    "int": _FieldCodec(
        tag="int",
        encode=lambda feature, index: [int(feature.GetFieldAsInteger(index))],
        decode=lambda encoded: _as_int(encoded[1]),
        get=lambda feature, index: feature.GetFieldAsInteger(index) or 0,
        restore=_restore_integer,
    ),
    "i64": _FieldCodec(
        tag="i64",
        encode=lambda feature, index: [int(feature.GetFieldAsInteger64(index))],
        decode=lambda encoded: _as_int(encoded[1]),
        get=lambda feature, index: feature.GetFieldAsInteger64(index) or 0,
        restore=_restore_integer64,
    ),
    "real": _FieldCodec(
        tag="real",
        encode=lambda feature, index: [float.hex(feature.GetFieldAsDouble(index))],
        decode=lambda encoded: _as_float(encoded[1]),
        get=lambda feature, index: feature.GetFieldAsDouble(index) or 0.0,
        restore=_restore_real,
    ),
    "str": _FieldCodec(
        tag="str",
        encode=lambda feature, index: [feature.GetFieldAsString(index)],
        decode=lambda encoded: _as_text(encoded[1]),
        get=lambda feature, index: feature.GetFieldAsString(index) or "",
        restore=_restore_string,
    ),
    "bin": _FieldCodec(
        tag="bin",
        encode=_encode_binary,
        decode=lambda encoded: base64.b64decode(encoded[1]),
        get=lambda feature, index: bytes(feature.GetFieldAsBinary(index) or b""),
        restore=_restore_binary,
    ),
    "date": _FieldCodec(
        tag="date",
        encode=lambda feature, index: list(_date_payload(feature, index)),
        decode=lambda encoded: (_as_text(encoded[1]), list(encoded[2])),
        get=_date_payload,
        restore=_restore_date,
    ),
    "ilist": _FieldCodec(
        tag="ilist",
        encode=_encode_integer_list,
        decode=lambda encoded: _as_int_list(encoded[1]),
        get=lambda feature, index: list(feature.GetFieldAsIntegerList(index) or []),
        restore=_restore_integer_list,
    ),
    "i64list": _FieldCodec(
        tag="i64list",
        encode=_encode_integer64_list,
        decode=lambda encoded: _as_int_list(encoded[1]),
        get=lambda feature, index: list(feature.GetFieldAsInteger64List(index) or []),
        restore=_restore_integer64_list,
    ),
    "dlist": _FieldCodec(
        tag="dlist",
        encode=_encode_real_list,
        decode=lambda encoded: _as_float_list(encoded[1]),
        get=lambda feature, index: list(feature.GetFieldAsDoubleList(index) or []),
        restore=_restore_real_list,
    ),
    "slist": _FieldCodec(
        tag="slist",
        encode=_encode_string_list,
        decode=lambda encoded: _as_text_list(encoded[1]),
        get=lambda feature, index: list(feature.GetFieldAsStringList(index) or []),
        restore=_restore_string_list,
    ),
}

#: The codec of every OGR field type a prepared cell supports.
_FIELD_CODECS: Dict[int, _FieldCodec] = {
    ogr.OFTInteger: _CODECS["int"],
    ogr.OFTInteger64: _CODECS["i64"],
    ogr.OFTReal: _CODECS["real"],
    ogr.OFTString: _CODECS["str"],
    ogr.OFTBinary: _CODECS["bin"],
    ogr.OFTDate: _CODECS["date"],
    ogr.OFTTime: _CODECS["date"],
    ogr.OFTDateTime: _CODECS["date"],
    ogr.OFTIntegerList: _CODECS["ilist"],
    ogr.OFTInteger64List: _CODECS["i64list"],
    ogr.OFTRealList: _CODECS["dlist"],
    ogr.OFTStringList: _CODECS["slist"],
}


def _codec_for_tag(tag: object) -> Optional[_FieldCodec]:
    """The codec of a tagged value, or None when the tag is unknown."""
    if not isinstance(tag, str):
        return None
    return _CODECS.get(tag)


def _encode_value(feature, index: int, field_type: int) -> List[object]:
    """Encode one field value losslessly as a tagged JSON value.

    Null is distinct from an unset field, doubles keep their exact binary
    value, binary and date fields keep their payload and integer lists keep
    their element type.  A date or time keeps both its canonical OGR text
    (which carries the fraction of a second and the time zone) and the parts
    OGR reports, so a rebuild can be checked against the source value.
    """
    if feature.IsFieldNull(index):
        return ["null", None]
    if not feature.IsFieldSet(index):
        return ["unset", None]
    codec = _FIELD_CODECS.get(field_type)
    if codec is None:
        raise PreparedCellError(
            "unsupported OGR field type %s cannot be prepared"
            % _field_type_name(field_type)
        )
    encoded: List[object] = [codec.tag]
    encoded_values = codec.encode(feature, index)
    if encoded_values is None:
        raise PreparedCellError(
            "encoder for field type %s returned None instead of a list"
            % _field_type_name(field_type)
        )
    encoded.extend(encoded_values)
    return encoded


def _expected_value(field_type: int, encoded: Sequence[object]):
    """The value a rebuilt feature must read back.

    The value follows the tag of the payload, not the type of the field: a
    payload that does not belong to its field is caught by the read back of
    the rebuilt feature.
    """
    tag = encoded[0]
    if tag in ("null", "unset"):
        return None
    codec = _codec_for_tag(tag)
    if codec is None:
        raise PreparedCellCorruptError("unknown encoded value tag %r" % (tag,))
    return codec.decode(encoded)


def _read_field(feature, index: int, field_type: int):
    """Read one field back with the getter that matches its type."""
    codec = _FIELD_CODECS.get(field_type)
    if codec is None:
        raise PreparedCellError(
            "unsupported OGR field type %s cannot be prepared"
            % _field_type_name(field_type)
        )
    return codec.get(feature, index)


def _apply_empty_value(feature, index: int, tag: str) -> None:
    """Restore a null or an unset field and check that it took."""
    if tag == "null":
        feature.SetFieldNull(index)
        if not feature.IsFieldNull(index):
            raise PreparedCellCorruptError("field %d could not be set to null" % index)
        return
    feature.UnsetField(index)
    if feature.IsFieldSet(index):
        raise PreparedCellCorruptError("field %d could not be unset" % index)


def _restore_value(
    feature, index: int, field_type: int, encoded: Sequence[Any]
) -> None:
    """Restore one tagged value into a feature."""
    tag = encoded[0]
    if tag in ("null", "unset"):
        _apply_empty_value(feature, index, tag)
        return
    codec = _codec_for_tag(tag)
    if codec is None:
        raise PreparedCellCorruptError("unknown encoded value tag %r" % (tag,))
    try:
        codec.restore(feature, index, encoded)
    except PreparedCellError:
        raise
    except Exception as error:
        raise PreparedCellCorruptError(
            "field %d (%s) could not be rebuilt: %s"
            % (index, _field_type_name(field_type), error)
        ) from error


def _apply_value(
    feature, index: int, field_type: int, encoded: Sequence[object]
) -> None:
    """Set one field and verify that the value survived the round trip.

    The OGR setters do not report failures consistently across releases and a
    silently dropped value would corrupt the chart, so every value is read
    back and compared.  Null and unset fields are verified by their field
    state, not by reading a getter that returns a default for null.
    """
    if not isinstance(encoded, (list, tuple)) or not encoded:
        raise PreparedCellCorruptError("the prepared artifact holds a broken value")
    tag = encoded[0]
    if tag in ("null", "unset"):
        _restore_value(feature, index, field_type, encoded)
        if tag == "null":
            if not feature.IsFieldNull(index):
                raise PreparedCellCorruptError(
                    "field %d (%s) did not survive the prepared round trip: not null"
                    % (index, _field_type_name(field_type))
                )
        else:
            if feature.IsFieldSet(index):
                raise PreparedCellCorruptError(
                    "field %d (%s) did not survive the prepared round trip: not unset"
                    % (index, _field_type_name(field_type))
                )
        return
    _restore_value(feature, index, field_type, encoded)
    expected = _expected_value(field_type, encoded)
    actual = _read_field(feature, index, field_type)
    if actual != expected:
        raise PreparedCellCorruptError(
            "field %d (%s) did not survive the prepared round trip: %r != %r"
            % (index, _field_type_name(field_type), actual, expected)
        )


def _field_definitions(layer) -> List[Dict[str, Any]]:
    """Capture the field definitions of a layer."""
    definition = layer.GetLayerDefn()
    fields: List[Dict[str, object]] = []
    for index in range(definition.GetFieldCount()):
        field_defn = definition.GetFieldDefn(index)
        fields.append(
            {
                "name": field_defn.GetName(),
                "type": field_defn.GetType(),
                "subtype": field_defn.GetSubType(),
                "width": field_defn.GetWidth(),
                "precision": field_defn.GetPrecision(),
                "nullable": bool(field_defn.IsNullable()),
            }
        )
    return fields


def _check_field_types(fields: Sequence[Dict[str, object]], layer_name: str) -> None:
    """Reject unsupported field types instead of dropping their values."""
    for field_defn in fields:
        if field_defn["type"] not in _SUPPORTED_FIELD_TYPES:
            raise PreparedCellError(
                "layer %r field %r uses unsupported OGR type %s"
                % (layer_name, field_defn["name"], _field_type_name(field_defn["type"]))
            )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _check_cancel(cancel: Optional[Callable[[], bool]]) -> None:
    """Abort preparation when the caller cancels."""
    if cancel is not None and cancel():
        raise PreparedCellCanceledError("prepared cell preparation cancelled")


def _memory_driver():
    """Return the OGR in-memory driver, preferring the non-deprecated name."""
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        driver = ogr.GetDriverByName("MEM")
        if driver is None:
            driver = ogr.GetDriverByName("Memory")
    finally:
        gdal.PopErrorHandler()
    if driver is None:
        raise PreparedCellError("the OGR in-memory driver is unavailable")
    return driver


def _spatial_reference(wkt: str):
    """Rebuild a spatial reference from WKT, tolerating an unusable reference.

    The spatial reference lives in the OSR library, not in OGR: rebuilding it
    needs ``osr.SpatialReference`` and an explicit WKT import.
    """
    if not wkt:
        return None
    try:
        reference = osr.SpatialReference()
        if reference.ImportFromWkt(wkt) != 0:
            logger.debug("the prepared cell cannot restore the spatial reference")
            return None
        return reference
    except Exception as error:
        logger.debug(
            "the prepared cell cannot restore the spatial reference: %s", error
        )
        return None


def _release_dataset(dataset) -> None:
    """Release a GDAL dataset this process owns."""
    if dataset is None:
        return
    release = getattr(dataset, "Release", None)
    if release is None:
        return
    try:
        release()
    except Exception as error:  # pragma: no cover - defensive.
        logger.debug("cannot release a dataset: %s", error)


def _sync_directory(path: str) -> None:
    """Make a rename inside a directory durable."""
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - defensive.
        return
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - not every file system allows it.
        pass
    finally:
        os.close(handle)


def process_rss_bytes() -> int:
    """Peak resident set size of this process in bytes (informative only)."""
    if resource is None:  # pragma: no cover - non Unix.
        return 0
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(usage)
    return int(usage) * 1024


def _process_alive(pid: int) -> bool:
    """Whether a process id is still running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# ---------------------------------------------------------------------------
# The cache lock
# ---------------------------------------------------------------------------


def _lock_file(path: str, timeout: float = 60.0):
    """Take the cross-process lock of a cache directory.

    The lock is mandatory: a cache that cannot be locked is not used at all,
    because two writers would publish over each other.  There is no silent
    unprotected fallback.
    """
    if fcntl is None and msvcrt is None:
        raise PreparedCellError(
            "no cross-process lock is available for the prepared cell cache"
        )
    handle = open(path, "a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows only.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise PreparedCellError(
                            "the prepared cell cache lock could not be taken"
                        )
                    time.sleep(0.05)
    except Exception:
        handle.close()
        raise
    return handle


def _unlock_file(handle) -> None:
    """Release the cross-process lock of a cache directory."""
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows only.
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception as error:  # pragma: no cover - defensive.
        logger.debug("cannot release the prepared cell cache lock: %s", error)
    finally:
        try:
            handle.close()
        except OSError:  # pragma: no cover - defensive.
            pass


class _CacheState:
    """The state every store of one cache directory shares in this process.

    The thread lock serializes the threads of this process, the file lock
    serializes the processes, and the reference counts say which artifacts are
    still in use by a live chart.
    """

    def __init__(self, cache_dir: str) -> None:
        self.cache_dir = cache_dir
        self.lock = threading.RLock()
        self.depth = 0
        self.handle = None
        self.refs: Dict[str, int] = {}

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Serialize cache operations across threads and processes."""
        with self.lock:
            if self.depth == 0:
                self.handle = _lock_file(os.path.join(self.cache_dir, _LOCK_NAME))
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
                if self.depth == 0:
                    handle, self.handle = self.handle, None
                    _unlock_file(handle)


_CACHE_STATES: Dict[str, _CacheState] = {}
_CACHE_STATES_GUARD = threading.Lock()


def _cache_state(cache_dir: str) -> _CacheState:
    """The shared state of a cache directory."""
    key = os.path.realpath(cache_dir)
    with _CACHE_STATES_GUARD:
        state = _CACHE_STATES.get(key)
        if state is None:
            state = _CacheState(cache_dir)
            _CACHE_STATES[key] = state
        return state


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FeatureSums:
    """The accounted sums of the features a prepared artifact stores."""

    total: int
    counted: int
    longest_wkb: int
    longest_attrs: int
    distinct: int
    layer_counts: List[Tuple[int, int]]


@dataclass
class PreparedCellStore:
    """Durable, transactional cache of prepared ENC cells."""

    cache_dir: str
    cell_byte_limit: int = DEFAULT_CELL_BYTE_LIMIT
    disk_byte_limit: int = DEFAULT_DISK_BYTE_LIMIT

    def __post_init__(self) -> None:
        self.cache_dir = os.path.abspath(os.path.expanduser(self.cache_dir))
        os.makedirs(self.cache_dir, exist_ok=True)
        self._state = _cache_state(self.cache_dir)

    # -- public API ---------------------------------------------------------

    def open_chart(
        self,
        path: str,
        palette: str,
        cancel: Optional[Callable[[], bool]] = None,
        opener: Optional[Callable[[str], object]] = None,
    ):
        """Open a chart as a prepared, immediately drawable chart.

        ``palette`` names an entry of ``Style.chart_palettes``.  ``opener``
        receives the chart path and returns a GDAL dataset; it defaults to
        ``ogr.Open`` and exists so a worker can supply its own decoder.  It is
        called on the cold path only: a warm load never decodes the source.
        The returned chart holds an OGR in-memory dataset and an
        ``S57ChartDrawer`` bound to ``palette``; no callback has to be
        attached before drawing.
        """
        _check_cancel(cancel)
        if palette not in Style.chart_palettes:
            raise PreparedCellError("unknown chart palette %r" % (palette,))
        source = os.path.abspath(os.path.expanduser(path))
        for _attempt in range(3):
            identity = source_identity(source)
            artifact = self._artifact_path(identity.digest)
            with self._state.exclusive():
                if os.path.exists(artifact):
                    try:
                        chart = self._load_chart(artifact, source, palette, identity)
                    except PreparedCellCorruptError as error:
                        logger.debug(
                            "discarding an invalid prepared artifact: %s", error
                        )
                        self._discard(artifact)
                    else:
                        current = source_identity(source)
                        if current.digest != identity.digest:
                            # The source changed while the artifact was read:
                            # the artifact is stale and is never served.
                            logger.debug("the chart source changed while it was loaded")
                            _release_dataset(chart.vector_file)
                            chart.vector_file = None
                            self._discard(artifact)
                            continue
                        self._retain(artifact)
                        self._bind_release(chart, artifact)
                        return chart
                return self._build_chart(
                    source, palette, identity, artifact, cancel, opener
                )
        raise PreparedCellSourceChangedError(
            "the chart source kept changing while it was prepared"
        )

    def stats(self) -> Dict[str, int]:
        """Accounted cache statistics.

        ``retained_bytes`` counts published artifacts only.  The accounted
        feature payload of a cell is reported on the chart as
        ``prepared_payload_bytes``; ``rss_bytes`` is the process peak resident
        set size and is *not* the accounted payload.
        """
        retained = 0
        count = 0
        for artifact in self._artifact_paths():
            retained += os.path.getsize(artifact)
            count += 1
        with self._state.lock:
            references = sum(self._state.refs.values())
            in_use = len([key for key, value in self._state.refs.items() if value])
        return {
            "artifact_count": count,
            "retained_bytes": retained,
            "in_use_artifacts": in_use,
            "in_use_references": references,
            "disk_byte_limit": self.disk_byte_limit,
            "cell_byte_limit": self.cell_byte_limit,
            "rss_bytes": process_rss_bytes(),
        }

    # -- locking ------------------------------------------------------------

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialize cache operations across threads and processes."""
        with self._state.exclusive():
            yield

    # -- artifact bookkeeping ----------------------------------------------

    def _artifact_path(self, digest: str) -> str:
        """Path of the artifact that holds a cell with this identity."""
        return os.path.join(self.cache_dir, digest + _ARTIFACT_SUFFIX)

    def _artifact_paths(self) -> List[str]:
        """Published artifacts in this cache, in a deterministic order."""
        paths = []
        for name in sorted(os.listdir(self.cache_dir)):
            if _ARTIFACT_RE.match(name):
                path = os.path.join(self.cache_dir, name)
                if os.path.isfile(path):
                    paths.append(path)
        return paths

    def _staging_paths(self) -> List[str]:
        """Staging files currently present in the cache."""
        paths = []
        for name in sorted(os.listdir(self.cache_dir)):
            if name.startswith(_STAGING_PREFIX):
                path = os.path.join(self.cache_dir, name)
                if os.path.isfile(path):
                    paths.append(path)
        return paths

    def _is_in_use(self, artifact: str) -> bool:
        """Whether a live chart still holds an artifact."""
        with self._state.lock:
            return self._state.refs.get(artifact, 0) > 0

    def _retain(self, artifact: str) -> None:
        """Record one more live chart on an artifact."""
        with self._state.lock:
            self._state.refs[artifact] = self._state.refs.get(artifact, 0) + 1

    def _release_artifact(self, artifact: str) -> None:
        """Record one chart less on an artifact."""
        with self._state.lock:
            count = self._state.refs.get(artifact, 0) - 1
            if count > 0:
                self._state.refs[artifact] = count
            else:
                self._state.refs.pop(artifact, None)

    def _discard(self, artifact: str) -> None:
        """Remove one artifact; it will be rebuilt on the next request."""
        try:
            os.remove(artifact)
        except FileNotFoundError:
            return
        except OSError as error:
            raise PreparedCellError(
                "cannot remove the invalid prepared artifact %s: %s" % (artifact, error)
            )

    def _cleanup_staging(self) -> None:
        """Remove staging files left behind by processes that are gone."""
        for path in self._staging_paths():
            name = os.path.basename(path)
            tail = name[len(_STAGING_PREFIX) :].split("-", 1)
            if not tail or not tail[0].isdigit():
                continue
            pid = int(tail[0])
            if pid == os.getpid() or _process_alive(pid):
                continue
            try:
                os.remove(path)
            except OSError as error:
                logger.debug("cannot remove stale staging file %s: %s", name, error)

    def _retained_bytes(self) -> int:
        """The bytes the published artifacts of this cache occupy."""
        return sum(os.path.getsize(path) for path in self._artifact_paths())

    def _enforce_quota(self, reserve: int = 0) -> None:
        """Keep the retained cache, staging included, inside the disk limit.

        Unused artifacts are removed oldest first.  Files that the store does
        not own are never touched, and an artifact a live chart still holds is
        never removed.  ``reserve`` counts bytes that are about to be written
        on top of what the cache already holds.
        """
        total = self._cache_bytes() + max(0, reserve)
        if total <= self.disk_byte_limit:
            return
        total = self._evict_artifacts(total)
        if total > self.disk_byte_limit:
            raise PreparedCellLimitError(
                "prepared cache needs %d bytes but the disk limit is %d bytes"
                % (total, self.disk_byte_limit)
            )

    def _cache_bytes(self) -> int:
        """The bytes this cache occupies, the staging files included."""
        retained = self._retained_bytes()
        staging = sum(os.path.getsize(path) for path in self._staging_paths())
        return retained + staging

    def _evictable_artifacts(self) -> List[str]:
        """The artifacts that may be removed, oldest first."""
        candidates = []
        for path in self._artifact_paths():
            if self._is_in_use(path):
                continue
            try:
                candidates.append(
                    (os.path.getmtime(path), os.path.basename(path), path)
                )
            except OSError as error:
                logger.debug("cannot stat prepared artifact %s: %s", path, error)
        return [path for _, _, path in sorted(candidates)]

    def _evict_artifacts(self, total: int) -> int:
        """Remove unused artifacts, oldest first, until the cache fits."""
        for path in self._evictable_artifacts():
            if total <= self.disk_byte_limit:
                break
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except OSError as error:
                logger.debug("cannot evict prepared artifact %s: %s", path, error)
                continue
            total -= size
        return total

    # -- building -----------------------------------------------------------

    def _build_chart(
        self,
        path: str,
        palette: str,
        identity: SourceIdentity,
        artifact: str,
        cancel: Optional[Callable[[], bool]],
        opener: Optional[Callable[[str], object]],
    ):
        """Decode the source into a validated artifact and load the chart."""
        self._cleanup_staging()
        self._enforce_quota()
        staging = os.path.join(
            self.cache_dir,
            "%s%d-%s%s"
            % (_STAGING_PREFIX, os.getpid(), uuid.uuid4().hex, _ARTIFACT_SUFFIX),
        )
        connection = None
        dataset = None
        owned = False
        published = False
        try:
            dataset, owned = self._open_source(path, opener)
            connection = self._open_staging(staging)
            payload_bytes = self._stream_source(
                dataset, connection, cancel, staging, self._retained_bytes()
            )
            self._write_meta(connection, identity, payload_bytes)
            self._commit(connection)
            connection.close()
            connection = None

            # Validate the staged artifact completely before it is published.
            chart = self._load_chart(staging, path, palette, identity)
            _check_cancel(cancel)

            current = source_identity(path)
            if current.digest != identity.digest:
                raise PreparedCellSourceChangedError(
                    "the chart source changed while it was being prepared"
                )

            self._enforce_quota()
            os.replace(staging, artifact)
            _sync_directory(self.cache_dir)
            published = True
            chart.prepared_artifact = artifact
            self._retain(artifact)
            self._bind_release(chart, artifact)
            return chart
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:  # pragma: no cover - defensive.
                    pass
            if dataset is not None and owned:
                _release_dataset(dataset)
            if not published and os.path.exists(staging):
                try:
                    os.remove(staging)
                except OSError as error:
                    logger.debug("cannot remove staging file %s: %s", staging, error)

    def _open_source(self, path: str, opener: Optional[Callable[[str], object]]):
        """Open the chart source; the opener owns what it returns."""
        if opener is not None:
            dataset = opener(path)
            if dataset is None:
                raise PreparedCellError("cannot open the chart source %s" % path)
            return dataset, False
        try:
            dataset = ogr.Open(path)
        except Exception as error:
            raise PreparedCellError(
                "cannot open the chart source %s: %s" % (path, error)
            ) from error
        if dataset is None:
            raise PreparedCellError("cannot open the chart source %s" % path)
        return dataset, True

    def _open_staging(self, staging: str) -> sqlite3.Connection:
        """Open a staging database with a hard physical size limit."""
        connection = sqlite3.connect(staging)
        connection.execute("PRAGMA page_size=%d" % SQLITE_PAGE_SIZE)
        connection.execute("PRAGMA cache_size=-%d" % (SQLITE_CACHE_PAGES * 4))
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=DELETE")
        # The staging file may not grow past the smaller of the disk budget
        # and the payload budget plus the room the index and the page slack
        # need before SQLite refuses further writes.
        max_bytes = min(
            self.disk_byte_limit, self.cell_byte_limit * 2 + 16 * 1024 * 1024
        )
        max_pages = max(1024, max_bytes // SQLITE_PAGE_SIZE)
        connection.execute("PRAGMA max_page_count=%d" % max_pages)
        setlimit = getattr(connection, "setlimit", None)
        if setlimit is not None:
            try:
                setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_FEATURE_PAYLOAD)
            except (AttributeError, ValueError):  # pragma: no cover - defensive.
                pass
        connection.executescript(_ARTIFACT_SCHEMA)
        return connection

    def _commit(self, connection: sqlite3.Connection) -> None:
        """Commit the staged artifact, mapping a full disk to a limit error."""
        try:
            connection.commit()
        except sqlite3.Error as error:
            raise PreparedCellLimitError(
                "staging the prepared cell exceeded its space: %s" % error
            ) from error

    def _stream_source(
        self,
        dataset,
        connection: sqlite3.Connection,
        cancel: Optional[Callable[[], bool]],
        staging: str,
        retained: int = 0,
    ) -> int:
        """Stream the source features into the staging database.

        The payload limit is enforced while streaming, so an oversized cell is
        rejected before its content is fully read.
        """
        payload_bytes = 0
        for layer_index in range(dataset.GetLayerCount()):
            _check_cancel(cancel)
            layer = dataset.GetLayerByIndex(layer_index)
            payload_bytes = self._stream_layer(
                layer, layer_index, connection, cancel, staging, retained, payload_bytes
            )
        return payload_bytes

    def _stream_layer(
        self,
        layer,
        layer_index: int,
        connection: sqlite3.Connection,
        cancel: Optional[Callable[[], bool]],
        staging: str,
        retained: int,
        payload_bytes: int,
    ) -> int:
        """Stream one layer into the staging database.

        The accounted payload of the whole cell is carried along, so the
        limit is enforced across the layers and not per layer.
        """
        name, fields, field_defs, srs_wkt, geom_type, geom_fields = (
            self._layer_metadata(layer)
        )
        self._execute(
            connection,
            "INSERT INTO layers (layer_id, ord, name, geom_type, geom_fields,"
            " field_defs, srs_wkt, feature_count, fids_preserved)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                layer_index,
                layer_index,
                name,
                geom_type,
                geom_fields,
                field_defs,
                srs_wkt,
                0,
                1,
            ),
        )

        layer.ResetReading()
        order = 0
        fids: List[int] = []
        for feature in layer:
            _check_cancel(cancel)
            self._check_staging_size(staging, retained)
            payload_bytes = self._stream_feature(
                feature, layer_index, order, fields, name, connection, payload_bytes
            )
            fids.append(int(feature.GetFID()))
            order += 1

        unique = len(set(fids)) == len(fids)
        self._execute(
            connection,
            "UPDATE layers SET feature_count=?, fids_preserved=? WHERE layer_id=?",
            (order, 1 if unique else 0, layer_index),
        )
        return payload_bytes

    def _stream_feature(
        self,
        feature,
        layer_index: int,
        order: int,
        fields: Sequence[Dict[str, Any]],
        name: str,
        connection: sqlite3.Connection,
        payload_bytes: int,
    ) -> int:
        """Stage one feature and account for its payload."""
        wkb, envelope = self._feature_geometry(feature)
        values: Dict[str, object] = {}
        for index, field_defn in enumerate(fields):
            encoded = _encode_value(feature, index, field_defn["type"])
            values[field_defn["name"]] = encoded
        attrs = json.dumps(values, separators=(",", ":"), ensure_ascii=False)
        attrs_bytes = attrs.encode("utf-8")
        if len(attrs_bytes) > MAX_ATTRIBUTE_BYTES:
            self._reject_oversized_attribute(values, name)
        payload = (len(wkb) if wkb is not None else 0) + len(attrs_bytes)
        if payload > MAX_FEATURE_PAYLOAD:
            raise PreparedCellLimitError(
                "a feature of layer %r exceeds the feature payload limit" % name
            )
        payload_bytes += payload
        if payload_bytes > self.cell_byte_limit:
            raise PreparedCellLimitError(
                "the cell payload exceeds the %d byte limit" % self.cell_byte_limit
            )
        self._execute(
            connection,
            "INSERT INTO features (layer_id, ord, fid, wkb, attrs,"
            " minx, miny, maxx, maxy) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                layer_index,
                order,
                int(feature.GetFID()),
                wkb,
                attrs,
                envelope[0] if envelope else None,
                envelope[2] if envelope else None,
                envelope[1] if envelope else None,
                envelope[3] if envelope else None,
            ),
        )
        return payload_bytes

    def _reject_oversized_attribute(self, values: Dict[str, object], name: str) -> None:
        """Report the attribute that exceeds the attribute limit."""
        for field_name, encoded in values.items():
            size = len(json.dumps(encoded, separators=(",", ":")).encode("utf-8"))
            if size > MAX_ATTRIBUTE_BYTES:
                raise PreparedCellLimitError(
                    "attribute %r of layer %r exceeds the attribute limit"
                    % (field_name, name)
                )

    def _layer_metadata(
        self, layer
    ) -> Tuple[str, List[Dict[str, Any]], str, str, int, int]:
        """Capture the metadata of one layer."""
        name = layer.GetName()
        fields = _field_definitions(layer)
        _check_field_types(fields, name)
        field_defs = json.dumps(fields, separators=(",", ":"))
        if len(field_defs.encode("utf-8")) > MAX_FIELD_DEFS_BYTES:
            raise PreparedCellLimitError(
                "layer %r has more field metadata than the limit allows" % name
            )
        srs_wkt = self._export_srs(layer)
        definition = layer.GetLayerDefn()
        geom_type = int(layer.GetGeomType())
        geom_fields = int(definition.GetGeomFieldCount())
        return name, fields, field_defs, srs_wkt, geom_type, geom_fields

    @staticmethod
    def _export_srs(layer) -> str:
        """The WKT of the spatial reference of a layer, or an empty string."""
        reference = layer.GetSpatialRef()
        if reference is None:
            return ""
        try:
            return reference.ExportToWkt() or ""
        except Exception as error:
            logger.debug("cannot export the spatial reference: %s", error)
            return ""

    @staticmethod
    def _execute(connection: sqlite3.Connection, statement: str, parameters) -> None:
        """Run one statement, mapping a full disk to a limit error."""
        try:
            connection.execute(statement, parameters)
        except sqlite3.Error as error:
            message = str(error).lower()
            if "full" in message or "too large" in message:
                raise PreparedCellLimitError(
                    "staging the prepared cell exceeded its space: %s" % error
                ) from error
            raise PreparedCellError(
                "the prepared cell could not be staged: %s" % error
            ) from error

    @staticmethod
    def _execute_many(
        connection: sqlite3.Connection, statement: str, parameters
    ) -> None:
        """Run one statement for every row of parameters."""
        try:
            connection.executemany(statement, parameters)
        except sqlite3.Error as error:
            message = str(error).lower()
            if "full" in message or "too large" in message:
                raise PreparedCellLimitError(
                    "staging the prepared cell exceeded its space: %s" % error
                ) from error
            raise PreparedCellError(
                "the prepared cell could not be staged: %s" % error
            ) from error

    def _check_staging_size(self, staging: str, retained: int = 0) -> None:
        """Keep the staged file inside the disk budget while it grows."""
        try:
            size = os.path.getsize(staging)
        except OSError:  # pragma: no cover - defensive.
            return
        if retained + size > self.disk_byte_limit:
            raise PreparedCellLimitError(
                "the prepared cell exceeds the %d byte disk limit"
                % self.disk_byte_limit
            )

    @staticmethod
    def _feature_geometry(
        feature,
    ) -> Tuple[Optional[bytes], Optional[Tuple[float, ...]]]:
        """Export one feature geometry as canonical WKB plus its envelope.

        A feature without a geometry has none to export; an empty geometry is
        a geometry and keeps its own bytes.
        """
        geometry = feature.GetGeometryRef()
        if geometry is None:
            return None, None
        return bytes(geometry.ExportToWkb()), geometry.GetEnvelope()

    def _write_meta(
        self,
        connection: sqlite3.Connection,
        identity: SourceIdentity,
        payload_bytes: int,
    ) -> None:
        """Record the identity and the accounting of a staged artifact."""
        meta = {
            "format": FORMAT_NAME,
            "schema_version": str(SCHEMA_VERSION),
            "source_identity": identity.digest,
            "source_path": identity.path,
            "decoder": json.dumps(identity.decoder, sort_keys=True),
            "components": json.dumps(
                [
                    {
                        "name": component.name,
                        "number": component.number,
                        "size": component.size,
                        "digest": component.digest,
                    }
                    for component in identity.components
                ]
            ),
            "applied_updates": ",".join(str(n) for n in identity.applied_updates),
            "gaps": ",".join(str(n) for n in identity.gaps),
            "payload_bytes": str(payload_bytes),
        }
        self._execute_many(
            connection,
            "INSERT INTO meta (key, value) VALUES (?,?)",
            sorted(meta.items()),
        )

    # -- loading ------------------------------------------------------------

    def _load_chart(
        self,
        db_path: str,
        path: str,
        palette: str,
        identity: SourceIdentity,
    ):
        """Rebuild a chart from a prepared artifact."""
        connection = self._connect_readonly(db_path)
        try:
            self._validate_artifact(connection, db_path, identity)
            dataset = self._rebuild_dataset(connection)
            payload_bytes = self._payload_bytes(connection)
        finally:
            connection.close()
        chart = self._make_chart(dataset, path, palette)
        chart.prepared_payload_bytes = payload_bytes
        chart.prepared_artifact = db_path
        return chart

    @staticmethod
    def _connect_readonly(db_path: str) -> sqlite3.Connection:
        """Open a published artifact read only; it never changes in place."""
        uri = "file:" + urllib.parse.quote(db_path) + "?mode=ro&immutable=1"
        try:
            return sqlite3.connect(uri, uri=True)
        except sqlite3.Error as error:
            raise PreparedCellCorruptError(
                "the prepared artifact %s cannot be opened: %s" % (db_path, error)
            ) from error

    def _validate_artifact(
        self, connection: sqlite3.Connection, db_path: str, identity: SourceIdentity
    ) -> None:
        """Check schema, identity, size and accounted sums before loading.

        Every check happens before a single feature is allocated: a corrupt
        artifact is rejected while it is still only bytes on disk.
        """
        self._check_integrity(connection, db_path)
        meta = self._read_meta(connection, db_path)
        self._check_meta(meta, db_path, identity)
        recorded = self._recorded_payload(meta, db_path)
        self._check_feature_sums(connection, db_path, recorded)
        self._check_artifact_size(db_path, recorded)

    @staticmethod
    def _check_integrity(connection: sqlite3.Connection, db_path: str) -> None:
        """The artifact must pass the integrity check of the database."""
        try:
            check = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as error:
            raise PreparedCellCorruptError(
                "the prepared artifact %s cannot be checked: %s" % (db_path, error)
            ) from error
        if not check or check[0] != "ok":
            raise PreparedCellCorruptError(
                "the prepared artifact %s failed its integrity check" % db_path
            )

    @staticmethod
    def _read_meta(connection: sqlite3.Connection, db_path: str) -> Dict[str, str]:
        """The metadata an artifact records."""
        try:
            return dict(connection.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.Error as error:
            raise PreparedCellCorruptError(
                "the prepared artifact %s cannot be read: %s" % (db_path, error)
            ) from error

    @staticmethod
    def _check_meta(
        meta: Dict[str, str], db_path: str, identity: SourceIdentity
    ) -> None:
        """The artifact must describe this format, schema and source."""
        if meta.get("format") != FORMAT_NAME:
            raise PreparedCellCorruptError(
                "the prepared artifact %s is not an ENC prepared cell" % db_path
            )
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise PreparedCellCorruptError(
                "the prepared artifact %s uses an incompatible schema" % db_path
            )
        if meta.get("source_identity") != identity.digest:
            raise PreparedCellCorruptError(
                "the prepared artifact %s belongs to another source revision" % db_path
            )

    def _recorded_payload(self, meta: Dict[str, str], db_path: str) -> int:
        """The accounted payload an artifact records, inside the cell limit."""
        try:
            recorded = int(meta.get("payload_bytes", "-1"))
        except (TypeError, ValueError) as error:
            raise PreparedCellCorruptError(
                "the prepared artifact %s has an invalid payload record" % db_path
            ) from error
        if recorded < 0:
            raise PreparedCellCorruptError(
                "the prepared artifact %s has an invalid payload record" % db_path
            )
        if recorded > self.cell_byte_limit:
            raise PreparedCellLimitError(
                "the prepared artifact %s holds %d bytes, more than the %d byte"
                " cell limit" % (db_path, recorded, self.cell_byte_limit)
            )
        return recorded

    def _check_feature_sums(
        self, connection: sqlite3.Connection, db_path: str, recorded: int
    ) -> None:
        """The stored features must match the payload record and the counts."""
        sums = self._feature_sums(connection, db_path)
        if sums.total != recorded:
            raise PreparedCellCorruptError(
                "the prepared artifact %s does not match its payload record" % db_path
            )
        if (
            sums.longest_wkb > MAX_FEATURE_PAYLOAD
            or sums.longest_attrs > MAX_FEATURE_PAYLOAD
        ):
            raise PreparedCellCorruptError(
                "the prepared artifact %s holds a feature beyond the payload limit"
                % db_path
            )
        if sums.distinct > len(sums.layer_counts):
            raise PreparedCellCorruptError(
                "the prepared artifact %s holds features of an unknown layer" % db_path
            )
        self._check_layer_counts(connection, db_path, sums.layer_counts, sums.counted)

    @staticmethod
    def _feature_sums(connection: sqlite3.Connection, db_path: str) -> _FeatureSums:
        """The accounted sums of the features an artifact stores."""
        try:
            row = connection.execute(
                "SELECT COALESCE(SUM(COALESCE(length(CAST(wkb AS BLOB)), 0)"
                " + length(CAST(attrs AS BLOB))), 0), COUNT(*),"
                " COALESCE(MAX(COALESCE(length(CAST(wkb AS BLOB)), 0)), 0),"
                " COALESCE(MAX(length(CAST(attrs AS BLOB))), 0) FROM features"
            ).fetchone()
            layers = connection.execute(
                "SELECT layer_id, feature_count FROM layers"
            ).fetchall()
            distinct = connection.execute(
                "SELECT COUNT(DISTINCT layer_id) FROM features"
            ).fetchone()[0]
        except sqlite3.Error as error:
            raise PreparedCellCorruptError(
                "the prepared artifact %s cannot be read: %s" % (db_path, error)
            ) from error
        layer_counts = [
            (int(layer_id), int(feature_count)) for layer_id, feature_count in layers
        ]
        return _FeatureSums(
            int(row[0]),
            int(row[1]),
            int(row[2]),
            int(row[3]),
            int(distinct),
            layer_counts,
        )

    def _check_layer_counts(
        self,
        connection: sqlite3.Connection,
        db_path: str,
        layers: List[Tuple[int, int]],
        counted: int,
    ) -> None:
        """Every layer must hold exactly the features it records."""
        expected = 0
        for layer_id, feature_count in layers:
            try:
                actual = connection.execute(
                    "SELECT COUNT(*) FROM features WHERE layer_id=?", (layer_id,)
                ).fetchone()[0]
            except sqlite3.Error as error:
                raise PreparedCellCorruptError(
                    "the prepared artifact %s cannot be read: %s" % (db_path, error)
                ) from error
            if int(actual) != int(feature_count):
                raise PreparedCellCorruptError(
                    "the prepared artifact %s does not match its layer counts" % db_path
                )
            expected += int(feature_count)
        if expected != counted:
            raise PreparedCellCorruptError(
                "the prepared artifact %s does not match its feature count" % db_path
            )

    def _check_artifact_size(self, db_path: str, recorded: int) -> None:
        """The file must be able to hold the payload it records."""
        try:
            size = os.path.getsize(db_path)
        except OSError as error:
            raise PreparedCellCorruptError(
                "the prepared artifact %s cannot be read: %s" % (db_path, error)
            ) from error
        if size < recorded:
            raise PreparedCellCorruptError(
                "the prepared artifact %s is smaller than its payload" % db_path
            )
        if size > self.disk_byte_limit:
            raise PreparedCellLimitError(
                "the prepared artifact %s is larger than the disk limit" % db_path
            )

    @staticmethod
    def _payload_bytes(connection: sqlite3.Connection) -> int:
        """Accounted serialized payload of a prepared artifact."""
        row = connection.execute(
            "SELECT value FROM meta WHERE key='payload_bytes'"
        ).fetchone()
        return int(row[0]) if row else 0

    def _rebuild_dataset(self, connection: sqlite3.Connection):
        """Rebuild worker-local OGR memory layers from a prepared artifact."""
        dataset = _memory_driver().CreateDataSource("prepared-cell")
        try:
            layers = connection.execute(
                "SELECT layer_id, ord, name, geom_type, geom_fields, field_defs,"
                " srs_wkt, feature_count, fids_preserved FROM layers ORDER BY ord"
            ).fetchall()
            for row in layers:
                self._rebuild_layer(dataset, connection, row)
        except Exception:
            _release_dataset(dataset)
            raise
        return dataset

    def _rebuild_layer(self, dataset, connection, row) -> None:
        """Rebuild one layer of a prepared artifact and its features."""
        (
            layer_id,
            _ord,
            name,
            geom_type,
            geom_fields,
            field_defs,
            srs_wkt,
            feature_count,
            fids_preserved,
        ) = row
        fields = self._layer_fields(field_defs, name)
        geometry_type = ogr.wkbNone if not geom_fields else int(geom_type)
        layer = dataset.CreateLayer(
            name, srs=_spatial_reference(srs_wkt), geom_type=geometry_type
        )
        if layer is None:
            raise PreparedCellCorruptError(
                "the prepared artifact holds a layer that cannot be rebuilt"
            )
        for field_defn in fields:
            self._create_field(layer, field_defn)

        counted = 0
        for fid, wkb, attrs in connection.execute(
            "SELECT fid, wkb, attrs FROM features WHERE layer_id=? ORDER BY ord",
            (layer_id,),
        ):
            self._rebuild_feature(layer, fid, wkb, attrs, fields, name, fids_preserved)
            counted += 1
        if counted != int(feature_count):
            raise PreparedCellCorruptError(
                "the prepared artifact does not match the feature count of %r" % name
            )

    @staticmethod
    def _layer_fields(field_defs, name) -> List[Dict[str, Any]]:
        """The field definitions of a layer, checked for supported types."""
        try:
            fields = json.loads(field_defs)
        except (TypeError, ValueError) as error:
            raise PreparedCellCorruptError(
                "the prepared artifact holds invalid field definitions: %s" % error
            ) from error
        if not isinstance(fields, list):
            raise PreparedCellCorruptError(
                "the prepared artifact holds invalid field definitions"
            )
        _check_field_types(fields, name)
        return fields

    def _rebuild_feature(
        self, layer, fid, wkb, attrs, fields, name, fids_preserved
    ) -> None:
        """Rebuild one feature of a layer."""
        feature = ogr.Feature(layer.GetLayerDefn())
        try:
            if fids_preserved and fid is not None:
                feature.SetFID(int(fid))
            if wkb is not None:
                geometry = ogr.CreateGeometryFromWkb(bytes(wkb))
                if geometry is None:
                    raise PreparedCellCorruptError(
                        "the prepared artifact holds an invalid geometry"
                    )
                feature.SetGeometry(geometry)
            values = self._feature_values(attrs)
            for index, field_defn in enumerate(fields):
                encoded = values.get(field_defn["name"])
                if encoded is None:
                    raise PreparedCellCorruptError(
                        "the prepared artifact is missing the value of %r"
                        % field_defn["name"]
                    )
                _apply_value(feature, index, field_defn["type"], encoded)
            if layer.CreateFeature(feature) != 0:
                raise PreparedCellCorruptError(
                    "a prepared feature of layer %r could not be rebuilt" % name
                )
        finally:
            feature = None

    @staticmethod
    def _feature_values(attrs) -> Dict[str, Any]:
        """The encoded attributes of one feature."""
        try:
            values = json.loads(attrs)
        except (TypeError, ValueError) as error:
            raise PreparedCellCorruptError(
                "the prepared artifact holds invalid attributes: %s" % error
            ) from error
        if not isinstance(values, dict):
            raise PreparedCellCorruptError(
                "the prepared artifact holds invalid attributes"
            )
        return values

    @staticmethod
    def _create_field(layer, field_defn: Dict[str, object]) -> None:
        """Recreate one field definition, including its subtype."""
        definition = ogr.FieldDefn(field_defn["name"], field_defn["type"])
        subtype = field_defn.get("subtype") or ogr.OFSTNone
        if subtype != ogr.OFSTNone:
            definition.SetSubType(subtype)
        width = int(field_defn.get("width") or 0)
        if width:
            definition.SetWidth(width)
        precision = int(field_defn.get("precision") or 0)
        if precision:
            definition.SetPrecision(precision)
        definition.SetNullable(bool(field_defn.get("nullable", True)))
        if layer.CreateField(definition) != 0:
            raise PreparedCellError(
                "field %r of layer %r could not be recreated"
                % (field_defn["name"], layer.GetName())
            )
        created = layer.GetLayerDefn().GetFieldDefn(
            layer.GetLayerDefn().GetFieldIndex(field_defn["name"])
        )
        if created.GetType() != field_defn["type"]:
            raise PreparedCellError(
                "field %r of layer %r changed its type"
                % (field_defn["name"], layer.GetName())
            )
        if created.GetSubType() != subtype:
            raise PreparedCellError(
                "field %r of layer %r changed its subtype"
                % (field_defn["name"], layer.GetName())
            )

    def _make_chart(self, dataset, path: str, palette: str):
        """Wrap the rebuilt layers in a chart that can draw right away."""
        coverage = GDALVectorChart.read_coverage(dataset)
        native_scale = 0
        identification = dataset.GetLayerByName("DSID")
        if identification is not None:
            identification.ResetReading()
            feature = identification.GetNextFeature()
            if feature is not None and feature.GetFieldIndex("DSPM_CSCL") >= 0:
                value = feature.GetField("DSPM_CSCL")
                native_scale = 0 if value is None else value

        chart = GDALVectorChart.__new__(GDALVectorChart)
        # The base class owns the common attributes: it is asked to set them
        # itself, so a chart can never drift from the real ChartLayer.
        ChartLayer.__init__(chart, path, "vector", None, None, True)
        chart.vector_file = dataset
        chart._coverage = coverage
        chart._coverage_envelopes = tuple(
            geometry.GetEnvelope() for geometry in coverage
        )
        chart._native_scale = native_scale

        drawer = S57ChartDrawer.__new__(S57ChartDrawer)
        drawer.palette = palette
        drawer.symbolProvider = None
        chart.drawer = drawer
        return chart

    def _bind_release(self, chart, artifact: str) -> None:
        """Give the chart an explicit release of its worker-local resources."""

        def release() -> None:
            chart.vector_file = None
            self._release_artifact(artifact)

        chart.close = release
