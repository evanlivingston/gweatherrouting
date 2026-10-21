"""Prepared-cell acceptance must enforce individual observations, not just done."""

import importlib.util
from pathlib import Path

import pytest


def load_runner():
    path = Path(__file__).resolve().parents[1] / "tools" / "gate_runner.py"
    spec = importlib.util.spec_from_file_location("prepared_registry_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepared_gate_rejects_empty_report():
    runner = load_runner()
    rows = runner.evaluate(runner.gates.GATES["enc-prepared-cell"], {})
    assert rows
    assert all(row["verdict"] == "fail" for row in rows)


@pytest.mark.parametrize("missing", [False, True])
def test_prepared_gate_enforces_each_observation_even_when_complete(missing):
    runner = load_runner()
    gate = runner.gates.GATES["enc-prepared-cell"]
    names = [
        check["metric"].split(".", 1)[1]
        for check in gate["checks"]
        if check["metric"].startswith("checks.")
    ]
    assert len(names) == 15
    report = {"complete": True, "checks": dict.fromkeys(names, True)}
    assert all(row["verdict"] == "pass" for row in runner.evaluate(gate, report))
    for name in names:
        if missing:
            del report["checks"][name]
        else:
            report["checks"][name] = False
        rows = runner.evaluate(gate, report)
        assert sum(row["verdict"] == "fail" for row in rows) == 1, name
        report["checks"][name] = True


def test_prepared_gate_has_runner_execution_metadata():
    runner = load_runner()
    gate = runner.gates.GATES["enc-prepared-cell"]
    for key in ("reference", "tool_output", "result", "endpoint", "checks"):
        assert key in gate
    assert gate["reference"]["kind"] == "recorded-evidence"
    assert gate["timeout_s"] <= 120
