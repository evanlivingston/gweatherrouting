"""P1 prepared-cell gate controls, without a real cell and without a fixture.

The gate's own verdict is pure: it reads the report it is given and says
whether every check is present, exactly true and backed by the evidence
the report carries.  The engine is injected, so these tests never open a
chart, never read a snapshot, never spawn a process and never need the
approved fixture.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name, path):
    """Load a tool module by path, registered in ``sys.modules``.

    The registration matters: a module that declares dataclasses resolves
    its annotations through ``sys.modules``, and a module that is not
    registered there fails while its own class body runs.
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


def load():
    path = ROOT / "tools" / "check_prepared_enc.py"
    return _load_module("enc_prepared_gate", path)


def observations(**overrides):
    """A complete set of observations, as the real engine would return them."""
    base = {
        "source": "/sandbox/enc/US5SEAMG/US5SEAMG.000",
        "cell": {
            "found": True,
            "name": "US5SEAMG",
            "path": "/sandbox/enc/US5SEAMG/US5SEAMG.000",
            "enabled": True,
            "class_enabled": True,
        },
        "camera": {
            "center_lat": 48.1125,
            "center_lon": -122.7375,
            "zoom": 14.236,
            "width": 710,
            "height": 743,
            "time": "2026-10-19T08:00:00",
            "density": "very_dense",
            "over_land": False,
        },
        "factor": 2,
        "palette": "dark",
        "drawer": "S57ChartDrawer",
        "drawer_origin": "prepared",
        "raw": {
            "native_scale": 45000,
            "pixel_sha256": "a" * 64,
            "ink_pixels": 4096,
            "coverage_sha256": "b" * 64,
            "coverage_geometry_count": 2,
            "inventory_sha256": "c" * 64,
            "feature_count": 120,
        },
        "prepared": {
            "built": True,
            "native_scale": 45000,
            "pixel_sha256": "a" * 64,
            "ink_pixels": 4096,
            "coverage_sha256": "b" * 64,
            "coverage_geometry_count": 2,
            "inventory_sha256": "c" * 64,
            "feature_count": 120,
        },
        "differences": [],
        "inventory_differences": [],
        "coverage_differences": [],
        "timings_s": {"cold_prepare": 1.0, "raw_draw": 0.2, "prepared_draw": 0.2},
        "cache_disk_bytes": 4096,
        "cell_payload_bytes": 2048,
        "source_opens": 1,
    }
    base.update(overrides)
    return base


class FakeEngine:
    """The gate's engine, replaced: no GDAL, no store, no snapshot."""

    def __init__(self, digest=None, measured=None, error=None):
        self.digest = digest if digest is not None else load().EXPECTED_SNAPSHOT_SHA
        self.measured = measured
        self.error = error
        self.sandbox_root = None

    def verify_snapshot(self, snapshot):
        if self.error is not None:
            raise self.error
        return {
            "content_sha256": self.digest,
            "forecast_time": "2026-10-19T08:00:00",
        }

    def prepare_sandbox(self, snapshot, root):
        self.sandbox_root = Path(root)
        return Path(root) / "data"

    def measure(self, data_dir, cache_dir, forecast_time):
        if self.error is not None:
            raise self.error
        return observations() if self.measured is None else self.measured


def child_result(**overrides):
    """A warm child that succeeded, unless it is told otherwise."""
    tool = load()
    report = {
        "role": "warm-child",
        "pid": os.getpid() + 1,
        "checks": {name: True for name in tool.CHILD_CHECK_NAMES},
        "source_open_count": 0,
        "errors": [],
        "complete": True,
        "pixel_sha256": "a" * 64,
        "ink_pixels": 4096,
        "inventory_sha256": "c" * 64,
        "coverage_sha256": "b" * 64,
        "native_scale": 45000,
        "timings_s": {"disk_reload": 0.5, "draw": 0.2},
    }
    report.update(overrides)
    return {
        "returncode": 0,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "report": report,
        "error": "",
    }


def child_runner(outcome):
    def runner(config_path, output_path, timeout_s):
        return outcome

    return runner


def passing_report(tool):
    """A report whose flags and evidence all agree."""
    report = tool.new_report()
    for name in tool.CHECK_NAMES:
        report["checks"][name] = True
    report["render"] = {"ink_pixels": 4096, "pixel_sha256": "a" * 64}
    report["identity"] = {
        "source_opens_during_preparation": 1,
        "raw": {
            "pixel_sha256": "a" * 64,
            "coverage_sha256": "b" * 64,
            "inventory_sha256": "c" * 64,
            "coverage_geometry_count": 2,
            "feature_count": 120,
            "native_scale": 45000,
        },
        "prepared": {
            "pixel_sha256": "a" * 64,
            "coverage_sha256": "b" * 64,
            "inventory_sha256": "c" * 64,
            "coverage_geometry_count": 2,
            "feature_count": 120,
            "native_scale": 45000,
        },
    }
    report["bytes"] = {"cache_disk_bytes": 4096, "cell_payload_bytes": 2048}
    report["child"] = child_result()
    return report


