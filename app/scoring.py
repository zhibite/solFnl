"""Wallet quality score (0-100).

The score is intentionally transparent and uses only data we already store:
- daily_profit calendar (window, default 90 days)
- wallet_meta from GMGN public endpoints

Weights (sum = 100):
  profitability 30  - absolute realized profit (15) + return ratios (15)
  win_rate      20  - win_sells / (win_sells + loss_sells)
  consistency   20  - positive-day ratio (10) + profit factor (10)
  social        15  - GMGN follow_count (10) + remark_count (5)
  risk          15  - 1 - weighted GMGN risk ratios

When the meta snapshot is missing, social/risk/return components are 0,
so the score is provisional until the next meta sync. Win-rate and
consistency points are scaled by a sample-confidence factor when fewer
than 30 calendar days are available (floor 50%). Scores are clamped to
[0, 100] and rounded to one decimal.
"""

import math
from typing import Any, Dict, List, Optional

WEIGHTS = {
    "profitability": 30.0,
    "win_rate": 20.0,
    "consistency": 20.0,
    "social": 15.0,
    "risk": 15.0,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def compute_wallet_score(days: List[Dict[str, Any]],
                         meta: Optional[Dict[str, Any]],
                         window_days: int = 90) -> Dict[str, Any]:
    """Compute a 0-100 score from calendar rows and a GMGN meta snapshot."""
    meta = meta or {}
    if "has_data" in meta:
        meta_available = bool(meta.get("has_data"))
    else:
        # Fallback for callers that build a meta dict by hand.
        meta_available = (bool(meta.get("fetched_at"))
                          and not meta.get("last_error"))

    # ---- calendar metrics over the scoring window -------------------------
    n_days = len(days)
    total_pnl = sum(_num(d.get("realized_profit")) for d in days)
    gains = sum(_num(d.get("gains")) for d in days)
    losses = sum(_num(d.get("losses")) for d in days)  # negative
    win_sells = sum(_int(d.get("win_sells")) for d in days)
    loss_sells = sum(_int(d.get("loss_sells")) for d in days)
    positive_days = sum(1 for d in days
                        if _num(d.get("realized_profit")) > 0)
    positive_ratio = positive_days / n_days if n_days else 0.0
    sells = win_sells + loss_sells
    win_rate = win_sells / sells if sells else 0.0
    if losses < 0:
        profit_factor = gains / abs(losses)
    elif gains > 0:
        profit_factor = 3.0  # no losing sells: treat as the top bucket
    else:
        profit_factor = 0.0

    # ---- meta metrics -----------------------------------------------------
    total_profit = _num(meta.get("total_profit")) or total_pnl
    pnl_7d = _num(meta.get("pnl_7d"))
    total_profit_pnl = _num(meta.get("total_profit_pnl"))
    follow_count = _int(meta.get("follow_count"))
    remark_count = _int(meta.get("remark_count"))
    tags = meta.get("tags") or []
    risk = meta.get("risk") or {}

    # ---- profitability: 15 absolute + 15 returns --------------------------
    # log10 cap at 6.5 (~3.16M USD) => full absolute-profit points
    absolute_score = 15.0 * _clamp(
        math.log10(1.0 + max(total_profit, 0.0)) / 6.5)
    if meta_available:
        ret = (pnl_7d + total_profit_pnl) / 2.0
        # -5% return -> 0, +30% return -> full
        return_score = 15.0 * _clamp((ret + 0.05) / 0.35)
    else:
        return_score = 0.0
    profitability = absolute_score + return_score

    # Fewer than 30 calendar days are treated as a partial sample: the
    # win-rate and consistency points are scaled down to at least 50%.
    calendar_confidence = _clamp(n_days / 30.0)
    calendar_factor = 0.5 + 0.5 * calendar_confidence

    # ---- win rate: 30% -> 0, 70% -> 20 ------------------------------------
    win_rate_score = 20.0 * _clamp((win_rate - 0.30) / 0.40) * calendar_factor

    # ---- consistency: positive days + profit factor -----------------------
    positive_score = 10.0 * _clamp((positive_ratio - 0.30) / 0.40)
    pf_score = 10.0 * _clamp(
        math.log(max(profit_factor, 1.0)) / math.log(3.0))
    consistency = (positive_score + pf_score) * calendar_factor

    # ---- social: follow_count (10) + remark_count (5) ---------------------
    def _social_norm(n: int) -> float:
        return _clamp(math.log10(1 + max(n, 0)) / 3.5)

    social = 10.0 * _social_norm(follow_count) + 5.0 * _social_norm(remark_count)
    if "top_followed" in tags and meta_available:
        social = min(WEIGHTS["social"], social + 1.0)

    # ---- risk: 1 - weighted honeypot / no-buy / sell-pass / fast-tx ------
    weighted_risk: Optional[float] = None
    if meta_available:
        weighted_risk = (
            0.5 * _clamp(_num(risk.get("token_honeypot_ratio")))
            + 0.2 * _clamp(_num(risk.get("no_buy_hold_ratio")))
            + 0.2 * _clamp(_num(risk.get("sell_pass_buy_ratio")))
            + 0.1 * _clamp(_num(risk.get("fast_tx_ratio")))
        )
        risk_score = 15.0 * (1.0 - weighted_risk)
    else:
        risk_score = 0.0

    total = _clamp(profitability + win_rate_score + consistency + social
                   + risk_score, 0.0, 100.0)
    score = round(total, 1)

    components = {
        "profitability": {
            "score": round(profitability, 1),
            "max": WEIGHTS["profitability"],
            "absolute_profit": round(absolute_score, 1),
            "returns": round(return_score, 1),
        },
        "win_rate": {
            "score": round(win_rate_score, 1),
            "max": WEIGHTS["win_rate"],
            "value": round(win_rate, 4),
        },
        "consistency": {
            "score": round(consistency, 1),
            "max": WEIGHTS["consistency"],
            "positive_day_ratio": round(positive_ratio, 4),
            "profit_factor": round(profit_factor, 3),
        },
        "social": {
            "score": round(social, 1),
            "max": WEIGHTS["social"],
            "follow_count": follow_count,
            "remark_count": remark_count,
        },
        "risk": {
            "score": round(risk_score, 1),
            "max": WEIGHTS["risk"],
            "weighted_risk": (None if weighted_risk is None
                              else round(weighted_risk, 4)),
        },
    }
    metrics = {
        "window_days": window_days,
        "days": n_days,
        "net_pnl": round(total_pnl, 2),
        "gains": round(gains, 2),
        "losses": round(losses, 2),
        "win_sells": win_sells,
        "loss_sells": loss_sells,
        "positive_days": positive_days,
        "calendar_confidence": round(calendar_confidence, 3),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 3),
        "total_profit": round(total_profit, 2),
        "pnl_7d": round(pnl_7d, 4),
        "total_profit_pnl": round(total_profit_pnl, 4),
        "follow_count": follow_count,
        "remark_count": remark_count,
        "meta_available": meta_available,
    }
    return {
        "score": score,
        "grade": _grade(score),
        "window_days": window_days,
        "components": components,
        "metrics": metrics,
    }
