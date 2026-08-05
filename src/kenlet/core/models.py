"""
统一数据模型 — Bar / Signal / Position / TradeRecord / EngineAction。

回测与实盘共享这些数据结构，确保两条路径看到完全相同的对象。
"""

from __future__ import annotations

import pandas as pd

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Bar:
    """单根 K 线。"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_row(cls, row) -> "Bar":
        """从 pandas 行构造。兼容 'timestamp' 或整数索引。"""
        ts = row.get("timestamp", None)
        if ts is None:
            # 无 timestamp 列时: 仅当行索引本身是时间戳才采用;
            # 整数/字符串索引 (如 RangeIndex) 不是时间, 不能当 entry_time 用
            name = getattr(row, "name", None)
            if isinstance(name, (pd.Timestamp, datetime)):
                ts = name
        return cls(
            timestamp=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0)),
        )


@dataclass
class Signal:
    """策略输出的交易信号。"""
    direction: str               # "long" | "short" | "flat"
    strength: float = 0.5        # 0.0–1.0 置信度
    reason: str = ""             # 触发原因，便于回测复盘
    meta: dict = field(default_factory=dict)


@dataclass
class Position:
    """一个开仓批次 (支持分批建仓时每个批次一个 Position)。"""
    side: str                    # "long" | "short"
    entry_price: float
    quantity: float
    entry_time: datetime | None = None
    entry_index: int = 0         # 引擎内部的 bar 序号
    sl: float = 0.0
    tp: float = 0.0
    sl_origin: str = "atr"       # atr | pct | llm
    cost_basis: float = 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        """未实现盈亏 (按当前价)。"""
        if self.side == "long":
            return (current_price - self.entry_price) * self.quantity
        return (self.entry_price - current_price) * self.quantity

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "entry_time": str(self.entry_time) if self.entry_time else None,
            "sl": self.sl,
            "tp": self.tp,
            "sl_origin": self.sl_origin,
        }


@dataclass
class TradeRecord:
    """一笔已完成的交易。"""
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: str  # stop_loss | take_profit | ma_touch | llm_exit | end_of_data | trend_reversal

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "pnl": round(self.pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "exit_reason": self.exit_reason,
        }


@dataclass
class EngineAction:
    """引擎对当前 bar 做出的决策动作。"""
    action: str                  # "open_long" | "open_short" | "close" | "hold" | "adjust"
    reason: str = ""
    price: float = 0.0
    quantity: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def is_trade(self) -> bool:
        return self.action in ("open_long", "open_short", "close")