def test_cold_preparation_rejects_a_cached_parent(tmp_path):
    tool = load()
    output = tmp_path / "report.json"
    tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=FakeEngine(measured=observations(source_opens=0)),
        child_runner=child_runner(child_result()),
    )
    report = json.loads(output.read_text())
    assert report["complete"] is False
    assert report["checks"]["cold_source_opened_once"] is False


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------
def test_evaluate_report_requires_every_check():
    tool = load()
    report = passing_report(tool)
    del report["checks"]["pixels_match"]
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any("missing check: pixels_match" in problem for problem in problems)
    assert not any("failed check: pixels_match" in problem for problem in problems)


def test_evaluate_report_rejects_a_false_check():
    tool = load()
    report = passing_report(tool)
    report["checks"]["pixels_match"] = False
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any("failed check: pixels_match" in problem for problem in problems)


def test_evaluate_report_rejects_a_truthy_check_that_is_not_true():
    tool = load()
    report = passing_report(tool)
    report["checks"]["pixels_match"] = "false"
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any("failed check: pixels_match" in problem for problem in problems)


def test_evaluate_report_rejects_a_report_without_evidence():
    """The flags alone are not evidence: a forged report does not pass."""
    tool = load()
    report = tool.new_report()
    for name in tool.CHECK_NAMES:
        report["checks"][name] = True
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any("pixel digest" in problem for problem in problems)
    assert any("blank" in problem for problem in problems)
    assert any("cache disk use" in problem for problem in problems)
    assert any("warm child wrote no report" in problem for problem in problems)


def test_evaluate_report_rejects_a_blank_render():
    tool = load()
    report = passing_report(tool)
    report["render"]["ink_pixels"] = 0
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any("blank" in problem for problem in problems)


def test_evaluate_report_rejects_differing_raw_and_prepared_digests():
    tool = load()
    report = passing_report(tool)
    report["identity"]["prepared"]["pixel_sha256"] = "e" * 64
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any(
        "raw and prepared pixel_sha256 differ" in problem for problem in problems
    )


def test_evaluate_report_rejects_a_warm_child_in_the_parent_process():
    tool = load()
    report = passing_report(tool)
    report["child"]["report"]["pid"] = report["process"]["pid"]
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any("parent's process" in problem for problem in problems)


def test_evaluate_report_rejects_a_warm_child_that_opened_the_source():
    tool = load()
    report = passing_report(tool)
    report["child"]["report"]["source_open_count"] = 1
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any("opened the source" in problem for problem in problems)


def test_evaluate_report_rejects_an_unknown_check():
    tool = load()
    report = passing_report(tool)
    report["checks"]["invented"] = True
    ok, problems = tool.evaluate_report(report)
    assert not ok
    assert any("unknown checks: invented" in problem for problem in problems)


def test_evaluate_child_report_requires_exact_true():
    tool = load()
    report = {
        "checks": {name: True for name in tool.CHILD_CHECK_NAMES},
        "errors": [],
    }
    ok, problems = tool.evaluate_child_report(report)
    assert ok
    assert problems == []
    report["checks"]["zero_source_opens"] = "true"
    ok, problems = tool.evaluate_child_report(report)
    assert not ok
    assert any("failed check: zero_source_opens" in problem for problem in problems)


# ---------------------------------------------------------------------------
# the observations
# ---------------------------------------------------------------------------
def test_apply_observations_requires_the_accounted_bytes():
    tool = load()
    report = tool.new_report()
    measured = observations()
    measured["cell_payload_bytes"] = 0
    tool.apply_observations(report, measured)
    assert report["checks"]["cache_disk_accounted"] is False


def test_apply_observations_rejects_an_empty_coverage():
    """A cell with no coverage cannot pass as a blank rendering."""
    tool = load()
    report = tool.new_report()
    measured = observations()
    measured["raw"]["coverage_geometry_count"] = 0
    measured["prepared"]["coverage_geometry_count"] = 0
    tool.apply_observations(report, measured)
    assert report["checks"]["coverage_preserved"] is False


def test_apply_observations_rejects_a_missing_digest():
    tool = load()
    report = tool.new_report()
    measured = observations()
    del measured["prepared"]["pixel_sha256"]
    tool.apply_observations(report, measured)
    assert report["checks"]["pixels_match"] is False


def test_apply_observations_rejects_a_lost_scale():
    tool = load()
    report = tool.new_report()
    measured = observations()
    measured["prepared"]["native_scale"] = 0
    tool.apply_observations(report, measured)
    assert report["checks"]["native_scale_preserved"] is False


