"""Async broker facade. Live order submission is disabled unless explicitly enabled."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logging.Formatter.converter = time.gmtime
logger = logging.getLogger(__name__)
Side = Literal["BUY", "SELL"]


def is_crypto_symbol(symbol: str) -> bool:
    """Return True if symbol represents a cryptocurrency trading pair or coin."""
    s = str(symbol).strip().upper()
    return "/" in s or s.endswith("USD") or s.endswith("USDT") or s.endswith("BTC") or s in {
        "BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "LTC", "BCH", "UNI", "AAVE", "SHIB"
    }


def normalize_crypto_symbol(symbol: str) -> str:
    """Normalize crypto symbols to Alpaca trading pair format (e.g. BTCUSD -> BTC/USD)."""
    s = str(symbol).strip().upper()
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "USD", "BTC"):
        if s.endswith(quote) and len(s) > len(quote):
            base = s[:-len(quote)]
            return f"{base}/{quote}"
    return s


class BrokerError(RuntimeError):
    """Broker operation failed or returned an unusable response."""


class TradingDisabledError(BrokerError):
    """Order submission is locked until TRADING_ENABLED=true is configured."""


class UnsupportedProtectionError(BrokerError):
    """The adapter cannot guarantee the requested attached protective orders."""


@dataclass(frozen=True)
class BrokerSettings:
    environment: str = "PAPER_ALPACA"
    trading_enabled: bool = False
    request_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "BrokerSettings":
        return cls(
            environment=os.getenv("BROKER_ENV", "PAPER_ALPACA").upper(),
            trading_enabled=os.getenv("TRADING_ENABLED", "false").lower() == "true",
            request_timeout_seconds=float(os.getenv("BROKER_TIMEOUT_SECONDS", "10")),
        )


class AbstractBroker(ABC):
    def __init__(self, settings: BrokerSettings | None = None) -> None:
        self.settings = settings or BrokerSettings.from_env()

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def get_latest_price(self, symbol: str) -> float: ...

    @abstractmethod
    async def stream_ticks(self, symbols: list[str]):
        """Yield normalized {symbol, timestamp, price, volume, source} tick dictionaries for multiple symbols."""
        ...

    @abstractmethod
    async def place_order(
        self, symbol: str, side: Side, qty: float, order_type: str = "market",
        limit_price: float | None = None, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    async def get_account_equity(self) -> float: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    async def get_recent_candles_batch(self, symbols: list[str], timeframe_minutes: int = 1,
                                        limit: int = 200) -> dict[str, list[dict[str, Any]]]:
        """Default implementation: sequential single-symbol calls."""
        result = {}
        for sym in symbols:
            result[sym] = await self.get_recent_candles(sym, timeframe_minutes, limit)
        return result

    def _require_trading_enabled(self) -> None:
        if not self.settings.trading_enabled:
            raise TradingDisabledError(
                "Order submission is locked. Set TRADING_ENABLED=true only after "
                "validating broker, account, risk limits, and paper operation."
            )
        if self.settings.environment.startswith("LIVE_") and os.getenv("LIVE_TRADING_ENABLED", "false").lower() != "true":
            raise TradingDisabledError("Live order submission is locked; LIVE_TRADING_ENABLED must be explicitly set to true.")

    def _credentials(self) -> tuple[str | None, str | None]:
        if self.settings.environment == "LIVE_ALPACA":
            return os.getenv("ALPACA_LIVE_API_KEY"), os.getenv("ALPACA_LIVE_SECRET_KEY")
        return os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")


class AlpacaBroker(AbstractBroker):
    """Alpaca adapter; paper mode is the default and uses alpaca-py lazily."""

    def __init__(self, settings: BrokerSettings | None = None) -> None:
        super().__init__(settings)
        self._trading = None
        self._stock_data = None
        self._crypto_data = None
        self._data = None
        self._news = None

    async def connect(self) -> None:
        key, secret = self._credentials()
        if not key or not secret:
            required = "ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY" if self.settings.environment == "LIVE_ALPACA" else "ALPACA_API_KEY and ALPACA_SECRET_KEY"
            raise BrokerError(f"Set {required} in the environment.")
        if self.settings.environment not in {"PAPER_ALPACA", "LIVE_ALPACA"}:
            raise BrokerError("Alpaca requires PAPER_ALPACA or LIVE_ALPACA.")
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
            from alpaca.data.historical.news import NewsClient
        except ImportError as exc:
            raise BrokerError("Install the optional 'alpaca-py' dependency.") from exc
        is_paper = self.settings.environment == "PAPER_ALPACA"
        trade_url = "https://paper-api.alpaca.markets" if is_paper else "https://api.alpaca.markets"
        self._trading = TradingClient(key, secret, paper=is_paper, url_override=trade_url)
        self._stock_data = StockHistoricalDataClient(key, secret)
        self._crypto_data = CryptoHistoricalDataClient(key, secret)
        self._data = self._stock_data  # backward compatibility
        self._news = NewsClient(key, secret)
        await asyncio.wait_for(asyncio.to_thread(self._trading.get_account), self.settings.request_timeout_seconds)
        logger.info("Connected to Alpaca %s account.", "paper" if is_paper else "live")

    async def get_latest_price(self, symbol: str) -> float:
        if is_crypto_symbol(symbol):
            if self._crypto_data is None:
                raise BrokerError("Call connect() before requesting market data.")
            try:
                from alpaca.data.requests import CryptoLatestTradeRequest
                from alpaca.data.enums import CryptoFeed
                pair = normalize_crypto_symbol(symbol)
                response = await asyncio.wait_for(
                    asyncio.to_thread(self._crypto_data.get_crypto_latest_trade, CryptoLatestTradeRequest(symbol_or_symbols=pair), CryptoFeed.US),
                    self.settings.request_timeout_seconds,
                )
                trade_obj = response.get(pair) if hasattr(response, "get") else getattr(response, pair, None)
                if trade_obj is None and hasattr(response, "__getitem__"):
                    trade_obj = response[pair]
                return float(trade_obj.price)
            except Exception as exc:
                raise BrokerError(f"Alpaca crypto latest-price request failed for {symbol}: {exc}") from exc
        else:
            if self._stock_data is None:
                raise BrokerError("Call connect() before requesting market data.")
            try:
                from alpaca.data.requests import StockLatestTradeRequest
                response = await asyncio.wait_for(
                    asyncio.to_thread(self._stock_data.get_stock_latest_trade, StockLatestTradeRequest(symbol_or_symbols=symbol)),
                    self.settings.request_timeout_seconds,
                )
                return float(response[symbol].price)
            except Exception as exc:
                raise BrokerError(f"Alpaca latest-price request failed for {symbol}: {exc}") from exc

    async def get_account_equity(self) -> float:
        if self._trading is None:
            raise BrokerError("Call connect() before requesting account.")
        try:
            account = await asyncio.wait_for(
                asyncio.to_thread(self._trading.get_account),
                self.settings.request_timeout_seconds
            )
            return float(account.equity)
        except Exception as exc:
            raise BrokerError(f"Alpaca account equity request failed: {exc}") from exc

    async def disconnect(self) -> None:
        logger.info("Alpaca broker disconnected")

    async def get_recent_candles(self, symbol: str, timeframe_minutes: int = 1,
                                 limit: int = 200) -> list[dict[str, Any]]:
        """Fetch closed intraday bars for indicator warm-up; streaming remains authoritative."""
        batch = await self.get_recent_candles_batch([symbol], timeframe_minutes, limit)
        return batch.get(symbol, [])

    async def get_recent_candles_batch(self, symbols: list[str], timeframe_minutes: int = 1,
                                        limit: int = 200) -> dict[str, list[dict[str, Any]]]:
        """Fetch closed intraday bars for multiple symbols in a single request."""
        if self._stock_data is None and self._crypto_data is None:
            raise BrokerError("Call connect() before requesting market data.")
        if timeframe_minutes <= 0 or limit <= 0:
            raise ValueError("timeframe_minutes and limit must be positive")
        try:
            import pandas as pd
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            now = datetime.now(timezone.utc)
            result: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}

            crypto_symbols = [s for s in symbols if is_crypto_symbol(s)]
            stock_symbols = [s for s in symbols if not is_crypto_symbol(s)]

            if crypto_symbols and self._crypto_data is not None:
                from alpaca.data.requests import CryptoBarsRequest
                async def _fetch_crypto_bars(sym: str) -> tuple[str, list[dict[str, Any]]]:
                    pair = normalize_crypto_symbol(sym)
                    lookback_mins = limit * timeframe_minutes
                    req = CryptoBarsRequest(
                        symbol_or_symbols=pair,
                        timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
                        start=now - timedelta(minutes=lookback_mins),
                        end=now,
                        limit=max(limit, 1000)
                    )
                    res = await asyncio.wait_for(asyncio.to_thread(self._crypto_data.get_crypto_bars, req),
                                                 self.settings.request_timeout_seconds)
                    bars = []
                    frame = res.df.reset_index()
                    for record in frame.to_dict("records"):
                        stamp = pd.Timestamp(record["timestamp"]).to_pydatetime()
                        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
                        if stamp + timedelta(minutes=timeframe_minutes) > now:
                            continue
                        bars.append({
                            "timestamp": stamp, "open": float(record["open"]),
                            "high": float(record["high"]), "low": float(record["low"]),
                            "close": float(record["close"]), "volume": float(record.get("volume", 0) or 0)
                        })
                    return sym, bars[-limit:]

                crypto_results = await asyncio.gather(*(_fetch_crypto_bars(s) for s in crypto_symbols))
                for sym, bars in crypto_results:
                    result[sym] = bars

            if stock_symbols and self._stock_data is not None:
                from alpaca.data.enums import DataFeed
                from alpaca.data.requests import StockBarsRequest
                request = StockBarsRequest(
                    symbol_or_symbols=stock_symbols,
                    timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
                    start=now - timedelta(days=5),
                    end=now,
                    limit=limit,
                    feed=DataFeed.IEX
                )
                response = await asyncio.wait_for(asyncio.to_thread(self._stock_data.get_stock_bars, request),
                                                  self.settings.request_timeout_seconds)
                frame = response.df.reset_index()
                for record in frame.to_dict("records"):
                    sym = record["symbol"]
                    if sym not in result:
                        continue
                    stamp = pd.Timestamp(record["timestamp"]).to_pydatetime()
                    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
                    if stamp + timedelta(minutes=timeframe_minutes) > now:
                        continue
                    result[sym].append({
                        "timestamp": stamp, "open": float(record["open"]),
                        "high": float(record["high"]), "low": float(record["low"]),
                        "close": float(record["close"]), "volume": float(record.get("volume", 0) or 0)
                    })

            for sym in result:
                result[sym] = result[sym][-limit:]
            return result
        except Exception as exc:
            raise BrokerError(f"Alpaca historical candle batch request failed: {exc}") from exc

    async def get_recent_news(self, symbol: str, lookback_minutes: int = 120,
                              limit: int = 50) -> list[str]:
        """Return recent Alpaca market-news headlines for FinBERT scoring."""
        if self._news is None:
            raise BrokerError("Call connect() before requesting news.")
        if lookback_minutes <= 0 or limit <= 0:
            raise ValueError("lookback_minutes and limit must be positive")
        try:
            from alpaca.data.requests import NewsRequest
            now = datetime.now(timezone.utc)
            news_sym = normalize_crypto_symbol(symbol) if is_crypto_symbol(symbol) else symbol
            request = NewsRequest(symbols=news_sym, start=now - timedelta(minutes=lookback_minutes),
                                  end=now, sort="desc", limit=min(limit, 50), include_content=False)
            response = await asyncio.wait_for(asyncio.to_thread(self._news.get_news, request),
                                              self.settings.request_timeout_seconds)
            return [str(item.headline).strip() for item in response.data.get("news", [])
                    if getattr(item, "headline", "").strip()]
        except Exception as exc:
            raise BrokerError(f"Alpaca news request failed for {symbol}: {exc}") from exc

    async def stream_ticks(self, symbols: list[str]):
        if self._stock_data is None and self._crypto_data is None:
            raise BrokerError("Call connect() before starting market data.")
        try:
            from alpaca.data.live import StockDataStream, CryptoDataStream
        except ImportError as exc:
            raise BrokerError("Install the optional 'alpaca-py' dependency.") from exc
        key, secret = self._credentials()
        delay = 1.0

        crypto_symbols = [s for s in symbols if is_crypto_symbol(s)]
        stock_symbols = [s for s in symbols if not is_crypto_symbol(s)]

        while True:
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            runners = []
            streams = []

            if crypto_symbols:
                crypto_stream = CryptoDataStream(key, secret)
                crypto_pairs = [normalize_crypto_symbol(s) for s in crypto_symbols]

                async def on_crypto_trade(trade):
                    sym = str(trade.symbol)
                    matched = next((s for s in crypto_symbols if s == sym or normalize_crypto_symbol(s) == sym or s.replace("/", "") == sym.replace("/", "")), sym)
                    event = {"symbol": matched, "timestamp": trade.timestamp,
                             "price": float(trade.price), "volume": float(trade.size), "source": "alpaca"}
                    loop.call_soon_threadsafe(queue.put_nowait, event)

                crypto_stream.subscribe_trades(on_crypto_trade, *crypto_pairs)
                runners.append(asyncio.create_task(asyncio.to_thread(crypto_stream.run)))
                streams.append(crypto_stream)
                logger.info("Starting Alpaca 24/7 crypto trade stream for %s", ", ".join(crypto_symbols))

            if stock_symbols:
                stock_stream = StockDataStream(key, secret)

                async def on_stock_trade(trade):
                    event = {"symbol": str(trade.symbol), "timestamp": trade.timestamp,
                             "price": float(trade.price), "volume": float(trade.size), "source": "alpaca"}
                    loop.call_soon_threadsafe(queue.put_nowait, event)

                stock_stream.subscribe_trades(on_stock_trade, *stock_symbols)
                runners.append(asyncio.create_task(asyncio.to_thread(stock_stream.run)))
                streams.append(stock_stream)
                logger.info("Starting Alpaca stock trade stream for %s", ", ".join(stock_symbols))

            delay = 1.0
            try:
                while True:
                    for r in runners:
                        if r.done():
                            error = r.exception()
                            if error:
                                raise error
                    try:
                        yield await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
            except asyncio.CancelledError:
                for s in streams:
                    stop = getattr(s, "stop", None)
                    if stop:
                        try:
                            await asyncio.to_thread(stop)
                        except Exception:
                            pass
                for r in runners:
                    r.cancel()
                raise
            except Exception as exc:
                logger.warning("Alpaca stream ended for %s; reconnecting: %s", ", ".join(symbols), exc)
            finally:
                for s in streams:
                    stop = getattr(s, "stop", None)
                    if stop:
                        try:
                            await asyncio.to_thread(stop)
                        except Exception:
                            pass
                for r in runners:
                    if not r.done():
                        r.cancel()
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)

    async def place_order(self, symbol: str, side: Side, qty: float, order_type: str = "market",
                          limit_price: float | None = None, stop_loss: float | None = None,
                          take_profit: float | None = None) -> dict[str, Any]:
        self._require_trading_enabled()
        if self._trading is None:
            raise BrokerError("Call connect() before submitting orders.")
        if side not in ("BUY", "SELL") or qty <= 0:
            raise ValueError("side must be BUY/SELL and qty must be positive.")
        if order_type not in ("market", "limit"):
            raise ValueError("order_type must be market or limit.")
        if order_type == "limit" and (limit_price is None or limit_price <= 0):
            raise ValueError("A positive limit_price is required for limit orders.")
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass
            from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, TakeProfitRequest, StopLossRequest
            request_cls = MarketOrderRequest if order_type == "market" else LimitOrderRequest

            if is_crypto_symbol(symbol):
                pair = normalize_crypto_symbol(symbol)
                args: dict[str, Any] = dict(
                    symbol=pair,
                    qty=float(qty),
                    side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                    time_in_force=TimeInForce.GTC
                )
                if order_type == "limit":
                    args["limit_price"] = limit_price
                result = await asyncio.wait_for(asyncio.to_thread(self._trading.submit_order, request_cls(**args)),
                                                self.settings.request_timeout_seconds)
                return {"order_id": str(result.id), "status": str(result.status), "symbol": symbol,
                        "side": side, "qty": qty, "order_class": "simple",
                        "broker": "alpaca-paper" if self.settings.environment == "PAPER_ALPACA" else "alpaca-live"}
            else:
                args = dict(symbol=symbol, qty=int(qty),
                            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                            time_in_force=TimeInForce.DAY)
                if order_type == "limit":
                    args["limit_price"] = limit_price
                if (stop_loss is None) != (take_profit is None):
                    raise ValueError("Alpaca bracket orders require both stop_loss and take_profit.")
                if stop_loss is not None and take_profit is not None:
                    args.update(order_class=OrderClass.BRACKET,
                                stop_loss=StopLossRequest(stop_price=stop_loss),
                                take_profit=TakeProfitRequest(limit_price=take_profit))
                result = await asyncio.wait_for(asyncio.to_thread(self._trading.submit_order, request_cls(**args)),
                                                self.settings.request_timeout_seconds)
                return {"order_id": str(result.id), "status": str(result.status), "symbol": symbol,
                        "side": side, "qty": qty, "order_class": "bracket" if (stop_loss and take_profit) else "simple",
                        "broker": "alpaca-paper" if self.settings.environment == "PAPER_ALPACA" else "alpaca-live"}
        except Exception as exc:
            raise BrokerError(f"Alpaca order submission failed: {exc}") from exc

    async def get_positions(self) -> list[dict[str, Any]]:
        if self._trading is None:
            raise BrokerError("Call connect() before requesting positions.")
        try:
            rows = await asyncio.wait_for(asyncio.to_thread(self._trading.get_all_positions), self.settings.request_timeout_seconds)
            positions = []
            for p in rows:
                sym = p.symbol
                if is_crypto_symbol(sym):
                    sym = normalize_crypto_symbol(sym)
                positions.append({
                    "symbol": sym, "qty": float(p.qty), "side": str(p.side),
                    "market_value": float(p.market_value), "unrealized_pl": float(p.unrealized_pl),
                    "asset_class": getattr(p, "asset_class", "crypto" if is_crypto_symbol(sym) else "us_equity")
                })
            return positions
        except Exception as exc:
            raise BrokerError(f"Alpaca position request failed: {exc}") from exc

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return open Alpaca orders so the engine can avoid duplicate entries."""
        if self._trading is None:
            raise BrokerError("Call connect() before requesting open orders.")
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol] if symbol else None)
            orders = await asyncio.wait_for(asyncio.to_thread(self._trading.get_orders, filter=request),
                                            self.settings.request_timeout_seconds)
            return [{"order_id": str(order.id), "symbol": order.symbol,
                     "side": str(order.side), "status": str(order.status)} for order in orders]
        except Exception as exc:
            raise BrokerError(f"Alpaca open-order request failed: {exc}") from exc

    async def get_order_details(self, order_id: str) -> dict[str, Any]:
        """Fetch the parent and nested bracket legs for paper-fill reconciliation."""
        if self._trading is None:
            raise BrokerError("Call connect() before requesting order details.")
        try:
            from alpaca.trading.requests import GetOrderByIdRequest
            order = await asyncio.wait_for(
                asyncio.to_thread(self._trading.get_order_by_id, order_id,
                                  filter=GetOrderByIdRequest(nested=True)),
                self.settings.request_timeout_seconds)

            def normalize_status(value: Any) -> str:
                return str(value).split(".")[-1].lower()

            def optional_float(value: Any) -> float | None:
                return float(value) if value not in (None, "") else None

            return {
                "order_id": str(order.id), "symbol": order.symbol,
                "status": normalize_status(order.status),
                "filled_qty": optional_float(order.filled_qty),
                "filled_avg_price": optional_float(order.filled_avg_price),
                "legs": [{"order_id": str(leg.id), "status": normalize_status(leg.status),
                          "filled_qty": optional_float(leg.filled_qty),
                          "filled_avg_price": optional_float(leg.filled_avg_price)}
                         for leg in (order.legs or [])],
            }
        except Exception as exc:
            raise BrokerError(f"Alpaca order reconciliation failed for {order_id}: {exc}") from exc

    async def cancel_order(self, order_id: str) -> None:
        self._require_trading_enabled()
        if self._trading is None:
            raise BrokerError("Call connect() before cancelling orders.")
        try:
            await asyncio.wait_for(asyncio.to_thread(self._trading.cancel_order_by_id, order_id), self.settings.request_timeout_seconds)
        except Exception as exc:
            raise BrokerError(f"Alpaca cancellation failed for {order_id}: {exc}") from exc


