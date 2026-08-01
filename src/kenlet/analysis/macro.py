"""
Layer 2 辅助 — 宏观市场信号 (Macro Regime)。

引入外部宏观指数作为加密市场的风险偏好代理 (risk proxy):
- QQQ (Invesco QQQ, 纳指100 ETF): 全球科技/成长风险偏好的风向标。
  加密与纳指在 2020 年后高度相关 (corr > 0.6)，QQQ 趋势是很好的领先信号。
- DXY (美元指数): 强势美元通常压制 BTC 等以美元计价的加密资产
  (历史上 DXY 与 BTC 负相关)。
- VIX (CBOE 波动率指数): 市场恐慌程度。VIX > 25 意味着系统性风险上升。

数据来源: 本地 CSV 缓存 (macro/QQQ.csv 等)，列格式: date,close。
无数据时返回 neutral，不影响主流程 (设计为可选增强)。

输出: MacroState.risk_regime ∈ {risk_on, neutral, risk_off}，
以及对应的组合层调节系数 (risk_scale, max_positions_scale)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class MacroState:
    """宏观状态快照 + 对组合层的调节建议。"""

    risk_regime: str = "neutral"        # risk_on | neutral | risk_off
    qqq_trend: float | None = None      # +1 上升, -1 下降, 0 未知
    dxy_strong: bool | None = None      # True 强势美元
    vix_panic: bool | None = None       # True 恐慌
    reasons: list[str] = field(default_factory=list)

    @property
    def risk_scale(self) -> float:
        """风险缩放系数 — Layer2 计算仓位时乘上它。"""
        if self.risk_regime == "risk_off":
            return 0.5
        if self.risk_regime == "risk_on":
            return 1.2
        return 1.0

    @property
    def max_positions_scale(self) -> float:
        if self.risk_regime == "risk_off":
            return 0.6
        return 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_regime": self.risk_regime,
            "risk_scale": self.risk_scale,
            "max_positions_scale": self.max_positions_scale,
            "qqq_trend": self.qqq_trend,
            "dxy_strong": self.dxy_strong,
            "vix_panic": self.vix_panic,
            "reasons": self.reasons,
        }


class MacroRegime:
    """宏观指数加载与判定。

    Parameters
    ----------
    data_dir : str | Path | None
        存放宏观指数 CSV 的目录 (默认 ./macro)。
        每个文件名为 <TICKER>.csv，列为 date,close (date 可解析为 datetime)。
    """

    TREND_FAST = 20
    TREND_SLOW = 60

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else Path("macro")
        self._cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------
    def load(self, ticker: str) -> pd.DataFrame | None:
        """加载 <ticker>.csv 并缓存。失败返回 None。"""
        ticker = ticker.upper()
        if ticker in self._cache:
            return self._cache[ticker]

        path = self.data_dir / f"{ticker}.csv"
        if not path.exists():
            # 也兼容 macro/data/<ticker>.csv
            alt = self.data_dir / "data" / f"{ticker}.csv"
            if not alt.exists():
                logger.debug("[Macro] 缺少 %s 数据，跳过", path)
                return None
            path = alt

        try:
            df = pd.read_csv(path)
            df.columns = [c.strip().lower() for c in df.columns]
            if "date" not in df.columns or "close" not in df.columns:
                logger.warning("[Macro] %s 需要 date,close 列", path)
                return None
            df["date"] = pd.to_datetime(df["date"])
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
            if len(df) < self.TREND_SLOW:
                logger.warning("[Macro] %s 数据不足 %d 行", ticker, self.TREND_SLOW)
                return None
            self._cache[ticker] = df
            logger.info("[Macro] 加载 %s: %d 行 (%s ~ %s)",
                        ticker, len(df), df["date"].iloc[0].date(), df["date"].iloc[-1].date())
            return df
        except Exception as e:
            logger.warning("[Macro] 加载 %s 失败: %s", ticker, e)
            return None

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def evaluate(self) -> MacroState:
        """综合评估宏观风险偏好。"""
        state = MacroState()

        qqq = self.load("QQQ")
        dxy = self.load("DXY")
        vix = self.load("VIX")

        # --- QQQ 趋势 (risk-on/off 主信号) ---
        if qqq is not None:
            fast = qqq["close"].rolling(self.TREND_FAST).mean()
            slow = qqq["close"].rolling(self.TREND_SLOW).mean()
            last_fast = float(fast.dropna().iloc[-1])
            last_slow = float(slow.dropna().iloc[-1])
            state.qqq_trend = 1 if last_fast > last_slow else -1
            state.reasons.append(
                f"QQQ MA{self.TREND_FAST}/{self.TREND_SLOW}: "
                f"{'多头' if state.qqq_trend > 0 else '空头'}"
            )

        # --- DXY (美元强弱) ---
        if dxy is not None:
            # 用 60 日均线判断美元趋势
            ma60 = dxy["close"].rolling(60).mean()
            last = float(dxy["close"].dropna().iloc[-1])
            last_ma = float(ma60.dropna().iloc[-1])
            state.dxy_strong = last > last_ma
            state.reasons.append(
                f"DXY: {'强势(压制风险资产)' if state.dxy_strong else '弱势(利好风险资产)'}"
            )

        # --- VIX (恐慌) ---
        if vix is not None:
            last_vix = float(vix["close"].dropna().iloc[-1])
            state.vix_panic = last_vix > 25.0
            state.reasons.append(f"VIX: {last_vix:.1f} ({'恐慌' if state.vix_panic else '正常'})")

        # --- 综合判定 ---
        if state.qqq_trend == -1 and (state.vix_panic or state.dxy_strong):
            state.risk_regime = "risk_off"
        elif state.qqq_trend == 1 and not state.vix_panic:
            state.risk_regime = "risk_on"
        else:
            state.risk_regime = "neutral"

        state.reasons.append(f"综合: {state.risk_regime} (风险缩放 ×{state.risk_scale})")
        return state

    # ------------------------------------------------------------------
    # 便捷: 直接生成一组 demo CSV (方便离线体验)
    # ------------------------------------------------------------------
    def write_demo_data(self, n: int = 400, seed: int = 7) -> None:
        """生成模拟的 QQQ/DXY/VIX CSV，便于无数据时测试宏观模块。"""
        import numpy as np

        self.data_dir.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(seed)
        dates = pd.date_range("2024-01-01", periods=n, freq="D")

        specs = {
            "QQQ": dict(start=420.0, drift=0.0004, vol=0.008),
            "DXY": dict(start=104.0, drift=-0.0001, vol=0.002),
            "VIX": dict(start=15.0, drift=0.0, vol=0.05),
        }
        for ticker, sp in specs.items():
            rets = rng.normal(sp["drift"], sp["vol"], n)
            close = sp["start"] * np.exp(np.cumsum(rets))
            pd.DataFrame({"date": dates, "close": close}).to_csv(
                self.data_dir / f"{ticker}.csv", index=False
            )
        logger.info("[Macro] 已生成 demo 数据到 %s", self.data_dir)
