"""
绩效指标 — Sharpe / MaxDD / Profit Factor / Win Rate 等。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from kenlet.core.models import TradeRecord


@dataclass
class PerformanceMetrics:
    """完整绩效报告。"""

    initial_capital: float
    final_capital: float
    total_return_pct: float
    total_return_abs: float
    win_rate_pct: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    num_trades: int
    num_wins: int
    num_losses: int
    gross_profit: float
    gross_loss: float
    avg_trade_duration: str = "N/A"
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_capital": self.initial_capital,
            "final_capital": round(self.final_capital, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "total_return_abs": round(self.total_return_abs, 2),
            "win_rate_pct": round(self.win_rate_pct, 1),
            "profit_factor": round(self.profit_factor, 2) if math.isfinite(self.profit_factor) else "inf",
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "num_trades": self.num_trades,
            "num_wins": self.num_wins,
            "num_losses": self.num_losses,
            "gross_profit": round(self.gross_profit, 2),
            "gross_loss": round(self.gross_loss, 2),
            "avg_trade_duration": self.avg_trade_duration,
            "avg_win_pct": round(self.avg_win_pct, 2),
            "avg_loss_pct": round(self.avg_loss_pct, 2),
        }


def compute_metrics(
    trades: list[TradeRecord],
    equity_curve: list[float],
    initial_capital: float,
    annual_factor: float = 365.0,
) -> PerformanceMetrics:
    """从交易记录和权益曲线计算全部指标。"""
    final = equity_curve[-1] if equity_curve else initial_capital
    total_abs = final - initial_capital
    total_pct = (total_abs / initial_capital * 100.0) if initial_capital > 0 else 0.0

    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    wr = (len(wins) / n * 100.0) if n else 0.0
    gp = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in losses))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)

    avg_win = (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0

    mdd = _max_drawdown(equity_curve)
    sharpe = _sharpe(equity_curve, annual_factor)
    avg_dur = _avg_duration(trades)

    return PerformanceMetrics(
        initial_capital=initial_capital,
        final_capital=final,
        total_return_pct=total_pct,
        total_return_abs=total_abs,
        win_rate_pct=wr,
        profit_factor=pf,
        max_drawdown_pct=mdd,
        sharpe_ratio=sharpe,
        num_trades=n,
        num_wins=len(wins),
        num_losses=len(losses),
        gross_profit=gp,
        gross_loss=gl,
        avg_trade_duration=avg_dur,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
    )


def _max_drawdown(equity: list[float]) -> float:
    if len(equity) < 2:
        return 0.0
    arr = np.array(equity, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = (arr - peak) / peak * 100.0
    return float(abs(dd.min())) if dd.min() < 0 else 0.0


def _sharpe(equity: list[float], annual_factor: float = 365.0) -> float:
    if len(equity) < 3:
        return 0.0
    arr = np.array(equity, dtype=float)
    rets = np.diff(arr) / arr[:-1]
    rets = rets[np.isfinite(rets)]
    if len(rets) < 2 or rets.std() < 1e-12:
        return 0.0
    return float(rets.mean() / rets.std() * math.sqrt(annual_factor))


def _avg_duration(trades: list[TradeRecord]) -> str:
    if not trades:
        return "N/A"
    hours: list[float] = []
    for t in trades:
        try:
            import pandas as pd
            et = pd.Timestamp(t.entry_time)
            xt = pd.Timestamp(t.exit_time)
            hours.append((xt - et).total_seconds() / 3600.0)
        except Exception:
            continue
    if not hours:
        return "N/A"
    avg = sum(hours) / len(hours)
    if avg >= 24:
        return f"{avg / 24:.1f}d"
    return f"{avg:.1f}h"
