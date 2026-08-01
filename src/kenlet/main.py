"""
kenlet-v1 — CLI 入口。

命令:
    status [symbol]          行情摘要
    backtest SYMBOL START END [--llm] [--timeframe] [--capital] [--risk]
    run [symbol]             实盘/纸交易循环
    watchlist                监控列表
    dashboard                多标的概览
"""

from __future__ import annotations

import sys
from typing import Any

from kenlet.config import get_watchlist, load_config, build_strategy_params
from kenlet.utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


def _normalize_symbol(raw: str) -> str:
    sym = raw.upper()
    if "/" not in sym:
        sym = f"{sym}/USDT"
    return sym


def _load_cached_market_data(symbol: str, timeframe: str):
    """网络不可用时回退到本地缓存行情 (tests/market_data.pkl, 日线)。"""
    import pickle
    from pathlib import Path

    cache_path = Path(__file__).resolve().parent.parent.parent / "tests" / "market_data.pkl"
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, "rb") as f:
            dfs = pickle.load(f)
        base = symbol.split("/")[0]
        if base in dfs:
            df = dfs[base]
            if timeframe not in ("1d", "day", "daily"):
                print("[yellow]警告: 缓存数据只有日线，请求按日线处理[/]")
            return df
    except Exception as e:
        print(f"[yellow]缓存读取失败: {e}[/]")
    return None


# ===========================================================================
# status
# ===========================================================================

def cmd_status(args: list[str]) -> None:
    setup_logging()
    from rich.console import Console
    from kenlet.data.binance import fetch_ohlcv
    from kenlet.cli.display import print_status

    config = load_config()
    if not args:
        for coin in get_watchlist(config):
            _status_one(_normalize_symbol(coin), config)
        return
    _status_one(_normalize_symbol(args[0]), config)


def _status_one(symbol: str, config: dict) -> None:
    from kenlet.data.binance import fetch_ohlcv
    from kenlet.cli.display import print_status
    from rich.console import Console

    try:
        df = fetch_ohlcv(symbol=symbol, timeframe="1d", limit=5, config_override=config)
    except Exception as e:
        Console().print(f"[red]拉取 {symbol} 失败: {e}[/]")
        return
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    chg = latest["close"] - prev["close"]
    pct = chg / prev["close"] * 100 if prev["close"] else 0
    print_status(symbol, {
        "最新价": f"${latest['close']:,.2f}",
        "24h 涨跌": f"{chg:+,.2f} ({pct:+.2f}%)",
        "最高": f"${latest['high']:,.2f}",
        "最低": f"${latest['low']:,.2f}",
        "成交量": f"{latest['volume']:,.0f}",
    })


# ===========================================================================
# backtest — 统一引擎
# ===========================================================================

