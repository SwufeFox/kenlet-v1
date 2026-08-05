# Repository Guidelines

## Project Overview

kenlet-v1 (`kenlet-trader`, import name `kenlet`) is a three-layer adaptive cryptocurrency trading system (BTC/ETH/SOL/XRP/BNB) written in Python >= 3.11. Its three core design commitments:

1. **Unified execution engine** — backtest and live trading share one decision path, `TradingEngine.on_bar()` (`src/kenlet/core/engine.py`), eliminating backtest/live logic drift.
2. **Layered adaptive architecture** — Layer 1 per-symbol engine + risk; Layer 2 portfolio manager (risk-based sizing, ranking/pruning, index algorithms); Layer 3 optional LLM fund manager with hard guardrails.
3. **Risk-first sizing** — position quantity = allowed loss / |entry − stop| (`risk/sizing.py`), never fixed USD stops.

Source comments, docstrings, and log messages are predominantly **Chinese**; identifiers are English. Match this in new code.

## Architecture & Data Flow

```
Layer 3  LLMAdvisor (llm/advisor.py)          — param overrides + portfolio commands, clamped
              │ apply() ↓
Layer 2  PortfolioManager (portfolio/manager.py) — multi-engine scheduling, sizing injection,
              │                                  ranking/prune/rotate, index gate (portfolio/index.py)
              │ on_bars() ↓
Layer 1  TradingEngine (core/engine.py)       — per-symbol, per-bar: exits → entries → equity
          └─ Strategy.on_bar() (strategy/) · Regime (risk/regime.py) · Stops (risk/stops.py) · Sizing (risk/sizing.py)
```

- **Bar pipeline**: `fetch_ohlcv()` (`data/binance.py`, CCXT with exponential-backoff retries) → `compute_indicators()` (`analysis/indicators.py`, vectorized pandas: SMA/EMA/ATR/RSI/ADX/Bollinger) + `detect_regime()` (`risk/regime.py`, bull/bear/ranging/choppy from MA20/60/180 alignment + ADX + volatility percentile) → `Bar.from_row()` → `PortfolioManager.on_bars()` → actions.
- **Backtest** (`core/runner.py` `BacktestRunner.run()`): precomputes indicators once per symbol, then per bar `i` slices indicators with `_slice_indicators()` **to prevent future leakage**. Capital split evenly across symbols (`initial_capital / n_symbols`).
- **Live** (`main.py` `cmd_run`): polls Binance every timeframe interval (15m=900s … 1d=86400s), recomputes indicators on a rolling window, feeds the last bar into the same `portfolio.on_bars()` path. Fully synchronous, `time.sleep` loop — **no asyncio**.
- **Layer 2 injection**: `PortfolioManager.inject_sizing()` monkey-patches `engine._compute_quantity` so portfolio-level sizing (equity-wide risk, index gate scale, volatility targeting, cash floor) overrides the engine default. `entry_gate`/`gate_risk_scale` on the engine carry the index risk-on/off state per bar and are reset after each bar.
- **Layer 3** (`llm/advisor.py`): triggers on every N bars, drawdown > 8%, or regime switch; calls an OpenAI-compatible API (**pure `urllib`, no openai package**, DeepSeek default) with JSON output tolerated for markdown fences; clamps overrides to `HARD_LIMITS`; confidence < 0.4 → downgraded to `hold`; any API failure → `hold`, never crashes the loop. `validate_commands()` filters hallucinated symbols and `record_decision()`/`feedback()` persist the ledger (`llm_ledger.json`, gitignored) — both wired into `runner.py` and `main.py cmd_run`. `--ensemble N` enables majority-vote sampling via `evaluate_ensemble()`.
- **Index algorithms** (`portfolio/index.py`): trend gate (price vs 200-DMA of index), relative strength vs index, volatility targeting (clamp(target_vol / realized_vol, floor, cap)). Index is `MarketIndex` — external (QQQ/TWI via `data/indices.py` or `macro/` CSVs) or synthesized equal-weight basket (`build_from_basket`) with automatic fallback.

## Key Directories

| Path | Purpose |
|---|---|
| `src/kenlet/core/` | `engine.py` (unified engine), `runner.py` (backtest/live runner), `params.py` (mutable `StrategyParams`), `models.py` (dataclasses), `metrics.py` (Sharpe/MaxDD/PF) |
| `src/kenlet/strategy/` | `base.py` (Strategy ABC), `ma_crossover.py` (default strategy) |

