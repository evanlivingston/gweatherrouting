"""Tests for the durable prepared ENC cell store (P1).

Every fixture here is synthetic and self contained: a dummy chart file on disk
supplies the identity bytes, and an OGR in-memory dataset supplies the
content.  No real ENC cell, no snapshot and no private fixture module is
needed, so the tests exercise the store alone.
"""

import dataclasses
import os
import sqlite3
import threading

import pytest
from osgeo import ogr

from gweatherrouting.rendering import encprepared
from gweatherrouting.rendering.encprepared import (
    PreparedCellCanceledError,
    PreparedCellError,
    PreparedCellLimitError,
    PreparedCellSourceChangedError,
    PreparedCellStore,
    source_identity,
)

CHART_NAME = "synthetic"
PALETTE = "dark"


# ---------------------------------------------------------------------------
# The synthetic source
# ---------------------------------------------------------------------------


def chart_directory(tmp_path):
    """The directory the dummy chart files live in."""
    directory = tmp_path / "charts"
    directory.mkdir(exist_ok=True)
    return str(directory)


def write_chart(directory, content=b"synthetic-chart-bytes", number=0, name=CHART_NAME):
    """A dummy chart file: the store only ever hashes it, never reads it."""
    path = os.path.join(directory, "%s.%03d" % (name, number))
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def _polygon(rings):
    """A polygon built from a list of rings of (x, y) pairs."""
    geometry = ogr.Geometry(ogr.wkbPolygon)
    for ring in rings:
        linear = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ring:
            linear.AddPoint_2D(x, y)
        geometry.AddGeometry(linear)
    return geometry


