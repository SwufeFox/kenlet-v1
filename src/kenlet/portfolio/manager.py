"""
Layer 2 — 投资组合层 (Portfolio Manager)。

职责:
1. 跨标的仓位分配与排名 (优胜劣汰)
2. 仓位计算公式: 仓位 = 允许亏损 / 止损偏差
3. 组合级风控 (总风险预算、持仓上限)
4. 多引擎调度与权益汇总

Layer 2 位于 Layer 1 (单标的引擎) 之上，被 Layer 3 (LLM manager) 控制。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kenlet.analysis.macro import MacroRegime, MacroState
from kenlet.core.engine import TradingEngine
from kenlet.core.models import Bar, EngineAction, TradeRecord
from kenlet.core.params import StrategyParams
from kenlet.risk.sizing import MIN_DEVIATION_PCT
from kenlet.strategy.base import Strategy

logger = logging.getLogger(__name__)


@dataclass
class PortfolioConfig:
    """组合层配置 (可被 Layer3 LLM 调整)。"""

    max_total_risk: float = 0.10          # 组合总风险占权益上限
    default_risk_per_trade: float = 0.02  # 单标的默认风险
    max_positions: int = 5                # 组合最大持仓数
    ranking_metric: str = "composite"     # composite | momentum | profit
    prune_threshold: float = -0.5         # 排名分低于该值 → 淘汰 (负数=只砍明显亏损)
    rotate_on_cap: bool = True            # 满仓时用新信号替换最弱持仓
    max_per_sector: int = 3
    min_cash_ratio: float = 0.05          # 保留至少 5% 现金

    # ── 指数因子 (Layer2 增强: QQQ/TWI/合成指数) ──
    use_index_gate: bool = False          # 指数趋势门控 (risk_on 才开新仓)
    gate_risk_scale: float = 0.0          # risk_off 时风险预算乘数 (乘在 risk_per_trade 上; 0=全禁, 0.33=1/3, 0.5=半仓)
    use_relative_strength: bool = False   # 相对强弱参与持仓排名 (跑赢指数加分)
    use_vol_targeting: bool = False       # 波动率目标仓位缩放
    index_ma_period: int = 200            # 指数门控均线周期 (200-DMA 经典)
    index_vol_target: float = 0.15        # 目标年化波动率 (15%)
    index_vol_floor: float = 0.2          # 缩放系数下限
    index_vol_cap: float = 1.5            # 缩放系数上限
    index_name: str = "MARKET"            # 指数名称 (QQQ/TWI/MARKET)


@dataclass
class PositionScore:
    """单个持仓的评分结果 (供优胜劣汰排序)。"""

    symbol: str
    score: float
    reason: str
    pnl_pct: float
    details: dict[str, float] = field(default_factory=dict)


class PortfolioManager:
    """组合管理器 — Layer 2。

    持有多个 TradingEngine (每个标的一个)，统一调度 on_bar，
    计算每笔仓位 (风险本位)，并在满仓/低质时执行优胜劣汰。

    指数因子 (QQQ/TWI 等): 接入宏观 risk-on/off 信号，
    调制组合风险预算与单笔风险比例 — 大盘走弱时自动降仓。
    """

    def __init__(
        self,
        config: PortfolioConfig | None = None,
        params: StrategyParams | None = None,
    ) -> None:
        self.config = config or PortfolioConfig()
        self.params = params or StrategyParams()
        self.engines: dict[str, TradingEngine] = {}
        self.strategy: Strategy | None = None
        self._sectors: dict[str, str] = {}
        self._pending_rotations: list[str] = []

        # 组合权益
        self.combined_equity: list[float] = []
        self.total_equity: float = self.params.initial_capital
        self._last_prices: dict[str, float] = {}

        # 指数因子 (宏观风险偏好)
        self.index_factors: dict[str, Any] = {}
        self.risk_appetite: float = 0.5

        # Layer3 挂钩
        self.llm_manager: Any = None

        # 指数因子 (QQQ/TWI/合成指数) — 趋势门控/相对强弱/波动率目标
        self.index: Any = None
        self._price_series: dict[str, Any] = {}   # 各标的收盘序列缓存
        self._gate_neutral_logged: bool = False    # 门控数据不足警告只打一次

        # 宏观信号 (可选增强)
        self.macro: Any = None
        self.macro_state: Any = None
        try:
            from kenlet.analysis.macro import MacroState
            self.macro_state = MacroState()
        except Exception:
            self.macro_state = None

    def attach_index(self, index: Any) -> None:
        """接入市场指数 (MarketIndex 实例)。"""
        self.index = index
        self.config.index_name = getattr(index, "name", "MARKET")
        logger.info("[Layer2] 已接入指数 %s", self.config.index_name)

    def _record_price(self, symbol: str, close: float, ts) -> None:
        """缓存收盘价序列 (供合成指数 / 相对强弱计算)。"""
        import pandas as pd
        series = self._price_series.get(symbol)
        if series is None:
            series = pd.Series(dtype=float)
            self._price_series[symbol] = series
        self._price_series[symbol] = pd.concat([series, pd.Series([close], index=[ts])])

    def ensure_index(self) -> Any:
        """若未接入外部指数，用组合内标的价格等权合成。"""
        if self.index is not None:
            return self.index
        if len(self._price_series) < 1:
            return None
        from kenlet.portfolio.index import MarketIndex
        self.index = MarketIndex(
            name=self.config.index_name,
            ma_period=self.config.index_ma_period,
        )
        self.index.build_from_basket(self._price_series)
        logger.info("[Layer2] 已用组合标的合成指数 %s", self.index.name)
        return self.index

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def attach(self, symbol: str, engine: TradingEngine, sector: str = "crypto") -> None:
        self.engines[symbol] = engine
        self._sectors[symbol] = sector

    def attach_strategy(self, strategy: Strategy) -> None:
        """共享一个策略实例给所有引擎 (策略无状态，可复用)。"""
        self.strategy = strategy
        for engine in self.engines.values():
            engine.strategy = strategy

    def attach_macro(self, macro: MacroRegime) -> None:
        """接入宏观指数模块 (QQQ/DXY/VIX)。"""
        self.macro = macro
        self.refresh_macro()

    def refresh_macro(self) -> MacroState:
        """重新评估宏观状态。无宏观模块时返回 neutral。"""
        if self.macro is None:
            self.macro_state = MacroState()
        else:
            self.macro_state = self.macro.evaluate()
        return self.macro_state

    # ------------------------------------------------------------------
    # 主调度 — 回测与实盘共用
    # ------------------------------------------------------------------
    def on_bars(
        self,
        bars: dict[str, Bar],
        indicators: dict[str, dict],
    ) -> list[EngineAction]:
        """组合级逐 bar 处理。

        Parameters
        ----------
        bars : dict[str, Bar]
            {symbol: Bar}
        indicators : dict[str, dict]
            {symbol: 指标快照}
        """
        actions: list[EngineAction] = []

        # 0. 记录价格序列 (供指数合成/相对强弱)
        for symbol, bar in bars.items():
            self._record_price(symbol, bar.close, bar.timestamp)

        # 0.5 指数门控: risk_off 时收缩风险预算或禁止开新仓 (出场不受影响)
        gate_open = True
        if self.config.use_index_gate or self.config.use_relative_strength                 or self.config.use_vol_targeting:
            idx = self.ensure_index()
            if idx is not None and hasattr(idx, "update_basket"):
                # 每 bar 增量重建, 让指数状态跟随行情滚动
                idx.update_basket(self._price_series)
            if self.config.use_index_gate and idx is not None:
                gate_open = idx.trend_gate()
                if idx.state().trend == "neutral" and not self._gate_neutral_logged:
                    logger.warning(
                        "[Layer2] 指数 %s 数据不足 (%d 点 < MA%d)，门控按 risk_off 处理，禁止开新仓",
                        idx.name, idx.state().data_points, self.config.index_ma_period,
                    )
                    self._gate_neutral_logged = True
                if not gate_open:
                    logger.info("[Layer2] 指数 %s risk_off, 开仓缩放 ×%.2f",
                                idx.name, self.config.gate_risk_scale)

        # 1. 先处理所有标的的出场/入场 (引擎内部逻辑)
        for symbol, bar in bars.items():
            if symbol not in self.engines:
                continue
            engine = self.engines[symbol]
            # 门控: 全禁模式 (scale=0) 关闭入场; 缩放模式 (scale>0) 降仓
            if self.config.use_index_gate and gate_open:
                engine.entry_gate = True
                engine.gate_risk_scale = 1.0
            elif self.config.use_index_gate and self.config.gate_risk_scale <= 0:
                engine.entry_gate = False
                engine.gate_risk_scale = 1.0
            else:
                engine.entry_gate = True
                engine.gate_risk_scale = self.config.gate_risk_scale if not gate_open else 1.0
            ind = indicators.get(symbol, {})
            engine_actions = engine.on_bar(bar, ind)
            for a in engine_actions:
                a.meta.setdefault("symbol", symbol)
            actions.extend(engine_actions)
            # 新信号因满仓被拒 → 请求 Layer2 旋转 (关闭最弱持仓为新信号腾出预算)
            if engine.entry_rejected_capacity and self.config.rotate_on_cap:
                self.request_rotation(symbol)
            self._last_prices[symbol] = bar.close
            engine.entry_gate = True
            engine.gate_risk_scale = 1.0

        # 2. 更新组合权益
        self._update_equity()

        # 3. 组合级优胜劣汰
        prune = self._prune_weak_positions()
        actions.extend(prune)

        # 4. 满仓旋转
        rotations = self._rotate_on_capacity()
        actions.extend(rotations)

        return actions

    # ------------------------------------------------------------------
    # 仓位公式 — 仓位 = 允许亏损 / 止损偏差
    # ------------------------------------------------------------------
    def compute_position_quantity(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        risk_per_trade: float | None = None,
    ) -> float:
        """核心公式: quantity = allowed_loss / stop_deviation。

        allowed_loss  = 组合权益 × 单标的风险比例
        stop_deviation = |entry - stop|
        """
        risk = risk_per_trade if risk_per_trade is not None else self.config.default_risk_per_trade
        # 宏观风险缩放: risk_off 时整体缩仓
        risk = risk * self.macro_state.risk_scale
        # 波动率目标缩放: 组合波动率高 → 降仓
        if self.config.use_vol_targeting and self.index is not None:
            risk = risk * self.index.exposure_scale(
                target_vol=self.config.index_vol_target,
                floor=self.config.index_vol_floor,
                cap=self.config.index_vol_cap,
            )
        # 指数门控: risk_off 时按 gate_risk_scale 收缩风险预算 (0=全禁, 0.33=1/3)
        engine = self.engines.get(symbol)
        if engine is not None and self.config.use_index_gate:
            risk = risk * engine.gate_risk_scale
        equity = self.total_equity
        allowed_loss = equity * risk
        deviation = abs(entry_price - stop_loss)
        if deviation <= 0:
            return 0.0
        # 最小偏差下限: 极窄止损时防止仓位爆炸 (与 risk/sizing.py 同口径)
        deviation = max(deviation, entry_price * MIN_DEVIATION_PCT)
        quantity = allowed_loss / deviation

        # 现金约束
        engine = self.engines.get(symbol)
        if engine is not None and entry_price > 0:
            max_by_cash = engine.cash / entry_price
            # 保留最低现金比例
            max_by_cash *= (1.0 - self.config.min_cash_ratio)
            quantity = min(quantity, max_by_cash)
        return max(0.0, quantity)

    def inject_sizing(self) -> None:
        """把组合层的仓位公式注入到各引擎 (覆盖 Layer1 默认)。"""
        for symbol, engine in self.engines.items():
            engine._compute_quantity = (  # type: ignore[method-assign]
                lambda price, sl, sym=symbol: self.compute_position_quantity(sym, price, sl)
            )

    # ------------------------------------------------------------------
    # 优胜劣汰 — 持仓排名
    # ------------------------------------------------------------------
    def rank_positions(self, prices: dict[str, float] | None = None) -> list[PositionScore]:
        """对所有引擎的持仓评分排序，返回从好到差。"""
        prices = prices or self._last_prices
        scores: list[PositionScore] = []

        for symbol, engine in self.engines.items():
            price = prices.get(symbol)
            if price is None:
                continue
            for pos in engine.positions:
                if pos.side == "long":
                    pnl_pct = (price - pos.entry_price) / pos.entry_price * 100.0
                else:
                    pnl_pct = (pos.entry_price - price) / pos.entry_price * 100.0

                momentum = pnl_pct / 10.0
                trend_align = self._trend_alignment(symbol, price, pos.side)

                metric = self.config.ranking_metric
                rs_boost = 0.0
                if self.config.use_relative_strength and self.index is not None:
                    close_series = self._price_series.get(symbol)
                    if close_series is not None and len(close_series) > 2:
                        rs = self.index.relative_strength(close_series)
                        rs_boost = max(-0.3, min(0.3, rs * 0.5))

                if metric == "momentum":
                    score = momentum + rs_boost
                elif metric == "profit":
                    score = pnl_pct / 100.0 + rs_boost
                else:  # composite
                    score = momentum * 0.5 + trend_align * 0.5 + rs_boost

                scores.append(PositionScore(
                    symbol=symbol,
                    score=round(max(-1.0, min(1.0, score)), 4),
                    reason=f"pnl={pnl_pct:+.1f}% trend={trend_align:.2f}",
                    pnl_pct=round(pnl_pct, 2),
                    details={"momentum": momentum, "trend_align": trend_align},
                ))

        scores.sort(key=lambda s: s.score, reverse=True)
        return scores

    def _trend_alignment(self, symbol: str, price: float, side: str) -> float:
        """价格与出场均线的对齐度 (0~1)。"""
        engine = self.engines.get(symbol)
        if engine is None:
            return 0.5
        ma_x = engine.indicators.get("mas", {}).get(engine.params.ma_exit)
        if ma_x is None:
            return 0.5
        try:
            val = float(ma_x.dropna().iloc[-1])
            if val <= 0:
                return 0.5
            if side == "long":
                return min(1.0, max(0.0, 0.5 + (price / val - 1.0) * 10.0))
            return min(1.0, max(0.0, 0.5 + (val / price - 1.0) * 10.0))
        except Exception:
            return 0.5

    # ------------------------------------------------------------------
    # 优胜劣汰 — 淘汰与旋转
    # ------------------------------------------------------------------
    def _prune_weak_positions(self) -> list[EngineAction]:
        """淘汰排名低于阈值的持仓。"""
        if self.config.prune_threshold <= -1.0:
            return []
        scores = self.rank_positions()
        actions: list[EngineAction] = []
        for s in scores:
            if s.score >= self.config.prune_threshold:
                continue
            engine = self.engines.get(s.symbol)
            if engine is None or not engine.positions:
                continue
            qty = sum(p.quantity for p in engine.positions)
            price = self._last_prices.get(s.symbol, 0.0)
            self._force_close(symbol=s.symbol, reason=f"prune_{s.score:.2f}")
            actions.append(EngineAction(
                action="close",
                reason=f"prune_score_{s.score:.2f}",
                price=price,
                quantity=qty,
                meta={"symbol": s.symbol, "rank_score": s.score, "layer": "layer2"},
            ))
        return actions

    def _rotate_on_capacity(self) -> list[EngineAction]:
        """满仓时：替换最弱持仓。"""
        if not self.config.rotate_on_cap or not self._pending_rotations:
            return []
        total_positions = sum(len(e.positions) for e in self.engines.values())
        if total_positions < self.config.max_positions:
            self._pending_rotations.clear()
            return []

        scores = self.rank_positions()
        if not scores:
            return []
        weakest = scores[-1]
        engine = self.engines.get(weakest.symbol)
        if engine is None or not engine.positions:
            return []

        qty = sum(p.quantity for p in engine.positions)
        price = self._last_prices.get(weakest.symbol, 0.0)
        self._force_close(symbol=weakest.symbol, reason=f"rotate_{weakest.score:.2f}")
        self._pending_rotations.clear()
        return [EngineAction(
            action="close",
            reason=f"rotate_out_weakest_{weakest.score:.2f}",
            price=price,
            quantity=qty,
            meta={"symbol": weakest.symbol, "layer": "layer2"},
        )]

    def request_rotation(self, symbol: str) -> None:
        """外部 (引擎新信号) 请求旋转。"""
        if symbol not in self._pending_rotations:
            self._pending_rotations.append(symbol)

    # ------------------------------------------------------------------
    # 强制平仓
    # ------------------------------------------------------------------
    def _force_close(self, symbol: str, reason: str) -> None:
        """直接平掉某标的的全部持仓。"""
        engine = self.engines.get(symbol)
        if engine is None or not engine.positions:
            return
        price = self._last_prices.get(symbol)
        if price is None or price <= 0:
            return

        for pos in list(engine.positions):
            if pos.side == "long":
                pnl = (price - pos.entry_price) * pos.quantity
                pnl_pct = (price - pos.entry_price) / pos.entry_price * 100.0
            else:
                pnl = (pos.entry_price - price) * pos.quantity
                pnl_pct = (pos.entry_price - price) / pos.entry_price * 100.0

            engine.trades.append(TradeRecord(
                side=pos.side,
                entry_time=str(pos.entry_time) if pos.entry_time else "?",
                exit_time=str(datetime.now()),
                entry_price=pos.entry_price,
                exit_price=price,
                quantity=pos.quantity,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=reason,
            ))
            engine.cash += pos.quantity * price
        engine.positions.clear()
        logger.info("[Layer2] 强制平仓 %s (%s)", symbol, reason)

    def force_close(self, symbol: str, reason: str = "llm_action") -> None:
        """外部 (Layer3) 调用的强制平仓入口。"""
        self._force_close(symbol, reason)

    def force_close_all(self, reason: str = "llm_flatten") -> None:
        """清仓全部持仓 (LLM 紧急指令)。"""
        for symbol in list(self.engines.keys()):
            self._force_close(symbol, reason)

    def rebalance_to_cash(self, ratio: float = 1.0, reason: str = "llm_rebalance") -> None:
        """按比例减仓回笼现金。ratio=1.0 全平。"""
        if ratio >= 1.0:
            self.force_close_all(reason)
            return
        # 部分减仓: 按排名从弱到强砍
        scores = self.rank_positions()
        target_close = int(len(scores) * ratio)
        for s in scores[-target_close:]:
            self._force_close(s.symbol, reason)

    # ------------------------------------------------------------------
    # 权益与状态
    # ------------------------------------------------------------------
    def _update_equity(self) -> None:
        total = 0.0
        for symbol, engine in self.engines.items():
            price = self._last_prices.get(symbol)
            total += engine.current_equity(price)
        # 若引擎数 > 1，cash 已在各自引擎里，直接加即可
        # 但初始资金是共享的，所以取平均避免重复计算
        # 简化: 用第一个引擎的 cash + 所有持仓市值
        if len(self.engines) == 1:
            self.total_equity = total
        else:
            # 多引擎模式: cash 之和 + 持仓市值
            cash_sum = sum(e.cash for e in self.engines.values())
            pos_value = 0.0
            for symbol, engine in self.engines.items():
                price = self._last_prices.get(symbol, 0.0)
                for pos in engine.positions:
                    pos_value += pos.quantity * price
            self.total_equity = cash_sum + pos_value
        self.combined_equity.append(self.total_equity)

    def snapshot(self) -> dict[str, Any]:
        """给 Layer3 LLM 的组合快照。"""
        scores = self.rank_positions()
        open_positions = []
        for symbol, engine in self.engines.items():
            for pos in engine.positions:
                price = self._last_prices.get(symbol, pos.entry_price)
                open_positions.append({
                    "symbol": symbol,
                    "side": pos.side,
                    "entry": pos.entry_price,
                    "current": price,
                    "sl": pos.sl,
                    "tp": pos.tp,
                    "qty": pos.quantity,
                })
        all_trades = []
        for eng in self.engines.values():
            all_trades.extend([t.to_dict() for t in eng.trades[-10:]])

        return {
            "total_equity": round(self.total_equity, 2),
            "initial_capital": self.params.initial_capital,
            "return_pct": round(
                (self.total_equity / self.params.initial_capital - 1) * 100, 2
            ) if self.params.initial_capital > 0 else 0.0,
            "open_positions": open_positions,
            "rankings": [
                {"symbol": s.symbol, "score": s.score, "pnl_pct": s.pnl_pct, "reason": s.reason}
                for s in scores
            ],
            "recent_trades": all_trades[-10:],
            "num_engines": len(self.engines),
            "macro": self.macro_state.to_dict(),
            "index": self.index.to_dict() if self.index is not None else None,
            "config": {
                "max_total_risk": self.config.max_total_risk,
                "default_risk_per_trade": self.config.default_risk_per_trade,
                "max_positions": self.config.max_positions,
                "prune_threshold": self.config.prune_threshold,
            },
        }

    def apply_llm_override(self, updates: dict) -> list[str]:
        """Layer3 入口 — 修改组合配置。"""
        applied: list[str] = []
        for key, value in updates.items():
            if hasattr(self.config, key):
                try:
                    old = getattr(self.config, key)
                    if isinstance(old, float):
                        setattr(self.config, key, float(value))
                    elif isinstance(old, int):
                        setattr(self.config, key, int(value))
                    elif isinstance(old, bool):
                        setattr(self.config, key, bool(value))
                    else:
                        setattr(self.config, key, value)
                    applied.append(key)
                except (TypeError, ValueError):
                    continue
            # 同时可下发到 params
            if hasattr(self.params, key):
                self.params.override({key: value})
                if key not in applied:
                    applied.append(key)
        # 同步到所有引擎
        for eng in self.engines.values():
            eng.params.override(updates)
        return applied
