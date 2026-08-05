# kenlet-v1：三层自适应加密资产交易系统的设计与实现

**Design and Implementation of kenlet-v1: A Three-Layer Adaptive Cryptocurrency Trading System**

---

## 摘要 (Abstract)

本文提出并实现 kenlet-v1——一个面向加密资产市场的三层自适应交易系统。针对现有交易系统中普遍存在的"回测—实盘逻辑漂移"与"参数僵化"两大缺陷，本文从方法论层面给出三项核心贡献：(i) **统一执行引擎** (Unified Execution Engine)：回测与实盘共用同一条 bar 级决策路径 `on_bar()`，从工程上杜绝回测失真；(ii) **分层自适应架构**：Layer 1 交易引擎、Layer 2 投资组合层、Layer 3 LLM 决策层，三层职责分离、逐层控制，使系统具备根据市场状态动态调整的能力；(iii) **指数驱动的组合管理**：将经典量化中的指数趋势门控 (Faber 200-DMA 择时)、相对强弱动量 (Jegadeesh–Titman) 与波动率目标 (Volatility Targeting) 引入 Layer 2，在加密市场语境下实现风险预算的自动化管理。实验基于 2023-10 至 2026-07 三个主要标的 (BTC/ETH/SOL) 的日线数据。结果显示，在保留全部交易机会的前提下，指数增强配置 (趋势门控 + 相对强弱 + 波动率目标) 将组合最大回撤由 21.0% 压缩至 6.4%，盈亏比由 1.23 提升至 2.10，Calmar 比率由 3.87 升至 4.83，验证了方法的有效性。

**关键词**: 量化交易；自适应系统；回测-实盘一致性；指数择时；相对强弱；波动率目标；LLM 决策

---

## 1 引言 (Introduction)

### 1.1 研究背景

加密资产市场以高波动、强趋势与周期性震荡并存著称。统计上，BTC 等主流资产的年化波动率长期高于传统权益类资产，且存在显著的正偏度与肥尾特征。这为趋势跟踪策略提供了理论土壤，也带来了严峻的风控挑战。

### 1.2 现有系统的缺陷

对既有系统 (前代 kenzon) 的代码审查揭示了三类系统性缺陷：

1. **回测-实盘逻辑漂移**：回测引擎、共享策略模块与实盘循环各自独立实现策略逻辑，均线周期、止损机制、入场规则互不一致。回测结果因此对实盘行为不具有预测效力——这是工程层面的"测量工具污染了测量对象"问题。

2. **参数僵化 (Parameter Rigidity)**：策略参数 (均线周期、止损幅度) 编译期写死。市场状态切换 (牛→震荡→熊) 时系统无法自适应，导致震荡市中频繁开仓、反复止损。

3. **风控尺度失当**：止损以绝对美元金额设定 (如 $10 SL / $110 TP)，在不同价位区间下实质风险敞口相差悬殊；仓位采用固定比例，与单笔亏损的可控性脱钩。

### 1.3 本文贡献

针对上述缺陷，本文提出 kenlet-v1 系统，贡献如下：

- **C1. 统一执行引擎**：单一 `TradingEngine.on_bar()` 作为回测与实盘共用的决策原语，策略、风控、仓位在同一代码路径中执行。
- **C2. 三层自适应架构**：Layer 1 交易引擎 (策略+风控原语)、Layer 2 投资组合层 (仓位公式+优胜劣汰+指数算法)、Layer 3 LLM 决策层 (参数与组合的动态管理)，实现"宏观决策—组合配置—单笔执行"的层级控制。
- **C3. 风险本位仓位公式**：将仓位定义为 `允许亏损额 / 止损偏差` 的闭式解，使单笔风险敞口与组合权益严格成比例。
- **C4. 指数驱动的组合管理**：在 Layer 2 引入三类经典量化算法并适配加密市场，显著改善组合风险收益特征。

---

## 2 相关工作 (Related Work)

**趋势跟踪 (Trend Following)**：Faber (2007) 证明 S&P 500 的 200 日均线择时在 1973–2006 年间显著优于买入持有，且大幅降低回撤。本文将其推广为组合层的"指数趋势门控"。

**动量效应 (Momentum)**:Jegadeesh & Titman (1993) 报告了 3–12 个月价格动量效应；Moskowitz 等 (2012) 提出"时间序列动量" (自身动量)。本文采用标的相对指数的横截面超额动量作为持仓排序因子之一。

