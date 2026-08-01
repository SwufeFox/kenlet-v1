"""
仓位计算 — 风险本位公式。

核心公式 (Layer2 核心理念):
    仓位数量 = 允许亏损金额 / 止损偏差
    quantity = (equity × risk_per_trade) / |entry - stop|
"""

from __future__ import annotations


def risk_based_size(
    equity: float,
    entry_price: float,
    stop_loss: float,
    risk_per_trade: float = 0.02,
    max_cash: float | None = None,
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
