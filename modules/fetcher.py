# async_fetcher.py (updated - with currency filtering)
"""
Asynchronous data fetcher from crypto exchanges
"""
import asyncio
import sys
from typing import Optional
import pandas as pd
import ccxt.async_support as ccxt
from datetime import datetime, timezone
from .universal_resolver import create_aiohttp_session
from .logger import perf_logger

# Windows specific setup
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class AsyncExchangeFetcher:
    """Data fetcher from exchanges with ranking by daily growth"""

    def __init__(self, exchange_id: str, market_type: str = 'spot'):
        self.exchange_id = exchange_id.lower()
        self.market_type = market_type.lower()

        # Determine ccxt_market_type
        if self.market_type == 'futures':
            if self.exchange_id in ['binance', 'kucoin']:
                self.ccxt_market_type = 'future'
            elif self.exchange_id in ['mexc', 'okx']:
                self.ccxt_market_type = 'swap'
            elif self.exchange_id in ['bybit']:
                self.ccxt_market_type = 'linear'
            else:
                self.ccxt_market_type = 'future'
        else:
            self.ccxt_market_type = self.market_type

        self.exchange = None
        self.session = None
        self.logger = perf_logger.get_logger('async_fetcher', 'fetcher')

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def initialize(self):
        """Initialize connection to the exchange"""
        if self.exchange is None:
            try:
                # Create session
                self.session = create_aiohttp_session()

                # Get exchange class
                exchange_class = getattr(ccxt, self.exchange_id)

                # Configuration with correct market type
                config = {
                    'enableRateLimit': True,
                    'timeout': 30000,
                    'session': self.session,
                    'options': {
                        'defaultType': self.ccxt_market_type,  # Use precomputed type
                    }
                }

                # Create instance
                self.exchange = exchange_class(config)

                # Load markets
                await self.exchange.load_markets()

                # Log information about loaded pairs
                filtered_pairs = []
                for symbol, market in self.exchange.markets.items():
                    if market.get('type', '').lower() == self.ccxt_market_type:
                        filtered_pairs.append(symbol)

                self.logger.info(f"✅ {self.exchange_id} ({self.ccxt_market_type}) ready. Pairs: {len(filtered_pairs)}")
                if filtered_pairs:
                    self.logger.debug(f"   Sample pairs: {filtered_pairs[:5]}...")

            except Exception as e:
                self.logger.error(f"❌ Error initializing {self.exchange_id}: {e}")
                raise

        return self.exchange

    def _get_exchange_timestamp(self, ticker: dict) -> str:
        """
        Get exchange timestamp from ticker.
        Priority:
        1. timestamp from ticker (milliseconds)
        2. datetime from ticker
        3. System time as fallback
        """
        try:
            # 1. Try to get timestamp (milliseconds)
            if 'timestamp' in ticker and ticker['timestamp']:
                # Convert from milliseconds to seconds
                timestamp_ms = ticker['timestamp']
                # Create datetime from timestamp (milliseconds)
                exchange_time = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                return exchange_time.isoformat()

            # 2. Try to get datetime
            elif 'datetime' in ticker and ticker['datetime']:
                # If it's an ISO format string, try to parse
                if isinstance(ticker['datetime'], str):
                    try:
                        exchange_time = datetime.fromisoformat(ticker['datetime'].replace('Z', '+00:00'))
                        return exchange_time.isoformat()
                    except ValueError:
                        pass

            # 3. Get exchange time via API (if available)
            try:
                # Some exchanges have fetch_time method
                if hasattr(self.exchange, 'fetch_time'):
                    # This method might be async, but we can't call it here
                    # Instead return None and handle below
                    pass
            except:
                pass

        except Exception as e:
            self.logger.warning(f"⚠ Could not get exchange time: {e}")

        # 4. Fallback: return system time with a note
        return datetime.now(tz=timezone.utc).isoformat()

    async def fetch_ranked_pairs(self, limit: int = 50, quote_currency: Optional[str] = None) -> pd.DataFrame:
        """
        Fetch and rank pairs by daily growth with filtering by quote currency

        Args:
            limit: Maximum number of pairs
            quote_currency: Filter by quote currency (BTC, USDT, USDC, ETH, etc.)
                           If None - all pairs

        Returns:
            DataFrame with columns:
            - rank: position by growth (1 - highest growth)
            - pair: pair symbol
            - price: current price
            - change_24h: 24h percentage change
            - volume_24h: 24h trading volume
            - timestamp: data retrieval time (exchange time)
            - system_timestamp: system time (for debugging)
        """
        await self.initialize()

        try:
            # Get all tickers of the required market type
            all_tickers = await self.exchange.fetch_tickers()

            # Get current exchange time for comparison
            current_exchange_time = None
            try:
                if hasattr(self.exchange, 'fetch_time'):
                    exchange_time_ms = await self.exchange.fetch_time()
                    current_exchange_time = datetime.fromtimestamp(exchange_time_ms / 1000, tz=timezone.utc)
            except Exception as e:
                self.logger.warning(f"⚠ Could not get current exchange server time: {e}")

            # If exchange time could not be obtained, use system time as fallback
            if current_exchange_time is None:
                current_exchange_time = datetime.now(tz=timezone.utc)
                self.logger.warning("⚠ Using system time for data freshness check")

            data = []
            inactive_pairs = []

            for symbol, ticker in all_tickers.items():
                # Filter by market type
                market = self.exchange.markets.get(symbol, {})

                # Use ccxt_market_type for filtering (need to add this attribute)
                if market.get('type', '').lower() != getattr(self, 'ccxt_market_type', self.market_type):
                    continue

                # Filter by quote currency if specified
                if quote_currency and quote_currency != "All pairs":
                    # Get quote currency from market
                    market_quote = market.get('quote', '')

                    # If quote currency does not match filter - skip
                    if not market_quote or market_quote.upper() != quote_currency.upper():
                        continue

                # Check for required data
                if not ticker or ticker.get('last') is None or ticker.get('percentage') is None:
                    continue

                # Check that price is non-zero
                if ticker['last'] <= 0:
                    continue

                # Check volume (optional)
                volume = ticker.get('quoteVolume', 0)
                if volume and volume <= 0:
                    continue

                # Get exchange time for this ticker
                ticker_datetime = None

                try:
                    # 1. Try to get timestamp (milliseconds)
                    if 'timestamp' in ticker and ticker['timestamp']:
                        ticker_datetime = datetime.fromtimestamp(ticker['timestamp'] / 1000, tz=timezone.utc)

                    # 2. Try to get datetime string
                    elif 'datetime' in ticker and ticker['datetime']:
                        if isinstance(ticker['datetime'], str):
                            try:
                                dt_str = ticker['datetime'].replace('Z', '+00:00')
                                ticker_datetime = datetime.fromisoformat(dt_str)
                            except ValueError:
                                pass
                except Exception as e:
                    self.logger.warning(f"⚠ Could not get ticker time for {symbol}: {e}")

                # DATA FRESHNESS CHECK
                if ticker_datetime:
                    # Calculate time difference between current exchange time and ticker time
                    time_difference = current_exchange_time - ticker_datetime

                    # If data is older than 24 hours, skip this pair
                    if time_difference.total_seconds() > 24 * 3600:  # 24 hours in seconds
                        inactive_pairs.append((symbol, ticker_datetime.isoformat()))
                        continue  # Skip inactive pair
                else:
                    # If ticker time could not be obtained, consider pair inactive and skip
                    self.logger.warning(f"⚠ Could not get time for pair {symbol}, skipping")
                    continue

                # If ticker time obtained, format it
                exchange_timestamp = ticker_datetime.isoformat() if ticker_datetime else datetime.now(
                    tz=timezone.utc).isoformat()

                data.append({
                    'pair': symbol,
                    'price': ticker['last'],
                    'change_24h': ticker.get('percentage', 0),
                    'volume_24h': ticker.get('quoteVolume', 0),
                    'timestamp': exchange_timestamp,  # Exchange time
                    'system_timestamp': datetime.now(tz=timezone.utc).isoformat()  # System time for debugging
                })

            # Log information about inactive pairs
            if inactive_pairs:
                self.logger.info(f"⚠ Filtered out {len(inactive_pairs)} inactive pairs (data older than 24 hours):")
                for pair, timestamp in inactive_pairs[:10]:  # Show first 10
                    self.logger.debug(f"   - {pair}: last update {timestamp}")
                if len(inactive_pairs) > 10:
                    self.logger.debug(f"   ... and {len(inactive_pairs) - 10} more pairs")

            # Create DataFrame and sort
            df = pd.DataFrame(data)

            # IF NO DATA - RETURN EMPTY DATAFRAME WITH CORRECT COLUMNS
            if df.empty:
                self.logger.info(f"📊 No active pairs from {self.exchange_id}")
                return pd.DataFrame(columns=['rank', 'pair', 'price', 'change_24h', 'volume_24h', 'timestamp', 'system_timestamp'])

            # Process non-empty DataFrame
            # Sort by growth percentage (descending)
            df = df.sort_values('change_24h', ascending=False)

            # Add rank
            df['rank'] = range(1, len(df) + 1)

            # Limit number
            if limit and len(df) > limit:
                df = df.head(limit)

            # Format values
            df['change_24h'] = df['change_24h'].round(3)
            df['volume_formatted'] = (df['volume_24h'] / 1_000_000).round(2)

            filter_info = f" (filter: {quote_currency})" if quote_currency else ""
            self.logger.debug(f"📊 Retrieved {len(df)} active pairs from {self.exchange_id}{filter_info}")

            # Print time information
            if 'timestamp' in df.columns and len(df) > 0:
                first_time = df['timestamp'].iloc[0]
                last_time = df['timestamp'].iloc[-1]
                self.logger.debug(f"⏰ Exchange time in data: from {first_time} to {last_time}")

            return df[['rank', 'pair', 'price', 'change_24h', 'volume_24h', 'timestamp', 'system_timestamp']]

        except Exception as e:
            self.logger.error(f"⚠ Error fetching data: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return pd.DataFrame(
                columns=['rank', 'pair', 'price', 'change_24h', 'volume_24h', 'timestamp', 'system_timestamp'])

    async def close(self):
        """Close connections"""
        if self.exchange:
            try:
                await self.exchange.close()
            except:
                pass

        if self.session:
            try:
                await self.session.close()
            except:
                pass

    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1m', limit: int = 100) -> pd.DataFrame:
        """Получает OHLCV свечи с биржи"""
        await self.initialize()
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            self.logger.debug(f"Fetching OHLCV for {symbol} on {self.exchange_id} (market_type={self.market_type})")
            return df
        except Exception as e:
            self.logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return pd.DataFrame()