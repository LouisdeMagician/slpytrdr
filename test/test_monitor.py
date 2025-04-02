import os
import time
import asyncio
import logging
from decimal import Decimal
from typing import Optional, Dict
from moralis import sol_api
from tentwentybot import JupiterTrader
from dotenv import load_dotenv
load_dotenv()

# Logging Setup
logger = logging.getLogger("monitor_test")
logger.setLevel(logging.INFO)
log_file = os.getenv("TEST_MONITOR_LOG_FILE", "monitor_test.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

# Constants
MAX_DURATION = 1800  # 30 min max monitoring time
API_KEY = os.getenv("MORALIS_API_KEY")  # Moralis API Key (must be set in env)
POLL_INTERVAL = 5  # Seconds between price checks
MAX_RETRIES = 3  # Max API retries per token
SEMAPHORE_LIMIT = 30  # Limits concurrent API calls


class PriceMonitor:
    """Fetches token prices from Moralis API."""
    
    def __init__(self):
        self.semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async def get_price(self, token_address: str) -> Optional[Decimal]:
        """Fetch price from Moralis."""
        async with self.semaphore:
            try:
                params = {"network": "mainnet", "address": token_address}
                response = sol_api.token.get_token_price(api_key=API_KEY, params=params)
                price = Decimal(response["usdPrice"])
                logger.info(f"✅ [Moralis] {token_address} Price: ${price:.8f}")
                return price
            except Exception as e:
                logger.error(f"⚠️ Moralis API Error: {e}")
                return None


class TradingMonitor:
    """Monitors multiple tokens and dynamically handles TP/SL execution."""

    def __init__(self, trader):
        self.trader = trader
        self.monitor = PriceMonitor()
        self.active_monitors: Dict[str, Dict] = {}  # Token Address → Config
        self.tasks: Dict[str, asyncio.Task] = {}  # Token Address → Task
    
    async def start_monitoring(
        self, token_address: str, entry_price: Decimal, tp_multiplier: Decimal, sl_multiplier: Decimal
    ):
        """Add a new token for monitoring with TP/SL logic."""
        if token_address in self.active_monitors:
            logger.warning(f"⚠️ Already monitoring {token_address}, skipping.")
            return

        take_profit = entry_price * tp_multiplier
        stop_loss = entry_price * sl_multiplier

        self.active_monitors[token_address] = {
            "entry_price": entry_price,
            "tp_price": take_profit,
            "sl_price": stop_loss,
            "start_time": time.time(),
        }
        logger.info(f"🔍 Monitoring {token_address} (Entry: ${entry_price}, TP: ${take_profit}, SL: ${stop_loss})")

        task = asyncio.create_task(self._monitor_loop(token_address))
        self.tasks[token_address] = task

    async def _monitor_loop(self, token_address: str):
        """Handles price monitoring and removes token when TP/SL is hit."""
        config = self.active_monitors[token_address]
        retries = 0

        while token_address in self.active_monitors:
            try:
                elapsed_time = time.time() - config["start_time"]

                # Stop monitoring after max duration
                if elapsed_time > MAX_DURATION:
                    logger.info(f"⏳ Time limit reached for {token_address}, stopping monitor.")
                    self.stop_monitoring(token_address)
                    return

                # Fetch latest price
                current_price = await self.monitor.get_price(token_address)
                if not current_price:
                    retries += 1
                    if retries >= MAX_RETRIES:
                        logger.warning(f"⚠️ Max retries reached for {token_address}, stopping monitor.")
                        self.stop_monitoring(token_address)
                        return
                    continue  # Retry

                retries = 0  # Reset retries after a successful price fetch
                await self._check_triggers(token_address, current_price, config)

            except Exception as e:
                logger.error(f"❌ Error monitoring {token_address}: {e}")

            await asyncio.sleep(POLL_INTERVAL)

    async def _check_triggers(self, token_address: str, current_price: Decimal, config: Dict):
        """Check TP/SL conditions and execute sell if needed."""
        tp_price, sl_price = config["tp_price"], config["sl_price"]

        if current_price >= tp_price:
            logger.info(f"🚀 Take Profit hit for {token_address} at ${current_price:.8f} (TP: ${tp_price:.8f})")
            await self._safe_liquidate(token_address)
        elif current_price <= sl_price:
            logger.info(f"📉 Stop Loss hit for {token_address} at ${current_price:.8f} (SL: ${sl_price:.8f})")
            await self._safe_liquidate(token_address)

    async def _safe_liquidate(self, token_address: str):
        """Trigger sell execution and clean up the monitor."""
        logger.info(f"💰 Selling {token_address}")
        try:
            await self.trader.execute_sell_all(token_address)
        except Exception as e:
            logger.error(f"❌ Sell failed for {token_address}: {e}")

        self.stop_monitoring(token_address)

    def stop_monitoring(self, token_address: str):
        """Stop tracking a token and remove it from active monitoring."""
        if token_address in self.active_monitors:
            del self.active_monitors[token_address]
            logger.info(f"🛑 Stopped monitoring {token_address}")

        # Cancel background task if running
        if task := self.tasks.pop(token_address, None):
            task.cancel()

        # **NEW: Shutdown when no tokens are left**
        if not self.active_monitors:
            logger.info("🎯 No tokens left to monitor. Shutting down.")
            asyncio.create_task(self.shutdown())

    async def shutdown(self):
        """Gracefully shut down all tasks and exit."""
        logger.info("🛑 Stopping all monitoring tasks...")
        for task in list(self.tasks.values()):
            task.cancel()
        self.tasks.clear()
        self.active_monitors.clear()
        logger.info("✅ TradingMonitor has shut down.")
        asyncio.get_running_loop().stop()  # **Stop the event loop**


# Example Execution Code
async def main():
    trader = None  # Replace with actual trading module
    monitor = TradingMonitor(trader)

    # Example tokens
    tokens = {
        "AZoRLqw9XHiZShASgM8Lh4LQRBNqHS4wSmtLzmRcpump": Decimal("0.000161"),  # Entry Price
        "EoMCnTbqt2metzoCXcGFyQf5WjSFZXX6sraBH9tFpump": Decimal("0.000018"),
        "7pazHXKLhNDFYbrDfQminAcvCotpiaCSJ6xcZnwTpump": Decimal("0.0000036")
    }

    for token, entry_price in tokens.items():
        await monitor.start_monitoring(
            token, entry_price, tp_multiplier=Decimal("1.2"), sl_multiplier=Decimal("0.9")
        )

    await asyncio.sleep(1800)  # Let it run for 3 minutes
    for token in tokens.keys():
        monitor.stop_monitoring(token)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
    loop.run_forever()  # **Ensures event loop stays active until explicitly stopped**


