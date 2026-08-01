"""
指数数据源 — QQQ / TWI / SPY 等传统市场指数。

用途: Layer 2 组合层用它判断 risk-on / risk-off 宏观环境，
      crypto 与传统股指有正相关性 (尤其纳指 QQQ)。

数据获取优先级:
1. yfinance (如果安装了, 实时拉取)
2. 本地缓存文件 (data/indices_<CODE>.pkl, 网络不可用时用)
3. 手动 CSV (data/indices/<CODE>.csv: date,close)

数据源失败不阻塞主流程 — 返回 None, 组合层回退为中性。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# 默认关注指数 (代码 → 名称)
DEFAULT_INDICES: dict[str, str] = {
    "QQQ": "纳斯达克100 ETF (美国科技股)",
    "TWI": "台湾加权指数 (TWI)",
    "SPY": "标普500 ETF",
    "BTC.D": "比特币市占率 (风险偏好代理)",
}

# 项目数据目录
_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"


class IndexData:
    """指数行情源 — 多级回退。"""

    def __init__(self, indices: dict[str, str] | None = None) -> None:
        self.indices = indices or dict(DEFAULT_INDICES)
        self._cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def fetch(self, code: str, days: int = 500) -> pd.DataFrame | None:
        """拉取某指数日线收盘序列，返回 DataFrame(timestamp, close)。

        多级回退: yfinance → 本地缓存 → CSV。全失败返回 None。
        """
        if code in self._cache:
            return self._cache[code]

        df = None
        for loader in (self._from_yfinance, self._from_pickle, self._from_csv):
            try:
                df = loader(code, days)
                if df is not None and not df.empty:
                    break
            except Exception as e:
                logger.debug("指数 %s 回退源失败: %s", code, e)
                df = None

        if df is not None:
            self._cache[code] = df
        return df

    def fetch_all(self, days: int = 500) -> dict[str, pd.DataFrame | None]:
        """拉取所有配置指数。"""
        return {code: self.fetch(code, days) for code in self.indices}

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------
    def _from_yfinance(self, code: str, days: int) -> pd.DataFrame | None:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            return None
        if code in ("BTC.D", "BTCUSDT"):
            # 市占率用 BTC/USDT 代替
            return None
        ticker = yf.Ticker(code)
        df = ticker.history(period="2y") if days >= 500 else ticker.history(period="1y")
        if df is None or df.empty:
            return None
        df = df.reset_index()[["Date", "Close"]].rename(
            columns={"Date": "timestamp", "Close": "close"}
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
        return df.tail(days).reset_index(drop=True)

    def _from_pickle(self, code: str, days: int) -> pd.DataFrame | None:
        path = _DATA_DIR / f"indices_{code}.pkl"
        if not path.exists():
            return None
        with open(path, "rb") as f:
            df = pd.read_pickle(f)
        return df.tail(days).reset_index(drop=True)

    def _from_csv(self, code: str, days: int) -> pd.DataFrame | None:
        path = _DATA_DIR / "indices" / f"{code}.csv"
        if not path.exists():
            return None
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["date"])
        df = df.rename(columns={"close": "close"})
        return df.tail(days).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 指数因子 — 供 Layer 2 使用的宏观信号
# ---------------------------------------------------------------------------

@dataclass
class IndexFactor:
    """单个指数的因子状态。"""

    code: str
    trend: str            # up | down | flat
    trend_strength: float  # 0~1
    price: float = 0.0
    ma20: float = 0.0
    ma50: float = 0.0
    pct_20d: float = 0.0  # 20 日涨跌幅
    risk_on: bool = True  # 该指数当前是否风险偏好

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "trend": self.trend,
            "trend_strength": round(self.trend_strength, 3),
            "price": round(self.price, 2),
            "ma20": round(self.ma20, 2),
            "ma50": round(self.ma50, 2),
            "pct_20d": round(self.pct_20d, 2),
            "risk_on": self.risk_on,
        }


def compute_index_factors(
    data: dict[str, pd.DataFrame | None],
    ma_fast: int = 20,
    ma_slow: int = 50,
) -> dict[str, IndexFactor]:
    """把指数行情转成因子。

    risk_on 判定 (金融常识):
    - 价格 > MA20 > MA50 且 20日涨幅为正 → risk_on (强)
    - 价格 < MA20 < MA50 且 20日涨幅为负 → risk_off (强)
    - 其他 → 中性 (risk_on=True, 但 trend_strength 低)
    """
    factors: dict[str, IndexFactor] = {}
    for code, df in data.items():
        if df is None or df.empty or len(df) < ma_slow + 1:
            continue
        close = df["close"].dropna()
        if len(close) < ma_slow + 1:
            continue
        price = float(close.iloc[-1])
        ma20 = float(close.rolling(ma_fast).mean().iloc[-1])
        ma50 = float(close.rolling(ma_slow).mean().iloc[-1])
        pct20 = (price / close.iloc[-ma_fast] - 1) * 100.0

        if price > ma20 > ma50 and pct20 > 0:
            trend, risk_on = "up", True
            strength = min(1.0, 0.5 + abs(pct20) / 10.0)
        elif price < ma20 < ma50 and pct20 < 0:
            trend, risk_on = "down", False
            strength = min(1.0, 0.5 + abs(pct20) / 10.0)
        else:
            trend, risk_on = "flat", True
            strength = 0.3

        factors[code] = IndexFactor(
            code=code, trend=trend, trend_strength=round(strength, 3),
            price=price, ma20=ma20, ma50=ma50, pct_20d=round(pct20, 2),
            risk_on=risk_on,
        )
    return factors


def aggregate_risk_appetite(factors: dict[str, IndexFactor]) -> float:
    """综合所有指数的风险偏好得分 0~1。

    0 = 全面 risk-off (应降仓), 1 = 全面 risk-on (可加仓)。
    权重: QQQ/SPY 这类大盘指数权重高。
    """
    if not factors:
        return 0.5  # 无数据 → 中性
    total = 0.0
    for code, f in factors.items():
        base = 0.5
        if f.trend == "up":
            base = 0.6 + f.trend_strength * 0.4
        elif f.trend == "down":
            base = 0.4 - f.trend_strength * 0.4
        # BTC.D 上涨 = 资金避险到 BTC → 也视为风险偏好降低
        if code in ("BTC.D", "BTCUSDT") and f.trend == "up":
            base = 1.0 - base
        total += base
    return max(0.0, min(1.0, total / len(factors)))
