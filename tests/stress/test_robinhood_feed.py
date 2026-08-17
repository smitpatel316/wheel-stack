"""Stress tests: Robinhood shadow feed (core/robinhood_feed.py).

All RH traffic is mocked at _run_cli — the suite must never spawn the real
rh_mcp_client subprocess (it talks to Robinhood over the network).
"""
import json
from datetime import date, timedelta

import pytest

from core.robinhood_feed import RobinhoodFeed
from core.execution import sell_puts
from models.contract import Contract
from tests.stress.fakes import FakeBrokerClient, FakeAccount, make_put


def _cli_result(data, rc=0):
    """Build a fake CompletedProcess matching rh_mcp_client call output."""

    class P:
        returncode = rc
        stdout = json.dumps({"is_error": False, "structured_content": {"data": data}}) if rc == 0 else ""
        stderr = "" if rc == 0 else "boom"

    return P()


CHAIN = {"chains": [{"id": "chain-1", "expiration_dates": ["2099-01-16"], "can_open_position": True}]}
INSTR = {"instruments": [{"id": "inst-1", "chain_symbol": "F", "strike_price": "14.0000",
                          "expiration_date": "2099-01-16", "type": "put", "state": "active"}]}
QUOTE = {"results": [{"quote": {"bid_price": "0.30", "ask_price": "0.34"}, "close": {}}]}
EQ_QUOTE = {"results": [{"quote": {"symbol": "F", "bid_price": "14.04", "ask_price": "14.08",
                                   "last_trade_price": "14.05"}, "close": {}}]}


def _feed(tmp_path, responses=None, fail_with=None):
    f = RobinhoodFeed(client_dir=tmp_path, log_path=tmp_path / "cmp.jsonl", enabled=True)
    # fake the presence checks
    f.enabled = True
    calls = []

    def fake_run(tool, args):
        calls.append((tool, args))
        if fail_with is not None:
            raise fail_with
        return _cli_result(responses.get(tool, {}))

    f._run_cli = fake_run
    f._calls_log = calls
    return f


class TestFindPutQuote:
    def test_happy_path_matches_chain_instrument_quote(self, tmp_path):
        f = _feed(tmp_path, {"get_option_chains": CHAIN,
                             "get_option_instruments": INSTR,
                             "get_option_quotes": QUOTE})
        out = f.find_put_quote("F", 14.0, "2099-01-16")
        assert out == {"bid": 0.30, "ask": 0.34, "instrument_id": "inst-1", "rh_symbol": "F"}
        # strike passed as exact 4-decimal string, filtered to puts
        inst_call = [a for t, a in f._calls_log if t == "get_option_instruments"][0]
        assert inst_call["strike_price"] == "14.0000" and inst_call["type"] == "put"

    def test_expiration_not_on_chain_returns_none(self, tmp_path):
        f = _feed(tmp_path, {"get_option_chains": CHAIN})
        assert f.find_put_quote("F", 14.0, "2099-02-20") is None

    def test_chain_cached_per_underlying(self, tmp_path):
        f = _feed(tmp_path, {"get_option_chains": CHAIN,
                             "get_option_instruments": INSTR,
                             "get_option_quotes": QUOTE})
        f.find_put_quote("F", 14.0, "2099-01-16")
        f.find_put_quote("F", 14.0, "2099-01-16")
        chains = [t for t, _ in f._calls_log if t == "get_option_chains"]
        assert len(chains) == 1

    def test_missing_args_no_call(self, tmp_path):
        f = _feed(tmp_path, {})
        assert f.find_put_quote("F", None, "2099-01-16") is None
        assert not f._calls_log


class TestFailureModes:
    def test_subprocess_failure_returns_none_and_counts(self, tmp_path):
        f = _feed(tmp_path, fail_with=RuntimeError("timeout"))
        assert f.find_put_quote("F", 14.0, "2099-01-16") is None
        assert f._failures == 1

    def test_feed_disables_after_max_failures(self, tmp_path):
        f = _feed(tmp_path, fail_with=RuntimeError("down"))
        for _ in range(4):
            f.get_equity_quotes(["F"])
        assert f.enabled is False, "3 consecutive failures must disable the feed"

    def test_tool_error_payload_treated_as_failure(self, tmp_path):
        f = _feed(tmp_path)
        class P:
            returncode = 0
            stdout = json.dumps({"is_error": True, "content": [{"text": "bad"}]})
            stderr = ""
        f._run_cli = lambda t, a: P()
        assert f.get_equity_quotes(["F"]) == {}
        assert f._failures == 1

    def test_garbage_stdout_treated_as_failure(self, tmp_path):
        f = _feed(tmp_path)
        class P:
            returncode = 0
            stdout = "not json at all"
            stderr = ""
        f._run_cli = lambda t, a: P()
        assert f.get_equity_quotes(["F"]) == {}
        assert f._failures == 1


