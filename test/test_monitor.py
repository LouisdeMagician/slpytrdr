import os
import time
import asyncio
import aiohttp
import logging
from decimal import Decimal
from typing import Optional

# Configure logging
logger = logging.getLogger('monitor')
logger.setLevel(logging.INFO)

log_file = os.getenv("MONITOR_LOG_FILE", "monitor.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
))
logger.addHandler(file_handler)

MAX_DURATION = 1800  # 30 min max duration
DEFAULT_SUPPLY = Decimal(1_000_000_000)  # 1B token supply assumption


class PriceMonitor:
    def __init__(self):
        self.jupiter_session = aiohttp.ClientSession()
        self.birdeye_session = aiohttp.ClientSession(
            headers={"X-API-KEY": os.getenv("BIRDEYE_API_KEY", "")}
        )
        self.rate_limiter = asyncio.Semaphore(30)

    async def close(self):
        await self.jupiter_session.close()
        await self.birdeye_session.close()

    async def get_price(self, token_address: str) -> Optional[Decimal]:
        """Fetch price from Jupiter, fallback to BirdEye if needed."""
        price = await self._get_jupiter_price(token_address)
        if not price:
            price = await self._get_birdeye_price(token_address)
        return price

    async def _get_jupiter_price(self, token_address: str) -> Optional[Decimal]:
        async with self.rate_limiter:
            try:
                async with self.jupiter_session.get(
                    "https://api.jup.ag/price/v2",
                    params={"ids": token_address, "vsToken": "So11111111111111111111111111111111111111112"}
                ) as resp:
                    data = (await resp.json())["data"]
                    return Decimal(data[token_address]["price"])
            except Exception:
                return None

    async def _get_birdeye_price(self, token_address: str) -> Optional[Decimal]:
        try:
            async with self.birdeye_session.get(
                "https://public-api.birdeye.so/public/price",
                params={"address": token_address}
            ) as resp:
                data = await resp.json()
                return Decimal(data["data"]["value"])
        except Exception:
            return None


class TradingMonitor:
    def __init__(self, poll_interval: int = 5, max_retries: int = 3):
        self.monitor = PriceMonitor()
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.active_monitors = {}

    async def start_monitoring(
        self,
        token_address: str,
        entry_mcap: Decimal,
        tp_multiplier: Decimal = Decimal(1.2),
        sl_multiplier: Decimal = Decimal(0.9),
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
                current_mcap = current_price * DEFAULT_SUPPLY  # ✅ FIXED: Real-time MCAP calculation

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
        print(f"   🔹 Estimated MCAP: ${mcap:,.0f}")
        print(f"   🔹 Take Profit MCAP: ${take_profit_mcap:,.0f} {'✅ TRIGGERED' if tp_triggered else ''}")
        print(f"   🔹 Stop Loss MCAP: ${stop_loss_mcap:,.0f} {'✅ TRIGGERED' if sl_triggered else ''}")

    def stop_monitoring(self, token_address: str):
        """Stop monitoring a token position."""
        if token_address in self.active_monitors:
            del self.active_monitors[token_address]
            print(f"🛑 Stopped monitoring {token_address}")


# Example Execution Code (for testing in a script)
async def main():
    monitor = TradingMonitor()

    # Tokens with entry MCAP values
    tokens = {
        "AnbpjnyZE5ig2dWE85YgS8ZLBR1c1JShnbRVfJZppump": Decimal(26_000),
        "2B14yZAipoEryj6p3JN26g8jAEzNZmD7KdxPBEHspump": Decimal(195_000)
    }

    for token, entry_mcap in tokens.items():
        await monitor.start_monitoring(token, entry_mcap)

    await asyncio.sleep(180)  # Let it run for 3 minutes
    await monitor.monitor.close()  # Cleanup sessions

if __name__ == "__main__":
    asyncio.run(main())