def build_dataset():
    """A synthetic ENC-like dataset: DSID, M_COVR, SOUNDG and BOYLAT.

    It carries the cases that must survive the round trip: a geometryless
    identification layer, a coverage with a hole and an exclusion panel, a
    multipoint sounding with depths, a colour list and the fields whose empty
    states differ (null, unset, date, time, binary and a wide integer).
    """
    driver = ogr.GetDriverByName("MEM")
    dataset = driver.CreateDataSource("synthetic")

    identification = dataset.CreateLayer("DSID", geom_type=ogr.wkbNone)
    identification.CreateField(ogr.FieldDefn("DSPM_CSCL", ogr.OFTInteger))
    identification.CreateField(ogr.FieldDefn("DSPM_NM", ogr.OFTString))
    feature = ogr.Feature(identification.GetLayerDefn())
    feature.SetField("DSPM_CSCL", 45000)
    feature.SetField("DSPM_NM", "SYNTHETIC")
    identification.CreateFeature(feature)

    coverage = dataset.CreateLayer("M_COVR", geom_type=ogr.wkbPolygon)
    coverage.CreateField(ogr.FieldDefn("CATCOV", ogr.OFTInteger))
    feature = ogr.Feature(coverage.GetLayerDefn())
    feature.SetField("CATCOV", 1)
    feature.SetGeometry(
        _polygon(
            [
                [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
                [(2, 2), (4, 2), (4, 4), (2, 4), (2, 2)],
            ]
        )
    )
    coverage.CreateFeature(feature)
    feature = ogr.Feature(coverage.GetLayerDefn())
    feature.SetField("CATCOV", 2)
    feature.SetGeometry(_polygon([[(6, 6), (8, 6), (8, 8), (6, 8), (6, 6)]]))
    coverage.CreateFeature(feature)

    soundings = dataset.CreateLayer("SOUNDG", geom_type=ogr.wkbMultiPoint25D)
    soundings.CreateField(ogr.FieldDefn("SOUNDG", ogr.OFTReal))
    feature = ogr.Feature(soundings.GetLayerDefn())
    geometry = ogr.Geometry(ogr.wkbMultiPoint25D)
    for x, y, z in ((1.0, 1.0, 12.5), (3.0, 3.0, -7.25)):
        point = ogr.Geometry(ogr.wkbPoint25D)
        point.AddPoint(x, y, z)
        geometry.AddGeometry(point)
    feature.SetGeometry(geometry)
    feature.SetField("SOUNDG", 12.5)
    soundings.CreateFeature(feature)
    # A second sounding group with an empty geometry and a null depth: an
    # empty geometry is a geometry and must keep its own bytes.
    feature = ogr.Feature(soundings.GetLayerDefn())
    feature.SetGeometry(ogr.Geometry(ogr.wkbMultiPoint25D))
    feature.SetFieldNull("SOUNDG")
    soundings.CreateFeature(feature)

    objects = dataset.CreateLayer("BOYLAT", geom_type=ogr.wkbPoint)
    objects.CreateField(ogr.FieldDefn("COLOUR", ogr.OFTIntegerList))
    objects.CreateField(ogr.FieldDefn("CATLAM", ogr.OFTInteger))
    objects.CreateField(ogr.FieldDefn("BIGVAL", ogr.OFTInteger64))
    objects.CreateField(ogr.FieldDefn("BLOB", ogr.OFTBinary))
    objects.CreateField(ogr.FieldDefn("DATEV", ogr.OFTDate))
    objects.CreateField(ogr.FieldDefn("TIMEV", ogr.OFTTime))
    objects.CreateField(ogr.FieldDefn("STAMP", ogr.OFTDateTime))
    objects.CreateField(ogr.FieldDefn("NULLV", ogr.OFTString))
    objects.CreateField(ogr.FieldDefn("UNSETV", ogr.OFTString))
    objects.CreateField(ogr.FieldDefn("NAMES", ogr.OFTStringList))
    feature = ogr.Feature(objects.GetLayerDefn())
    point = ogr.Geometry(ogr.wkbPoint)
    point.AddPoint_2D(5.0, 5.0)
    feature.SetGeometry(point)
    feature.SetFieldIntegerList(0, [3, 4])
    feature.SetField("CATLAM", 1)
    feature.SetFieldInteger64(2, 2**40)
    feature.SetFieldBinary(3, b"\x00\x01\xfe")
    feature.SetField("DATEV", "2019/01/02")
    feature.SetField("TIMEV", "12:34:56")
    feature.SetField("STAMP", "2019/01/02 12:34:56.250+02")
    feature.SetFieldNull("NULLV")
    # UNSETV is never given a value: it is unset, not null.
    feature.SetFieldStringList(9, ["alpha", "beta"])
    objects.CreateFeature(feature)

    return dataset


class Opener:
    """An opener that counts its calls and hands out fresh datasets."""

    def __init__(self, mutate=None):
        self.calls = 0
        self.mutate = mutate

    def __call__(self, path):
        self.calls += 1
        if self.mutate is not None:
            self.mutate(path)
        return build_dataset()


# ---------------------------------------------------------------------------
# Reading a dataset back
# ---------------------------------------------------------------------------


def _read_date(feature, position):
    """The text and the parts of a date, a time or a date-time field."""
    return (
        feature.GetFieldAsString(position),
        tuple(feature.GetFieldAsDateTime(position)),
    )


def _read_integer64_list(feature, position):
    """The elements of a wide integer list field."""
    return tuple(feature.GetFieldAsInteger64List(position))


#: The reader of every field type the synthetic dataset uses.  The readers
#: are written here, not taken from the product, so the expectations of the
#: tests stay independent from the implementation they check.
_FIELD_READERS = {
    ogr.OFTInteger: lambda feature, position: feature.GetFieldAsInteger(position),
    ogr.OFTInteger64: lambda feature, position: feature.GetFieldAsInteger64(position),
    ogr.OFTReal: lambda feature, position: feature.GetFieldAsDouble(position),
    ogr.OFTString: lambda feature, position: feature.GetFieldAsString(position),
    ogr.OFTBinary: lambda feature, position: bytes(feature.GetFieldAsBinary(position)),
    ogr.OFTDate: _read_date,
    ogr.OFTTime: _read_date,
    ogr.OFTDateTime: _read_date,
    ogr.OFTIntegerList: lambda feature, position: tuple(
        feature.GetFieldAsIntegerList(position)
    ),
    ogr.OFTInteger64List: _read_integer64_list,
    ogr.OFTRealList: lambda feature, position: tuple(
        feature.GetFieldAsDoubleList(position)
    ),
    ogr.OFTStringList: lambda feature, position: tuple(
        feature.GetFieldAsStringList(position)
    ),
}


def read_value(feature, position, field_type):
    """One field value, with null and unset kept apart."""
    if feature.IsFieldNull(position):
        return ("null", None)
    if not feature.IsFieldSet(position):
        return ("unset", None)
    reader = _FIELD_READERS.get(field_type)
    if reader is None:
        raise AssertionError("unexpected field type %s" % field_type)
    return ("value", reader(feature, position))


def snapshot(dataset):
    """Every layer, feature, field value and geometry of a dataset."""
    rows = []
    for index in range(dataset.GetLayerCount()):
        layer = dataset.GetLayerByIndex(index)
        definition = layer.GetLayerDefn()
        fields = [
            (
                definition.GetFieldDefn(position).GetName(),
                definition.GetFieldDefn(position).GetType(),
                definition.GetFieldDefn(position).GetSubType(),
                definition.GetFieldDefn(position).GetWidth(),
                bool(definition.GetFieldDefn(position).IsNullable()),
            )
            for position in range(definition.GetFieldCount())
        ]
        layer.ResetReading()
        for feature in layer:
            values = [
                (name, read_value(feature, position, field_type))
                for position, (name, field_type, _subtype, _w, _n) in enumerate(fields)
            ]
            geometry = feature.GetGeometryRef()
            rows.append(
                {
                    "layer": layer.GetName(),
                    "geom_fields": definition.GetGeomFieldCount(),
                    "geom_type": ogr.GT_Flatten(definition.GetGeomType()),
                    "fid": feature.GetFID(),
                    "fields": fields,
                    "values": values,
                    "wkb": (
                        bytes(geometry.ExportToWkb()) if geometry is not None else None
                    ),
                }
            )
    return rows


def artifact_names(cache_dir):
    """The published artifacts of a cache."""
    return sorted(
        name for name in os.listdir(str(cache_dir)) if name.endswith(".sqlite")
    )


def staging_names(cache_dir):
    """The staging files left in a cache."""
    return sorted(
        name for name in os.listdir(str(cache_dir)) if name.startswith(".staging-")
    )


# ---------------------------------------------------------------------------
# The cold build
# ---------------------------------------------------------------------------


def test_the_cold_build_reproduces_the_source_dataset(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))
    opener = Opener()

    chart = store.open_chart(source, PALETTE, opener=opener)

    assert opener.calls == 1
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


