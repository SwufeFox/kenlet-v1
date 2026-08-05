# Kenzon Trader — 快速接入指南

## 1️⃣ 拿 Testnet Key

打开 https://testnet.binance.vision/
- 右上角 Login → GitHub 登录
- 点 "Create HMAC Key"
- 复制 API Key 和 Secret Key

## 2️⃣ 配置

编辑 `.env`（没有就复制 `.env.example`）：

```env
BINANCE_API_KEY=你拿到的testnet_key
BINANCE_SECRET=你拿到的testnet_secret
BINANCE_TESTNET=true
BINANCE_FUTURES=true
KENLET_LOG_LEVEL=INFO
```

各参数：

| 变量 | 作用 | 值 |
|------|------|----|
| `BINANCE_TESTNET` | 启用模拟盘 | `true` |
| `BINANCE_FUTURES` | 启用合约模式（带杠杆） | `true` |
| `BINANCE_LEVERAGE` | 杠杆倍数 | `1~125` |

## 3️⃣ 不让笔记本休眠

双击 `keep_alive.bat`，或者：

```cmd
:: 彻底关闭休眠
powercfg /change standby-timeout-ac 0
powercfg /hibernate off

:: 合盖也不睡（关闭显示器但继续运行）
powercfg /change lid-action-ac 0
```

## 4️⃣ 跑起来

```bash
# 查看 BTC 技术状态
python -m kenlet status BTC

# 全量分析报告
python -m kenlet dashboard

# 回测（本地数据，无需联网）
python -m kenlet backtest BTC 2025-01-01 2026-07-12
```

## 5️⃣ 激进模式（4h周期）

```bash
python -m kenlet backtest BTC 2025-01-01 2026-07-12 --timeframe 4h
```

均线周期/止损等参数在 `config.yaml` 的 `strategy:` / `risk:` 段调整（旧版 `--ma-entry` 等 CLI 选项已移除；本系统不下真实订单，杠杆配置不生效）。

## 6️⃣ 白嫖服务器

**Oracle Cloud 永久免费 ARM**
1. 注册 https://www.oracle.com/cloud/free/
2. 开一台 Ubuntu 22.04 ARM (4核24G)
3. SSH 上去装 Python + Git
4. 把项目 git pull 上去
5. 后台跑 `nohup python -m kenlet status BTC &`（或 `while true; do python -m kenlet status BTC; sleep 300; done`）

**或 — 旧安卓手机装 Termux**
```bash
pkg install python git
pip install ccxt numpy pandas pyyaml
git clone ...
cd kenlet-v1
python -m kenlet dashboard
```

## 7️⃣ 常见问题

**Q: 连不上 testnet?**
A: 确认 API Key 是从 https://testnet.binance.vision/ 拿的，不是主站

**Q: 不想用杠杆?**
A: `.env` 里删掉 `BINANCE_FUTURES=true` 就行

**Q: 想切回实盘?**
A: `.env` 里注释掉 testnet 的 key，取消注释主站 key，设 `BINANCE_TESTNET=false`
