"""
市场状态 (Regime) 检测 — 多因子综合判定。

三个维度:
1. MA 方向: 用短/中/长三条均线 (ma_fast/ma_mid/ma_slow) 判定方向
   - 多头: fast > mid > slow
   - 空头: fast < mid < slow
   - 纠缠: 其他
2. ADX (趋势强度): >= 阈值 = 有趋势
3. 波动率分位: 高波动 → choppy

输出: bull / bear / ranging / choppy
规则 (金融常识):
- bull = 多头排列 + 有趋势 + 非高波动 (最强做多环境)
- bear = 空头排列 + 有趋势 + 非高波动 (最强做空环境)
- ranging = 方向明确但趋势弱 (温和趋势，可顺势开仓)
- choppy = 方向纠缠 或 高波动 (禁止开仓，避免来回打脸)
"""

from __future__ import annotations

import pandas as pd

from kenlet.analysis.indicators import adx, atr, compute_smas


def detect_regime(
    df: pd.DataFrame,
    periods: list[int] | None = None,
    atr_period: int = 14,
    adx_period: int = 14,
    adx_trend_threshold: float = 20.0,
    vol_lookback: int = 100,
    ma_fast: int = 20,
    ma_mid: int = 60,
    ma_slow: int = 180,
) -> pd.Series:
    """逐 bar 输出市场状态标签。

    Returns
    -------
    pd.Series — "bull" | "bear" | "ranging" | "choppy"，与 df 对齐。
    """
    if periods is None:
        periods = [20, 38, 60, 180, 540]

    mas = compute_smas(df, periods)
    close = df["close"]
    adx_series = adx(df, adx_period)
    atr_series = atr(df, atr_period)
    atr_pct = atr_series / close * 100.0
    atr_pct_rank = atr_pct.rolling(vol_lookback, min_periods=30).rank(pct=True)

    # 短/中/长三线方向 (从 mas 里选，找不到就 fallback)
    fast = _pick_ma(mas, ma_fast)
    mid = _pick_ma(mas, ma_mid)
    slow = _pick_ma(mas, ma_slow)

    valid = fast.notna() & mid.notna() & slow.notna()
    # 用价格修正滞后: 价格要站在对应均线同侧
    bullish = (fast > mid) & (mid > slow) & (close > slow)
    bearish = (fast < mid) & (mid < slow) & (close < slow)

    # 温和方向 (方向一致但趋势弱 / 或三线中有纠缠但价格占优)
    mild_bull = ~bullish & ~bearish & (close > mid) & (fast > slow)
    mild_bear = ~bullish & ~bearish & (close < mid) & (fast < slow)

    high_vol = atr_pct_rank > 0.85
    trending = adx_series >= adx_trend_threshold

    regime = pd.Series("choppy", index=df.index)
    # bull/bear 需要: 方向 + 趋势强度 + 非高波动
    regime[valid & bullish & trending & ~high_vol] = "bull"
    regime[valid & bearish & trending & ~high_vol] = "bear"
    # ranging: 温和方向，无论 ADX (趋势弱但方向明确)
    regime[valid & mild_bull & ~high_vol] = "ranging"
    regime[valid & mild_bear & ~high_vol] = "ranging"
    # 其余 (方向纠缠 或 高波动) 保持 choppy

    return regime


def regime_allows_trade(regime: str, direction: str) -> bool:
    """根据市场状态过滤交易方向。

    - bull:   只做多 (强多头环境)
    - bear:   只做空 (强空头环境)
    - ranging: 方向明确但弱 → 允许顺势，但标记为低置信 (由强度决定)
    - choppy: 不交易 (方向纠缠/高波动)
    """
    if regime == "bull":
        return direction == "long"
    if regime == "bear":
        return direction == "short"
    if regime == "ranging":
        # 温和趋势允许顺势开仓；策略强度会打折
        return True
    return False


def _pick_ma(mas: dict[int, pd.Series], period: int) -> pd.Series:
    """从 mas 字典取指定周期的均线，若不存在取最接近的。"""
    if period in mas:
        return mas[period]
    if not mas:
        return pd.Series(index=mas_index(mas), dtype=float)
    # 找最接近的周期
    keys = sorted(mas.keys())
    nearest = min(keys, key=lambda k: abs(k - period))
    return mas[nearest]


def mas_index(mas: dict[int, pd.Series]) -> pd.Index:
    for s in mas.values():
        return s.index
    return pd.Index([])
