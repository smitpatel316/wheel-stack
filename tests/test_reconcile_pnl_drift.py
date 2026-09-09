"""reconcile_pnl drift reporting (P4 audit).

Regression: without --alert -- the documented cron invocation
(`0 2 * * * ... python scripts/reconcile_pnl.py >> logs/reconcile.log`) --
the script printed "OK Drift $X within threshold $50" even when the drift
EXCEEDED the threshold. The nightly log looked clean while P/L had drifted.
The message must reflect the actual comparison; --alert keeps gating the
nonzero exit.
"""
import importlib.util
import os
import sys
import types

import pytest

SPEC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "reconcile_pnl.py",
)


def load_mod(name):
    spec = importlib.util.spec_from_file_location(name, SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def rp(monkeypatch, tmp_path):
    mod = load_mod("reconcile_pnl_drift")
    monkeypatch.setattr(mod, "ROOT", tmp_path)  # keep reconcile.jsonl out of the repo
    return mod


def _run(rp, monkeypatch, drift, extra_args=()):
    fake_tracker = types.ModuleType("core.pnl_tracker")
    fake_tracker.get_pnl_summary_for_logging = lambda client: {"discrepancy": drift}
    fake_tracker.reconcile_optionable_vs_alpaca = lambda client: {"discrepancy": drift}
    monkeypatch.setitem(sys.modules, "core.pnl_tracker", fake_tracker)
    fake_broker = types.ModuleType("core.broker_client")

    class _DummyClient:
        def __init__(self, *a, **k):
            pass

    fake_broker.BrokerClient = _DummyClient
    monkeypatch.setitem(sys.modules, "core.broker_client", fake_broker)
    monkeypatch.setattr(sys, "argv", ["reconcile_pnl.py", *extra_args])
    return rp.main()


def test_over_threshold_without_alert_reports_drift_not_ok(rp, monkeypatch, capsys):
    _run(rp, monkeypatch, drift=500.0)
    out = capsys.readouterr().out
    assert "within threshold" not in out
    assert "500.00" in out
    assert "exceed" in out.lower()


def test_within_threshold_reports_ok(rp, monkeypatch, capsys):
    _run(rp, monkeypatch, drift=10.0)
    out = capsys.readouterr().out
    assert "OK Drift $10.00 within threshold $50.0" in out


def test_over_threshold_with_alert_exits_nonzero(rp, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(rp, monkeypatch, drift=500.0, extra_args=("--alert",))
    assert exc.value.code == 1
    assert "ALERT" in capsys.readouterr().out
