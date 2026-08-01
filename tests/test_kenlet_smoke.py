"""
kenlet-v1 冒烟测试 — 三层架构核心链路验证。
运行: PYTHONPATH=src pytest tests/test_kenlet_smoke.py -v
"""

from __future__ import annotations

import pickle
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from kenlet.analysis.indicators import adx, atr, compute_indicators, rsi, sma
from kenlet.core.engine import TradingEngine
from kenlet.core.metrics import compute_metrics
from kenlet.core.models import Bar, Position, Signal
from kenlet.core.params import StrategyParams
from kenlet.core.runner import BacktestRunner
from kenlet.llm.advisor import LLMAdvisor, LLMDecision
from kenlet.portfolio.manager import PortfolioConfig, PortfolioManager
from kenlet.risk.regime import detect_regime, regime_allows_trade
from kenlet.risk.sizing import risk_based_size
from kenlet.risk.stops import compute_stops
from kenlet.strategy.ma_crossover import MACrossoverStrategy


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def _make_df(n: int = 300, seed: int = 42, trend: float = 0.05) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(trend / 100, 1.5 / 100, n)))
    ts = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "timestamp": ts,
        "open": close * (1 + rng.normal(0, 0.001, n)),
        "high": close * (1 + rng.normal(0.005, 0.002, n)),
        "low": close * (1 - rng.normal(0.005, 0.002, n)),
        "close": close,
        "volume": rng.uniform(800, 1200, n),
    })


def test_indicators():
    df = _make_df()
    assert sma(df["close"], 20).iloc[-1] > 0
    assert atr(df, 14).iloc[-1] > 0
    assert 0 <= rsi(df["close"], 14).iloc[-1] <= 100
    assert adx(df, 14).iloc[-1] >= 0
    ind = compute_indicators(df, [20, 60, 180], 14)
    assert set(ind.keys()) >= {"mas", "atr", "rsi", "adx"}


def test_regime():
    df = _make_df(n=300, trend=0.15)  # 强上升趋势
    r = detect_regime(df)
    labels = set(r.dropna().unique())
    assert labels <= {"bull", "bear", "ranging", "choppy"}
    assert regime_allows_trade("bull", "long") is True
    assert regime_allows_trade("bull", "short") is False
    assert regime_allows_trade("choppy", "long") is False


# ---------------------------------------------------------------------------
# 仓位公式与止损
# ---------------------------------------------------------------------------

def test_risk_sizing_formula():
    """仓位 = 允许亏损 / 止损偏差"""
    qty = risk_based_size(10000, 100, 97, 0.02)
    assert abs(qty - 200 / 3) < 1e-6
    # 零偏差 → 0
    assert risk_based_size(10000, 100, 100, 0.02) == 0.0


def test_atr_stops():
    sl, tp = compute_stops(100, 2.0, "long", 3.0, 6.0)
    assert sl == 94.0 and tp == 112.0
    sl2, tp2 = compute_stops(100, 2.0, "short", 3.0, 6.0)
    assert sl2 == 106.0 and tp2 == 88.0


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

def test_engine_opens_and_closes():
    params = StrategyParams(
        initial_capital=10000, ma_entry=10, ma_exit=30, ma_trend=60,
        regime_filter=False, trend_filter=False, use_risk_sizing=True,
        risk_per_trade=0.02, atr_sl_mult=3.0, atr_tp_mult=6.0,
    )
    engine = TradingEngine("TEST/USDT", params, MACrossoverStrategy())
    df = _make_df(n=300, trend=0.1)
    ind = compute_indicators(df, [10, 30, 60, 180], 14)
    total_actions = 0
    trades = 0
    for i in range(60, len(df)):
        bar = Bar.from_row(df.iloc[i])
        sliced = {
            "mas": {p: s.iloc[: i + 1] for p, s in ind["mas"].items()},
            "atr": ind["atr"].iloc[: i + 1],
            "adx": ind["adx"].iloc[: i + 1],
            "rsi": ind["rsi"].iloc[: i + 1],
            "close": df["close"].iloc[: i + 1],
        }
        acts = engine.on_bar(bar, sliced)
        total_actions += len(acts)
        trades = len(engine.trades)
    assert total_actions > 0
    assert trades >= 0
    assert engine.capital > 0


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------

