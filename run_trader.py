"""
kenlet-v1 实盘入口 — 转发给统一 CLI。

用法: python run_trader.py [SYMBOL] [--timeframe 1h] [--llm]
"""
import sys
from kenlet.main import cmd_run

if __name__ == "__main__":
    cmd_run(sys.argv[1:])
