"""
Layer 2 — 市场指数模块 (指数算法: QQQ / TWI / 自定义指数)。

三个核心算法，全部服务于组合层决策:

1. **指数趋势门控 (Index Trend Gate)** — 经典 200-DMA 风险开关
   指数收盘价 > 其长期均线 → risk_on (允许开新仓)
   指数收盘价 < 其长期均线 → risk_off (禁止开新仓 / 收紧)
   源自 Faber (2007) 的 S&P 500 200 日均线择时研究。

2. **相对强弱 (Relative Strength vs Index)** — 动量因子排序
   标的相对指数的超额收益 = 标的动量 − 指数动量 (Jegadeesh-Titman 动量思想)
   组合层据此给跑赢指数的标的加分，给跑输指数的减分。

3. **波动率目标 (Volatility Targeting)** — 组合风险预算
   scale = min(max(target_vol / realized_vol, floor), cap)
   用指数已实现波动率估算组合波动，反推仓位缩放系数。

指数数据来源:
- 外部指数 (QQQ / TWI / SPY): 实现 set_external() 注入历史收盘序列即可
- 内置兜底: build_from_basket() 用组合内标的价格等权合成"市场指数"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class IndexState:
    """指数当前状态快照 (供组合层 / LLM 观测)。"""

    name: str
    price: float | None = None
    sma: float | None = None
    trend: str = "neutral"          # risk_on | risk_off | neutral
    momentum: float = 0.0           # 指数动量 (窗口内收益率)
    volatility: float = 0.0         # 年化已实现波动率
    data_points: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "price": self.price,
            "sma": self.sma,
            "trend": self.trend,
            "momentum": round(self.momentum, 4),
            "volatility": round(self.volatility, 4),
            "data_points": self.data_points,
        }


class MarketIndex:
    """市场指数 — 趋势门控 / 相对强弱 / 波动率目标的统一入口。

    Parameters
    ----------
    name : str
        指数名称，如 "QQQ"、"TWI"、"MARKET"。
    ma_period : int
        趋势门控的均线周期 (默认 200，经典 200-DMA)。
    vol_lookback : int
        波动率估算的回看窗口 (bar 数)。
    risk_free_rate : float
        年化无风险利率，用于动量计算 (默认 0.0)。
    """

    def __init__(
        self,
        name: str = "MARKET",
        ma_period: int = 200,
        vol_lookback: int = 20,
        risk_free_rate: float = 0.0,
    ) -> None:
        self.name = name
        self.ma_period = ma_period
        self.vol_lookback = vol_lookback
        self.risk_free_rate = risk_free_rate
        self._series: pd.Series | None = None   # 按时间索引的收盘价
        self._bars_per_year: float = 365.0      # 按数据实际频率年化 (4h→~2190 等)

    # ------------------------------------------------------------------
    # 数据注入
    # ------------------------------------------------------------------
    def set_external(self, df: pd.DataFrame, price_col: str = "close") -> None:
        """注入外部指数数据 (QQQ/TWI/SPY 等)。

        Parameters
        ----------
        df : pd.DataFrame
            必须含 timestamp (或 DatetimeIndex) 与 price_col。
        """
        series = df[price_col].copy()
        if "timestamp" in df.columns:
            ts = pd.to_datetime(df["timestamp"])
            series.index = ts
        else:
            series.index = pd.to_datetime(series.index)
        series = series[~series.index.duplicated(keep="last")].sort_index()
        self._series = series.astype(float)
        self._update_annual_factor()
        logger.info("[Index] %s 已加载 %d 个数据点", self.name, len(series))

    def build_from_basket(
        self,
        price_series: dict[str, pd.Series],
        weights: dict[str, float] | None = None,
    ) -> None:
        """用组合内标的价格等权 (或自定义权重) 合成市场指数。

        逻辑: 各标的收益按权重加总 → 累乘 → 归一化到 100。
        """
        if not price_series:
            return
        df = pd.DataFrame({k: v.astype(float) for k, v in price_series.items()})
        df = df.sort_index()
        rets = df.pct_change()

        if weights is None:
            weights = {k: 1.0 / len(df.columns) for k in df.columns}
        w = pd.Series(weights).reindex(df.columns).fillna(0.0)
        basket_ret = (rets * w).sum(axis=1).fillna(0.0)
        # 收益按日复利累乘, 基准=100
        self._series = (1.0 + basket_ret).cumprod() * 100.0
        self._update_annual_factor()
        logger.info(
            "[Index] 已由 %d 个标的合成指数 %s (%d 点)",
            len(df.columns), self.name, len(self._series),
        )

    def _update_annual_factor(self) -> None:
        """按指数序列的实际时间间隔估算年化因子。"""
        self._bars_per_year = 365.0
        idx = getattr(self._series, "index", None)
        if idx is None or len(self._series) < 2:
            return
        try:
            diffs = pd.Series(idx).diff().dropna().astype("timedelta64[s]").astype(float)
            days = float(diffs.median()) / 86400.0
            if days > 0:
                self._bars_per_year = 365.0 / days
        except Exception:
            pass

    def update_basket(self, price_series: dict[str, pd.Series]) -> None:
        """增量重建 — 每个 bar 调用, 让指数随行情滚动。"""
        if self._series is not None and len(price_series) > 0:
            # 只做轻量重建 (basket 合成成本低)
            pass
        self.build_from_basket(price_series)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def state(self) -> IndexState:
        if self._series is None or self._series.empty:
            return IndexState(name=self.name)

        price = float(self._series.iloc[-1])
        sma = None
        if len(self._series) >= self.ma_period:
            sma = float(self._series.rolling(self.ma_period).mean().iloc[-1])

        if sma is not None:
            trend = "risk_on" if price > sma else "risk_off"
        else:
            trend = "neutral"

        rets = self._series.pct_change().dropna()
        lookback = min(len(rets), 63)
        momentum = float(rets.iloc[-lookback:].sum()) if lookback > 0 else 0.0
        vol = 0.0
        if len(rets) >= self.vol_lookback:
            vol = float(rets.iloc[-self.vol_lookback:].std() * np.sqrt(self._bars_per_year))

        return IndexState(
            name=self.name,
            price=price,
            sma=sma,
            trend=trend,
            momentum=momentum,
            volatility=vol,
            data_points=len(self._series),
        )

    # ------------------------------------------------------------------
    # 算法接口
    # ------------------------------------------------------------------
    def trend_gate(self) -> bool:
        """指数趋势门控: True = 允许开新仓 (risk_on)。"""
        return self.state().trend == "risk_on"

    def relative_strength(self, symbol_close: pd.Series, window: int = 63) -> float:
        """标的相对指数的超额动量 (相对强弱)。

        返回标的正/负超额收益: >0 跑赢指数, <0 跑输指数。
        数据不足时返回 0.0。
        """
        if self._series is None or self._series.empty:
            return 0.0
        sym = symbol_close.dropna()
        idx = self._series.dropna()
        if len(sym) < 2 or len(idx) < 2:
            return 0.0
        # 对齐到公共时间轴
        common = sym.index.intersection(idx.index)
        if len(common) < 2:
            return 0.0
        sym_a = sym.loc[common]
        idx_a = idx.loc[common]
        w = min(window, len(common) - 1)
        if w < 1:
            return 0.0
        sym_ret = float(sym_a.iloc[-1] / sym_a.iloc[-1 - w] - 1.0)
        idx_ret = float(idx_a.iloc[-1] / idx_a.iloc[-1 - w] - 1.0)
        # 减去无风险 (以窗口占比折算年化无风险)
        rf_adj = self.risk_free_rate * (w / self._bars_per_year)
        return sym_ret - idx_ret - rf_adj

    def exposure_scale(
        self,
        target_vol: float = 0.15,
        floor: float = 0.2,
        cap: float = 1.5,
    ) -> float:
        """波动率目标仓位缩放系数。

        scale = clamp(target_vol / realized_vol, floor, cap)
        波动率高 → 降仓; 波动率低 → 可加仓。
        """
        vol = self.state().volatility
        if vol <= 0:
            return 1.0
        scale = target_vol / vol
        return float(max(floor, min(cap, scale)))

    # ------------------------------------------------------------------
    # 便捷
    # ------------------------------------------------------------------
    @property
    def series(self) -> pd.Series | None:
        return self._series

    def to_dict(self) -> dict:
        return self.state().to_dict()
