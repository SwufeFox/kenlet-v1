"""
仓位计算 — 风险本位公式。

核心公式 (Layer2 核心理念):
    仓位数量 = 允许亏损金额 / 止损偏差
    quantity = (equity × risk_per_trade) / |entry - stop|
"""

from __future__ import annotations

# 最小止损偏差 (占入场价比例): 防止极窄止损下仓位公式除零爆炸
MIN_DEVIATION_PCT = 0.005


def risk_based_size(
    equity: float,
    entry_price: float,
    stop_loss: float,
    risk_per_trade: float = 0.02,
    max_cash: float | None = None,
    min_deviation_pct: float = MIN_DEVIATION_PCT,
) -> float:
    """风险本位仓位。

    Parameters
    ----------
    equity : float
        当前权益。
    entry_price : float
        入场价。
    stop_loss : float
        止损价。
    risk_per_trade : float
        单笔风险占权益比例 (默认 2%)。
    max_cash : float, optional
        可用现金上限。
    min_deviation_pct : float
        止损偏差下限 (占入场价比例, 默认 0.5%)。
        偏差低于该值时按该值计算, 防止极窄止损导致仓位爆炸。

    Returns
    -------
    float
        可开仓数量 (币的数量，非金额)。
    """
    if entry_price <= 0 or risk_per_trade <= 0 or equity <= 0:
        return 0.0
    deviation = abs(entry_price - stop_loss)
    if deviation <= 0:
        return 0.0
    deviation = max(deviation, entry_price * min_deviation_pct)
    allowed_loss = equity * risk_per_trade
    quantity = allowed_loss / deviation
    if max_cash is not None and max_cash > 0:
        quantity = min(quantity, max_cash / entry_price)
    return max(0.0, quantity)


def fixed_fraction_size(
    cash: float,
    entry_price: float,
    fraction: float = 0.25,
) -> float:
    """固定比例仓位 (use_risk_sizing=False 时的兜底)。"""
    if entry_price <= 0 or cash <= 0 or fraction <= 0:
        return 0.0
    return cash * fraction / entry_price
