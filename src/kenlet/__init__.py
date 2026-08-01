"""
kenlet — 自适应加密交易系统 (Adaptive Crypto Trading System)

核心理念: 回测与实盘使用同一个引擎 (TradingEngine)，杜绝"回测一套、
实盘一套"的逻辑漂移。参数可在运行时被规则或 LLM 动态调整，
使系统随市场状态 (regime) 自适应变化。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