**波动率目标 (Volatility Targeting)**:Moreira & Muir (2017) 证明波动率管理策略可提升风险调整后收益。本文以其作为组合仓位缩放机制。

**LLM 在金融中的应用**：大语言模型已被用于新闻情绪分析 (Lopez-Lira & Tang, 2023) 与零样本交易信号生成。本文的 Layer 3 采用"参数与组合指令的约束式生成"，并通过硬护栏限制其行动边界——LLM 作为**决策者而非执行者**，所有输出经结构化校验后方可落地。

---

## 3 系统架构 (System Architecture)

### 3.1 总体设计

系统采用严格的层级控制结构，上层对下层只做"参数下发与指令下发"，下层对上层只暴露"状态观测接口"，数据单向流动：

```
┌───────────────────────────────────────────────────────────┐
│ Layer 3 · LLM Fund Manager (kenlet-v1-etf)                │
│   决策: 参数覆盖 / 组合指令 / 风险偏好                     │
│   护栏: 硬边界 + 置信度阈值 + 指令校验                     │
└──────────────────────────┬────────────────────────────────┘
                           │ 控制 (override)
┌──────────────────────────▼────────────────────────────────┐
│ Layer 2 · PortfolioManager                                │
│   仓位公式: q = L / |p_e − p_sl|                          │
│   优胜劣汰: 排名 → 淘汰 / 旋转                             │
│   指数算法: 趋势门控 · 相对强弱 · 波动率目标               │
└──────────────────────────┬────────────────────────────────┘
                           │ 调度 (on_bars)
┌──────────────────────────▼────────────────────────────────┐
│ Layer 1 · TradingEngine (×N 标的)                          │
│   统一 on_bar() 路径: 出场检查 → 入场信号 → 仓位计算       │
│   ATR 止损 · 风险仓位 · 市场状态过滤                       │
└───────────────────────────────────────────────────────────┘
```

### 3.2 Layer 1：统一交易引擎

**设计原则**：回测与实盘共享同一决策原语。回测循环将历史 bar 逐个喂入 `on_bar()`；实盘循环将新收盘 bar 喂入同一方法。策略、风控、仓位逻辑零分叉。

**入场与出场规则**（默认策略 `strategy/ma_crossover.py`，本节为权威语义描述，代码实现与之一致）：

- **入场**：收盘价上穿/下穿快均线 `ma_entry`（config 默认 38；实验基准用 20）→ 做多/做空。入场触发是**价格穿越快均线**，而非快慢均线交叉——`ma_exit` 不参与入场判定。可选过滤：趋势过滤（做多要求价格在 `ma_trend` 上方）与市场状态过滤（`regime_filter`：bull 只做多、bear 只做空、choppy 不开仓）。
- **出场**：由引擎统一处理——价格跌破/升破慢均线 `ma_exit`（默认 60）触发 `trend_reversal`，或触及 ATR 止损/止盈（`atr_sl_mult`/`atr_tp_mult`，默认 3.0×/6.0×），或 Layer 2 淘汰/旋转/强制平仓。

**市场状态 (Regime) 检测**：综合三个因子判定 `R(t) ∈ {bull, bear, ranging, choppy}`：

- **方向因子**：短/中/长三条均线 (MA20/MA60/MA180) 的排列关系；
- **趋势强度因子**：ADX 指数 (Wilder, 1978)，阈值 20；
- **波动率因子**：ATR 百分比的历史分位，高波动 (>85 分位) 直接判为 `choppy`。

**ATR 止损**：止损价定义为

$$p_{sl} = p_e \mp \lambda_{sl} \cdot \text{ATR}(14), \qquad p_{tp} = p_e \pm \lambda_{tp} \cdot \text{ATR}(14)$$

其中 $\lambda_{sl}, \lambda_{tp}$ 为可调倍数 (默认 3.0/6.0)。ATR 随波动率自适应，解决了绝对美元止损在价位漂移下的尺度失效问题。

### 3.3 Layer 2：投资组合层

#### 3.3.1 风险本位仓位公式

**定理 (仓位公式)**：设允许亏损额 $L = E \cdot r$ ($E$ 为组合权益，$r$ 为单笔风险比例)，入场价 $p_e$，止损价 $p_{sl}$，则使单笔最大亏损恰为 $L$ 的持仓数量为

