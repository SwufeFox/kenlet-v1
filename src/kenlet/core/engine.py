"""
Layer 1 — 统一交易引擎。

回测与实盘共用同一个 on_bar() 处理路径：
  - 回测: 循环喂历史 bar
  - 实盘: 每根新 K 线收盘喂一次

引擎只负责"单标的、单 bar 的决策"；组合层 (Layer 2) 负责多标的的
仓位分配与优胜劣汰；LLM (Layer 3) 负责调整参数。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kenlet.core.models import Bar, EngineAction, Position, Signal, TradeRecord
from kenlet.core.params import StrategyParams
from kenlet.risk.sizing import MIN_DEVIATION_PCT

if TYPE_CHECKING:
    from kenlet.strategy.base import Strategy

logger = logging.getLogger(__name__)


class TradingEngine:
    """统一引擎 — 处理逐根 K 线。

    Parameters
    ----------
    symbol : str
        交易对，如 "BTC/USDT"。
    params : StrategyParams
        可变参数对象 (可由 Layer2/Layer3 修改)。
    strategy : Strategy
        信号策略 (Layer1 逻辑)。
    """

    def __init__(
        self,
        symbol: str,
        params: StrategyParams,
        strategy: Strategy,
    ) -> None:
        self.symbol = symbol
        self.params = params
        self.strategy = strategy  # type: Strategy (TYPE_CHECKING only)

        # 账户状态
        self.capital: float = params.initial_capital
        self.cash: float = params.initial_capital
        self.positions: list[Position] = []
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[float] = []

        # 内部状态
        self.bar_index: int = 0
        self.entry_gate: bool = True    # Layer2 指数门控: risk_off 时置 False 禁开新仓
        self.gate_risk_scale: float = 1.0  # Layer2 指数门控: risk_off 时风险预算乘数 (1.0=不变)
        self._entry_window: int = 0       # 分批建仓剩余批次数
        self._entry_window_start: int = -1
        self._pending_signal: Signal | None = None
        self._last_action: EngineAction | None = None
        # 本 bar 是否有入场信号因满仓被拒 (供 Layer2 触发优胜劣汰旋转)
        self.entry_rejected_capacity: bool = False

        # 供 Layer2/Layer3 观测的快照
        self.indicators: dict[str, Any] = {}
        self.regime: str = "unknown"

    # ------------------------------------------------------------------
    # 主入口 — 回测与实盘共用
    # ------------------------------------------------------------------
    def on_bar(self, bar: Bar, indicators: dict[str, Any]) -> list[EngineAction]:
        """处理一根 K 线，返回决策动作列表。

        Parameters
        ----------
        bar : Bar
            当前 bar。
        indicators : dict
            指标快照，至少含 mas/atr/regime 等键。
        """
        actions: list[EngineAction] = []
        self.indicators = indicators
        self.regime = str(indicators.get("regime", "unknown"))
        self.entry_rejected_capacity = False

        # 1. 检查现有持仓的出场条件
        actions.extend(self._check_exits(bar))

        # 2. 如果还有分批建仓剩余，继续加仓
        if self._entry_window > 0:
            actions.extend(self._continue_entry_window(bar))

        # 3. 生成入场信号
        signal = self.strategy.on_bar(bar, indicators, self.params, self.positions)
        if signal is not None and signal.direction in ("long", "short"):
            self._pending_signal = signal
            actions.extend(self._try_open(bar, signal))

        # 4. 记录权益
        self._record_equity(bar)
        self.bar_index += 1
        return actions

    # ------------------------------------------------------------------
    # 出场
    # ------------------------------------------------------------------
    def _check_exits(self, bar: Bar) -> list[EngineAction]:
        actions: list[EngineAction] = []
        if not self.positions:
            return actions

        ma_exit_val = _series_last(self.indicators.get("mas", {}).get(self.params.ma_exit))
        removed: list[int] = []

        for idx, pos in enumerate(self.positions):
            reason = self._exit_reason(pos, bar, ma_exit_val)
            if reason is None:
                continue
            exit_price = self._exit_price(pos, bar, reason)
            pnl = (exit_price - pos.entry_price) * pos.quantity if pos.side == "long" else \
                  (pos.entry_price - exit_price) * pos.quantity
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100.0 if pos.side == "long" else \
                      (pos.entry_price - exit_price) / pos.entry_price * 100.0

            trade = TradeRecord(
                side=pos.side,
                entry_time=str(pos.entry_time) if pos.entry_time else "?",
                exit_time=str(bar.timestamp),
                entry_price=pos.entry_price,
                exit_price=exit_price,
                quantity=pos.quantity,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=reason,
            )
            self.trades.append(trade)
            self.cash += pos.quantity * exit_price
            actions.append(EngineAction(
                action="close",
                reason=reason,
                price=exit_price,
                quantity=pos.quantity,
                meta={"side": pos.side},
            ))
            removed.append(idx)

        for idx in sorted(removed, reverse=True):
            self.positions.pop(idx)
        return actions

    def _exit_reason(self, pos: Position, bar: Bar, ma_exit_val: float | None) -> str | None:
        """决定出场原因: stop_loss / take_profit / trend_reversal。"""
        if pos.side == "long":
            if bar.low <= pos.sl:
                return "stop_loss"
            if pos.tp > 0 and bar.high >= pos.tp:
                return "take_profit"
            if ma_exit_val and bar.close < ma_exit_val:
                return "trend_reversal"
        else:  # short
            if bar.high >= pos.sl:
                return "stop_loss"
            if pos.tp > 0 and bar.low <= pos.tp:
                return "take_profit"
            if ma_exit_val and bar.close > ma_exit_val:
                return "trend_reversal"
        return None

    @staticmethod
    def _exit_price(pos: Position, bar: Bar, reason: str) -> float:
        """出场价估算 — 保守口径：触及止损/止盈价即按该价成交。"""
        if reason == "stop_loss":
            return pos.sl
        if reason == "take_profit":
            return pos.tp
        return bar.close

    # ------------------------------------------------------------------
    # 入场
    # ------------------------------------------------------------------
    def _try_open(self, bar: Bar, signal: Signal) -> list[EngineAction]:
        actions: list[EngineAction] = []
        if not self.entry_gate:
            logger.debug("[%s] 指数门控 risk_off，禁止开新仓", self.symbol)
            return actions
        if len(self.positions) >= self.params.max_positions:
            self.entry_rejected_capacity = True
            logger.debug("[%s] 已达最大持仓数 %d，忽略信号", self.symbol, self.params.max_positions)
            return actions

        # 仓位大小计算 — 公式: 仓位 = 允许亏损 / 止损偏差
        # 分批建仓时风险预算按批次均分 (总风险 = 一次预算)
        sl, tp = self._compute_stops(bar.close, signal.direction)
        quantity = self._compute_quantity(bar.close, sl) / self.params.num_entries

        if quantity <= 0:
            return actions

        pos = Position(
            side=signal.direction,
            entry_price=bar.close,
            quantity=quantity,
            entry_time=bar.timestamp,
            entry_index=self.bar_index,
            sl=sl,
            tp=tp,
        )
        cost = quantity * bar.close
        if cost > self.cash:
            # 现金不足 → 缩仓
            quantity = self.cash / bar.close if bar.close > 0 else 0.0
            if quantity <= 0:
                return actions
            pos.quantity = quantity
            cost = quantity * bar.close

        self.cash -= cost
        self.positions.append(pos)

        # 分批建仓
        if self.params.num_entries > 1:
            self._entry_window = self.params.num_entries - 1
            self._entry_window_start = self.bar_index
        self._pending_signal = None

        actions.append(EngineAction(
            action=f"open_{signal.direction}",
            reason=signal.reason or "signal",
            price=bar.close,
            quantity=quantity,
            meta={"sl": sl, "tp": tp, "strength": signal.strength},
        ))
        return actions

    def _continue_entry_window(self, bar: Bar) -> list[EngineAction]:
        """分批建仓的后续批次。"""
        actions: list[EngineAction] = []
        if self.bar_index - self._entry_window_start < self.params.entry_offset_bars:
            return actions
        if len(self.positions) >= self.params.max_positions:
            self._entry_window = 0
            return actions

        ref = self.positions[-1]
        # 与首笔同口径: 每批 = 一次预算 / 批次总数
        quantity = self._compute_quantity(bar.close, ref.sl) / self.params.num_entries
        cost = quantity * bar.close
        if cost <= 0 or cost > self.cash:
            self._entry_window = 0
            return actions

        pos = Position(
            side=ref.side,
            entry_price=bar.close,
            quantity=quantity,
            entry_time=bar.timestamp,
            entry_index=self.bar_index,
            sl=ref.sl,
            tp=ref.tp,
            sl_origin=ref.sl_origin,
        )
        self.cash -= cost
        self.positions.append(pos)
        self._entry_window -= 1
        actions.append(EngineAction(
            action=f"open_{ref.side}",
            reason="scale_in",
            price=bar.close,
            quantity=quantity,
            meta={"batch": True},
        ))
        return actions

    # ------------------------------------------------------------------
    # 风控计算
    # ------------------------------------------------------------------
    def _compute_stops(self, price: float, direction: str) -> tuple[float, float]:
        """ATR 止损止盈 (Layer1 默认)。Layer2/LLM 可覆盖。"""
        atr_val = _series_last(self.indicators.get("atr"))
        if not atr_val or atr_val <= 0:
            # 兜底：百分比止损
            sl_pct = 0.05
            tp_pct = 0.10
            if direction == "long":
                return price * (1 - sl_pct), price * (1 + tp_pct)
            return price * (1 + sl_pct), price * (1 - tp_pct)

        if direction == "long":
            return price - atr_val * self.params.atr_sl_mult, price + atr_val * self.params.atr_tp_mult
        return price + atr_val * self.params.atr_sl_mult, price - atr_val * self.params.atr_tp_mult

    def _compute_quantity(self, price: float, sl: float) -> float:
        """仓位计算 — 公式: 仓位 = 允许亏损 / 止损偏差。

        这是 Layer2 的核心理念在 Layer1 的默认实现，组合层可覆盖。
        风险基准用权益 (capital) 而非现金, 与 README 公式 E·r 一致。
        """
        if self.params.use_risk_sizing:
            risk_amount = self.capital * self.params.risk_per_trade
            deviation = abs(price - sl)
            if deviation <= 0:
                return 0.0
            # 最小偏差下限: 极窄止损时防止仓位爆炸 (与 risk/sizing.py 同口径)
            deviation = max(deviation, price * MIN_DEVIATION_PCT)
            return (risk_amount / deviation) * self.gate_risk_scale
        # 固定比例 (以可用现金为基数, 受资金约束)
        return self.cash * self.params.position_size / price if price > 0 else 0.0

    # ------------------------------------------------------------------
    # 权益与状态
    # ------------------------------------------------------------------
    def _record_equity(self, bar: Bar) -> None:
        equity = self.cash
        for pos in self.positions:
            equity += pos.quantity * bar.close
        self.equity_curve.append(equity)
        self.capital = equity

    def current_equity(self, price: float | None = None) -> float:
        if price is None:
            return self.capital
        equity = self.cash
        for pos in self.positions:
            equity += pos.quantity * price
        return equity

    def apply_llm_override(self, updates: dict) -> list[str]:
        """Layer3 入口 — 修改参数 (立即生效)。"""
        return self.params.override(updates)

    def apply_stop_override(self, sl: float, tp: float, reason: str = "llm") -> None:
        """Layer3/Layer2 动态调整所有持仓的止损止盈。"""
        for pos in self.positions:
            pos.sl = sl
            pos.tp = tp
            pos.sl_origin = reason


def _series_last(series) -> float | None:
    """取 Series 最后一个非 NaN 值。"""
    if series is None:
        return None
    try:
        vals = series.dropna()
        if vals.empty:
            return None
        return float(vals.iloc[-1])
    except Exception:
        return None