def cmd_backtest(args: list[str]) -> None:
    setup_logging()
    from rich.console import Console
    console = Console()

    if len(args) < 3:
        console.print("[red]用法: kenlet backtest <SYMBOL> <START> <END> [options][/]")
        console.print("[yellow]例: kenlet backtest BTC 2024-01-01 2024-12-31 --llm[/]")
        return

    symbol = _normalize_symbol(args[0])
    start_date = args[1]
    end_date = args[2]
    remaining = args[3:]

    # 解析 flags
    timeframe = "1d"
    capital = 10000.0
    risk = None
    use_llm = "--llm" in remaining
    limit = 1000

    j = 0
    while j < len(remaining):
        a = remaining[j]
        if a == "--timeframe" and j + 1 < len(remaining):
            timeframe = remaining[j + 1]; j += 2
        elif a == "--capital" and j + 1 < len(remaining):
            capital = float(remaining[j + 1]); j += 2
        elif a == "--risk" and j + 1 < len(remaining):
            risk = float(remaining[j + 1]); j += 2
        elif a == "--limit" and j + 1 < len(remaining):
            limit = int(remaining[j + 1]); j += 2
        elif a == "--llm":
            j += 1
        else:
            j += 1

    config = load_config()
    from kenlet.data.binance import fetch_ohlcv
    from kenlet.core.runner import create_runner_from_config, BacktestRunner
    from kenlet.config import build_strategy_params, build_portfolio_config
    from kenlet.llm.advisor import LLMAdvisor
    from kenlet.cli.display import print_backtest_results

    console.print(f"[cyan]拉取 {symbol} {timeframe} 数据...[/]")
    df = None
    try:
        df = fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit, config_override=config)
    except Exception as e:
        console.print(f"[yellow]网络拉取失败: {e}[/]")
    if df is None or df.empty:
        cached = _load_cached_market_data(symbol, timeframe)
        if cached is not None:
            console.print(f"[green]已回退到本地缓存数据 ({len(cached)} 根 K 线)[/]")
            df = cached
    if df is None or df.empty:
        console.print("[red]数据源不可用: 网络失败且无本地缓存[/]")
        sys.exit(1)

    if len(df) < 60:
        console.print(f"[red]数据不足: 需要 ≥60 根 K 线，实际 {len(df)}[/]")
        sys.exit(1)

    # 构造参数
    params = build_strategy_params(config)
    params.initial_capital = capital
    params.mode = "backtest"
    if risk is not None:
        params.risk_per_trade = risk
    if use_llm:
        params.llm_enabled = True

    pcfg = build_portfolio_config(config)
    llm = None
    if params.llm_enabled:
        llm = LLMAdvisor(enabled=True, check_interval=params.llm_check_interval)
        console.print("[yellow]Layer3 LLM 顾问已启用[/]")

    console.print(
        f"[cyan]回测 {symbol} {start_date}→{end_date} | "
        f"资金 ${capital:,.0f} | 风险 {params.risk_per_trade:.1%} | "
        f"MA{params.ma_entry}/{params.ma_exit}[/]"
    )

    runner = BacktestRunner(params=params, portfolio_config=pcfg, llm=llm)
    try:
        result = runner.run_single(df, symbol=symbol, start_date=start_date, end_date=end_date)
        print_backtest_results(result)
    except Exception as e:
        logger.error("回测失败: %s", e, exc_info=True)
        console.print(f"[red]回测错误: {e}[/]")
        sys.exit(1)


# ===========================================================================
# run — 实盘/纸交易循环 (统一引擎)
# ===========================================================================

def cmd_run(args: list[str]) -> None:
    """实盘循环 — 与回测共用同一引擎。"""
    setup_logging()
    import time
    from datetime import datetime
    from rich.console import Console
    console = Console()

    config = load_config()
    symbol = _normalize_symbol(args[0]) if args else "BTC/USDT"
    timeframe = "1h"
    use_llm = "--llm" in args

    # 解析
    j = 0
    while j < len(args):
        if args[j] == "--timeframe" and j + 1 < len(args):
            timeframe = args[j + 1]; j += 2
        else:
            j += 1

    from kenlet.data.binance import BinanceData
    from kenlet.config import build_strategy_params, build_portfolio_config
    from kenlet.core.engine import TradingEngine
    from kenlet.core.models import Bar
    from kenlet.core.runner import BacktestRunner
    from kenlet.analysis.indicators import compute_indicators
    from kenlet.risk.regime import detect_regime
    from kenlet.strategy.ma_crossover import MACrossoverStrategy
    from kenlet.portfolio.manager import PortfolioManager
    from kenlet.llm.advisor import LLMAdvisor

    params = build_strategy_params(config)
    params.mode = "paper"
    if use_llm:
        params.llm_enabled = True
    pcfg = build_portfolio_config(config)
    strategy = MACrossoverStrategy()
    portfolio = PortfolioManager(pcfg, params)
    engine = TradingEngine(symbol, params, strategy)
    portfolio.attach(symbol, engine)
    portfolio.inject_sizing()

    llm = None
    if params.llm_enabled:
        llm = LLMAdvisor(enabled=True, check_interval=params.llm_check_interval)

    bd = BinanceData(config)
    sleep_map = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    sleep_s = sleep_map.get(timeframe, 3600)

    console.print(f"[bold green]kenlet-v1 实盘循环[/]")
    console.print(f"  {symbol} | {timeframe} | MA{params.ma_entry}/{params.ma_exit}")
    console.print(f"  风险 {params.risk_per_trade:.1%} | ATR SL×{params.atr_sl_mult} TP×{params.atr_tp_mult}")
    if llm:
        console.print("  [yellow]Layer3 LLM 已启用[/]")

    periods = sorted(set([params.ma_entry, params.ma_exit, params.ma_trend, 20, 38, 60, 180, 540]))

    while True:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            console.print(f"\n[{now}] 检查 {symbol}...")

            limit = max(params.ma_trend * 2, 200)
            df = bd.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if hasattr(df["timestamp"].dtype, "tz") and df["timestamp"].dtype.tz is not None:
                df["timestamp"] = df["timestamp"].dt.tz_localize(None)

            ind = compute_indicators(df, periods, params.atr_period)
            regime_s = detect_regime(df, periods=periods)
            regime = str(regime_s.iloc[-1]) if not regime_s.empty else "unknown"
            ind["regime"] = regime

            bar = Bar.from_row(df.iloc[-1])
            actions = portfolio.on_bars({symbol: bar}, {symbol: ind})

            price = bar.close
            console.print(
                f"  价格 ${price:,.2f} | 状态 {regime} | "
                f"权益 ${portfolio.total_equity:,.2f} | "
                f"持仓 {len(engine.positions)}"
            )
            for a in actions:
                if a.is_trade:
                    console.print(f"  → [{a.action}] {a.reason} @ ${a.price:,.2f} qty={a.quantity:.6f}")

            # Layer3
            if llm and llm.should_evaluate(portfolio.total_equity, regime=regime):
                ctx = portfolio.snapshot()
                ctx["params"] = params.to_dict()
                ctx["market"] = {"regime": regime, "price": price}
                decision = llm.evaluate(ctx)
                applied = llm.apply(decision, portfolio)
                if applied:
                    console.print(f"  [yellow]LLM: {decision.reason} → {applied}[/]")

        except KeyboardInterrupt:
            console.print("\n停止交易循环")
            break
        except Exception as e:
            console.print(f"  [red]ERROR: {e}[/]")

        console.print(f"  下次检查: {sleep_s // 60} 分钟后")
        time.sleep(sleep_s)


