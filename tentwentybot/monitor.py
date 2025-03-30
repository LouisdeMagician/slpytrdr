import os
import time
import asyncio
import logging
from decimal import Decimal
from typing import Optional
from moralis import sol_api  # Moralis API for Solana
from dotenv import load_dotenv

# Configure module-specific logger
logger = logging.getLogger('monitor')
logger.setLevel(logging.INFO)
log_file = os.getenv("MONITOR_LOG_FILE", "monitor.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
))
logger.addHandler(file_handler)

MAX_DURATION = 1800  # 30 min max duration
DEFAULT_SUPPLY = Decimal("1000000000")  # 1 Billion token supply
load_dotenv()

class PriceMonitor:
    def __init__(self):
        self.api_key = os.getenv("MORALIS_API_KEY", "")
        if not self.api_key:
            logger.error("MORALIS_API_KEY is not set!")
        self.rate_limiter = asyncio.Semaphore(30)
        logger.debug("Initialized PriceMonitor with Moralis API key.")

    async def close(self):
        # No persistent session to close in Moralis SDK; pass.
        logger.debug("PriceMonitor cleanup complete.")

    async def get_price(self, token_address: str) -> Optional[Decimal]:
        """Fetch token price using Moralis API."""
        logger.debug("Fetching price for %s using Moralis API", token_address)
        loop = asyncio.get_running_loop()
        async with self.rate_limiter:
            try:
                # Moralis sol_api.token.get_token_price is a synchronous call,
                # so we wrap it in run_in_executor to avoid blocking the event loop.
                params = {"network": "mainnet", "address": token_address}
                result = await loop.run_in_executor(None, sol_api.token.get_token_price, self.api_key, params)
                if "usdPrice" not in result:
                    logger.error(f"Moralis API response missing 'usdPrice' for {token_address}: {result}")
                    return None
                price = Decimal(str(result["usdPrice"]))
                logger.info(f"✅ [Moralis] {token_address} Price: ${price}")
                return price
            except Exception as e:
                logger.error(f"Moralis API error for {token_address}: {e}", exc_info=True)
                return None


class TradingMonitor:
    def __init__(self, trader=None, poll_interval: int = 5, max_retries: int = 3):
        self.trader = trader  # trader reference (unused in monitor-only mode)
        self.monitor = PriceMonitor()
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.active_monitors = {}
        logger.info("Initialized TradingMonitor")

    async def start_monitoring(
        self,
        token_address: str,
        entry_mcap: Decimal,
        tp_multiplier: Decimal = Decimal("1.2"),
        sl_multiplier: Decimal = Decimal("0.9"),
        max_duration: int = MAX_DURATION
    ):
        """
        Start monitoring a token's price using MCAP-based TP/SL logic.
        Calculation:
          entry_price = entry_mcap / DEFAULT_SUPPLY
          Take Profit MCAP = entry_mcap * tp_multiplier
          Stop Loss MCAP = entry_mcap * sl_multiplier
        """
        entry_price = entry_mcap / DEFAULT_SUPPLY
        take_profit_mcap = entry_mcap * tp_multiplier
        stop_loss_mcap = entry_mcap * sl_multiplier
        take_profit_price = take_profit_mcap / DEFAULT_SUPPLY
        stop_loss_price = stop_loss_mcap / DEFAULT_SUPPLY

        self.active_monitors[token_address] = {
            "entry_mcap": entry_mcap,
            "entry_price": entry_price,
            "tp_mcap": take_profit_mcap,
            "sl_mcap": stop_loss_mcap,
            "tp_price": take_profit_price,
            "sl_price": stop_loss_price,
            "start_time": time.time(),
            "max_duration": max_duration
        }
        logger.info(f"Starting monitoring for {token_address} | Entry MCAP: ${entry_mcap:,}")
        asyncio.create_task(self._monitor_loop(token_address))

    async def _monitor_loop(self, token_address: str):
        """Main monitoring loop: checks price and prints trigger info."""
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
                    print(f"⏳ Time limit reached for {token_address}, stopping monitor.")
                    break

                current_price = await self.monitor.get_price(token_address)
                if not current_price:
                    retries += 1
                    if retries >= self.max_retries:
                        print(f"⚠️ Max retries reached for {token_address}, stopping monitor.")
                        break
                    continue

                retries = 0
                current_mcap = (current_price * DEFAULT_SUPPLY).quantize(Decimal("1"))
                self._print_monitor_status(
                    token_address, current_price, current_mcap,
                    config["tp_mcap"], config["sl_mcap"]
                )
            except Exception as e:
                print(f"❌ Monitoring error for {token_address}: {e}")
            await asyncio.sleep(self.poll_interval)

    def _print_monitor_status(self, token_address, price, mcap, take_profit_mcap, stop_loss_mcap):
        """Print rich trigger info based on current MCAP."""
        tp_triggered = mcap >= take_profit_mcap
        sl_triggered = mcap <= stop_loss_mcap

        print(f"\n📊 **Monitoring {token_address}**")
        print(f"   🔹 Price: ${price:.8f}")
        print(f"   🔹 Estimated MCAP: ${mcap:,}")
        print(f"   🔹 Take Profit MCAP: ${take_profit_mcap:,} {'✅ TRIGGERED' if tp_triggered else ''}")
        print(f"   🔹 Stop Loss MCAP: ${stop_loss_mcap:,} {'✅ TRIGGERED' if sl_triggered else ''}")

    def stop_monitoring(self, token_address: str):
        """Stop monitoring a token position."""
        logger.info("Stopping monitoring for %s", token_address)
        if token_address in self.active_monitors:
            del self.active_monitors[token_address]
            print(f"🛑 Stopped monitoring {token_address}")


# Example Execution Code (for testing in a script)
async def main():
    # Retrieve Moralis API key from environment
    api_key = os.getenv("MORALIS_API_KEY", "")
    if not api_key:
        logger.error("MORALIS_API_KEY is not set. Exiting.")
        return

    monitor = TradingMonitor(poll_interval=5, max_retries=3)

    # Define tokens with their entry MCAP values (in USD)
    tokens = {
        "DqHJFnU2KqC6B2qskERJjkPqhFS4FY2xaxgEfjUVp7ng": Decimal(1500000),   # e.g., $40K
        "EoMCnTbqt2metzoCXcGFyQf5WjSFZXX6sraBH9tFpump": Decimal(63000),  # e.g., $105K
    }

    # Start monitoring each token (MCAP-based thresholds)
    for token, entry_mcap in tokens.items():
        await monitor.start_monitoring(token, entry_mcap)

    # Let the monitoring run for a period (e.g., 3 minutes)
    await asyncio.sleep(180)
    # Cleanup: close the PriceMonitor (if any resources need closing)
    await monitor.monitor.close()

if __name__ == "__main__":
    asyncio.run(main())
