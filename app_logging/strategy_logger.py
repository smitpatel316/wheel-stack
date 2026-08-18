"""
Strategy Logger - Hybrid Architecture v2 with 27 decision factors per trade
v2.5.4 - 30 factors including real_pnl vs optionable_pnl discrepancy tracking

Paper arXiv:2512.01123 claims 27 decision factors per trade for full transparency:
- Volatility assessment, OTM %, premium rates, risk levels, position sizing,
  market context, explicit decision rationale, assignment probability, etc.

This logger expands original to support Bayesian CPT building.

Each trade entry now includes:
- Market context: regime, vix, trend, technical, vol level, IV rank
- Option metrics: delta, theta, vega, gamma (if available), bid/ask spread, OI, volume, IV
- Risk: assignment prob, premium rate, annualized yield, OTM %, breach %, DTE, loss/profit %
- Position: size, portfolio %, exposure, buying power
- Decision: roll type, urgency, reasons, BN nodes/edges, scoring
- Outcome placeholders for feedback loop
- v2.5.4 P/L: real_pnl vs optionable_pnl discrepancy (phantom $568 bug), fees, gross/net

Logs to:
- logs/strategy_log.json (legacy list format)
- logs/wheel_trades.jsonl (new: one JSON per trade, 27+ factors, append-only for CPT building)
- logs/market_context.json (from context_analyzer)
"""
from pathlib import Path
from datetime import datetime, date
from core.utils import get_ny_timestamp
import json
import math

# Paper's 27 factors expanded v2.5 - now includes dividends, fundamentals, iv_rank, execution, assignment defense
# v2.5.4 30 factors + P/L tracking
FACTOR_CATEGORIES = {
    "market_regime": ["market_regime", "vix", "vix_level", "trend", "technical_position", "spy_change_5d", "market_open"],
    "volatility": ["volatility_level", "iv_rank", "iv", "vega", "vix_level", "realized_vol_20d"],
    "option_fundamentals": ["delta", "theta", "gamma", "vega", "iv", "bid_price", "ask_price", "spread_pct", "oi", "volume", "iv_rank", "execution_improvement"],
    "premium": ["premium_rate", "annualized_yield", "bid_price", "strike", "otm_pct", "underlying_price"],
    "risk": ["assignment_prob_est", "itm_pct", "breach_pct", "dte", "loss_pct", "profit_pct", "delta_abs", "debt_equity", "earnings_days"],
    "position": ["position_size_pct", "portfolio_pct", "qty", "buying_power", "options_bp", "risk_exposure", "max_risk"],
    "decision": ["score", "roll_type", "urgency", "reasons", "bn_reasoning", "strategy_version", "dividend_ex_days", "pe_ratio"],
    "fundamentals": ["pe_ratio", "debt_equity", "dividend_yield", "beta", "market_cap"],
    "pnl": ["real_pnl", "real_pnl_realized", "real_pnl_unrealized", "optionable_pnl", "pnl_discrepancy", "pnl_discrepancy_pct", "fees", "real_pnl_gross", "real_pnl_net", "profit_dollars_gross", "profit_dollars_net"],
}

ALL_27_FACTORS = [
    "timestamp", "market_regime", "volatility_level", "vix", "vix_level", "technical_position",
    "underlying", "underlying_price", "strike", "otm_pct", "itm_pct", "dte",
    "delta", "theta", "gamma", "vega", "iv", "bid_price", "ask_price", "spread_pct",
    "oi", "premium_rate", "annualized_yield", "assignment_prob_est",
    "position_size_pct", "portfolio_pct", "score", "roll_type", "reasons", "outcome",
    # v2.5 additions
    "iv_rank", "pe_ratio", "debt_equity", "dividend_ex_days", "earnings_days", "execution_improvement", "market_open"
]

# v2.5.4 30 factors definitive - includes real vs optionable P/L
ALL_30_FACTORS = [
    "timestamp", "market_regime", "volatility_level", "vix", "vix_level", "technical_position",
    "underlying", "underlying_price", "strike", "otm_pct", "itm_pct", "dte",
    "delta", "theta", "gamma", "vega", "iv", "bid_price", "ask_price", "spread_pct",
    "oi", "premium_rate", "annualized_yield", "assignment_prob_est",
    "position_size_pct", "portfolio_pct", "score", "roll_type", "reasons", "outcome",
    # v2.5 additions
    "iv_rank", "pe_ratio", "debt_equity", "dividend_ex_days", "earnings_days", "execution_improvement", "market_open",
    # v2.5.4 P/L 30-factor expansion - real vs optionable discrepancy (fixes $568 phantom)
    "real_pnl", "real_pnl_realized", "real_pnl_unrealized", "optionable_pnl", "pnl_discrepancy", "pnl_discrepancy_pct", "fees",
    "real_pnl_gross", "real_pnl_net", "profit_dollars_gross", "profit_dollars_net",
    "sgov_yield", "rh_mcp_enabled", "close_before_open_delay", "spread_max_abs", "commission"
]
# Note: actually >30 to be safe, but core 30 map to paper + v2.5.4 P/L fix

