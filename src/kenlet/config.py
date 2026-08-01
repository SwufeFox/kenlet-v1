"""
统一配置加载 — YAML + 环境变量覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from kenlet.core.params import StrategyParams
from kenlet.portfolio.manager import PortfolioConfig

# 项目根目录
_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载 config.yaml，并用环境变量覆盖敏感字段。"""
    load_dotenv()
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    # env 覆盖
    if os.environ.get("BINANCE_API_KEY"):
        cfg.setdefault("binance", {})["apiKey"] = os.environ["BINANCE_API_KEY"]
    secret = os.environ.get("BINANCE_SECRET_KEY") or os.environ.get("BINANCE_SECRET")
    if secret:
        cfg.setdefault("binance", {})["secret"] = secret
    if os.environ.get("KENLET_LOG_LEVEL"):
        cfg.setdefault("logging", {})["level"] = os.environ["KENLET_LOG_LEVEL"]

    return cfg


def get_watchlist(config: dict | None = None) -> list[str]:
    """获取监控币种列表。"""
    cfg = config or load_config()
    # 优先 symbols，兼容旧 watchlist.symbols
    symbols = cfg.get("symbols")
    if symbols:
        return list(symbols)
    wl = cfg.get("watchlist", {})
    if isinstance(wl, dict):
        return list(wl.get("symbols", []))
    return ["BTC", "ETH", "SOL"]


def build_strategy_params(config: dict | None = None) -> StrategyParams:
    """从 config 构造 StrategyParams。"""
    cfg = config or load_config()
    s = cfg.get("strategy", {})
    r = cfg.get("risk", {})
    b = cfg.get("backtest", {})
    llm = cfg.get("llm", {})
    return StrategyParams(
        ma_entry=int(s.get("ma_entry", 38)),
        ma_exit=int(s.get("ma_exit", 60)),
        ma_trend=int(s.get("ma_trend", 180)),
        min_cross_pct=float(s.get("min_cross_pct", 0.0)),
        regime_filter=bool(s.get("regime_filter", True)),
        trend_filter=bool(s.get("trend_filter", True)),
        atr_period=int(r.get("atr_period", 14)),
        atr_sl_mult=float(r.get("atr_sl_mult", 3.0)),
        atr_tp_mult=float(r.get("atr_tp_mult", 6.0)),
        risk_per_trade=float(r.get("risk_per_trade", 0.02)),
        max_positions=int(r.get("max_positions", 3)),
        use_risk_sizing=bool(r.get("use_risk_sizing", True)),
        initial_capital=float(b.get("initial_capital", 10000.0)),
        position_size=float(b.get("position_size", 0.25)),
        llm_enabled=bool(llm.get("enabled", False)),
        llm_check_interval=int(llm.get("check_interval", 50)),
    )


def build_portfolio_config(config: dict | None = None) -> PortfolioConfig:
    """从 config 构造 PortfolioConfig。"""
    cfg = config or load_config()
    p = cfg.get("portfolio", {})
    idx = cfg.get("index", {})
    r = cfg.get("risk", {})
    return PortfolioConfig(
        max_total_risk=float(p.get("max_total_risk", 0.10)),
        default_risk_per_trade=float(p.get("default_risk_per_trade", r.get("risk_per_trade", 0.02))),
        max_positions=int(p.get("max_positions", r.get("max_positions", 5))),
        ranking_metric=str(p.get("ranking_metric", "composite")),
        prune_threshold=float(p.get("prune_threshold", -0.5)),
        rotate_on_cap=bool(p.get("rotate_on_cap", True)),
        min_cash_ratio=float(p.get("min_cash_ratio", 0.05)),
        # 指数因子 (QQQ/TWI/合成指数)
        use_index_gate=bool(idx.get("use_index_gate", False)),
        gate_risk_scale=float(idx.get("gate_risk_scale", 0.0)),
        use_relative_strength=bool(idx.get("use_relative_strength", False)),
        use_vol_targeting=bool(idx.get("use_vol_targeting", False)),
        index_ma_period=int(idx.get("ma_period", 200)),
        index_vol_target=float(idx.get("vol_target", 0.15)),
        index_vol_floor=float(idx.get("vol_floor", 0.2)),
        index_vol_cap=float(idx.get("vol_cap", 1.5)),
        index_name=str(idx.get("name", "MARKET")),
    )