# ---------------------------------------------------------------------------
# the warm child
# ---------------------------------------------------------------------------
def test_compare_child_rejects_a_warm_child_that_opened_the_source():
    tool = load()
    outcome = child_result()
    outcome["report"]["source_open_count"] = 1
    checks = tool.compare_child(outcome, observations()["prepared"], os.getpid())
    assert checks["warm_zero_source_opens"] is False
    assert checks["warm_child_succeeded"] is True


def test_compare_child_rejects_a_child_that_is_not_fresh_and_does_not_match():
    tool = load()
    outcome = child_result()
    outcome["report"]["pid"] = os.getpid()
    outcome["report"]["pixel_sha256"] = "d" * 64
    checks = tool.compare_child(outcome, observations()["prepared"], os.getpid())
    assert checks["warm_child_fresh_process"] is False
    assert checks["warm_output_matches"] is False
    assert checks["warm_zero_source_opens"] is True


def test_compare_child_rejects_a_child_that_timed_out():
    tool = load()
    outcome = child_result()
    outcome["timed_out"] = True
    outcome["error"] = "the warm child exceeded 60 s"
    checks = tool.compare_child(outcome, observations()["prepared"], os.getpid())
    assert checks == {name: False for name in tool.WARM_CHECK_NAMES}


def test_compare_child_rejects_a_child_that_exited_non_zero():
    tool = load()
    outcome = child_result()
    outcome["returncode"] = 1
    checks = tool.compare_child(outcome, observations()["prepared"], os.getpid())
    assert checks == {name: False for name in tool.WARM_CHECK_NAMES}


def test_compare_child_rejects_a_forged_complete_child():
    """A child that says it completed is not believed on its word."""
    tool = load()
    outcome = child_result()
    outcome["report"]["errors"] = ["RuntimeError: the artifact could not be read"]
    checks = tool.compare_child(outcome, observations()["prepared"], os.getpid())
    assert checks == {name: False for name in tool.WARM_CHECK_NAMES}


def test_compare_child_rejects_a_child_whose_own_check_failed():
    tool = load()
    outcome = child_result()
    outcome["report"]["checks"]["zero_source_opens"] = False
    checks = tool.compare_child(outcome, observations()["prepared"], os.getpid())
    assert checks["warm_child_succeeded"] is False


def test_compare_child_rejects_a_child_that_did_not_complete():
    tool = load()
    outcome = child_result()
    outcome["report"]["complete"] = False
    checks = tool.compare_child(outcome, observations()["prepared"], os.getpid())
    assert checks["warm_child_succeeded"] is False


def test_compare_child_rejects_a_child_with_a_different_scale():
    tool = load()
    outcome = child_result()
    outcome["report"]["native_scale"] = 9000
    checks = tool.compare_child(outcome, observations()["prepared"], os.getpid())
    assert checks["warm_output_matches"] is False


def test_child_config_carries_the_expected_hashes():
    tool = load()
    measured = observations()
    config = tool.child_config(measured)
    assert config["expected"]["pixel_sha256"] == measured["prepared"]["pixel_sha256"]
    assert (
        config["expected"]["inventory_sha256"]
        == measured["prepared"]["inventory_sha256"]
    )
    assert (
        config["expected"]["coverage_sha256"] == measured["prepared"]["coverage_sha256"]
    )
    assert config["expected"]["native_scale"] == measured["prepared"]["native_scale"]
    assert config["camera"] == measured["camera"]


# ---------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------
class _Drawer:
    def __init__(self, palette="dark"):
        self.palette = palette

    def draw(self, gpsmap, cr, vector_file, bounding):
        return None


class _Chart:
    def __init__(self, drawer=None):
        if drawer is not None:
            self.drawer = drawer

    def do_draw(self, gpsmap, cr):
        return None


def test_require_drawer_accepts_a_chart_that_brings_its_own():
    tool = load()
    drawer, origin = tool.require_drawer(_Chart(_Drawer("dark")), "dark")
    assert isinstance(drawer, _Drawer)
    assert origin == "prepared"


def test_require_drawer_refuses_a_chart_without_a_drawer():
    tool = load()
    with pytest.raises(tool.GateError):
        tool.require_drawer(_Chart(), "dark")


def test_require_drawer_refuses_a_drawer_that_cannot_draw():
    tool = load()
    chart = _Chart()
    chart.drawer = type("Empty", (), {"palette": "dark"})()
    with pytest.raises(tool.GateError):
        tool.require_drawer(chart, "dark")


def test_require_drawer_refuses_a_chart_that_cannot_be_drawn():
    tool = load()
    chart = type("NoDraw", (), {"drawer": _Drawer("dark")})()
    with pytest.raises(tool.GateError):
        tool.require_drawer(chart, "dark")