$$q^* = \frac{L}{|p_e - p_{sl}|} = \frac{E \cdot r}{|p_e - p_{sl}|}.$$

*证明*：多头单笔亏损 $\Pi = q(p_{sl} - p_e) = -q|p_e - p_{sl}|$；令 $|\Pi| = L$ 即得 $q^*$。□

该公式将"风险预算"与"止损距离"解耦：止损越近 (波动小)，仓位越大；止损越远 (波动大)，仓位越小——天然实现波动率反向加权。

#### 3.3.2 优胜劣汰 (Portfolio Ranking)

对每个持仓定义复合评分：

$$S_i = 0.5 \cdot M_i + 0.5 \cdot T_i$$

其中 $M_i$ 为归一化动量 (未实现盈亏/10%)，$T_i$ 为与出场均线的对齐度。评分低于阈值 $\theta_{prune}$ 的持仓被淘汰；满仓时新信号触发"旋转"——关闭最弱持仓为新持仓腾出风险预算。

#### 3.3.3 指数算法 (Index Algorithms)

**指数趋势门控 (Trend Gate)**：设指数收盘 $P_t^{idx}$，其 $N$-日均线 $\overline{P}_t^{idx}$ (默认 $N=200$)，则

$$\text{gate}(t) = \begin{cases} \text{risk\_on} & P_t^{idx} > \overline{P}_t^{idx} \\ \text{risk\_off} & P_t^{idx} < \overline{P}_t^{idx} \end{cases}$$

门控以**风险预算乘数** $\kappa_g \in [0,1]$ 作用于单笔风险比例 (而非直接缩放仓位)：

$$r_{	ext{eff}} = r \cdot \kappa_g, \qquad \kappa_g = egin{cases} 1 & 	ext{risk\_on} \ 	ext{gate\_risk\_scale} & 	ext{risk\_off} \end{cases}$$

再代入仓位公式 $q = E \cdot r_{	ext{eff}} / |p_e - p_{sl}|$。$\kappa_g = 0$ 对应经典 Faber 模式的完全禁仓；$\kappa_g = 0.33$ 表示风险预算收缩至 1/3——交易机会全部保留，但系统性下跌期每笔风险敞口显著降低。出场始终不受门控影响。该机制在加密市场扮演"系统性风险开关"角色，对应 Faber 择时思想在资产配置层面的推广。

**相对强弱 (Relative Strength)**：标的 $i$ 在窗口 $W$ 内相对指数的超额动量

$$\text{RS}_i(t) = \underbrace{\frac{P_i(t)}{P_i(t-W)} - 1}_{\text{标的动量}} - \underbrace{\left(\frac{P^{idx}(t)}{P^{idx}(t-W)} - 1\right)}_{\text{指数动量}} - r_f \cdot \frac{W}{365}.$$

$\text{RS}_i > 0$ 表示跑赢指数，作为持仓评分的加分项，将"与谁为伍"纳入排序。

**波动率目标 (Volatility Targeting)**：以指数已实现波动率 $\hat{\sigma}$ 作为组合波动率的代理，仓位缩放系数

$$\kappa = \text{clamp}\left(\frac{\sigma_{target}}{\hat{\sigma}}, \kappa_{floor}, \kappa_{cap}\right)$$

使组合风险预算在低波动期充分使用、高波动期自动收缩。

**指数数据来源**：支持外部指数 (QQQ/TWI/SPY CSV 注入) 与内置合成指数 (组合内标的价格等权构建) 两种模式，无外部数据时自动回退。

### 3.4 Layer 3：LLM 决策层

**角色定位**：Layer 3 扮演 "kenlet-v1-etf" 的基金经理，控制 Layer 2 (组合配置) 与 Layer 1 (策略参数)，但不直接接触订单执行。

**触发条件**：周期性评估 (每 $N$ 根 bar)、组合回撤超阈值 (默认 8%)、市场状态切换。

**决策输出 Schema**（结构化 JSON）：
```json
{
  "action": "hold | adjust | rebalance | flatten",
  "reason": "中文决策理由",
  "confidence": 0.0,
  "param_overrides": {"risk_per_trade": 0.02, ...},
  "portfolio_commands": [{"cmd": "force_close", "symbol": "..."}]
}
```

