"""
Layer 1 — 策略抽象基类。

策略只负责"给信号"，不碰仓位与资金。回测与实盘共用同一接口，
新策略只需实现 on_bar()。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from kenlet.core.models import Bar, Position, Signal
from kenlet.core.params import StrategyParams


class Strategy(ABC):
    """信号策略接口。

    策略观察当前 bar 与指标，结合已有持仓，返回交易信号或 None。
    """

    name: str = "base"

    @abstractmethod
    def on_bar(
        self,
        bar: Bar,
        indicators: dict[str, Any],
        params: StrategyParams,
        positions: list[Position],
    ) -> Signal | None:
        """处理一根 K 线，返回信号。

        Parameters
        ----------
        bar : Bar
            当前 K 线。
        indicators : dict
            指标快照 (mas/atr/adx/rsi/regime 等)。
        params : StrategyParams
            当前参数 (可能已被 Layer2/Layer3 调整)。
        positions : list[Position]
            当前该标的的持仓。

        Returns
        -------
        Signal | None
            direction ∈ {"long", "short"}，或 None (不交易)。
        """
        raise NotImplementedError

    def to_dict(self) -> dict:
        return {"name": self.name}
