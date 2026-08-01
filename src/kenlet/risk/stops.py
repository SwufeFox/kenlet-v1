"""
止损止盈计算 — ATR 倍数 + 百分比兜底。

绝对不用绝对美元金额 (那是旧架构的硬伤)。
"""

from __future__ import annotations


def compute_stops(
    entry_price: float,
    atr_val: float | None,
    direction: str = "long",
    atr_sl_mult: float = 3.0,
    atr_tp_mult: float = 6.0,
    fallback_sl_pct: float = 0.05,
    fallback_tp_pct: float = 0.10,
) -> tuple[float, float]:
    """计算止损/止盈价位。

    Returns
    -------
    (stop_loss, take_profit)
    """
    if atr_val and atr_val > 0:
        if direction == "long":
            return entry_price - atr_val * atr_sl_mult, entry_price + atr_val * atr_tp_mult
        return entry_price + atr_val * atr_sl_mult, entry_price - atr_val * atr_tp_mult

    # 兜底: 百分比
    if direction == "long":
        return entry_price * (1 - fallback_sl_pct), entry_price * (1 + fallback_tp_pct)
    return entry_price * (1 + fallback_sl_pct), entry_price * (1 - fallback_tp_pct)


def trailing_stop(
    current_price: float,
    current_sl: float,
    atr_val: float,
    direction: str = "long",
    trail_mult: float = 2.0,
) -> float:
    """跟踪止损 — 只朝有利方向移动。"""
    if atr_val <= 0:
        return current_sl
    if direction == "long":
        new_sl = current_price - atr_val * trail_mult
        return max(current_sl, new_sl)
    new_sl = current_price + atr_val * trail_mult
    return min(current_sl, new_sl)