class TestCompareLogging:
    def _contract(self):
        return Contract(symbol="F29990116P00014000", underlying="F", strike=14.0,
                        expiration="2099-01-16", dte=30, bid_price=0.31,
                        ask_price=0.35, delta=-0.25)

    def test_compare_put_writes_side_by_side_line(self, tmp_path):
        f = _feed(tmp_path, {"get_option_chains": CHAIN,
                             "get_option_instruments": INSTR,
                             "get_option_quotes": QUOTE})
        line = f.compare_put(self._contract())
        assert line["alpaca"] == {"bid": 0.31, "ask": 0.35}
        assert line["rh"] == {"bid": 0.30, "ask": 0.34}
        assert line["alpaca_mid"] == pytest.approx(0.33)
        assert line["rh_mid"] == pytest.approx(0.32)
        assert line["mid_diff_pct"] == pytest.approx(-3.03, abs=0.01)
        on_disk = [json.loads(l) for l in (tmp_path / "cmp.jsonl").read_text().splitlines()]
        assert len(on_disk) == 1 and on_disk[0]["kind"] == "candidate"

    def test_rh_down_still_logs_alpaca_side(self, tmp_path):
        f = _feed(tmp_path, fail_with=RuntimeError("down"))
        line = f.compare_put(self._contract())
        assert line["rh"] is None and line["mid_diff_pct"] is None
        assert line["alpaca_mid"] == pytest.approx(0.33)

    def test_compare_underlyings(self, tmp_path):
        f = _feed(tmp_path, {"get_equity_quotes": EQ_QUOTE})
        lines = f.compare_underlyings({"F": 14.10, "KO": 87.5})
        by_sym = {l["symbol"]: l for l in lines}
        assert by_sym["F"]["rh_last"] == 14.05
        assert by_sym["F"]["diff_pct"] == pytest.approx(-0.355, abs=0.01)
        assert by_sym["KO"]["rh_last"] is None

    def test_summary_counts(self, tmp_path):
        f = _feed(tmp_path, {"get_option_chains": CHAIN,
                             "get_option_instruments": INSTR,
                             "get_option_quotes": QUOTE})
        f.compare_put(self._contract())
        s = f.summary()
        assert "1 candidates, 1 matched" in s


class TestSellPutsIntegration:
    """RH compare hooks into sell_puts but must never change decisions."""

    def test_candidates_compared_and_trade_unaffected(self, tmp_path):
        f = _feed(tmp_path, {"get_option_chains": CHAIN,
                             "get_option_instruments": INSTR,
                             "get_option_quotes": QUOTE})
        exp = date.today() + timedelta(days=30)
        c = FakeBrokerClient(FakeAccount(options_buying_power=100_000))
        raw, snap = make_put("F", 14.0, dte=30, bid=0.31, ask=0.35, delta=-0.25, oi=500)
        c.option_chain["F"] = [(raw, snap)]
        c.stock_trades["F"] = 15.40
        # point the fake chain expiry at what the engine actually produced
        f2 = _feed(tmp_path, {"get_option_chains": {"chains": [
            {"id": "chain-1", "expiration_dates": [exp.isoformat()], "can_open_position": True}]},
            "get_option_instruments": INSTR, "get_option_quotes": QUOTE})
        f2.log_path = tmp_path / "cmp.jsonl"
        sell_puts(c, ["F"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  rh_feed=f2)
        assert c.option_sells, "RH comparison must not block a normal sale"
        lines = (tmp_path / "cmp.jsonl").read_text().splitlines()
        assert len(lines) == 1
        d = json.loads(lines[0])
        assert d["alpaca_mid"] == pytest.approx(0.33) and d["rh_mid"] == pytest.approx(0.32)

    def test_rh_feed_crash_does_not_stop_selling(self, tmp_path):
        class Exploding(RobinhoodFeed):
            def compare_put(self, contract):
                raise RuntimeError("catastrophic")

        f = Exploding(client_dir=tmp_path, log_path=tmp_path / "x.jsonl", enabled=True)
        c = FakeBrokerClient(FakeAccount(options_buying_power=100_000))
        raw, snap = make_put("AAA", 50.0, dte=30, bid=1.0, ask=1.04, delta=-0.25, oi=500)
        c.option_chain["AAA"] = [(raw, snap)]
        c.stock_trades["AAA"] = 55.0
        sell_puts(c, ["AAA"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  rh_feed=f)
        assert c.option_sells, "a broken RH feed must never affect trading"

    def test_no_feed_by_default(self):
        c = FakeBrokerClient(FakeAccount(options_buying_power=100_000))
        raw, snap = make_put("AAA", 50.0, dte=30, bid=1.0, ask=1.04, delta=-0.25, oi=500)
        c.option_chain["AAA"] = [(raw, snap)]
        c.stock_trades["AAA"] = 55.0
        sell_puts(c, ["AAA"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert c.option_sells  # no rh_feed arg -> zero RH involvement