class AngelOneBroker(AbstractBroker):
    """Angel One adapter. Entry orders are refused when protective prices are supplied.

    SmartAPI normal orders do not provide an atomic bracket equivalent here. This adapter
    deliberately fails closed instead of submitting an entry and pretending its SL/TP exist.
    """

    def __init__(self, settings: BrokerSettings | None = None) -> None:
        super().__init__(settings)
        self._client = None
        self._auth_token = None

    async def connect(self) -> None:
        if self.settings.environment != "LIVE_ANGEL_ONE":
            raise BrokerError("Angel One requires BROKER_ENV=LIVE_ANGEL_ONE.")
        names = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET")
        values = [os.getenv(name) for name in names]
        if not all(values):
            raise BrokerError("Set " + ", ".join(names) + " in the environment.")
        try:
            from SmartApi import SmartConnect
            import pyotp
        except ImportError as exc:
            raise BrokerError("Install 'smartapi-python' and 'pyotp' for Angel One.") from exc
        client = SmartConnect(api_key=values[0])
        try:
            session = await asyncio.wait_for(asyncio.to_thread(client.generateSession, values[1], values[2], pyotp.TOTP(values[3]).now()),
                                             self.settings.request_timeout_seconds)
            self._auth_token = (session or {}).get("data", {}).get("jwtToken")
            if not self._auth_token:
                raise BrokerError("Angel One authentication response did not contain a JWT token.")
            self._client = client
            logger.info("Authenticated with Angel One SmartAPI.")
        except Exception as exc:
            raise BrokerError(f"Angel One authentication failed: {exc}") from exc

    async def get_latest_price(self, symbol: str) -> float:
        if self._client is None:
            raise BrokerError("Call connect() before requesting market data.")
        exchange = os.getenv("ANGEL_EXCHANGE", "NSE")
        token = os.getenv("ANGEL_SYMBOL_TOKEN")
        if not token:
            raise BrokerError("Set ANGEL_SYMBOL_TOKEN for the requested Angel One symbol.")
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self._client.ltpData, exchange, symbol, token),
                                            self.settings.request_timeout_seconds)
            return float(result["data"]["ltp"])
        except Exception as exc:
            raise BrokerError(f"Angel One latest-price request failed: {exc}") from exc

    async def stream_ticks(self, symbols: list[str]):
        if self._client is None:
            raise BrokerError("Call connect() before starting market data.")
        token = os.getenv("ANGEL_SYMBOL_TOKEN")
        if not token:
            raise BrokerError("Set ANGEL_SYMBOL_TOKEN for the requested Angel One symbol.")
        # LIMITATION: Angel One SmartAPI WebSocket V2 currently supports only a single token per connection.
        # Multi-symbol streaming would require multiple WebSocket connections or a token multiplexing approach.
        # For now, only the first symbol in the list is streamed.
        # TODO: Implement multi-token subscription when Angel One API supports it.
        if len(symbols) > 1:
            logger.warning("Angel One streaming limited to first symbol only: %s (ignoring %d additional symbols)", 
                           symbols[0], len(symbols) - 1)
        symbol = symbols[0] if symbols else ""
        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        except ImportError as exc:
            raise BrokerError("Install 'smartapi-python' for Angel One streaming.") from exc
        auth = self._auth_token
        feed = self._client.getfeedToken()
        api_key, client_code = os.getenv("ANGEL_API_KEY"), os.getenv("ANGEL_CLIENT_CODE")
        delay = 1.0
        while True:
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            ws = SmartWebSocketV2(auth, api_key, client_code, feed)
            ws.on_open = lambda app: app.subscribe(f"codex-{int(time.time())}", 1,
                [{"exchangeType": 1, "tokens": [token]}])

            def on_data(app, message):
                try:
                    raw_stamp = message.get("exchange_timestamp") or message.get("last_traded_timestamp")
                    if isinstance(raw_stamp, (int, float)):
                        stamp = datetime.fromtimestamp(float(raw_stamp) / 1000.0, timezone.utc)
                    else:
                        stamp = datetime.now(timezone.utc)
                    event = {"symbol": symbol, "timestamp": stamp,
                             "price": float(message["last_traded_price"]) / 100.0,
                             "volume": float(message.get("last_traded_quantity", 0) or 0), "source": "angel-one"}
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("Skipping malformed Angel One tick: %s", exc)

            ws.on_data = on_data
            ws.on_error = lambda app, error: loop.call_soon_threadsafe(queue.put_nowait, error)
            runner = asyncio.create_task(asyncio.to_thread(ws.connect))
            logger.info("Starting Angel One trade stream for %s", symbol)
            try:
                while True:
                    if runner.done():
                        error = runner.exception()
                        if error:
                            raise error
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(event, Exception):
                        raise event
                    yield event
            except asyncio.CancelledError:
                close = getattr(ws, "close_connection", None)
                if close:
                    await asyncio.to_thread(close)
                runner.cancel()
                raise
            except Exception as exc:
                logger.warning("Angel One stream ended for %s; reconnecting: %s", symbol, exc)
            finally:
                close = getattr(ws, "close_connection", None)
                if close:
                    try:
                        await asyncio.to_thread(close)
                    except Exception:
                        pass
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)

    async def place_order(self, symbol: str, side: Side, qty: float, order_type: str = "market",
                          limit_price: float | None = None, stop_loss: float | None = None,
                          take_profit: float | None = None) -> dict[str, Any]:
        self._require_trading_enabled()
        if stop_loss is not None or take_profit is not None:
            raise UnsupportedProtectionError("Angel One order is not submitted: atomic protective SL/TP orders are not implemented.")
        raise UnsupportedProtectionError("Angel One order placement is intentionally unavailable until protective-order handling is implemented.")

    async def get_positions(self) -> list[dict[str, Any]]:
        if self._client is None:
            raise BrokerError("Call connect() before requesting positions.")
        try:
            response = await asyncio.wait_for(asyncio.to_thread(self._client.position), self.settings.request_timeout_seconds)
            rows = response.get("data") or []
            return [{"symbol": p.get("tradingsymbol"), "qty": float(p.get("netqty", 0)),
                     "side": "BUY" if float(p.get("netqty", 0)) >= 0 else "SELL", "broker": "angel-one"} for p in rows]
        except Exception as exc:
            raise BrokerError(f"Angel One position request failed: {exc}") from exc

    async def get_account_equity(self) -> float:
        if self._client is None:
            raise BrokerError("Call connect() before requesting account equity.")
        try:
            response = await asyncio.wait_for(asyncio.to_thread(self._client.rmsLimit), self.settings.request_timeout_seconds)
            data = response.get("data", {})
            return float(data.get("equity", data.get("availablecash", 0)))
        except Exception as exc:
            raise BrokerError(f"Angel One equity request failed: {exc}") from exc

    async def cancel_order(self, order_id: str) -> None:
        self._require_trading_enabled()
        raise UnsupportedProtectionError("Angel One cancellation is not enabled until its broker order lifecycle is reconciled.")

    async def disconnect(self) -> None:
        logger.info("Angel One broker disconnected")


