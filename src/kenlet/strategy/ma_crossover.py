"""
Layer 1 — MA 价格穿越策略 (默认策略)。

规则 (融合金融常识):
1. 入场: 收盘价上穿/下穿快均线 (ma_entry) → 做多/做空
2. 出场: 引擎统一处理 — 价格跌破/升破慢均线 (ma_exit) 趋势反转 / ATR 止损止盈
3. 过滤: 趋势过滤 (价格需在 ma_trend 上方才做多) + 市场状态过滤 (regime_filter)
4. 强度: 基于穿越幅度、ADX 趋势强度、RSI 位置综合打分 (0~1)
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from kenlet.core.models import Bar, Position, Signal
from kenlet.core.params import StrategyParams
from kenlet.risk.regime import regime_allows_trade
from kenlet.strategy.base import Strategy


class MACrossoverStrategy(Strategy):
    """价格穿越快均线入场 + 趋势/状态过滤策略。"""

    name = "ma_crossover"

    def on_bar(
        self,
        bar: Bar,
        indicators: dict[str, Any],
        params: StrategyParams,
        positions: list[Position],
    ) -> Signal | None:
        mas = indicators.get("mas", {})
        if not mas or params.ma_entry not in mas or params.ma_exit not in mas:
            return None

        ma_e = _last(mas[params.ma_entry])
        ma_x = _last(mas[params.ma_exit])
        if ma_e is None or ma_x is None:
            return None

        # 穿越检测: 收盘价上穿/下穿快均线 (ma_entry); 慢均线 (ma_exit) 负责出场
        prev_ma_e = _prev(mas[params.ma_entry])
        prev_close = _prev(indicators.get("close"))
        if prev_close is None or prev_ma_e is None:
            return None

        cross_up = prev_close <= prev_ma_e and bar.close > ma_e
        cross_dn = prev_close >= prev_ma_e and bar.close < ma_e

        if not cross_up and not cross_dn:
            return None

        # 最小穿越幅度过滤 (价格与快均线距离)
        if params.min_cross_pct > 0:
            dist = abs(bar.close - ma_e) / ma_e * 100.0
            if dist < params.min_cross_pct:
                return None

        direction = "long" if cross_up else "short"

        # 趋势过滤 — 做多需价格在趋势均线上方
        if params.trend_filter and params.ma_trend in mas:
            ma_t = _last(mas[params.ma_trend])
            if ma_t is not None:
                if direction == "long" and bar.close < ma_t:
                    return None
                if direction == "short" and bar.close > ma_t:
                    return None

        # 市场状态过滤
        if params.regime_filter:
            regime = str(indicators.get("regime", "unknown"))
            if not regime_allows_trade(regime, direction):
                return None

        strength = self._compute_strength(bar, indicators, direction, params)
        return Signal(
            direction=direction,
            strength=strength,
            reason=f"ma_cross_{direction}",
        )

    @staticmethod
    def _compute_strength(bar: Bar, indicators: dict[str, Any], direction: str, params: StrategyParams) -> float:
        """综合信号强度 0~1: 穿越幅度 + ADX + RSI 位置。"""
        score = 0.5
        mas = indicators.get("mas", {})
        ma_e = _last(mas.get(params.ma_entry))
        if ma_e and ma_e > 0:
            score += min(0.25, abs(bar.close - ma_e) / ma_e * 100.0 / 4.0)

        adx_val = _last(indicators.get("adx"))
        if adx_val is not None:
            score += min(0.15, max(0.0, (adx_val - 20.0) / 60.0)) * 0.15

        rsi_val = _last(indicators.get("rsi"))
        if rsi_val is not None:
            if direction == "long" and 55 <= rsi_val <= 75:
                score += 0.10
            elif direction == "short" and 25 <= rsi_val <= 45:
                score += 0.10
        return max(0.0, min(1.0, score))


def _last(series) -> float | None:
    if series is None:
        return None
    try:
        vals = series.dropna()
        if vals.empty:
            return None
        return float(vals.iloc[-1])
    except Exception:
        return None


def _prev(series) -> float | None:
    if series is None:
        return None
    try:
        vals = series.dropna()
        if len(vals) < 2:
            return None
        return float(vals.iloc[-2])
    except Exception:
        return None
