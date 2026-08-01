"""
技术指标 — MA / EMA / ATR / RSI / ADX / 布林带。

统一向量化实现，回测与实盘共用。所有函数接收 pandas Series/DataFrame，
返回与输入对齐的 Series。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_smas(df: pd.DataFrame, periods: list[int], price_col: str = "close") -> dict[int, pd.Series]:
    """计算多个周期的简单均线，返回 {period: Series}。"""
    prices = df[price_col]
    return {p: sma(prices, p) for p in periods}


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder 平滑)。"""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index。"""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(50.0)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — 趋势强度指标。

    ADX > 25 视为有趋势，< 20 视为震荡。
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr_s = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_s.replace(0.0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_s.replace(0.0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (upper, middle, lower)。"""
    mid = sma(series, period)
    std = series.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def compute_indicators(df: pd.DataFrame, periods: list[int], atr_period: int = 14) -> dict:
    """一次性计算常用指标，供引擎/策略调用。"""
    mas = compute_smas(df, periods)
    return {
        "mas": mas,
        "atr": atr(df, atr_period),
        "rsi": rsi(df["close"], 14),
        "adx": adx(df, 14),
        "close": df["close"],
        "high": df["high"],
        "low": df["low"],
        "open": df["open"],
        "volume": df["volume"],
    }
