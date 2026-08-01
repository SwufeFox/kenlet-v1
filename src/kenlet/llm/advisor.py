"""
Layer 3 — LLM Fund Manager (kenlet-v1-etf manager)。

控制 Layer2 (组合) 和 Layer1 (策略参数)。
触发条件:
  - 周期性 (每 N 根 bar)
  - 市场状态切换
  - 组合回撤超过阈值
  - 外部手动调用

设计原则:
  - 可选: llm_enabled=False 时完全跳过
  - 容错: API 失败只打日志，不中断交易循环
  - 有界: 参数调整有硬护栏，防止 LLM 给出极端值
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kenlet.llm.prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


@dataclass
class LLMDecision:
    """LLM 的结构化决策。"""

    action: str = "hold"                  # hold | adjust | rebalance | flatten
    reason: str = ""
    confidence: float = 0.0
    param_overrides: dict = field(default_factory=dict)
    portfolio_commands: list[dict] = field(default_factory=list)
    raw: str = ""

    @classmethod
    def from_json(cls, text: str) -> "LLMDecision":
        """解析 LLM 输出的 JSON。容错处理 markdown 代码块。"""
        cleaned = text.strip()
        # 去掉可能的 ```json ... ``` 包裹
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # 尝试提取第一个 { ... }
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    return cls(action="hold", reason="parse_failed", raw=text)
            else:
                return cls(action="hold", reason="parse_failed", raw=text)

        return cls(
            action=str(data.get("action", "hold")),
            reason=str(data.get("reason", "")),
            confidence=float(data.get("confidence", 0.0)),
            param_overrides=dict(data.get("param_overrides") or {}),
            portfolio_commands=list(data.get("portfolio_commands") or []),
            raw=text,
        )


class LLMAdvisor:
    """kenlet-v1-etf 的 Fund Manager。

    通过 OpenAI 兼容 API (默认 DeepSeek) 做决策。
    """

    # 硬护栏 — 参数调整的绝对边界
    HARD_LIMITS = {
        "risk_per_trade": (0.005, 0.05),
        "default_risk_per_trade": (0.005, 0.05),
        "atr_sl_mult": (1.5, 8.0),
        "atr_tp_mult": (2.0, 20.0),
        "ma_entry": (5, 100),
        "ma_exit": (10, 200),
        "ma_trend": (20, 500),
        "max_positions": (1, 10),
        "min_cross_pct": (0.0, 5.0),
        "prune_threshold": (-1.0, 0.5),
    }

    def __init__(
        self,
        enabled: bool = False,
        check_interval: int = 50,
        drawdown_trigger: float = 0.08,   # 回撤超 8% 触发
        min_confidence: float = 0.4,      # 低于此置信度忽略决策
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.check_interval = check_interval
        self.drawdown_trigger = drawdown_trigger
        self.min_confidence = min_confidence

        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
        ).rstrip("/")
        # 兼容带 /chat/completions 的 base_url
        if self.base_url.endswith("/chat/completions"):
            self.base_url = self.base_url[: -len("/chat/completions")]

        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", "deepseek-chat")

        self._bar_counter = 0
        self._peak_equity = 0.0
        self._last_regime: str = "unknown"
        self._decision_log: list[dict] = []

        # 决策台账: 记录历史决策与事后效果，供后续决策参考
        self.ledger_path = Path("llm_ledger.json")
        self._ledger: list[dict] = []
        self._load_ledger()

    # ------------------------------------------------------------------
    # 触发判定
    # ------------------------------------------------------------------
    def should_evaluate(
        self,
        equity: float,
        regime: str | None = None,
        force: bool = False,
    ) -> bool:
        """判断是否需要调用 LLM。"""
        if not self.enabled:
            return False
        if force:
            return True

        self._bar_counter += 1

        # 周期性
        if self._bar_counter % self.check_interval == 0:
            return True

        # 回撤触发
        if equity > self._peak_equity:
            self._peak_equity = equity
        if self._peak_equity > 0:
            dd = (self._peak_equity - equity) / self._peak_equity
            if dd >= self.drawdown_trigger:
                logger.info("[Layer3] 回撤触发: %.1f%%", dd * 100)
                return True

        # 市场状态切换
        if regime and regime != self._last_regime and self._last_regime != "unknown":
            logger.info("[Layer3] 市场状态切换: %s → %s", self._last_regime, regime)
            self._last_regime = regime
            return True
        if regime:
            self._last_regime = regime

        return False

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------
    def evaluate(self, context: dict) -> LLMDecision:
        """调用 LLM 并返回决策。失败时返回 hold。"""
        if not self.enabled:
            return LLMDecision(action="hold", reason="llm_disabled")
        if not self.api_key:
            logger.warning("[Layer3] 未配置 OPENAI_API_KEY，跳过")
            return LLMDecision(action="hold", reason="no_api_key")

        user_prompt = build_user_prompt(context)
        try:
            raw = self._call_api(user_prompt)
            decision = LLMDecision.from_json(raw)
            # 护栏
            decision.param_overrides = self._clamp_params(decision.param_overrides)
            if decision.confidence < self.min_confidence and decision.action != "hold":
                logger.info(
                    "[Layer3] 置信度 %.2f < %.2f，降级为 hold",
                    decision.confidence, self.min_confidence,
                )
                decision.action = "hold"
                decision.reason += " (low_confidence)"
            self._decision_log.append({
                "action": decision.action,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "overrides": decision.param_overrides,
            })
            logger.info(
                "[Layer3] 决策: %s (%.0f%%) — %s",
                decision.action, decision.confidence * 100, decision.reason,
            )
            return decision
        except Exception as e:
            logger.warning("[Layer3] LLM 调用失败: %s", e)
            return LLMDecision(action="hold", reason=f"error: {e}")

    def apply(self, decision: LLMDecision, portfolio) -> list[str]:
        """把决策应用到 PortfolioManager (Layer2) 和各引擎 (Layer1)。

        Returns
        -------
        list[str] — 实际执行的操作描述
        """
        applied: list[str] = []

        if decision.action == "hold":
            return applied

        # 1. 参数覆盖 → Layer2 + Layer1
        if decision.param_overrides:
            keys = portfolio.apply_llm_override(decision.param_overrides)
            if keys:
                applied.append(f"params: {', '.join(keys)}")

        # 2. 组合指令
        for cmd in decision.portfolio_commands:
            cmd_name = cmd.get("cmd", "")
            if cmd_name == "force_close":
                symbol = cmd.get("symbol", "")
                if symbol:
                    portfolio.force_close(symbol, reason=f"llm:{decision.reason}")
                    applied.append(f"close {symbol}")
            elif cmd_name == "force_close_all":
                portfolio.force_close_all(reason=f"llm:{decision.reason}")
                applied.append("flatten_all")
            elif cmd_name == "rebalance_to_cash":
                ratio = float(cmd.get("ratio", 0.5))
                portfolio.rebalance_to_cash(ratio=ratio, reason=f"llm:{decision.reason}")
                applied.append(f"rebalance_{ratio:.0%}")

        # 3. 顶级 action 快捷指令
        if decision.action == "flatten":
            portfolio.force_close_all(reason=f"llm_flatten:{decision.reason}")
            applied.append("flatten_all")
        elif decision.action == "rebalance" and not any(
            c.get("cmd") == "rebalance_to_cash" for c in decision.portfolio_commands
        ):
            portfolio.rebalance_to_cash(ratio=0.5, reason=f"llm_rebalance:{decision.reason}")
            applied.append("rebalance_50%")

        return applied

    # ------------------------------------------------------------------
    # API 调用 (纯 urllib，不强制 openai 包)
    # ------------------------------------------------------------------
    def _call_api(self, user_prompt: str, timeout: int = 30) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 800,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {err_body[:200]}") from e

    # ------------------------------------------------------------------
    # 护栏
    # ------------------------------------------------------------------
    def _clamp_params(self, overrides: dict) -> dict:
        """把 LLM 输出的参数限制在硬护栏内。"""
        clamped = {}
        for key, value in overrides.items():
            if key not in self.HARD_LIMITS:
                # 布尔/字符串参数直接放行
                if isinstance(value, (bool, str)):
                    clamped[key] = value
                continue
            lo, hi = self.HARD_LIMITS[key]
            try:
                v = float(value)
                v = max(lo, min(hi, v))
                # 整数参数还原
                if key in ("ma_entry", "ma_exit", "ma_trend", "max_positions"):
                    v = int(round(v))
                clamped[key] = v
            except (TypeError, ValueError):
                continue
        return clamped

    @property
    def decision_history(self) -> list[dict]:
        return list(self._decision_log)

    # ------------------------------------------------------------------
    # 决策台账 (Decision Ledger) — 可靠空间的关键
    # ------------------------------------------------------------------
    def _load_ledger(self) -> None:
        """加载历史决策台账。"""
        try:
            if self.ledger_path.exists():
                with open(self.ledger_path, encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._ledger = data[-50:]  # 只保留最近 50 条
        except Exception as e:
            logger.warning("[Layer3] 台账加载失败: %s", e)
            self._ledger = []

    def _save_ledger(self) -> None:
        try:
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                json.dump(self._ledger[-100:], f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning("[Layer3] 台账保存失败: %s", e)

    def record_decision(self, decision: LLMDecision, context: dict) -> None:
        """记录一条决策 (含当时的市场/组合快照)。"""
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "action": decision.action,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "overrides": decision.param_overrides,
            "commands": decision.portfolio_commands,
            "context": {
                "regime": context.get("market", {}).get("regime", "?"),
                "equity": context.get("total_equity"),
                "return_pct": context.get("return_pct"),
            },
            "outcome": None,  # 事后回填
        }
        self._ledger.append(entry)
        self._save_ledger()

    def feedback(self, realized_return_pct: float, window_bars: int = 0) -> None:
        """回填最近未评估决策的事后效果。

        用法: 回测/实盘每跑一段时间后调用一次，
        让 LLM 在下次决策时能看到"上次调参后的实际收益"。
        """
        if not self._ledger:
            return
        last = self._ledger[-1]
        if last.get("outcome") is None:
            last["outcome"] = {
                "return_pct_after": round(realized_return_pct, 3),
                "window_bars": window_bars,
            }
            self._save_ledger()

    def _ledger_summary(self) -> list[dict]:
        """供 prompt 使用的台账摘要 (最近 8 条)。"""
        return [
            {
                "time": e["time"],
                "action": e["action"],
                "reason": e["reason"][:80],
                "outcome": e.get("outcome"),
            }
            for e in self._ledger[-8:]
        ]

    # ------------------------------------------------------------------
    # Self-Consistency 采样投票 — 提高决策可靠性
    # ------------------------------------------------------------------
    def evaluate_ensemble(
        self,
        context: dict,
        n_samples: int = 3,
    ) -> LLMDecision:
        """多次采样 LLM 决策，按 action 投票取多数，参数取中位数。

        理由: 单次 LLM 输出有随机性；多数投票显著降低幻觉/离群输出风险。
        代价: n 倍 API 调用 (n_samples 默认 3，可调 1 关闭)。

        Returns
        -------
        LLMDecision
            若 n_samples <= 1 或采样失败，退化为单次 evaluate()。
        """
        if not self.enabled or n_samples <= 1:
            return self.evaluate(context)

        decisions: list[LLMDecision] = []
        for _ in range(n_samples):
            d = self.evaluate(context)
            if d.action != "hold":
                decisions.append(d)
        if not decisions:
            return LLMDecision(action="hold", reason="ensemble_all_hold")

        # action 投票
        vote = Counter(d.action for d in decisions)
        winner = vote.most_common(1)[0][0]

        # 参数取中位数 (对数值字段)
        merged_overrides: dict[str, Any] = {}
        num_fields: dict[str, list[float]] = {}
        for d in decisions:
            for k, v in d.param_overrides.items():
                if isinstance(v, (int, float)):
                    num_fields.setdefault(k, []).append(float(v))
                else:
                    merged_overrides[k] = v
        import statistics
        for k, vals in num_fields.items():
            merged_overrides[k] = statistics.median(vals)

        # 命令取出现次数最多的
        cmd_counter = Counter(
            (c.get("cmd"), c.get("symbol", ""))
            for d in decisions for c in d.portfolio_commands
        )
        top_cmd = cmd_counter.most_common(1)[0][0] if cmd_counter else None
        commands = []
        if top_cmd:
            commands = [{"cmd": top_cmd[0], "symbol": top_cmd[1]}] if top_cmd[0] != "force_close_all" else [{"cmd": "force_close_all"}]

        win_d = max(
            (d for d in decisions if d.action == winner),
            key=lambda d: d.confidence,
        )
        return LLMDecision(
            action=winner,
            reason=f"[ensemble×{n_samples}] {win_d.reason}",
            confidence=round(len([d for d in decisions if d.action == winner]) / n_samples, 2),
            param_overrides=merged_overrides,
            portfolio_commands=commands,
            raw="\n---\n".join(d.raw for d in decisions),
        )

    # ------------------------------------------------------------------
    # 可靠校验: 只保留组合里真实存在的标的
    # ------------------------------------------------------------------
    def validate_commands(self, decision: LLMDecision, portfolio) -> LLMDecision:
        """过滤掉指向不存在标的的指令。"""
        valid_symbols = set(portfolio.engines.keys())
        kept = []
        for c in decision.portfolio_commands:
            if c.get("cmd") == "force_close_all":
                kept.append(c)
                continue
            sym = c.get("symbol", "")
            if sym in valid_symbols:
                kept.append(c)
        decision.portfolio_commands = kept
        return decision