class GrowwBroker(AbstractBroker):
    """Groww market-data adapter. Entries with SL/TP remain guarded pending OCO lifecycle wiring."""

    def __init__(self, settings: BrokerSettings | None = None) -> None:
        super().__init__(settings)
        self._api = None

    async def connect(self) -> None:
        if self.settings.environment != "LIVE_GROWW":
            raise BrokerError("Groww requires BROKER_ENV=LIVE_GROWW.")
        try:
            from growwapi import GrowwAPI
        except ImportError as exc:
            raise BrokerError("Install the optional 'growwapi' package.") from exc
        token = os.getenv("GROWW_ACCESS_TOKEN")
        if not token:
            key, secret = os.getenv("GROWW_API_KEY"), os.getenv("GROWW_API_SECRET")
            if not key or not secret:
                raise BrokerError("Set GROWW_ACCESS_TOKEN or GROWW_API_KEY and GROWW_API_SECRET.")
            try:
                token = await asyncio.wait_for(asyncio.to_thread(GrowwAPI.get_access_token, api_key=key, secret=secret),
                                               self.settings.request_timeout_seconds)
            except Exception as exc:
                raise BrokerError(f"Groww access-token request failed: {exc}") from exc
        self._api = GrowwAPI(token)
        logger.info("Authenticated with Groww API.")

    async def get_latest_price(self, symbol: str) -> float:
        if self._api is None:
            raise BrokerError("Call connect() before requesting market data.")
        exchange = os.getenv("GROWW_EXCHANGE", "NSE")
        segment = os.getenv("GROWW_SEGMENT", "CASH")
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self._api.get_quote, exchange=exchange,
                segment=segment, trading_symbol=symbol), self.settings.request_timeout_seconds)
            return float(result["last_price"])
        except Exception as exc:
            raise BrokerError(f"Groww quote request failed for {symbol}: {exc}") from exc

    async def stream_ticks(self, symbols: list[str]):
        if self._api is None:
            raise BrokerError("Call connect() before starting market data.")
        try:
            from growwapi import GrowwFeed
        except ImportError as exc:
            raise BrokerError("Install the optional 'growwapi' package.") from exc
        token = os.getenv("GROWW_EXCHANGE_TOKEN")
        if not token:
            raise BrokerError("Set GROWW_EXCHANGE_TOKEN for the selected Groww instrument.")
        exchange, segment = os.getenv("GROWW_EXCHANGE", "NSE"), os.getenv("GROWW_SEGMENT", "CASH")
        feed = GrowwFeed(self._api)
        # LIMITATION: Groww API LTP feed currently subscribes to a single exchange token.
        # Multi-symbol streaming would require multiple feed subscriptions or token multiplexing.
        # For now, only the first symbol in the list is streamed.
        # TODO: Implement multi-token subscription when Groww API supports it.
        if len(symbols) > 1:
            logger.warning("Groww streaming limited to first symbol only: %s (ignoring %d additional symbols)",
                           symbols[0], len(symbols) - 1)
        symbol = symbols[0] if symbols else ""
        instruments = [{"exchange": exchange, "segment": segment, "exchange_token": token}]
        delay = 1.0
        while True:
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def on_data(_meta):
                try:
                    data = feed.get_ltp()["ltp"][exchange][segment][token]
                    stamp = datetime.fromtimestamp(float(data["tsInMillis"]) / 1000.0, timezone.utc)
                    event = {"symbol": symbol, "timestamp": stamp, "price": float(data["ltp"]),
                             "volume": 0.0, "source": "groww"}
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except Exception as exc:
                    loop.call_soon_threadsafe(queue.put_nowait, exc)

            try:
                await asyncio.to_thread(feed.subscribe_ltp, instruments, on_data_received=on_data)
                runner = asyncio.create_task(asyncio.to_thread(feed.consume))
                logger.info("Starting Groww feed for %s", symbol)
                delay = 1.0
                while True:
                    if runner.done():
                        error = runner.exception()
                        if error:
                            raise error
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(event, Exception):
                        raise event
                    yield event
            except asyncio.CancelledError:
                try:
                    await asyncio.to_thread(feed.unsubscribe_ltp, instruments)
                finally:
                    raise
            except Exception as exc:
                logger.warning("Groww stream ended for %s; reconnecting: %s", symbol, exc)
            finally:
                try:
                    await asyncio.to_thread(feed.unsubscribe_ltp, instruments)
                except Exception:
                    pass
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)

    async def place_order(self, symbol: str, side: Side, qty: float, order_type: str = "market",
                          limit_price: float | None = None, stop_loss: float | None = None,
                          take_profit: float | None = None) -> dict[str, Any]:
        self._require_trading_enabled()
        if stop_loss is not None or take_profit is not None:
            raise UnsupportedProtectionError("Groww entry not sent: linked OCO exits are not yet reconciled with filled quantities.")
        raise UnsupportedProtectionError("Groww order routing is disabled until protective-exit lifecycle is implemented.")

    async def get_positions(self) -> list[dict[str, Any]]:
        if self._api is None:
            raise BrokerError("Call connect() before requesting positions.")
        try:
            response = await asyncio.wait_for(asyncio.to_thread(self._api.get_positions_for_user),
                                              self.settings.request_timeout_seconds)
            rows = response.get("positions", []) if isinstance(response, dict) else response
            return [{"symbol": p.get("trading_symbol"), "qty": float(p.get("quantity", 0)),
                     "side": "BUY" if float(p.get("quantity", 0)) >= 0 else "SELL",
                     "realized_pnl": p.get("realised_pnl"), "broker": "groww"} for p in rows]
        except Exception as exc:
            raise BrokerError(f"Groww positions request failed: {exc}") from exc

    async def get_account_equity(self) -> float:
        if self._api is None:
            raise BrokerError("Call connect() before requesting account equity.")
        try:
            response = await asyncio.wait_for(asyncio.to_thread(self._api.get_user_funds), self.settings.request_timeout_seconds)
            return float(response.get("equity", response.get("available_margin", 0)))
        except Exception as exc:
            raise BrokerError(f"Groww equity request failed: {exc}") from exc

    async def cancel_order(self, order_id: str) -> None:
        self._require_trading_enabled()
        raise UnsupportedProtectionError("Groww cancellation is not enabled until order-state reconciliation is implemented.")

    async def disconnect(self) -> None:
        logger.info("Groww broker disconnected")


def create_broker_from_env(environment: str | None = None) -> AbstractBroker:
    base = BrokerSettings.from_env()
    settings = BrokerSettings(environment=(environment or base.environment).upper(),
                              trading_enabled=base.trading_enabled,
                              request_timeout_seconds=base.request_timeout_seconds)
    if settings.environment in {"PAPER_ALPACA", "LIVE_ALPACA"}:
        return AlpacaBroker(settings)
    if settings.environment == "LIVE_ANGEL_ONE":
        return AngelOneBroker(settings)
    if settings.environment == "LIVE_GROWW":
        return GrowwBroker(settings)
    raise ValueError(f"Unsupported BROKER_ENV={settings.environment!r}; use PAPER_ALPACA, LIVE_ALPACA, LIVE_ANGEL_ONE, or LIVE_GROWW.")
