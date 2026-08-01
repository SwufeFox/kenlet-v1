"""
统一运行器 — 回测与实盘共用同一条路径。

回测: 历史 DataFrame → 逐 bar 喂给 PortfolioManager
实盘: 定时拉最新 K 线 → 同样喂给 PortfolioManager
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from kenlet.analysis.indicators import compute_indicators
from kenlet.analysis.macro import MacroRegime
from kenlet.config import build_portfolio_config, build_strategy_params, load_config
from kenlet.core.engine import TradingEngine
from kenlet.core.metrics import PerformanceMetrics, compute_metrics
from kenlet.core.models import Bar
from kenlet.core.params import StrategyParams
from kenlet.llm.advisor import LLMAdvisor
from kenlet.portfolio.manager import PortfolioConfig, PortfolioManager
from kenlet.risk.regime import detect_regime
from kenlet.strategy.base import Strategy
from kenlet.strategy.ma_crossover import MACrossoverStrategy

logger = logging.getLogger(__name__)


class BacktestRunner:
    """回测运行器 — 用统一引擎跑历史数据。"""

    def __init__(
        self,
        params: StrategyParams | None = None,
        portfolio_config: PortfolioConfig | None = None,
        strategy: Strategy | None = None,
        llm: LLMAdvisor | None = None,
    ) -> None:
        self.params = params or StrategyParams()
        self.portfolio_config = portfolio_config or PortfolioConfig()
        self.strategy = strategy or MACrossoverStrategy()
        self.llm = llm
        self.portfolio = PortfolioManager(self.portfolio_config, self.params)

    def run(
        self,
        data: dict[str, pd.DataFrame],
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """跑多标的回测。

        Parameters
        ----------
        data : dict[str, DataFrame]
            {symbol: OHLCV DataFrame}
        """
        # 注册引擎 — 总资金按标的数分摊 (防止虚拟杠杆)
        n_symbols = max(len(data), 1)
        per_symbol_capital = self.params.initial_capital / n_symbols
        for symbol, df in data.items():
            sub_params = self.params.clone()
            sub_params.initial_capital = per_symbol_capital
            engine = TradingEngine(symbol, sub_params, self.strategy)
            self.portfolio.attach(symbol, engine)
        self.portfolio.inject_sizing()

        # 宏观指数 (可选): 数据在 macro/ 目录时自动接入
        if self.portfolio.macro is None:
            try:
                self.portfolio.attach_macro(MacroRegime("macro"))
            except Exception as e:
                logger.debug("宏观模块未接入: %s", e)

        # 预计算指标
        all_indicators: dict[str, dict] = {}
        all_regimes: dict[str, pd.Series] = {}
        periods = [self.params.ma_entry, self.params.ma_exit, self.params.ma_trend, 20, 38, 60, 180, 540]
        periods = sorted(set(periods))

        for symbol, df in data.items():
            df = self._filter_dates(df, start_date, end_date)
            data[symbol] = df
            ind = compute_indicators(df, periods, self.params.atr_period)
            regime = detect_regime(df, periods=periods, atr_period=self.params.atr_period)
            ind["regime_series"] = regime
            all_indicators[symbol] = ind
            all_regimes[symbol] = regime

        # 找公共时间轴 (以第一个标的为准，简化)
        primary = next(iter(data.values()))
        n = len(primary)
        warmup = max(self.params.ma_exit, self.params.ma_trend, 60)

        for i in range(warmup, n):
            bars: dict[str, Bar] = {}
            inds: dict[str, dict] = {}

            for symbol, df in data.items():
                if i >= len(df):
                    continue
                row = df.iloc[i]
                bars[symbol] = Bar.from_row(row)

                # 切片到当前 bar (防止未来函数)
                sliced = self._slice_indicators(all_indicators[symbol], i)
                regime_val = "unknown"
                try:
                    regime_val = str(all_regimes[symbol].iloc[i])
                except Exception:
                    pass
                sliced["regime"] = regime_val
                inds[symbol] = sliced

            if not bars:
                continue

            self.portfolio.on_bars(bars, inds)

            # Layer3 评估
            if self.llm and self.llm.enabled:
                # 取主标的 regime
                primary_sym = next(iter(bars.keys()))
                regime = inds.get(primary_sym, {}).get("regime", "unknown")
                if self.llm.should_evaluate(self.portfolio.total_equity, regime=regime):
                    ctx = self.portfolio.snapshot()
                    ctx["params"] = self.params.to_dict()
                    ctx["market"] = {
                        "regime": regime,
                        "prices": {s: b.close for s, b in bars.items()},
                    }
                    decision = self.llm.evaluate(ctx)
                    applied = self.llm.apply(decision, self.portfolio)
                    if applied:
                        logger.info("[Layer3] 已执行: %s", applied)

        # 强制平仓残留
        for symbol, engine in self.portfolio.engines.items():
            if engine.positions:
                price = self.portfolio._last_prices.get(symbol)
                if price:
                    self.portfolio.force_close(symbol, "end_of_data")

        return self._build_result()

    def run_single(
        self,
        df: pd.DataFrame,
        symbol: str = "BTC/USDT",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """单标的便捷入口。"""
        return self.run({symbol: df}, start_date, end_date)

    def _build_result(self) -> dict[str, Any]:
        all_trades = []
        for eng in self.portfolio.engines.values():
            all_trades.extend(eng.trades)

        metrics = compute_metrics(
            all_trades,
            self.portfolio.combined_equity,
            self.params.initial_capital,
        )
        return {
            "metrics": metrics,
            "trades": [t.to_dict() for t in all_trades],
            "equity_curve": self.portfolio.combined_equity,
            "snapshot": self.portfolio.snapshot(),
            "llm_decisions": self.llm.decision_history if self.llm else [],
        }

    @staticmethod
    def _filter_dates(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
        if "timestamp" not in df.columns:
            return df.reset_index(drop=True)
        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        if hasattr(df["timestamp"].dtype, "tz") and df["timestamp"].dtype.tz is not None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(None)
        mask = pd.Series(True, index=df.index)
        if start:
            mask &= df["timestamp"] >= pd.Timestamp(start)
        if end:
            mask &= df["timestamp"] <= pd.Timestamp(end + " 23:59:59")
        return df[mask].reset_index(drop=True)

    @staticmethod
    def _slice_indicators(ind: dict, i: int) -> dict:
        """切片指标到当前 bar，杜绝未来函数。"""
        out = {}
        for key, val in ind.items():
            if key == "mas" and isinstance(val, dict):
                out["mas"] = {p: s.iloc[: i + 1] for p, s in val.items()}
            elif key == "regime_series":
                continue
            elif hasattr(val, "iloc"):
                out[key] = val.iloc[: i + 1]
            else:
                out[key] = val
        return out


def create_runner_from_config(config: dict | None = None) -> BacktestRunner:
    """从配置文件创建完整运行器 (含可选 LLM)。"""
    cfg = config or load_config()
    params = build_strategy_params(cfg)
    pcfg = build_portfolio_config(cfg)
    llm_cfg = cfg.get("llm", {})
    llm = None
    if params.llm_enabled or llm_cfg.get("enabled"):
        llm = LLMAdvisor(
            enabled=True,
            check_interval=int(llm_cfg.get("check_interval", params.llm_check_interval)),
            drawdown_trigger=float(llm_cfg.get("drawdown_trigger", 0.08)),
        )
    return BacktestRunner(params=params, portfolio_config=pcfg, llm=llm)