def test_backtest_single():
    params = StrategyParams(
        initial_capital=10000, ma_entry=20, ma_exit=60, ma_trend=180,
        regime_filter=True, trend_filter=True,
    )
    runner = BacktestRunner(params=params)
    df = _make_df(n=400, trend=0.1)
    result = runner.run_single(df, "TEST/USDT")
    metrics = result["metrics"]
    assert metrics.initial_capital == 10000
    assert metrics.final_capital > 0
    assert result["equity_curve"]


def test_backtest_with_cached_data():
    """用项目缓存的真实行情数据跑一次。"""
    with open("tests/market_data.pkl", "rb") as f:
        dfs = pickle.load(f)
    params = StrategyParams(
        initial_capital=10000, ma_entry=20, ma_exit=60, ma_trend=180,
        regime_filter=True, trend_filter=True,
    )
    runner = BacktestRunner(params=params)
    result = runner.run_single(dfs["BTC"], "BTC/USDT")
    assert result["metrics"].final_capital > 0


# ---------------------------------------------------------------------------
# 组合层
# ---------------------------------------------------------------------------

def test_portfolio_sizing_injection():
    pm = PortfolioManager(PortfolioConfig())
    params = StrategyParams(initial_capital=10000)
    engine = TradingEngine("BTC/USDT", params, MACrossoverStrategy())
    pm.attach("BTC/USDT", engine)
    pm.inject_sizing()
    qty = pm.compute_position_quantity("BTC/USDT", 100, 97)
    assert qty == pytest.approx(200 / 3, rel=1e-6)


def test_portfolio_force_close():
    pm = PortfolioManager(PortfolioConfig())
    params = StrategyParams(initial_capital=10000)
    engine = TradingEngine("BTC/USDT", params, MACrossoverStrategy())
    pm.attach("BTC/USDT", engine)
    engine.positions.append(Position(side="long", entry_price=100, quantity=1.0, sl=90, tp=120))
    pm._last_prices["BTC/USDT"] = 110
    pm.force_close("BTC/USDT", "test")
    assert len(engine.positions) == 0
    assert len(engine.trades) == 1


def test_portfolio_rank():
    pm = PortfolioManager(PortfolioConfig())
    e1 = TradingEngine("A/USDT", StrategyParams(), MACrossoverStrategy())
    e2 = TradingEngine("B/USDT", StrategyParams(), MACrossoverStrategy())
    pm.attach("A/USDT", e1)
    pm.attach("B/USDT", e2)
    e1.positions.append(Position(side="long", entry_price=100, quantity=1.0, sl=90, tp=120))
    e2.positions.append(Position(side="long", entry_price=200, quantity=1.0, sl=190, tp=240))
    pm._last_prices = {"A/USDT": 95, "B/USDT": 210}
    scores = pm.rank_positions()
    assert len(scores) == 2
    # B 盈利、A 亏损 → B 应该排前面
    assert scores[0].symbol == "B/USDT"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def test_llm_parse():
    d = LLMDecision.from_json(
        '{"action":"adjust","reason":"test","confidence":0.8,'
        '"param_overrides":{"risk_per_trade":0.03},'
        '"portfolio_commands":[{"cmd":"force_close","symbol":"X/USDT"}]}'
    )
    assert d.action == "adjust"
    assert d.param_overrides["risk_per_trade"] == 0.03


def test_llm_markdown_parse():
    d = LLMDecision.from_json('```json\n{"action":"flatten","confidence":0.9}\n```')
    assert d.action == "flatten"


def test_llm_guards():
    adv = LLMAdvisor(enabled=True)
    clamped = adv._clamp_params({"risk_per_trade": 0.5, "atr_sl_mult": 10, "ma_entry": 3})
    assert clamped["risk_per_trade"] == 0.05   # 上限
    assert clamped["atr_sl_mult"] == 8.0       # 上限
    assert clamped["ma_entry"] == 5            # 下限


def test_llm_disabled_returns_hold():
    adv = LLMAdvisor(enabled=False)
    assert not adv.should_evaluate(10000)
    d = adv.evaluate({})
    assert d.action == "hold"
