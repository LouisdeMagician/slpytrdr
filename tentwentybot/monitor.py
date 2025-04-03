import os
import time
import asyncio
import logging
from decimal import Decimal
from typing import Optional, Dict
from moralis import sol_api
import aiohttp
from dotenv import load_dotenv
from aiohttp import ClientError
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from tentwentybot import JupiterTrader

load_dotenv()

logger = logging.getLogger("TradingMonitor")
logger.setLevel(logging.INFO)
log_file = os.getenv("MONITOR_LOG_FILE", "monitor.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

MAX_DURATION = int(os.getenv("MAX_DURATION", "1800"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
SEMAPHORE_LIMIT = int(os.getenv("SEMAPHORE_LIMIT", "30"))
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

class PriceMonitor:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        self.moralis_session = aiohttp.ClientSession()
        self.birdeye_session = aiohttp.ClientSession(
            headers={"X-API-KEY": BIRDEYE_API_KEY} if BIRDEYE_API_KEY else {}
        )

    async def get_price(self, token_address: str) -> Optional[Decimal]:
        price = await self._get_moralis_price(token_address)
        if price is None:
            logger.warning(f"Failed to fetch price from Moralis for {token_address}, trying Birdeye")
            price = await self._get_birdeye_price(token_address)
        if price is None:
            logger.error(f"All price sources failed for {token_address}")
        return price

    async def _get_moralis_price(self, token_address: str) -> Optional[Decimal]:
        async with self.semaphore:
            try:
                if not MORALIS_API_KEY:
                    raise ValueError("Moralis API key not set")
                params = {"network": "mainnet", "address": token_address}
                response = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: sol_api.token.get_token_price(api_key=MORALIS_API_KEY, params=params)
                )
                price = Decimal(response["usdPrice"])
                logger.debug(f"Moralis price for {token_address}: ${price:.8f}")
                return price
            except (ValueError, ClientError) as e:
                logger.error(f"Moralis price fetch failed for {token_address}: {str(e)}")
                return None
            except Exception as e:
                logger.error(f"Unexpected Moralis error for {token_address}: {str(e)}", exc_info=True)
                return None

    async def _get_birdeye_price(self, token_address: str) -> Optional[Decimal]:
        async with self.semaphore:
            try:
                if not BIRDEYE_API_KEY:
                    logger.warning("Birdeye API key not set, skipping fallback")
                    return None
                async with self.birdeye_session.get(
                    "https://public-api.birdeye.so/public/price",
                    params={"address": token_address}
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    price = Decimal(data["data"]["value"])
                    logger.debug(f"Birdeye price for {token_address}: ${price:.8f}")
                    return price
            except ClientError as e:
                logger.error(f"Birdeye price fetch failed for {token_address}: {str(e)}")
                return None
            except Exception as e:
                logger.error(f"Unexpected Birdeye error for {token_address}: {str(e)}", exc_info=True)
                return None

    async def close(self):
        try:
            await self.moralis_session.close()
            await self.birdeye_session.close()
            logger.debug("PriceMonitor sessions closed")
        except Exception as e:
            logger.error("Error closing PriceMonitor sessions: %s", str(e), exc_info=True)

class TradingMonitor:
    def __init__(self, trader: 'JupiterTrader'):
        if not isinstance(trader, JupiterTrader):
            raise ValueError("Trader must be an instance of JupiterTrader")
        self.trader = trader
        self.monitor = PriceMonitor()
        self.active_monitors: Dict[str, Dict] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.running = True
        logger.info("TradingMonitor initialized")

    async def start_monitoring(
        self, token_address: str, entry_price: Decimal, tp_multiplier: Decimal = Decimal("1.2"), sl_multiplier: Decimal = Decimal("0.9"), max_duration: int = MAX_DURATION
    ):
        try:
            if not token_address or not isinstance(token_address, str):
                raise ValueError("Invalid token address provided")
            if token_address in self.active_monitors:
                logger.warning(f"Already monitoring {token_address}, skipping")
                return

            take_profit = entry_price * tp_multiplier
            stop_loss = entry_price * sl_multiplier

            self.active_monitors[token_address] = {
                "entry_price": entry_price,
                "tp_price": take_profit,
                "sl_price": stop_loss,
                "start_time": time.time(),
                "max_duration": max_duration
            }
            logger.info(f"Started monitoring {token_address} (Entry: ${entry_price:.8f}, TP: ${take_profit:.8f}, SL: ${stop_loss:.8f})")

            task = asyncio.create_task(self._monitor_loop(token_address))
            self.tasks[token_address] = task
        except Exception as e:
            logger.error("Failed to start monitoring for %s: %s", token_address, str(e), exc_info=True)

    async def _monitor_loop(self, token_address: str):
        config = self.active_monitors.get(token_address)
        if not config:
            return

        retries = 0
        backoff = 1

        while self.running and token_address in self.active_monitors:
            try:
                elapsed_time = time.time() - config["start_time"]
                if elapsed_time > config["max_duration"]:
                    logger.info(f"Time limit ({config['max_duration']}s) reached for {token_address}")
                    await self._safe_liquidate(token_address, "Time limit exceeded")
                    break

                current_price = await self.monitor.get_price(token_address)
                if current_price is None:
                    retries += 1
                    if retries >= MAX_RETRIES:
                        logger.warning(f"Max retries ({MAX_RETRIES}) reached for {token_address}, liquidating")
                        await self._safe_liquidate(token_address, "Max retries exceeded")
                        break
                    logger.debug(f"Price fetch failed, retry {retries}/{MAX_RETRIES} after {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                retries = 0
                backoff = 1
                await self._check_triggers(token_address, current_price, config)
            except Exception as e:
                logger.error(f"Monitor loop error for {token_address}: %s", str(e), exc_info=True)
            finally:
                if not self.active_monitors:
                    logger.info("No active monitors remaining, initiating cleanup")
                    await self.stop_all()
                    break
            await asyncio.sleep(POLL_INTERVAL)

    async def _check_triggers(self, token_address: str, current_price: Decimal, config: Dict):
        try:
            tp_price = config["tp_price"]
            sl_price = config["sl_price"]

            if current_price >= tp_price:
                logger.info(f"Take Profit hit for {token_address} at ${current_price:.8f} (TP: ${tp_price:.8f})")
                await self._safe_liquidate(token_address, "Take Profit")
            elif current_price <= sl_price:
                logger.info(f"Stop Loss hit for {token_address} at ${current_price:.8f} (SL: ${sl_price:.8f})")
                await self._safe_liquidate(token_address, "Stop Loss")
        except Exception as e:
            logger.error(f"Trigger check failed for {token_address}: %s", str(e), exc_info=True)

    async def _safe_liquidate(self, token_address: str, reason: str):
        logger.info(f"Liquidating {token_address} due to: {reason}")
        try:
            sell_tx = await self.trader.execute_sell_all(token_address)
            logger.info(f"Sell executed for {token_address}: {sell_tx}")
        except Exception as e:
            logger.error(f"Sell failed for {token_address}: {str(e)}", exc_info=True)
        finally:
            self.stop_monitoring(token_address)

    def stop_monitoring(self, token_address: str):
        if token_address in self.active_monitors:
            del self.active_monitors[token_address]
            logger.info(f"Stopped monitoring {token_address}")

        if task := self.tasks.pop(token_address, None):
            task.cancel()
            logger.debug(f"Cancelled monitoring task for {token_address}")

    async def stop_all(self):
        self.running = False
        for token_address in list(self.active_monitors.keys()):
            self.stop_monitoring(token_address)
        await self.monitor.close()
        logger.info("TradingMonitor fully stopped")