"""
自动调参器 — 让 LLM 帮你找最优参数组合
用法: python optimize.py
"""
import sys, itertools, json, time
sys.path.insert(0, 'src')

# ─── 要搜索的参数空间 ───
SEARCH_SPACE = {
    "timeframe":     ["4h", "1d"],
    "ma_entry":      [14, 20, 38],
    "ma_exit":       [30, 40, 60],
    "stop_pct":      [6, 8, 10],
    "tp_pct":        [40, 80, 120],
    "min_cross":     [0.0, 0.2],
    "filter_regime": [True],
}

# ─── 评分权重 ───
# total_return: +40%, sharpe: +25%, max_dd: -20%, win_rate: +10%, trades: +5%
WEIGHTS = {"total_return": 0.40, "sharpe": 0.25, "max_dd": -0.20, "win_rate": 0.10, "trades": 0.05}

def score(result):
    """给回测结果打分 (0-100)"""
    tr = result.get("total_return", 0)
    sp = result.get("sharpe", 0)
    dd = result.get("max_drawdown", 100)
    wr = result.get("win_rate", 0)
    n = result.get("num_trades", 0)

    # 归一化
    tr_score = max(0, min(1, (tr + 20) / 50))      # -20%→0, +30%→1
    sp_score = max(0, min(1, (sp + 1) / 3))         # -1→0, +2→1
    dd_score = max(0, min(1, 1 - dd / 30))          # 30%→0, 0%→1
    wr_score = max(0, min(1, wr / 60))              # 0%→0, 60%→1
    n_score  = max(0, min(1, n / 30))               # 0→0, 30→1

    total = (tr_score * WEIGHTS["total_return"] +
             sp_score * WEIGHTS["sharpe"] +
             dd_score * WEIGHTS["max_dd"] +
             wr_score * WEIGHTS["win_rate"] +
             n_score  * WEIGHTS["trades"])
    return round(total * 100, 1), {"tr": round(tr,2), "sp": sp, "dd": round(dd,2), "wr": round(wr,1), "n": n}

# ─── 生成参数组合 ───
keys = list(SEARCH_SPACE.keys())
combos = list(itertools.product(*[SEARCH_SPACE[k] for k in keys]))
print(f"\n参数空间: {len(combos)} 种组合\n")

# ─── 逐一回测并评分 ───
results = []
for idx, values in enumerate(combos):
    params = dict(zip(keys, values))
    tf = params["timeframe"]
    ma_e = params["ma_entry"]
    ma_x = params["ma_exit"]
    sl = params["stop_pct"]
    tp = params["tp_pct"]
    mc = params["min_cross"]
    flt = "--filter" if params["filter_regime"] else ""

    # 根据 timeframe 调整 limit
    if tf == "15m": limit = 5000
    elif tf == "1h": limit = 3000
    elif tf == "4h": limit = 2000
    else: limit = 1000

    cmd = (f"backtest BTC 2024-06-01 2026-07-12 "
           f"--timeframe {tf} --ma-entry {ma_e} --ma-exit {ma_x} "
           f"--stop-pct {sl} --tp-pct {tp} "
           f"--simple {flt} --min-cross {mc} --limit {limit}")

    print(f"[{idx+1}/{len(combos)}] {cmd}")

    import subprocess
    import os as _os
    env = dict(_os.environ)
    env["PYTHONPATH"] = "src" + _os.pathsep + env.get("PYTHONPATH", "")
    start = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "kenlet"] + cmd.split(),
        capture_output=True, text=True, timeout=180, env=env,
    )
    elapsed = time.time() - start

    out = proc.stdout
    # 解析结果
    def extract(label):
        for line in out.split("\n"):
            if label in line:
                parts = line.split()
                for p in parts:
                    try:
                        v = p.replace("+","").replace("%","").replace("$","")
                        return float(v)
                    except: pass
        return 0.0

    tr = extract("Total Return:")
    sp = extract("Sharpe Ratio:")
    dd = extract("Max Drawdown:")
    wr = extract("Win Rate:")
    n = extract("Trades:")

    result = {"total_return": tr, "sharpe": sp, "max_drawdown": dd, "win_rate": wr, "num_trades": n}
    s, details = score(result)

    results.append((s, params, details, elapsed))
    print(f"  → 得分: {s} | 收益: {tr:+.1f}% 夏普: {sp:.2f} 回撤: {dd:.1f}% 胜率: {wr:.1f}% 交易: {int(n)} ({elapsed:.0f}s)")

# ─── 排名 ───
results.sort(key=lambda x: x[0], reverse=True)

print("\n" + "=" * 80)
print("  🏆 最优参数 TOP 10")
print("=" * 80)
print(f"  {'排名':<4} {'得分':<6} {'周期':<6} {'MA入场':<8} {'MA出场':<8} {'止损':<6} {'止盈':<6} {'过滤':<6} {'收益':<8} {'夏普':<6} {'回撤':<6} {'胜率':<6}")
print("  " + "-" * 80)
for i, (s, params, det, t) in enumerate(results[:10]):
    tf = params["timeframe"]
    me = params["ma_entry"]
    mx = params["ma_exit"]
    sl = params["stop_pct"]
    tp = params["tp_pct"]
    fl = "Y" if params["filter_regime"] else "N"
    print(f"  {i+1:<4} {s:<6} {tf:<6} MA{me:<5} MA{mx:<5} {sl:<5}% {tp:<5}% {fl:<6} {det['tr']:>+6.1f}% {det['sp']:<6.2f} {det['dd']:<6.1f}% {det['wr']:<6.1f}%")

print("\n以上参数可以直接复制到 run_cli backtest 里用")