def test_require_drawer_refuses_a_different_palette():
    tool = load()
    with pytest.raises(tool.GateError):
        tool.require_drawer(_Chart(_Drawer("navionics")), "dark")


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def test_run_with_an_injected_engine_passes_when_everything_matches(tmp_path):
    tool = load()
    output = tmp_path / "report.json"
    code = tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=FakeEngine(),
        child_runner=child_runner(child_result()),
    )
    report = json.loads(output.read_text())
    assert code == 0
    assert report["complete"] is True
    assert report["verdict"] == "pass"
    assert report["problems"] == []
    assert all(report["checks"].values())
    assert report["render"]["ink_pixels"] == 4096
    assert report["bytes"]["cache_disk_bytes"] == 4096


def test_run_fails_when_an_observation_is_missing(tmp_path):
    tool = load()
    measured = observations()
    del measured["prepared"]["pixel_sha256"]
    output = tmp_path / "report.json"
    code = tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=FakeEngine(measured=measured),
        child_runner=child_runner(child_result()),
    )
    report = json.loads(output.read_text())
    assert code == 0
    assert report["complete"] is False
    assert report["verdict"] == "fail"
    assert report["checks"]["pixels_match"] is False
    assert any(
        "failed check: pixels_match" in problem for problem in report["problems"]
    )


def test_run_fails_when_an_observation_is_false(tmp_path):
    tool = load()
    measured = observations()
    measured["prepared"]["pixel_sha256"] = "e" * 64
    measured["differences"] = ["the drawn pixels differ"]
    output = tmp_path / "report.json"
    code = tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=FakeEngine(measured=measured),
        child_runner=child_runner(child_result()),
    )
    report = json.loads(output.read_text())
    assert code == 0
    assert report["complete"] is False
    assert report["checks"]["pixels_match"] is False
    assert report["checks"]["render_nonblank"] is True
    assert report["differences"]["summary"] == ["the drawn pixels differ"]
    assert any(
        "failed check: pixels_match" in problem for problem in report["problems"]
    )


def test_run_reports_a_snapshot_that_is_not_the_approved_one(tmp_path):
    tool = load()
    output = tmp_path / "report.json"
    code = tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=FakeEngine(digest="0" * 64),
        child_runner=child_runner(child_result()),
    )
    report = json.loads(output.read_text())
    assert code == 0
    assert report["checks"]["fixture_verified"] is False
    assert report["errors"]
    assert report["complete"] is False
    assert report["verdict"] == "fail"


def test_run_fails_when_the_prepared_chart_has_no_drawer(tmp_path):
    tool = load()
    output = tmp_path / "report.json"
    code = tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=FakeEngine(error=tool.GateError("the prepared chart carries no drawer")),
        child_runner=child_runner(child_result()),
    )
    report = json.loads(output.read_text())
    assert code == 0
    assert report["complete"] is False
    assert any("carries no drawer" in error for error in report["errors"])


def test_run_fails_when_the_warm_child_opened_the_source(tmp_path):
    tool = load()
    outcome = child_result()
    outcome["report"]["source_open_count"] = 1
    output = tmp_path / "report.json"
    tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=FakeEngine(),
        child_runner=child_runner(outcome),
    )
    report = json.loads(output.read_text())
    assert report["complete"] is False
    assert report["checks"]["warm_zero_source_opens"] is False
    assert any("opened the source" in problem for problem in report["problems"])


def test_run_fails_when_the_warm_child_timed_out(tmp_path):
    tool = load()
    outcome = child_result()
    outcome["timed_out"] = True
    outcome["error"] = "the warm child exceeded 60 s"
    output = tmp_path / "report.json"
    tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=FakeEngine(),
        child_runner=child_runner(outcome),
    )
    report = json.loads(output.read_text())
    assert report["complete"] is False
    assert report["checks"]["warm_child_succeeded"] is False
    assert any("timed out" in problem for problem in report["problems"])


def test_run_removes_its_sandbox(tmp_path):
    tool = load()
    engine = FakeEngine()
    output = tmp_path / "report.json"
    tool.run(
        Path("/nonexistent-snapshot"),
        output,
        engine=engine,
        child_runner=child_runner(child_result()),
    )
    assert engine.sandbox_root is not None
    assert not engine.sandbox_root.exists()


# ---------------------------------------------------------------------------
# the tool's own loading
# ---------------------------------------------------------------------------
def test_the_tool_loads_with_its_dataclass_annotations():
    tool = load()
    assert tool.CHECK_NAMES
    assert tool.CHILD_CHECK_NAMES
    assert set(tool.WARM_CHECK_NAMES) <= set(tool.CHECK_NAMES)


def test_load_module_registers_the_module_for_dataclasses():
    tool = load()
    module = tool._load_module(
        "enc_prepared_gate_reloaded", ROOT / "tools" / "check_prepared_enc.py"
    )
    assert sys.modules["enc_prepared_gate_reloaded"] is module
