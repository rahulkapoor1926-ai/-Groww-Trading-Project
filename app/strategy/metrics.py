"""Performance metrics over a list of trades (in time order)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TradePnl:
    net: float
    gross: float
    friction: float


def compute(trades: Sequence[TradePnl], capital: float = 200_000.0) -> dict:
    n = len(trades)
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net < 0]
    net = round(sum(t.net for t in trades), 2)
    gross = round(sum(t.gross for t in trades), 2)
    friction = round(sum(t.friction for t in trades), 2)
    win_sum, loss_sum = sum(wins), -sum(losses)
    avg_win = win_sum / len(wins) if wins else 0.0
    avg_loss = loss_sum / len(losses) if losses else 0.0

    equity = capital
    peak = capital
    max_dd = 0.0
    max_dd_pct = 0.0
    curve = []
    for t in trades:
        equity += t.net
        curve.append(round(equity, 2))
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            max_dd_pct = max(max_dd_pct, dd / peak * 100)

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 2) if n else None,
        "net": net,
        "gross": gross,
        "friction": friction,
        # None means "no losing trades" (infinite) or nothing to divide
        "profit_factor": round(win_sum / loss_sum, 2) if loss_sum else None,
        "payoff": round(avg_win / avg_loss, 2) if wins and losses else None,
        "expectancy": round(net / n, 2) if n else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2),
        "max_dd": round(max_dd, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "capital": capital,
        "equity": curve,
    }
