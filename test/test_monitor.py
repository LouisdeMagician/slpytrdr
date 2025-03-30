import os
import time
import asyncio
import aiohttp
import logging
from decimal import Decimal
from typing import Optional
from moralis import sol_api

# Logging Setup
logger = logging.getLogger('monitor')
logger.setLevel(logging.INFO)
log_file = os.getenv("MONITOR_LOG_FILE", "monitor.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

# Constants
MAX_DURATION = 1800  # 30 min max duration
DEFAULT_SUPPLY = Decimal("1000000000")  # 1 Billion token supply (as a Decimal for accuracy)

class PriceMonitor:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_price(self, token_address: str) -> Optional[Decimal]:
        """Fetch price using Moralis sol_api."""
        try:
            params = {
                "network": "mainnet",
                "address": token_address,
            }
            result = sol_api.token.get_token_price(api_key=self.api_key, params=params)
            price = Decimal(result["usdPrice"])
            logger.info(f"✅ [Moralis] {token_address} Price: ${price}")
            return price
        except Exception as e:
            logger.error(f"⚠️ Moralis API Error: {e}")
            return None

class TradingMonitor:
    def __init__(self, api_key: str, poll_interval: int = 5, max_retries: int = 3):
        self.monitor = PriceMonitor(api_key)
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.active_monitors = {}

    async def start_monitoring(
        self,
        token_address: str,
        entry_mcap: Decimal,
        tp_multiplier: Decimal = Decimal("1.2"),
        sl_multiplier: Decimal = Decimal("0.9"),
        max_duration: int = MAX_DURATION
    ):
        """Start monitoring using MCAP-based TP/SL logic."""
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
        asyncio.create_task(self._monitor_loop(token_address))

    async def _monitor_loop(self, token_address: str):
        """Monitor price and print triggers."""
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
        """Prints detailed monitoring info with correct MCAP values."""
        tp_triggered = mcap >= take_profit_mcap
        sl_triggered = mcap <= stop_loss_mcap

        print(f"\n📊 **Monitoring {token_address}**")
        print(f"   🔹 Price: ${price:.8f}")
        print(f"   🔹 Estimated MCAP: ${mcap:,}")
        print(f"   🔹 Take Profit MCAP: ${take_profit_mcap:,} {'✅ TRIGGERED' if tp_triggered else ''}")
        print(f"   🔹 Stop Loss MCAP: ${stop_loss_mcap:,} {'✅ TRIGGERED' if sl_triggered else ''}")

    def stop_monitoring(self, token_address: str):
        """Stop monitoring a token position."""
        if token_address in self.active_monitors:
            del self.active_monitors[token_address]
            print(f"🛑 Stopped monitoring {token_address}")

# Example Execution Code (for testing in a script)
async def main():
    api_key = os.getenv("MORALIS_API_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6ImViOTFjNDk1LTk1NjctNGFhOC04ZWM3LTY3MWFhZjUwYTU0NSIsIm9yZ0lkIjoiNDM4ODUwIiwidXNlcklkIjoiNDUxNDgyIiwidHlwZUlkIjoiYjQzNWZlMTYtYmVlMC00M2FhLTkwYmUtOTc0ZjdkYWU0NjU4IiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NDMzNTI0NTMsImV4cCI6NDg5OTExMjQ1M30.Eqqi6AInw85C9JZsOaGnnJm7znorrq3AO2Rmv0rLDNs")
    print(api_key)
    monitor = TradingMonitor(api_key)

    # Tokens with entry MCAP values
    tokens = {
        "3WpYkeVUkQBzJW8ET6YjsqUnsP74HzWvS5NnhPxhpump": Decimal(8_000),
        "EoMCnTbqt2metzoCXcGFyQf5WjSFZXX6sraBH9tFpump": Decimal(73_000)
    }

    for token, entry_mcap in tokens.items():
        await monitor.start_monitoring(token, entry_mcap)

    await asyncio.sleep(1800)  # Let it run for 3 minutes
    await monitor.monitor.close()  # Cleanup sessions

if __name__ == "__main__":
    asyncio.run(main())