> Entry semantics of `ma_crossover` (deliberate, verified against README §4 results):
> **price crossing the fast MA (`ma_entry`) opens positions; the slow MA (`ma_exit`) is exit-only**
> (`trend_reversal` in the engine). It is NOT a golden-cross (fast-MA-crosses-slow-MA) strategy —
> README's backtest tables were produced by the price-cross semantics; do not "fix" this back.
| `src/kenlet/portfolio/` | `manager.py` (Layer 2), `index.py` (trend gate / RS / vol targeting) |
| `src/kenlet/risk/` | `regime.py`, `stops.py` (ATR stops), `sizing.py` (risk-based size) |
| `src/kenlet/llm/` | `advisor.py` (LLMDecision, LLMAdvisor), `prompts.py` |
| `src/kenlet/analysis/` | `indicators.py`, `macro.py` (QQQ/DXY/VIX risk proxy, optional) |
| `src/kenlet/data/` | `binance.py` (CCXT), `indices.py` (index data sources) |
| `src/kenlet/cli/` | `display.py` (rich tables) |
| `src/kenlet/` | `config.py` (YAML+env config), `main.py` (CLI entry) |
| `tests/` | `test_kenlet_smoke.py` + cached market-data fixtures (`*.pkl`, `chart_data.json`) |
| `macro/`, `data/indices/`, `logs/` | gitignored runtime data caches and logs |

## Development Commands

```bash
# Install (src layout; editable)
pip install -e .            # pyproject (setuptools) — canonical
pip install -r requirements.txt   # alternative; includes ta + pytest-mock not in pyproject

# CLI (entry: python -m kenlet; console script `kenlet` after install)
python -m kenlet status BTC                 # market summary
python -m kenlet backtest BTC 2024-01-01 2024-12-31 [--llm] [--timeframe 1h|4h|1d] [--capital 10000] [--risk 0.02] [--limit 1000]
python -m kenlet run BTC --timeframe 1h --llm   # live/paper loop
python -m kenlet watchlist / dashboard

# Tests
pytest                      # src 已由 pyproject `pythonpath` 配置, 裸跑即可

# Windows wrappers (hardcode/assume the repo root)
run_cli.bat <cmd...>        # → python -m kenlet %*
keep_alive.bat              # disables sleep + loops status/dashboard every 5 min
```

No lint, format, or type-check tooling is configured — no ruff/black/mypy configs in `pyproject.toml`. Do not invent one.

## Code Conventions & Common Patterns

- **Python 3.11+**, `from __future__ import annotations` at the top of every module; full type hints on public signatures.
- **Dataclasses, not pydantic**: all models (`Bar`, `Signal`, `Position`, `TradeRecord`, `EngineAction`) and configs (`StrategyParams`, `PortfolioConfig`, `LLMDecision`) are `@dataclass` with `field(default_factory=…)` for mutable defaults.
- **Mutable params as the adaptation mechanism**: `StrategyParams` (`core/params.py`) is a single shared, mutable dataclass — the single source of truth for strategy/risk knobs. Runtime overrides go through `params.override(dict)` (validates types, rejects non-positive numerics except `min_cross_pct`, caps `risk_per_trade`/`llm_override_strength` at 1.0, returns applied keys). Layer 3 reaches engines via `engine.apply_llm_override()` and `PortfolioManager.apply_llm_override()`. Use `clone()` before mutating per-symbol copies (see `BacktestRunner.run`).
- **Dependency injection via constructor + documented monkey-patch**: strategies injected via `attach_strategy()` (one stateless strategy instance shared by all engines); portfolio sizing injected by replacing `engine._compute_quantity` (marked `# type: ignore[method-assign]`). Keep Layer 2/3 control paths exactly this way — do not add setters that bypass them.
- **Strategy interface**: implement `Strategy.on_bar(bar, indicators, params, positions) -> Signal | None`. Strategies only emit signals — never touch cash/positions.
- **Position sizing** (`risk/sizing.py`): risk-based `q = E·r / |p_e − p_sl|` is the default (`use_risk_sizing: true`); `fixed_fraction_size` (`position_size: 0.25`) only when `use_risk_sizing: false`. Risk base is **equity** (`engine.capital` / `portfolio.total_equity`), never cash. Stop deviation is floored at `MIN_DEVIATION_PCT` (0.5% of price) in all three paths (`risk_based_size`, `engine._compute_quantity`, `portfolio.compute_position_quantity`). Scaled entries (`num_entries > 1`, config `strategy.num_entries` / `--entries`) split **one** risk budget across batches (`q / num_entries` per batch). Portfolio path layers macro/vol/gate risk multipliers on top and caps by cash (`min_cash_ratio`).
- **Fail-soft error handling**: external failures degrade, never raise through the trading loop. Patterns: retry with backoff (`BinanceData`), local cache fallback (`_load_cached_market_data` → `tests/market_data.pkl`), neutral/None fallbacks (`MacroState`, `MarketIndex`), `hold` fallback for LLM. Guard clauses return `0.0`/`None` instead of raising. CLI catches and prints rich-formatted `[red]…[/]` errors.
- **Logging**: `logger = logging.getLogger(__name__)` at module top; `setup_logging()` once per process from CLI (idempotent). Console + rotating file `logs/kenlet-v1.log`, format `%(asctime)s | %(levelname)-8s | %(name)s | %(message)s`. Log messages in Chinese, tagged `[Layer2]`/`[Layer3]` where applicable.
- **No future leakage**: indicator/regime series are sliced to the current bar index before reaching `on_bar`; never index `.iloc[-1]` on full history inside engine/strategy code.
- **Naming**: snake_case functions/modules, CamelCase classes; symbols normalized to `XXX/USDT` (`_normalize_symbol`); exit reasons are short snake strings (`stop_loss`, `take_profit`, `trend_reversal`, `prune_score_…`, `rotate_out_weakest_…`).
- **CLI output** via `rich.Console()` with `[cyan]`/`[yellow]`/`[red]`/`[green]` markup; heavy modules imported lazily inside command functions.