**可靠性机制（决策层的"可信空间"）**：
1. **硬护栏 (Hard Limits)**：所有参数覆盖经 `clamp` 限制在预设区间 (如 $r \in [0.5\%, 5\%]$，$\lambda_{sl} \geq 1.5$)，LLM 无法输出物理上不合理的参数；
2. **置信度门限**：置信度低于阈值的决策降级为 `hold`；
3. **指令校验 (Command Validation)**：组合指令指向的标的必须真实存在，防止幻觉标的；
4. **集成投票 (Ensemble Voting)**：可选多次采样，按 action 多数投票、数值参数取中位数，抑制单次输出随机性；
5. **容错降级**：API 调用失败仅记录日志并返回 `hold`，交易循环不受影响；
6. **决策台账 (Ledger)**：决策记录落盘，支持事后回放与反馈学习。

---

## 4 实验 (Experiments)

### 4.1 实验设置

- **数据**：BTC/ETH/SOL 日线 OHLCV，2023-10-16 至 2026-07-11，各 1000 根；
- **初始资金**：$10{,}000$（多标的时按标的数均分，杜绝虚拟杠杆）；
- **基准参数**：MA20/MA60/MA180，ATR(14) 止损 3.0×/6.0×，单笔风险 2%，最大持仓 3；
- **对照配置**：基线 (无指数算法) vs 指数增强 (门控+相对强弱+波动率目标，指数 MA=100)。

### 4.2 单标的结果

| MA组合 | 交易数 | 收益% | 盈亏比 | MaxDD% | Sharpe |
|---|---|---|---|---|---|
| 20/60/180 | 18 | −4.6 | 2.33 | 13.1 | −0.31 |
| 38/60/180 | 13 | −4.2 | 0.68 | 8.2 | −0.60 |
| 10/30/90 | 26 | +0.5 | 2.30 | 13.3 | +0.06 |
| 20/100/200 | 20 | +1.7 | 1.40 | 14.0 | +0.14 |

单标的趋势策略收益为负，源于 2023–2026 区间内 BTC 日线趋势段短、震荡段长；此为趋势策略在区间依赖下的固有特性，也是引入 Layer 2 组合分散与指数择时的动机。

### 4.3 组合结果（指数算法 A/B）

| 配置 | 交易数 | 收益% | 盈亏比 | MaxDD% | Sharpe | Calmar |
|---|---|---|---|---|---|---|
| 基线（无指数） | 56 | +27.1 | 1.23 | 21.0 | 0.86 | 3.87 |
| 全禁 ($\kappa_g=0$) | 16 | +18.9 | 3.33 | 8.1 | 0.96 | 6.96 |
| **1/3 预算 ($\kappa_g=0.33$ + RS + Vol)** | 56 | +10.4 | **2.10** | **6.4** | 0.97 | **4.83** |

> 注 1：Calmar 为年化简化口径——1000 根日线 bar 近似 3 年，`Calmar ≈ 3 × 总收益 / 最大回撤`（按未舍入的原始数据计算；标准口径 总收益/最大回撤 对应 1.29 / 2.33 / 1.63，结论方向不变）。
> 注 2：“全禁”行为仅门控配置（不启用相对强弱/波动率目标缩放）。三行结果均可用仓库代码与 `tests/market_data.pkl` 数据精确复现（交易数/收益/最大回撤/夏普/盈亏比全数一致）。

**解读**：完全禁仓 ($\kappa_g=0$) 虽将最大回撤压至 8.1%，但交易机会由 56 减至 16，收益损失可观。**风险预算乘数** $\kappa_g=0.33$ 在保留全部 56 笔交易机会的同时，将风险预算收缩至 1/3，使组合最大回撤从 21.0% 收敛至 6.4%、盈亏比由 1.23 升至 2.10。收益下降 (27.1%→10.4%) 是风险预算收缩的代价，但以 Calmar 比率衡量的风险调整后收益 (3.87→4.83) 与回撤控制显著改善，符合波动率管理文献的预期。

### 4.4 LLM 决策层验证

- 结构化 JSON 解析（含 markdown 包裹容错）✓
- 硬护栏：`risk_per_trade=0.5→0.05`、`atr_sl_mult=10→8.0`、`ma_entry=3→5` ✓
- 置信度门限：低置信决策降级 `hold` ✓
- 指令校验：幻觉标的指令被过滤 ✓
- 容错：API 失败 → `hold`，交易循环不中断 ✓

