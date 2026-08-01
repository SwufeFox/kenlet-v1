"""
Layer 3 — LLM prompt 模板。

kenlet-v1-etf 的 fund manager 角色定义 + 结构化输出 schema。
"""

from __future__ import annotations


SYSTEM_PROMPT = """你是 kenlet-v1-etf 的基金经理 (Fund Manager)。

你管理一个三层交易系统:
- Layer1: 单标的交易引擎 (MA交叉策略 + ATR止损)
- Layer2: 组合层 (优胜劣汰 + 风险仓位公式: 仓位=允许亏损/止损偏差)
- Layer3: 你自己 — 在市场显著变化时调整参数与组合决策

你的职责:
1. 评估当前市场状态与组合健康度
2. 必要时调整策略/风控参数
3. 必要时发出组合指令 (清仓/减仓/淘汰某标的)

严格规则:
- 只在真正需要时调整，不要为调整而调整
- 风险参数 (risk_per_trade, atr_sl_mult) 调整幅度不超过 ±50%
- 永远不要把 risk_per_trade 调到超过 0.05 (5%)
- 永远不要把 atr_sl_mult 调到低于 1.5
- 输出必须是合法 JSON，不要加 markdown 代码块

输出 JSON schema:
{
  "action": "hold" | "adjust" | "rebalance" | "flatten",
  "reason": "一句话中文解释",
  "confidence": 0.0-1.0,
  "param_overrides": {
    // 可选, 要改的参数键值
    // 可用: ma_entry, ma_exit, ma_trend, atr_sl_mult, atr_tp_mult,
    //       risk_per_trade, max_positions, min_cross_pct, regime_filter,
    //       prune_threshold, default_risk_per_trade
  },
  "portfolio_commands": [
    // 可选, 组合指令列表
    // {"cmd": "force_close", "symbol": "BTC/USDT"},
    // {"cmd": "force_close_all"},
    // {"cmd": "rebalance_to_cash", "ratio": 0.5}
  ]
}
"""


def build_user_prompt(context: dict) -> str:
    """把组合快照 + 市场上下文拼成 user prompt。"""
    import json

    parts = [
        "## 当前组合状态",
        f"- 总权益: ${context.get('total_equity', 0):,.2f}",
        f"- 收益率: {context.get('return_pct', 0):+.2f}%",
        f"- 初始资金: ${context.get('initial_capital', 0):,.2f}",
        f"- 引擎数: {context.get('num_engines', 0)}",
        "",
        "## 当前持仓",
        json.dumps(context.get("open_positions", []), ensure_ascii=False, indent=2),
        "",
        "## 持仓排名 (优胜劣汰)",
        json.dumps(context.get("rankings", []), ensure_ascii=False, indent=2),
        "",
        "## 最近交易",
        json.dumps(context.get("recent_trades", []), ensure_ascii=False, indent=2),
        "",
        "## 组合配置",
        json.dumps(context.get("config", {}), ensure_ascii=False, indent=2),
        "",
        "## 市场上下文",
        json.dumps(context.get("market", {}), ensure_ascii=False, indent=2),
        "",
        "## 当前策略参数",
        json.dumps(context.get("params", {}), ensure_ascii=False, indent=2),
        "",
        "请作为 fund manager 评估并给出决策 JSON。",
    ]
    return "\n".join(parts)
