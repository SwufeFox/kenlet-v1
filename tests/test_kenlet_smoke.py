"""
kenlet-v1 冒烟测试 — 三层架构核心链路验证。
运行: pytest tests/test_kenlet_smoke.py -v (src 已由 pyproject pythonpath 配置)
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
from kenlet.strategy.base import Strategy


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
    # 引擎不变量: 现金 + 持仓市值 == 权益
    last_close = float(df["close"].iloc[-1])
    assert abs(engine.capital - (engine.cash + sum(p.quantity * last_close for p in engine.positions))) < 1e-6


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


# ---------------------------------------------------------------------------
# 回归: MA 金叉语义 / 参数护栏 / 组合指令校验
# ---------------------------------------------------------------------------

def test_strategy_uses_price_cross_not_ma_cross():
    """入场 = 收盘价上穿/下穿快均线 (ma_entry); 快慢均线交叉但价格未穿越 → 无信号。

    注意: 这是系统实际采用的入场语义 (README §4 实验数字均由它产出),
    慢均线 (ma_exit) 只负责出场 (trend_reversal)。
    """
    st = MACrossoverStrategy()
    params = StrategyParams(ma_entry=10, ma_exit=30, regime_filter=False, trend_filter=False)
    bar = Bar(timestamp=pd.Timestamp("2024-01-03"), open=100, high=103, low=99, close=101, volume=1000)

    def make_mas(prev_close, prev_e, cur_e, prev_x, cur_x):
        idx = pd.date_range("2024-01-01", periods=3, freq="D")
        return {
            "mas": {
                10: pd.Series([100.0, prev_e, cur_e], index=idx),
                30: pd.Series([100.0, prev_x, cur_x], index=idx),
            },
            "close": pd.Series([98.0, prev_close, 101.0], index=idx),
        }

    # 价格上穿 MA10 (97→101), 即使 MA10 未上穿 MA30 → 做多信号
    sig = st.on_bar(bar, make_mas(97.0, 98.0, 99.5, 99.0, 99.8), params, [])
    assert sig is not None and sig.direction == "long"
    # MA10 上穿 MA30 (真金叉), 但价格始终在快均线上方未穿越 → 无信号
    assert st.on_bar(bar, make_mas(102.0, 98.0, 100.2, 99.0, 99.8), params, []) is None
    # 价格下穿 MA10 (103→99) → 做空信号
    sig2 = st.on_bar(bar, make_mas(103.0, 101.0, 102.0, 100.0, 100.5), params, [])
    assert sig2 is not None and sig2.direction == "short"


def test_override_string_bool_parsed_safely():
    """字符串 "false"/"0" 必须解析为 False, 不能踩 bool("false")==True。"""
    p = StrategyParams()
    applied = p.override({"regime_filter": "false", "trend_filter": "0", "use_risk_sizing": "true"})
    assert p.regime_filter is False
    assert p.trend_filter is False
    assert p.use_risk_sizing is True
    assert "regime_filter" in applied


def test_validate_commands_filters_hallucinated_symbols():
    """组合指令指向不存在的标的 → 过滤 (README §3.4 可靠空间)。"""
    pm = PortfolioManager(PortfolioConfig())
    pm.attach("BTC/USDT", TradingEngine("BTC/USDT", StrategyParams(), MACrossoverStrategy()))
    adv = LLMAdvisor(enabled=True)
    d = LLMDecision(
        action="rebalance", confidence=0.9,
        portfolio_commands=[
            {"cmd": "force_close", "symbol": "BTC/USDT"},
            {"cmd": "force_close", "symbol": "XRP/USDT"},  # 幻觉标的
        ],
    )
    adv.validate_commands(d, pm)
    assert [c["symbol"] for c in d.portfolio_commands] == ["BTC/USDT"]


def test_ensemble_falls_back_when_disabled():
    """LLM 未启用时集成投票退化为 hold, 不发起 API 调用。"""
    adv = LLMAdvisor(enabled=False)
    d = adv.evaluate_ensemble({}, n_samples=3)
    assert d.action == "hold"


# ---------------------------------------------------------------------------
# 仓位公式: equity 口径 / 分批均分 / 最小偏差下限
# ---------------------------------------------------------------------------

class _SignalOnceStrategy(Strategy):
    """测试桩: 在指定时间戳发一次做多信号, 之后静默。"""
    name = "signal_once"

    def __init__(self, at: pd.Timestamp):
        self.at = at
        self.fired = False

    def on_bar(self, bar, indicators, params, positions):
        if not self.fired and bar.timestamp == self.at:
            self.fired = True
            return Signal(direction="long", strength=0.9, reason="test")
        return None


def test_engine_risk_sizing_uses_equity_not_cash():
    """引擎默认仓位公式以权益 (capital) 为基准, 与 README 公式 E·r 一致。"""
    params = StrategyParams(initial_capital=10000, use_risk_sizing=True, risk_per_trade=0.02)
    engine = TradingEngine("T/USDT", params, MACrossoverStrategy())
    engine.cash = 5000.0
    engine.capital = 10000.0
    qty = engine._compute_quantity(100, 95)
    assert qty == pytest.approx(10000 * 0.02 / 5.0)  # 40.0, 而非 5000×2%/5=20
    # 极窄止损 0.1%: 偏差下限 0.5% → qty = 200/0.5 = 400, 而非 200/0.1 = 2000
    assert engine._compute_quantity(100, 99.9) == pytest.approx(400.0)


def test_scaled_entries_split_risk_budget():
    """分批建仓: 总风险 = 一次预算 (每批 = 预算/批次), 而非每批独立 2%。"""
    params = StrategyParams(
        initial_capital=10000, use_risk_sizing=True, risk_per_trade=0.02,
        num_entries=3, entry_offset_bars=1,
    )
    engine = TradingEngine("TEST/USDT", params, _SignalOnceStrategy(pd.Timestamp("2024-01-03")))
    ind = {"mas": {}, "atr": pd.Series([0.0] * 5), "close": pd.Series([100.0] * 5)}
    for day in range(1, 6):
        ts = pd.Timestamp(f"2024-01-{day:02d}")
        engine.on_bar(Bar(timestamp=ts, open=100, high=100, low=100, close=100, volume=1000), ind)
    assert len(engine.positions) == 3
    total_risk = sum(p.quantity * abs(p.entry_price - p.sl) for p in engine.positions)
    assert total_risk == pytest.approx(10000 * 0.02, rel=1e-6)  # 200, 而非 600
    assert all(abs(p.quantity - 40.0 / 3) < 1e-9 for p in engine.positions)


def test_risk_based_size_min_deviation_floor():
    """最小止损偏差: 0.1% 止损按 0.5% 计算, 零偏差仍拒绝。"""
    assert risk_based_size(10000, 100, 99.9, 0.02) == pytest.approx(400.0)
    assert risk_based_size(10000, 100, 100, 0.02) == 0.0


def test_portfolio_sizing_min_deviation_floor():
    """组合路径同口径: 极窄止损被偏差下限约束, 且受现金上限 (保留 5%) 封顶。"""
    pm = PortfolioManager(PortfolioConfig())
    engine = TradingEngine("BTC/USDT", StrategyParams(), MACrossoverStrategy())
    pm.attach("BTC/USDT", engine)
    pm.inject_sizing()
    qty = pm.compute_position_quantity("BTC/USDT", 100, 99.9)
    # 公式 = 200/0.5 = 400; 现金上限 = 10000/100 × 0.95 = 95
    assert qty == pytest.approx(95.0)