---

## 5 讨论与局限性 (Discussion & Limitations)

**关于"指数"的选择**：QQQ (纳斯达克 100) 作为全球科技/成长风险偏好的代理，与加密市场在 2020 年后相关性显著 (相关 >0.6)，其 200-DMA 择时是成熟的领先信号；TWI (贸易加权美元指数) 与 DXY 刻画流动性环境。本文通过 `MarketIndex` 抽象层统一处理，数据注入即用，无需修改策略代码。

**局限性**：(i) 回测未计交易手续费与滑点，实际收益需打折；(ii) 加密市场 24×7 交易，日线 ATR 未捕捉日内极端波动；(iii) LLM 层依赖外部 API，存在延迟与费用约束，且护栏边界外行为不可控；(iv) 单标的区间收益为负说明趋势策略对参数区间敏感，仍需 Layer 3 的在线调参来缓解；(v) 合成指数在标的间相关性升高时近似失效（等价于等权篮子本身）。

---

## 6 结论与未来工作 (Conclusion & Future Work)

本文实现的三层自适应架构验证了"统一引擎 + 组合指数管理 + LLM 决策"的组合有效性：工程上消除回测-实盘漂移，方法上通过指数算法显著改善风险收益特征，架构上为 LLM 决策提供了有护栏的"可信空间"。

**未来工作**：(1) 引入交易成本与滑点模型；(2) 多周期融合 (4h/1d 联合信号)；(3) LLM 在线调参的 walk-forward 评估；(4) 将相对强弱与风险平价 (Risk Parity) 结合，替代等权合成指数；(5) 实盘 paper-trading 验证。

---

## 参考文献 (References)

1. Faber, M. T. (2007). A Quantitative Approach to Tactical Asset Allocation. *Journal of Wealth Management*, 9(4), 69–79.
2. Jegadeesh, N., & Titman, S. (1993). Returns to Buying Winners and Selling Losers. *Journal of Finance*, 48(1), 65–91.
3. Moreira, A., & Muir, T. (2017). Volatility-Managed Portfolios. *Journal of Finance*, 72(4), 1611–1644.
4. Moskowitz, T. J., Ooi, Y. H., & Pedersen, L. H. (2012). Time Series Momentum. *Journal of Financial Economics*, 104(2), 228–250.
5. Wilder, J. W. (1978). *New Concepts in Technical Trading Systems*. Trend Research.
6. Lopez-Lira, A., & Tang, Y. (2023). Can ChatGPT Forecast Stock Price Movements? *SSRN Working Paper*.

---

## 附录 A：快速开始

```bash
# 安装
pip install -e .            # 或 poetry install

# 配置 (可选)
cp .env.example .env        # Binance / LLM keys

# 回测 (统一引擎; 网络不可用时自动回退本地缓存)
python -m kenlet backtest BTC 2024-01-01 2024-12-31
python -m kenlet backtest BTC 2023-10-16 2026-07-11 --llm   # 启用 LLM

# 实盘/纸交易 (同一引擎)
python -m kenlet run BTC --timeframe 1h --llm

# 其他
python -m kenlet status BTC
python -m kenlet dashboard
python -m kenlet watchlist
```

## 附录 B：环境变量

| 变量 | 说明 |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` | Binance 凭证 |
| `BINANCE_TESTNET` | `true` 启用测试网 |
| `OPENAI_BASE_URL` | LLM API 地址 (默认 DeepSeek) |
| `OPENAI_API_KEY` | LLM Key |
| `OPENAI_MODEL` | 模型名 |
| `KENLET_LOG_LEVEL` | 日志级别 |

## 附录 C：项目结构

```
src/kenlet/
├── core/           engine.py (统一引擎) · runner.py · params.py · models.py · metrics.py
├── strategy/       base.py (ABC) · ma_crossover.py (默认策略)
├── portfolio/      manager.py (组合层) · index.py (指数算法)
├── llm/            advisor.py (LLM 决策) · prompts.py
├── risk/           regime.py (市场状态) · stops.py (ATR止损) · sizing.py (风险仓位)
├── analysis/       indicators.py (MA/ATR/RSI/ADX) · macro.py (QQQ/DXY/VIX)
├── data/           binance.py (CCXT)
├── cli/            display.py (Rich 输出)
├── config.py       YAML + env 配置
└── main.py         CLI 入口
```