class StrategyLogger:
    def __init__(self, enabled=True, log_path="logs/strategy_log.json", jsonl_path="logs/wheel_trades.jsonl"):
        self.enabled = enabled
        self.log_file = Path(log_path)
        self.jsonl_file = Path(jsonl_path)
        self.log_entry = {}

        if self.enabled:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self.jsonl_file.parent.mkdir(parents=True, exist_ok=True)
            self.log_entry["datetime"] = get_ny_timestamp()
            self.log_entry["version"] = "hybrid-v2-30factors-v2.5.4"
            self.log_entry["strategy_version"] = "model-first-hybrid-paper-2512.01123-v2.5.4-spaxx-pnl-fix"

    def set_fresh_start(self, is_fresh_start: bool):
        if self.enabled:
            self.log_entry["fresh_start"] = is_fresh_start
            self.log_entry["current_positions"] = []

    def add_current_positions(self, positions: list):
        if self.enabled and not self.log_entry.get("fresh_start"):
            self.log_entry["current_positions"] = [
                {
                    "asset_class": getattr(pos.asset_class, 'title', lambda: str(pos.asset_class))().lower() if hasattr(pos, 'asset_class') else str(getattr(pos, 'asset_class', 'unknown')),
                    "symbol": pos.symbol,
                    "side": getattr(pos, 'side', 'unknown'),
                    "qty": getattr(pos, 'qty', 0),
                    "purchase_price": getattr(pos, 'avg_entry_price', 0),
                    "current_price": getattr(pos, 'current_price', 0),
                    "pnl": getattr(pos, 'unrealized_pl', 0),
                }
                for pos in positions
            ]

    def add_state_dict(self, state_dict: dict):
        if self.enabled:
            self.log_entry["state_dict"] = state_dict

    def set_buying_power(self, buying_power: float):
        if self.enabled:
            self.log_entry["buying_power"] = buying_power

    def set_allowed_symbols(self, symbols: list):
        if self.enabled:
            self.log_entry["allowed_symbols"] = symbols

    def set_filtered_symbols(self, symbols: list):
        if self.enabled:
            self.log_entry["filtered_symbols"] = symbols

    def log_call_options(self, call_options: list[dict]):
        if self.enabled:
            self.log_entry["call_options"] = call_options

    def log_put_options(self, put_options: list[dict]):
        if self.enabled:
            self.log_entry["put_options"] = put_options

    def log_sold_calls(self, call_dict: dict):
        if self.enabled:
            if self.log_entry.get("sold_calls") is None:
                self.log_entry["sold_calls"] = []
            self.log_entry["sold_calls"].append(call_dict)

    def log_sold_puts(self, put_dict: dict):
        if self.enabled:
            if self.log_entry.get("sold_puts") is None:
                self.log_entry["sold_puts"] = []
            self.log_entry["sold_puts"].append(put_dict)

    # --- New hybrid methods ---

    def set_market_context(self, market_context):
        """Attach MarketContext from context_analyzer"""
        if not self.enabled:
            return
        try:
            if hasattr(market_context, 'to_dict'):
                ctx_dict = market_context.to_dict()
            else:
                ctx_dict = market_context
            self.log_entry["market_context"] = ctx_dict
            # Flatten top-level for quick access
            self.log_entry["market_regime"] = ctx_dict.get("market_regime", "unknown")
            self.log_entry["vix"] = ctx_dict.get("vix")
            self.log_entry["vix_level"] = ctx_dict.get("vix_level")
            self.log_entry["volatility_level"] = ctx_dict.get("volatility_level")
            self.log_entry["technical_position"] = ctx_dict.get("technical_position")
            self.log_entry["bn_nodes"] = ctx_dict.get("bn_nodes", [])
            self.log_entry["bn_edges"] = ctx_dict.get("bn_edges", [])
            self.log_entry["bn_reasoning"] = ctx_dict.get("bn_reasoning", "")
        except Exception as e:
            self.log_entry["market_context_error"] = str(e)

    def log_roll_decisions(self, roll_decisions: list):
        """Log rolling evaluation"""
        if not self.enabled:
            return
        try:
            serializable = []
            for d in roll_decisions:
                if hasattr(d, 'candidate'):
                    serializable.append({
                        "symbol": d.candidate.symbol,
                        "underlying": d.candidate.underlying,
                        "strike": d.candidate.strike,
                        "dte": d.candidate.dte,
                        "should_roll": d.should_roll,
                        "roll_type": d.roll_type,
                        "urgency": d.urgency,
                        "reasons": d.reasons,
                        "decision_factors": d.decision_factors,
                        "itm_pct": d.candidate.itm_pct,
                        "loss_pct": d.candidate.loss_pct,
                        "profit_pct": d.candidate.profit_pct,
                    })
                else:
                    serializable.append(d)
            self.log_entry["roll_decisions"] = serializable
        except Exception as e:
            self.log_entry["roll_decisions_error"] = str(e)

    def log_close_decisions(self, close_decisions: list):
        """v2.5.4 Log closer evaluation with real P/L including fees"""
        if not self.enabled:
            return
        try:
            serializable = []
            for d in close_decisions:
                if hasattr(d, 'candidate'):
                    # Include fees and gross/net from decision_factors if present
                    df = getattr(d, 'decision_factors', {}) or {}
                    serializable.append({
                        "symbol": d.candidate.symbol,
                        "underlying": d.candidate.underlying,
                        "strike": d.candidate.strike,
                        "dte": d.candidate.dte,
                        "should_close": d.should_close,
                        "close_type": d.close_type,
                        "urgency": d.urgency,
                        "reasons": d.reasons,
                        "decision_factors": df,
                        "profit_pct": d.profit_pct,
                        "profit_dollars": d.profit_dollars,
                        "profit_gross": df.get("profit_dollars_gross", d.profit_dollars),
                        "profit_net": df.get("profit_dollars_net", d.profit_dollars),
                        "fees": df.get("fees_estimated", 0),
                        "real_pnl_gross": df.get("real_pnl_gross", 0),
                        "real_pnl_net": df.get("real_pnl_net", 0),
                    })
                else:
                    serializable.append(d)
            self.log_entry["close_decisions"] = serializable
        except Exception as e:
            self.log_entry["close_decisions_error"] = str(e)

    def _enrich_contract_dict(self, contract_dict: dict, market_context: dict = None) -> dict:
        """
        Enrich single contract dict to 27 factors.
        Expects contract_dict from Contract.to_dict() plus optionally extra fields.
        v2.5.4 also adds P/L fields if available
        """
        try:
            underlying_price = contract_dict.get("underlying_price") or 0
            strike = contract_dict.get("strike") or 0
            bid = contract_dict.get("bid_price") or 0
            ask = contract_dict.get("ask_price") or bid
            dte = contract_dict.get("dte") or 30
            delta = contract_dict.get("delta") or 0
            oi = contract_dict.get("oi")

            # OTM %
            otm_pct = 0
            itm_pct = 0
            if underlying_price and strike:
                if contract_dict.get("contract_type") == "put":
                    otm_pct = (underlying_price - strike) / strike
                    itm_pct = (strike - underlying_price) / strike
                else:
                    otm_pct = (strike - underlying_price) / strike
                    itm_pct = (underlying_price - strike) / strike

            # Spread %
            spread_pct = 0
            if bid and ask:
                mid = (bid + ask) / 2
                if mid:
                    spread_pct = (ask - bid) / mid

            # Premium rate
            premium_rate = (bid / strike) if strike else 0
            ann_yield = (bid / strike * 365 / (dte+1)) if strike and dte else 0

            # Assignment prob estimate from delta (delta ~ prob ITM)
            assignment_prob = abs(delta) if delta else 0.25

            enriched = dict(contract_dict)  # copy
            enriched.update({
                "otm_pct": otm_pct,
                "itm_pct": itm_pct,
                "spread_pct": spread_pct,
                "premium_rate": premium_rate,
                "annualized_yield": ann_yield,
                "assignment_prob_est": assignment_prob,
                "delta_abs": abs(delta) if delta else 0,
                "theta": contract_dict.get("theta"),
                "gamma": contract_dict.get("gamma"),
                "vega": contract_dict.get("vega"),
                "iv": contract_dict.get("iv"),
                "volume": contract_dict.get("volume"),
                "enriched_at": get_ny_timestamp(),
            })

            # Merge market context if provided
            if market_context:
                enriched["market_regime"] = market_context.get("market_regime")
                enriched["vix_level"] = market_context.get("vix_level")
                enriched["volatility_level"] = market_context.get("volatility_level")
                enriched["technical_position"] = market_context.get("technical_position")

            # JSON-safety: fields like dividend_ex arrive as datetime.date
            # from the dividend calendar. Left as-is, json.dumps raises and
            # the WHOLE journal entry is silently dropped (2026-08-18: the KO
            # 59% profit-take close never reached wheel_trades.jsonl because
            # KO's ex-div date 2026-09-15 was a date object).
            for k, v in list(enriched.items()):
                if isinstance(v, (datetime, date)):
                    enriched[k] = v.isoformat()

            return enriched
        except Exception as e:
            # Return original with error flag
            d = dict(contract_dict)
            d["enrich_error"] = str(e)
            return d

    def log_detailed_trade(self, contract_dict: dict, score: float = None, decision_type: str = "new_put", market_context=None):
        """
        Log single trade with 27+ factors to JSONL for Bayesian CPT building.
        One line per trade.
        v2.5.4 adds P/L discrepancy fields
        """
        if not self.enabled:
            return
        try:
            mc_dict = None
            if market_context:
                if hasattr(market_context, 'to_dict'):
                    mc_dict = market_context.to_dict()
                else:
                    mc_dict = market_context

            enriched = self._enrich_contract_dict(contract_dict, mc_dict)

            # v2.5.4 P/L fields from log_entry if prior pnl_tracker called
            pnl_info = self.log_entry.get("pnl_summary", {}) or {}

            # Add scoring + decision metadata
            jsonl_entry = {
                "timestamp": get_ny_timestamp(),
                "trade_type": decision_type,  # new_put, new_call, roll_defensive, roll_offensive, close_profit, etc.
                "score": score,
                "contract": enriched,
                # Flatten top 27 for CPT query ease
                "underlying": enriched.get("underlying"),
                "symbol": enriched.get("symbol"),
                "strike": enriched.get("strike"),
                "dte": enriched.get("dte"),
                "delta": enriched.get("delta"),
                "delta_abs": enriched.get("delta_abs"),
                "bid_price": enriched.get("bid_price"),
                "ask_price": enriched.get("ask_price"),
                "spread_pct": enriched.get("spread_pct"),
                "oi": enriched.get("oi"),
                "otm_pct": enriched.get("otm_pct"),
                "itm_pct": enriched.get("itm_pct"),
                "premium_rate": enriched.get("premium_rate"),
                "annualized_yield": enriched.get("annualized_yield"),
                "assignment_prob_est": enriched.get("assignment_prob_est"),
                "market_regime": enriched.get("market_regime"),
                "vix": enriched.get("vix") if isinstance(enriched.get("vix"), (int, float)) else (mc_dict.get("vix") if mc_dict else None),
                "vix_level": enriched.get("vix_level"),
                "volatility_level": enriched.get("volatility_level"),
                "technical_position": enriched.get("technical_position"),
                "theta": enriched.get("theta"),
                "gamma": enriched.get("gamma"),
                "vega": enriched.get("vega"),
                "iv": enriched.get("iv"),
                # Paper factors: position sizing, risk, etc. filled from log_entry if available
                "buying_power": self.log_entry.get("buying_power"),
                "risk_exposure": self.log_entry.get("buying_power"),  # placeholder
                "bn_reasoning": self.log_entry.get("bn_reasoning", ""),
                # v2.5.4 P/L 30 factors - real vs optionable discrepancy
                "real_pnl": pnl_info.get("realized", enriched.get("real_pnl", 0)),
                "real_pnl_realized": pnl_info.get("realized", 0),
                "real_pnl_unrealized": pnl_info.get("unrealized", 0),
                "real_pnl_total": pnl_info.get("total", 0),
                "optionable_pnl": pnl_info.get("optionable_realized", enriched.get("optionable_pnl", 0)),
                "pnl_discrepancy": pnl_info.get("discrepancy", 0),
                "pnl_discrepancy_pct": pnl_info.get("discrepancy_pct", 0),
                "fees": pnl_info.get("fees", 0),
                "real_pnl_gross": enriched.get("real_pnl_gross", 0),
                "real_pnl_net": enriched.get("real_pnl_net", 0),
                "profit_dollars_gross": enriched.get("profit_dollars_gross", 0),
                "profit_dollars_net": enriched.get("profit_dollars_net", enriched.get("profit_dollars", 0)),
                # Execution & sweep factors v2.5.3/4
                "sgov_yield": self.log_entry.get("sgov_yield"),
                "rh_mcp_enabled": self.log_entry.get("rh_mcp_enabled", False),
                "close_before_open_delay": 2.0,
                "spread_max_abs": 0.15,
                "commission": enriched.get("commission", 0),
            }

            # Append to JSONL (default=str: never drop a trade over one
            # non-serializable field — a journaled trade with a stringified
            # field beats a silently missing trade)
            with open(self.jsonl_file, "a") as f:
                f.write(json.dumps(jsonl_entry, default=str) + "\n")

            # Also keep in main log for backward compat
            if self.log_entry.get("detailed_trades") is None:
                self.log_entry["detailed_trades"] = []
            self.log_entry["detailed_trades"].append(jsonl_entry)

        except Exception as e:
            # Don't crash strategy on logging failure
            if self.log_entry.get("logging_errors") is None:
                self.log_entry["logging_errors"] = []
            self.log_entry["logging_errors"].append(f"detailed_trade failed: {e}")

    def set_pnl_summary(self, pnl_summary: dict):
        """
        v2.5.4 NEW: attach real vs optionable P/L summary for 30-factor logging
        Call after get_real_pnl() from pnl_tracker
        """
        if not self.enabled:
            return
        try:
            self.log_entry["pnl_summary"] = pnl_summary
            # Flatten for quick access - these become top-level 30 factors
            self.log_entry["real_pnl"] = pnl_summary.get("realized", 0)
            self.log_entry["real_pnl_realized"] = pnl_summary.get("realized", 0)
            self.log_entry["real_pnl_unrealized"] = pnl_summary.get("unrealized", 0)
            self.log_entry["real_pnl_total"] = pnl_summary.get("total", 0)
            self.log_entry["real_pnl_fees"] = pnl_summary.get("fees", 0)
            self.log_entry["optionable_pnl"] = pnl_summary.get("optionable_realized", 0)
            self.log_entry["optionable_pnl_full"] = pnl_summary.get("optionable_pnl", {})
            self.log_entry["pnl_discrepancy"] = pnl_summary.get("discrepancy", 0)
            self.log_entry["pnl_discrepancy_pct"] = pnl_summary.get("discrepancy_pct", 0)
            self.log_entry["pnl_trade_count"] = len(pnl_summary.get("realized_breakdown", []))
            # Log warning if large discrepancy (phantom bug)
            if abs(pnl_summary.get("discrepancy",0)) > 50:
                if self.log_entry.get("warnings") is None:
                    self.log_entry["warnings"] = []
                self.log_entry["warnings"].append(
                    f"P/L discrepancy ${pnl_summary.get('discrepancy',0):.2f} real ${pnl_summary.get('realized',0):.2f} "
                    f"vs optionable ${pnl_summary.get('optionable_realized',0):.2f} - check closePrice=0 bug"
                )
        except Exception as e:
            self.log_entry["pnl_summary_error"] = str(e)

    def set_sgov_yield(self, sgov_info: dict):
        """v2.5.3 SGOV sweep yield for 30-factor logging"""
        if not self.enabled:
            return
        try:
            self.log_entry["sgov_yield"] = sgov_info
            self.log_entry["sgov_shares"] = sgov_info.get("shares",0) if isinstance(sgov_info, dict) else 0
        except Exception:
            pass

    def save(self):
        if not self.enabled:
            return

        # v2.5.4 ensure params snapshot for 30-factor completeness
        try:
            from config.params import (
                SPREAD_MAX_ABS, ROLL_CLOSE_BEFORE_OPEN_DELAY, SGOV_YIELD_APY, RH_MCP_ENABLED
            )
            self.log_entry["spread_max_abs"] = SPREAD_MAX_ABS
            self.log_entry["close_before_open_delay"] = ROLL_CLOSE_BEFORE_OPEN_DELAY
            self.log_entry["sgov_yield_apy"] = SGOV_YIELD_APY
            self.log_entry["rh_mcp_enabled"] = RH_MCP_ENABLED
            self.log_entry["factor_count"] = 30
            self.log_entry["factors_version"] = "v2.5.4-30factors-real-vs-optionable"
        except Exception:
            self.log_entry["factor_count"] = 30

        # Load existing log data if file exists
        if self.log_file.exists():
            with open(self.log_file, "r") as f:
                try:
                    data = json.load(f)
                    if not isinstance(data, list):
                        raise ValueError("Log file does not contain a list.")
                except json.JSONDecodeError:
                    data = []
        else:
            data = []

        # Append the new log entry
        data.append(self.log_entry)

        # Keep last 1000 entries to avoid bloat
        if len(data) > 1000:
            data = data[-1000:]

        # Write the updated list back
        with open(self.log_file, "w") as f:
            json.dump(data, f, indent=2)
