"""
Binance 数据层 — 通过 CCXT 拉取 OHLCV。

统一入口，回测与实盘共用。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import ccxt
import pandas as pd

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class BinanceData:
    """CCXT Binance 数据源。"""

    def __init__(self, config_override: dict[str, Any] | None = None) -> None:
        self._config = config_override
        self._exchange: ccxt.Exchange | None = None

    def fetch_ohlcv(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        limit: int = 500,
        since: int | None = None,
    ) -> pd.DataFrame:
        """拉取 OHLCV，返回带 timestamp 的 DataFrame。"""
        exchange = self._get_exchange()
        retry_cfg = self._get_retry_config()
        max_retries = retry_cfg.get("max_retries", 3)
        backoff = retry_cfg.get("backoff_factor", 2.0)
        max_per = 1000

        if limit > max_per:
            return self._fetch_paginated(symbol, timeframe, limit, max_per, max_retries, backoff)

        last_err: Exception | None = None
        for attempt in range(1, max_retries + 2):
            try:
                logger.info("Fetching %d %s candles for %s (attempt %d)", limit, timeframe, symbol, attempt)
                kwargs: dict[str, Any] = {"limit": limit}
                if since is not None:
                    kwargs["since"] = since
                raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, **kwargs)
                df = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                return df
            except (ccxt.RateLimitExceeded, ccxt.NetworkError) as e:
                last_err = e
                wait = backoff ** (attempt - 1)
                logger.warning("Retry in %.1fs: %s", wait, e)
                time.sleep(wait)
            except Exception as e:
                logger.error("Unexpected OHLCV error: %s", e, exc_info=True)
                raise
        raise ccxt.NetworkError(f"Failed after {max_retries + 1} attempts") from last_err

    def _fetch_paginated(
        self, symbol: str, timeframe: str, total: int,
        chunk: int, max_retries: int, backoff: float,
    ) -> pd.DataFrame:
        exchange = self._get_exchange()
        chunks: list[pd.DataFrame] = []
        since = None
        remaining = total
        fetched = 0

        while remaining > 0:
            this = min(remaining, chunk)
            last_err = None
            for attempt in range(1, max_retries + 2):
                try:
                    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=this)
                    break
                except (ccxt.RateLimitExceeded, ccxt.NetworkError) as e:
                    last_err = e
                    time.sleep(backoff ** (attempt - 1))
            else:
                raise last_err  # type: ignore[misc]

            if not raw:
                break
            df_c = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
            df_c["timestamp"] = pd.to_datetime(df_c["timestamp"], unit="ms", utc=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df_c[col] = pd.to_numeric(df_c[col], errors="coerce")
            chunks.append(df_c)
            fetched += len(df_c)
            remaining = total - fetched
            tf_ms = exchange.parse_timeframe(timeframe) * 1000
            oldest = int(df_c["timestamp"].iloc[0].timestamp() * 1000)
            since = oldest - chunk * tf_ms
            if len(df_c) < this:
                break

        if not chunks:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.concat(chunks, ignore_index=True)
        df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        return df

    def _get_exchange(self) -> ccxt.Exchange:
        if self._exchange is not None:
            return self._exchange
        cfg = self._load_binance_config()
        kwargs: dict[str, Any] = {"enableRateLimit": cfg.get("enableRateLimit", True)}
        api_key = os.environ.get("BINANCE_API_KEY") or cfg.get("apiKey")
        secret = os.environ.get("BINANCE_SECRET_KEY") or os.environ.get("BINANCE_SECRET") or cfg.get("secret")
        if api_key:
            kwargs["apiKey"] = api_key
        if secret:
            kwargs["secret"] = secret

        use_futures = os.environ.get("BINANCE_FUTURES", "").lower() in ("true", "1", "yes")
        options = cfg.get("options", {})
        if use_futures:
            kwargs["options"] = {**options, "defaultType": "future"}
            self._exchange = ccxt.binanceusdm(kwargs)
        else:
            if options:
                kwargs["options"] = options
            self._exchange = ccxt.binance(kwargs)

        if os.environ.get("BINANCE_TESTNET", "").lower() in ("true", "1", "yes"):
            self._exchange.setSandboxMode(True)
            logger.info("Testnet mode enabled")
        return self._exchange

    def _load_binance_config(self) -> dict[str, Any]:
        if self._config is not None:
            return self._config.get("binance", self._config)
        try:
            from kenlet.config import load_config
            return load_config().get("binance", {})
        except Exception:
            return {}

    def _get_retry_config(self) -> dict[str, Any]:
        return self._load_binance_config().get("retry", {})


def fetch_ohlcv(
    symbol: str = "BTC/USDT",
    timeframe: str = "1d",
    limit: int = 500,
    config_override: dict | None = None,
) -> pd.DataFrame:
    """便捷函数 — 兼容旧 CLI 调用方式。"""
    return BinanceData(config_override).fetch_ohlcv(symbol, timeframe, limit)