def test_the_cold_build_preserves_the_field_definitions(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    chart = store.open_chart(source, PALETTE, opener=Opener())

    prepared = snapshot(chart.vector_file)
    original = snapshot(build_dataset())
    assert [row["fields"] for row in prepared] == [row["fields"] for row in original]
    assert [row["geom_fields"] for row in prepared] == [
        row["geom_fields"] for row in original
    ]


def test_the_cold_build_keeps_the_empty_states_apart(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    chart = store.open_chart(source, PALETTE, opener=Opener())

    layer = chart.vector_file.GetLayerByName("BOYLAT")
    layer.ResetReading()
    feature = layer.GetNextFeature()
    definition = layer.GetLayerDefn()
    index = definition.GetFieldIndex
    assert feature.IsFieldNull(index("NULLV"))
    assert not feature.IsFieldSet(index("UNSETV"))
    assert not feature.IsFieldNull(index("UNSETV"))


def test_the_cold_build_keeps_the_typed_values(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    chart = store.open_chart(source, PALETTE, opener=Opener())

    layer = chart.vector_file.GetLayerByName("BOYLAT")
    layer.ResetReading()
    feature = layer.GetNextFeature()
    definition = layer.GetLayerDefn()
    index = definition.GetFieldIndex
    assert list(feature.GetFieldAsIntegerList(index("COLOUR"))) == [3, 4]
    assert feature.GetFieldAsInteger64(index("BIGVAL")) == 2**40
    assert bytes(feature.GetFieldAsBinary(index("BLOB"))) == b"\x00\x01\xfe"
    assert feature.GetFieldAsString(index("DATEV")) == "2019/01/02"
    assert feature.GetFieldAsString(index("TIMEV")) == "12:34:56"
    stamp = feature.GetFieldAsDateTime(index("STAMP"))
    # The fraction of a second survives, and so does the time zone: OGR
    # encodes the offset in quarter hours above 100 (100 + 8 * 15 minutes).
    assert stamp[5] == pytest.approx(56.25)
    assert stamp[6] == 108
    assert list(feature.GetFieldAsStringList(index("NAMES"))) == ["alpha", "beta"]


def test_the_accounted_payload_is_reported(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    cache = str(tmp_path / "cache")
    store = PreparedCellStore(cache)

    chart = store.open_chart(source, PALETTE, opener=Opener())

    assert chart.prepared_payload_bytes > 0
    stats = store.stats()
    assert stats["artifact_count"] == 1
    assert stats["retained_bytes"] > 0
    assert stats["cell_byte_limit"] == 64 * 1024 * 1024
    assert stats["disk_byte_limit"] == 256 * 1024 * 1024


def test_the_cold_build_preserves_the_native_scale(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    chart = store.open_chart(source, PALETTE, opener=Opener())

    assert chart._native_scale == 45000


def test_the_cold_build_preserves_the_exact_coverage(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    chart = store.open_chart(source, PALETTE, opener=Opener())

    raw = build_dataset()
    expected = encprepared.GDALVectorChart.read_coverage(raw)
    assert len(chart._coverage) == 1
    geometry = chart._coverage[0]
    # One outer ring and two holes: the original hole and exclusion panel.
    assert geometry.GetGeometryCount() == 3
    assert bytes(geometry.ExportToWkb()) == bytes(expected[0].ExportToWkb())
    assert geometry.GetArea() == pytest.approx(92.0)
    assert chart._coverage_envelopes == (geometry.GetEnvelope(),)


def test_the_cold_build_preserves_the_sounding_depths(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    chart = store.open_chart(source, PALETTE, opener=Opener())

    layer = chart.vector_file.GetLayerByName("SOUNDG")
    layer.ResetReading()
    feature = layer.GetNextFeature()
    geometry = feature.GetGeometryRef()
    depths = [
        geometry.GetGeometryRef(index).GetPoint()[2]
        for index in range(geometry.GetGeometryCount())
    ]
    assert depths == [12.5, -7.25]


def test_the_cold_build_keeps_an_empty_geometry(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    chart = store.open_chart(source, PALETTE, opener=Opener())

    layer = chart.vector_file.GetLayerByName("SOUNDG")
    layer.ResetReading()
    layer.GetNextFeature()
    feature = layer.GetNextFeature()
    geometry = feature.GetGeometryRef()
    assert geometry is not None
    assert geometry.IsEmpty()
    assert feature.IsFieldNull(layer.GetLayerDefn().GetFieldIndex("SOUNDG"))


def test_the_chart_is_ready_to_draw(tmp_path):
    from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
    from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    chart = store.open_chart(source, PALETTE, opener=Opener())

    assert isinstance(chart, GDALVectorChart)
    assert isinstance(chart.drawer, S57ChartDrawer)
    assert chart.drawer.palette == PALETTE
    assert chart.ctype == "vector"
    assert chart.path == os.path.abspath(source)
    assert chart.vector_file is not None
    assert chart.vector_file.GetLayerCount() == 4


def test_the_source_directory_is_never_written(tmp_path):
    directory = chart_directory(tmp_path)
    source = write_chart(directory)
    store = PreparedCellStore(str(tmp_path / "cache"))

    store.open_chart(source, PALETTE, opener=Opener())

    assert sorted(os.listdir(directory)) == ["synthetic.000"]


# ---------------------------------------------------------------------------
# The warm load
# ---------------------------------------------------------------------------


def test_a_warm_load_never_opens_the_source(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    cache = str(tmp_path / "cache")
    PreparedCellStore(cache).open_chart(source, PALETTE, opener=Opener())

    fresh = PreparedCellStore(cache)

    def refuse(path):
        raise AssertionError("the warm path must not open the source")

    chart = fresh.open_chart(source, PALETTE, opener=refuse)

    assert chart.vector_file is not None
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


def test_the_palette_is_not_part_of_the_identity(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))
    opener = Opener()

    first = store.open_chart(source, "cm93", opener=opener)
    second = store.open_chart(source, "dark", opener=opener)

    assert opener.calls == 1
    assert first.prepared_artifact == second.prepared_artifact
    assert first.drawer.palette == "cm93"
    assert second.drawer.palette == "dark"
    assert store.stats()["artifact_count"] == 1


def test_a_changed_source_rebuilds_the_cell(tmp_path):
    directory = chart_directory(tmp_path)
    source = write_chart(directory)
    store = PreparedCellStore(str(tmp_path / "cache"))
    opener = Opener()
    store.open_chart(source, PALETTE, opener=opener)
    assert opener.calls == 1

    write_chart(directory, content=b"the-source-changed")
    chart = store.open_chart(source, PALETTE, opener=opener)

    assert opener.calls == 2
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


def test_a_source_changed_during_the_build_is_rejected(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    cache = str(tmp_path / "cache")
    store = PreparedCellStore(cache)

    def mutate(path):
        with open(path, "wb") as handle:
            handle.write(b"mutated-while-the-cell-was-built")

    with pytest.raises(PreparedCellSourceChangedError):
        store.open_chart(source, PALETTE, opener=Opener(mutate=mutate))

    assert artifact_names(cache) == []
    assert staging_names(cache) == []


def test_a_source_changed_during_the_warm_load_is_not_served(tmp_path, monkeypatch):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))
    store.open_chart(source, PALETTE, opener=Opener())

    real = encprepared.source_identity
    calls = {"count": 0}

    def flaky(path):
        identity = real(path)
        calls["count"] += 1
        if calls["count"] == 2:
            # The second identity is the one taken after the artifact was
            # read: the source changed while it was being loaded.
            return dataclasses.replace(identity, digest="f" * 64)
        return identity

    monkeypatch.setattr(encprepared, "source_identity", flaky)
    opener = Opener()

    chart = store.open_chart(source, PALETTE, opener=opener)

    assert opener.calls == 1
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


# ---------------------------------------------------------------------------
# The source identity
# ---------------------------------------------------------------------------


def test_the_numbered_updates_are_part_of_the_identity(tmp_path):
    directory = chart_directory(tmp_path)
    source = write_chart(directory)
    write_chart(directory, number=1)
    write_chart(directory, number=2)

    identity = source_identity(source)

    assert [component.name for component in identity.components] == [
        "synthetic.000",
        "synthetic.001",
        "synthetic.002",
    ]
    assert identity.applied_updates == (1, 2)
    assert identity.gaps == ()


def test_the_gapped_updates_are_discovered(tmp_path):
    directory = chart_directory(tmp_path)
    source = write_chart(directory)
    write_chart(directory, number=3)

    identity = source_identity(source)

    assert [component.name for component in identity.components] == [
        "synthetic.000",
        "synthetic.003",
    ]
    assert identity.applied_updates == ()
    assert identity.gaps == (3,)

    write_chart(directory, number=1)
    filled = source_identity(source)

    assert filled.applied_updates == (1,)
    assert filled.gaps == (3,)
    assert filled.digest != identity.digest


def test_a_new_update_rebuilds_the_cell(tmp_path):
    directory = chart_directory(tmp_path)
    source = write_chart(directory)
    store = PreparedCellStore(str(tmp_path / "cache"))
    opener = Opener()
    store.open_chart(source, PALETTE, opener=opener)
    assert opener.calls == 1

    write_chart(directory, number=1)
    store.open_chart(source, PALETTE, opener=opener)

    assert opener.calls == 2


# ---------------------------------------------------------------------------
# Invalid artifacts
# ---------------------------------------------------------------------------


def _rebuild_after_damage(tmp_path, damage):
    """Build a cell, damage its artifact and open it again."""
    source = write_chart(chart_directory(tmp_path))
    cache = str(tmp_path / "cache")
    store = PreparedCellStore(cache)
    chart = store.open_chart(source, PALETTE, opener=Opener())
    artifact = chart.prepared_artifact
    chart.close()
    damage(artifact)
    opener = Opener()
    rebuilt = store.open_chart(source, PALETTE, opener=opener)
    return opener, rebuilt


def test_a_corrupt_artifact_is_rebuilt(tmp_path):
    def damage(artifact):
        with open(artifact, "wb") as handle:
            handle.write(b"this is not a database at all")

    opener, chart = _rebuild_after_damage(tmp_path, damage)

    assert opener.calls == 1
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


def test_an_artifact_with_a_foreign_schema_is_rebuilt(tmp_path):
    def damage(artifact):
        connection = sqlite3.connect(artifact)
        connection.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        connection.commit()
        connection.close()

    opener, chart = _rebuild_after_damage(tmp_path, damage)

    assert opener.calls == 1
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


def test_an_artifact_with_a_foreign_identity_is_rebuilt(tmp_path):
    def damage(artifact):
        connection = sqlite3.connect(artifact)
        connection.execute(
            "UPDATE meta SET value='deadbeef' WHERE key='source_identity'"
        )
        connection.commit()
        connection.close()

    opener, chart = _rebuild_after_damage(tmp_path, damage)

    assert opener.calls == 1
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


def test_an_artifact_with_broken_json_is_rebuilt(tmp_path):
    def damage(artifact):
        connection = sqlite3.connect(artifact)
        connection.execute("UPDATE features SET attrs='{not json' WHERE feature_id=1")
        connection.commit()
        connection.close()

    opener, chart = _rebuild_after_damage(tmp_path, damage)

    assert opener.calls == 1
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


def test_an_artifact_with_a_wrong_payload_record_is_rebuilt(tmp_path):
    def damage(artifact):
        connection = sqlite3.connect(artifact)
        connection.execute("UPDATE meta SET value='7' WHERE key='payload_bytes'")
        connection.commit()
        connection.close()

    opener, chart = _rebuild_after_damage(tmp_path, damage)

    assert opener.calls == 1
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


def test_an_artifact_with_a_missing_feature_is_rebuilt(tmp_path):
    def damage(artifact):
        connection = sqlite3.connect(artifact)
        connection.execute("DELETE FROM features WHERE feature_id=1")
        connection.commit()
        connection.close()

    opener, chart = _rebuild_after_damage(tmp_path, damage)

    assert opener.calls == 1
    assert snapshot(chart.vector_file) == snapshot(build_dataset())


# ---------------------------------------------------------------------------
# The limits and the cancellation
# ---------------------------------------------------------------------------


def test_the_cell_payload_limit_is_enforced(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    cache = str(tmp_path / "cache")
    store = PreparedCellStore(cache, cell_byte_limit=64)

    with pytest.raises(PreparedCellLimitError):
        store.open_chart(source, PALETTE, opener=Opener())

    assert artifact_names(cache) == []
    assert staging_names(cache) == []


def test_the_disk_limit_is_enforced(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    cache = str(tmp_path / "cache")
    store = PreparedCellStore(cache, disk_byte_limit=1024)

    with pytest.raises(PreparedCellLimitError):
        store.open_chart(source, PALETTE, opener=Opener())

    assert artifact_names(cache) == []
    assert staging_names(cache) == []


def test_a_cancelled_build_leaves_nothing_behind(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    cache = str(tmp_path / "cache")
    store = PreparedCellStore(cache)
    calls = {"count": 0}

    def cancel():
        calls["count"] += 1
        return calls["count"] > 3

    with pytest.raises(PreparedCellCanceledError):
        store.open_chart(source, PALETTE, cancel=cancel, opener=Opener())

    assert artifact_names(cache) == []
    assert staging_names(cache) == []


def test_an_unknown_palette_is_refused(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))

    with pytest.raises(PreparedCellError):
        store.open_chart(source, "no-such-palette", opener=Opener())


# ---------------------------------------------------------------------------
# The lifetime of the artifacts
# ---------------------------------------------------------------------------


def test_two_charts_share_one_artifact_and_release_it_once(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))
    opener = Opener()

    first = store.open_chart(source, PALETTE, opener=opener)
    second = store.open_chart(source, PALETTE, opener=opener)

    assert opener.calls == 1
    assert first.prepared_artifact == second.prepared_artifact
    assert store.stats()["in_use_references"] == 2
    assert store.stats()["in_use_artifacts"] == 1

    first.close()
    assert store.stats()["in_use_references"] == 1
    assert store.stats()["in_use_artifacts"] == 1

    second.close()
    assert store.stats()["in_use_references"] == 0
    assert store.stats()["in_use_artifacts"] == 0


def test_an_artifact_in_use_is_not_evicted(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    cache = str(tmp_path / "cache")
    store = PreparedCellStore(cache)
    chart = store.open_chart(source, PALETTE, opener=Opener())

    with pytest.raises(PreparedCellLimitError):
        store._enforce_quota(1 << 30)

    assert os.path.exists(chart.prepared_artifact)


def test_two_threads_can_open_the_same_cell(tmp_path):
    source = write_chart(chart_directory(tmp_path))
    store = PreparedCellStore(str(tmp_path / "cache"))
    charts = []
    errors = []

    def worker():
        try:
            charts.append(store.open_chart(source, PALETTE, opener=Opener()))
        except Exception as error:  # pragma: no cover - the failure path.
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    assert len(charts) == 2
    assert charts[0].prepared_artifact == charts[1].prepared_artifact
    assert snapshot(charts[0].vector_file) == snapshot(charts[1].vector_file)


def test_the_cache_lock_is_shared_between_stores(tmp_path):
    cache = str(tmp_path / "cache")
    first = PreparedCellStore(cache)
    second = PreparedCellStore(cache)

    assert first._state is second._state

    with first._exclusive():
        with second._exclusive():
            assert os.path.exists(os.path.join(cache, ".prepared-cell.lock"))