## Important Files

- `config.yaml` — all defaults (strategy MA periods, ATR multipliers 3.0/6.0, risk_per_trade 0.02, index gate `gate_risk_scale: 0.33`, vol target 0.25, LLM triggers); env vars override secrets (see `.env.example`: `BINANCE_*`, `OPENAI_BASE_URL/API_KEY/MODEL`, `KENLET_LOG_LEVEL`).
- `src/kenlet/config.py` — `load_config()` (YAML + env override), `build_strategy_params()`, `build_portfolio_config()`, `get_watchlist()`.
- `src/kenlet/core/engine.py` — `TradingEngine.on_bar()`: exit checks → entry-window continuation (scaled entries) → signal → equity. Conservative exit pricing (stop/tp hit at exactly that price).
- `src/kenlet/core/runner.py` — `BacktestRunner.run()` orchestration incl. LLM evaluation loop; `create_runner_from_config()`.
- `src/kenlet/llm/advisor.py` — `HARD_LIMITS` dict (edit here to change LLM parameter bounds), `should_evaluate()`/`evaluate()`/`apply()`.
- `src/kenlet/main.py` — hand-rolled arg parsing per command (no argparse); `--llm` flag, `--timeframe/--capital/--risk/--limit/--ensemble` for backtest.
- `tests/test_kenlet_smoke.py` — de-facto contract spec (see Testing).

## Runtime/Tooling Preferences

- **Python >= 3.11** (developed on 3.13). Windows is the primary dev OS (`.bat` wrappers), but `src/` is cross-platform and uses no OS-specific code.
- **Package manager**: `pip` (setuptools/pyproject). `poetry.lock` exists but is legacy — `pyproject.toml` uses `setuptools.build_meta`; keep deps in sync between `pyproject.toml` and `requirements.txt`.
- **Key runtime deps**: `ccxt>=4.0` (market data), `pandas`, `numpy`, `pyyaml`, `python-dotenv`, `rich` (CLI). LLM calls use stdlib `urllib` — no `openai` dependency.
- **Network reality**: backtest needs Binance reachability (or falls back to cached `tests/market_data.pkl`, daily only); live loop needs Binance; LLM layer needs `OPENAI_API_KEY` and costs money — default `llm.enabled: false`.
- **Known stale references** (do not copy): `REFERENCE.txt` is the legacy kenzon CLI manual — most options it documents (`--ma-entry/--stop-pct/--simple/--filter/--entries`) no longer exist; strategy params live in `config.yaml` (`strategy:`/`risk:` sections). `python -m kenlet help` is the authoritative command list. `run_trader.py` needs the package installed (or `PYTHONPATH=src`).

## Testing & QA

- **Framework**: pytest only. Config in `pyproject.toml` (`testpaths = ["tests"]`, `python_files = ["test_*.py"]`). No CI, no coverage thresholds, no lint gates.
- **Run**: `pytest` from repo root.
- **Structure**: single file `tests/test_kenlet_smoke.py` covering: indicators key contract (`mas/atr/rsi/adx`), regime classification + `regime_allows_trade`, risk sizing formula, ATR stop computation, engine open/close, single-symbol backtest, backtest against real cached data, portfolio sizing injection (approx `200/3`), force-close, ranking, and LLM parsing (plain + markdown-fenced JSON), hard-limit clamping, and disabled→`hold`.
- **Fixtures**: synthetic DataFrames from `np.random.default_rng(seed)` (deterministic); real data from `tests/market_data.pkl` (dict keyed by base symbol) for offline backtests. Prefer seeded synthetic data for new tests; avoid network calls.
- **Conventions**: pure-function tests with `pytest.approx` for floats; keep tests deterministic and fast (no sleeps, no network). New behavior (e.g., a new guardrail or exit reason) belongs in this file.
