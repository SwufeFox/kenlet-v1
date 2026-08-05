"""
统一参数系统 — StrategyParams。

所有策略/风控参数都放在这个可变 dataclass 里。
关键特性: 参数可以在运行中被修改 (规则引擎或 LLM 顾问)，引擎在下一次
on_bar 时立即生效。这是"自适应"的基础。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class StrategyParams:
    """可变的策略参数集合 (回测与实盘共用)。

    任何字段都可以在运行中被覆盖，例如:
        engine.params.ma_entry = 20   # 规则调整
        engine.apply_llm_override({...})  # LLM 调整
    """

    # ── 策略参数 ──────────────────────────────────────────────
    ma_entry: int = 38          # 入场均线周期
    ma_exit: int = 60           # 出场均线周期
    ma_trend: int = 180         # 趋势过滤均线周期
    min_cross_pct: float = 0.0  # 穿越最小幅度过滤 (%)

    # ── 风控参数 ──────────────────────────────────────────────
    atr_period: int = 14        # ATR 计算周期
    atr_sl_mult: float = 3.0    # 止损 = 入场价 ∓ ATR × 该倍数
    atr_tp_mult: float = 6.0    # 止盈 = 入场价 ± ATR × 该倍数
    risk_per_trade: float = 0.02  # 每笔交易承担的风险占资金比例 (2%)
    max_positions: int = 3      # 最大同时持仓数

    # ── 过滤器 ────────────────────────────────────────────────
    regime_filter: bool = True  # 是否按市场状态过滤交易
    trend_filter: bool = True   # 是否要求价格在趋势均线上方 (多)

    # ── 资金 ──────────────────────────────────────────────────
    initial_capital: float = 10_000.0
    position_size: float = 0.25   # 固定比例仓位 (use_risk_sizing=False 时用)
    use_risk_sizing: bool = True  # 使用风险百分比仓位而非固定比例

    # ── 分批建仓 (可选) ────────────────────────────────────────
    num_entries: int = 1          # 分批次数 (1 = 一次建仓)
    entry_offset_bars: int = 1    # 每批间隔 bar 数

    # ── LLM 顾问 ──────────────────────────────────────────────
    llm_enabled: bool = False
    llm_check_interval: int = 50  # 每 N 根 bar 触发一次 LLM 评估
    llm_override_strength: float = 0.5  # LLM 参数调整幅度上限比例

    # ── 运行模式 ──────────────────────────────────────────────
    mode: str = "backtest"        # "backtest" | "paper" | "live"
    start_date: str | None = None
    end_date: str | None = None

    def clone(self) -> "StrategyParams":
        """返回一份深拷贝，用于对比/回滚。"""
        import copy
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        return asdict(self)

    def override(self, updates: dict) -> list[str]:
        """批量覆盖参数。返回实际被修改的字段名列表。

        - 未知字段被忽略
        - 数值/类型不合法时回退为旧值
        """
        applied: list[str] = []
        for key, value in updates.items():
            if not hasattr(self, key):
                continue
            old = getattr(self, key)
            try:
                if isinstance(old, bool):
                    if isinstance(value, str):
                        new_val = value.strip().lower() in ("true", "1", "yes", "on")
                    else:
                        new_val = bool(value)
                elif isinstance(old, int):
                    new_val = int(value)
                elif isinstance(old, float):
                    new_val = float(value)
                else:
                    new_val = value
                # 数值合理性护栏 (bool 是 int 子类, 必须排除)
                if isinstance(new_val, (int, float)) and not isinstance(new_val, bool):
                    if new_val <= 0 and key not in ("min_cross_pct",):
                        continue
                    if key in ("risk_per_trade", "llm_override_strength") and new_val > 1.0:
                        new_val = min(new_val, 1.0)
                setattr(self, key, new_val)
                applied.append(key)
            except (TypeError, ValueError):
                continue
        return applied
