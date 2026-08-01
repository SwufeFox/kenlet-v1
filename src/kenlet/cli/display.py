"""
Rich CLI 输出。
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()


def print_backtest_results(result: dict[str, Any]) -> None:
    """打印回测结果。"""
    metrics = result.get("metrics")
    if metrics is None:
        console.print("[red]无回测结果[/]")
        return

    d = metrics.to_dict() if hasattr(metrics, "to_dict") else metrics

    # 摘要卡片
    ret = d.get("total_return_pct", 0)
    color = "green" if ret >= 0 else "red"
    console.print(Panel(
        f"[bold {color}]{ret:+.2f}%[/]  |  "
        f"胜率 {d.get('win_rate_pct', 0):.1f}%  |  "
        f"盈亏比 {d.get('profit_factor', 0)}  |  "
        f"最大回撤 {d.get('max_drawdown_pct', 0):.2f}%  |  "
        f"Sharpe {d.get('sharpe_ratio', 0):.2f}  |  "
        f"交易 {d.get('num_trades', 0)} 笔",
        title="kenlet-v1 回测结果",
        border_style="blue",
    ))

    # 详细指标表
    table = Table(title="绩效明细", show_header=True, header_style="bold cyan")
    table.add_column("指标", style="cyan")
    table.add_column("值", style="white")
    labels = {
        "initial_capital": "初始资金",
        "final_capital": "最终资金",
        "total_return_pct": "总收益率 %",
        "total_return_abs": "绝对收益",
        "win_rate_pct": "胜率 %",
        "profit_factor": "盈亏比",
        "max_drawdown_pct": "最大回撤 %",
        "sharpe_ratio": "夏普比率",
        "num_trades": "交易次数",
        "num_wins": "盈利次数",
        "num_losses": "亏损次数",
        "gross_profit": "总盈利",
        "gross_loss": "总亏损",
        "avg_trade_duration": "平均持仓",
        "avg_win_pct": "平均盈利 %",
        "avg_loss_pct": "平均亏损 %",
    }
    for key, label in labels.items():
        val = d.get(key, "N/A")
        table.add_row(label, str(val))
    console.print(table)

    # 最近交易
    trades = result.get("trades", [])
    if trades:
        ttable = Table(title=f"交易记录 (最近 {min(15, len(trades))} 笔)", show_header=True)
        ttable.add_column("#", style="dim")
        ttable.add_column("方向")
        ttable.add_column("入场")
        ttable.add_column("出场")
        ttable.add_column("PnL", justify="right")
        ttable.add_column("PnL%", justify="right")
        ttable.add_column("原因")
        for i, t in enumerate(trades[-15:], 1):
            pnl = t.get("pnl", 0)
            c = "green" if pnl >= 0 else "red"
            ttable.add_row(
                str(i),
                t.get("side", ""),
                f"{t.get('entry_price', 0):.2f}",
                f"{t.get('exit_price', 0):.2f}",
                f"[{c}]{pnl:+.2f}[/]",
                f"[{c}]{t.get('pnl_pct', 0):+.2f}%[/]",
                t.get("exit_reason", ""),
            )
        console.print(ttable)

    # LLM 决策
    decisions = result.get("llm_decisions", [])
    if decisions:
        console.print(f"\n[bold]Layer3 LLM 决策记录 ({len(decisions)} 次):[/]")
        for d_item in decisions[-5:]:
            console.print(
                f"  · {d_item.get('action')} "
                f"({d_item.get('confidence', 0):.0%}) — {d_item.get('reason')}"
            )


def print_status(symbol: str, summary: dict) -> None:
    table = Table(title=f"{symbol} 状态", show_header=False)
    for k, v in summary.items():
        table.add_row(str(k), str(v))
    console.print(table)


def print_help() -> None:
    table = Table(title="kenlet-v1 — CLI Commands", style="bold blue")
    table.add_column("命令", style="cyan")
    table.add_column("说明", style="green")
    table.add_row("status [SYMBOL]", "查看行情摘要")
    table.add_row("backtest <SYMBOL> <START> <END>", "统一引擎回测")
    table.add_row("  --timeframe 1d", "K 线周期")
    table.add_row("  --capital 10000", "初始资金")
    table.add_row("  --llm", "启用 Layer3 LLM 顾问")
    table.add_row("  --risk 0.02", "单笔风险比例")
    table.add_row("run [SYMBOL]", "实盘/纸交易循环")
    table.add_row("watchlist", "监控列表")
    table.add_row("dashboard", "多标的概览")
    console.print(table)
    console.print("\n[yellow]用法:[/] python -m kenlet <command> [options]")