# ===========================================================================
# watchlist / dashboard
# ===========================================================================

def cmd_watchlist(args: list[str]) -> None:
    setup_logging()
    from rich.console import Console
    config = load_config()
    wl = get_watchlist(config)
    Console().print(f"监控列表: {', '.join(wl) if wl else '(空)'}")


def cmd_dashboard(args: list[str]) -> None:
    setup_logging()
    from rich.console import Console
    from rich.table import Table
    from kenlet.data.binance import fetch_ohlcv
    from kenlet.analysis.indicators import compute_indicators
    from kenlet.risk.regime import detect_regime

    config = load_config()
    params = build_strategy_params(config)
    console = Console()
    table = Table(title="kenlet-v1 Dashboard")
    table.add_column("标的")
    table.add_column("价格", justify="right")
    table.add_column("状态")
    table.add_column("MA入场", justify="right")
    table.add_column("MA出场", justify="right")

    periods = sorted(set([params.ma_entry, params.ma_exit, params.ma_trend, 20, 38, 60]))
    for coin in get_watchlist(config):
        symbol = _normalize_symbol(coin)
        try:
            df = fetch_ohlcv(symbol, timeframe="1d", limit=200, config_override=config)
            ind = compute_indicators(df, periods, params.atr_period)
            regime_s = detect_regime(df, periods=periods)
            regime = str(regime_s.iloc[-1]) if not regime_s.empty else "?"
            price = float(df["close"].iloc[-1])
            ma_e = float(ind["mas"][params.ma_entry].dropna().iloc[-1])
            ma_x = float(ind["mas"][params.ma_exit].dropna().iloc[-1])
            table.add_row(symbol, f"${price:,.2f}", regime, f"${ma_e:,.2f}", f"${ma_x:,.2f}")
        except Exception as e:
            table.add_row(symbol, "ERR", str(e)[:20], "-", "-")
    console.print(table)


# ===========================================================================
# main
# ===========================================================================

def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        from kenlet.cli.display import print_help
        print_help()
        return

    command = sys.argv[1]
    args = sys.argv[2:]
    try:
        if command == "status":
            cmd_status(args)
        elif command == "backtest":
            cmd_backtest(args)
        elif command == "run":
            cmd_run(args)
        elif command == "watchlist":
            cmd_watchlist(args)
        elif command == "dashboard":
            cmd_dashboard(args)
        else:
            from rich.console import Console
            from kenlet.cli.display import print_help
            Console().print(f"[red]未知命令: {command}[/]")
            print_help()
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        logger.error("未处理错误 '%s': %s", command, e, exc_info=True)
        from rich.console import Console
        Console().print(f"[red]错误: {e}[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
