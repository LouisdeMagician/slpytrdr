# monitor.py
import os
import time
import asyncio
import aiohttp
import logging
from decimal import Decimal
from typing import Optional

# Configure module-specific logger
logger = logging.getLogger('monitor')
logger.setLevel(logging.INFO)

# Configure file handler from environment
log_file = os.getenv("MONITOR_LOG_FILE", "monitor.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
))
logger.addHandler(file_handler)

MAX_DURATION = 1800


class PriceMonitor:
    def __init__(self):
        self.jupiter_session = aiohttp.ClientSession()
        self.birdeye_session = aiohttp.ClientSession(
            headers={"X-API-KEY": os.getenv("BIRDEYE_API_KEY", "")}
        )
        self.rate_limiter = asyncio.Semaphore(30)
        logger.debug("Initialized PriceMonitor")

    async def close(self):
        await self.jupiter_session.close()
        await self.birdeye_session.close()
        logger.debug("Closed PriceMonitor resources")

    async def get_price(self, token_address: str) -> Optional[Decimal]:
        """Hybrid price check with fallback logic"""
        logger.debug("Fetching price for %s", token_address)
        price = await self._get_jupiter_price(token_address)
        if not price:
            price = await self._get_birdeye_price(token_address)
        return price

    async def _get_jupiter_price(self, token_address: str) -> Optional[Decimal]:
        """Use Jupiter's price API v2"""
        async with self.rate_limiter:
            try:
                logger.debug("Checking Jupiter price for %s", token_address)
                async with self.jupiter_session.get(
                    "https://api.jup.ag/price/v2",
                    params={
                        "ids": token_address,
                        "vsToken": "So11111111111111111111111111111111111111112",
                        "showExtraInfo": "true"
                    }
                ) as resp:
                    data = (await resp.json())["data"]
                    return Decimal(data[token_address]["price"])
            except Exception as e:
                logger.error("Jupiter price error: %s", str(e), exc_info=True)
                return None

    async def _get_birdeye_price(self, token_address: str) -> Optional[Decimal]:
        """Fallback to BirdEye's free tier API"""
        try:
            logger.debug("Checking BirdEye price for %s", token_address)
            async with self.birdeye_session.get(
                "https://public-api.birdeye.so/public/price",
                params={"address": token_address}
            ) as resp:
                data = await resp.json()
                return Decimal(data["data"]["value"])
        except Exception as e:
            logger.error("BirdEye price error: %s", str(e), exc_info=True)
            return None


class TradingMonitor:
    def __init__(self, trader, poll_interval: int = 5, max_retries: int = 3):
        self.trader = trader
        self.monitor = PriceMonitor()
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.active_monitors = {}
        logger.info("Initialized TradingMonitor")

    async def start_monitoring(
        self,
        token_address: str,
        entry_price: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal,
        max_duration: int = 1800
    ):
        """Start monitoring with time-based exit"""
        logger.info("Starting monitoring for %s", token_address)
        self.active_monitors[token_address] = {
            "entry": entry_price,
            "tp": take_profit,
            "sl": stop_loss,
            "start_time": time.time(),
            "max_duration": max_duration
        }
        asyncio.create_task(self._monitor_loop(token_address))

    async def _monitor_loop(self, token_address: str):
        """Main monitoring loop with time check"""
        logger.debug("Starting monitor loop for %s", token_address)
        config = self.active_monitors.get(token_address)
        if not config:
            return

        retries = 0
        while token_address in self.active_monitors:
            try:
                current_time = time.time()
                elapsed = current_time - config["start_time"]

                if elapsed > config["max_duration"]:
                    logger.info("Time limit reached for %s", token_address)
                    await self._safe_liquidate(token_address)
                    break

                current_price = await self.monitor.get_price(token_address)
                if not current_price:
                    retries += 1
                    if retries >= self.max_retries:
                        logger.warning("Max retries reached for %s", token_address)
                        await self._safe_liquidate(token_address)
                        break
                    continue

                retries = 0
                await self._check_triggers(
                    token_address,
                    current_price,
                    config["tp"],
                    config["sl"]
                )

            except Exception as e:
                logger.error("Monitoring error: %s", str(e), exc_info=True)

            await asyncio.sleep(self.poll_interval)

    async def _check_triggers(
        self,
        token_address: str,
        current_price: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal
    ):
        """Evaluate TP/SL conditions"""
        logger.debug("Checking triggers for %s @ %s", token_address, current_price)
        if current_price >= take_profit:
            logger.info("Take profit triggered @ %s for %s", current_price, token_address)
            await self._safe_liquidate(token_address)
        elif current_price <= stop_loss:
            logger.info("Stop loss triggered @ %s for %s", current_price, token_address)
            await self._safe_liquidate(token_address)

    async def _safe_liquidate(self, token_address: str):
        """Handle position liquidation with cleanup"""
        logger.info("Initiating liquidation for %s", token_address)
        try:
            await self.trader.execute_sell_all(token_address)
            self.stop_monitoring(token_address)
            logger.info("Successfully liquidated %s", token_address)
        except Exception as e:
            logger.error("Liquidation failed for %s: %s", token_address, str(e), exc_info=True)

    def stop_monitoring(self, token_address: str):
        """Stop monitoring a token position"""
        logger.info("Stopping monitoring for %s", token_address)
        if token_address in self.active_monitors:
            del self.active_monitors[token_address]