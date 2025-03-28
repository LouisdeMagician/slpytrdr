# monitor.py
import asyncio
import aiohttp
from decimal import Decimal
from typing import Optional, Tuple

MAX_DURATION = 1800

class PriceMonitor:
    def __init__(self):
        self.jupiter_session = aiohttp.ClientSession()
        self.birdeye_session = aiohttp.ClientSession(
            headers={"X-API-KEY": os.getenv("BIRDEYE_API_KEY", "")}
        )
        self.rate_limiter = asyncio.Semaphore(30)  # Jupiter's free tier rate limit

    async def get_price(self, token_address: str) -> Optional[Decimal]:
        """Hybrid price check with fallback logic"""
        price = await self._get_jupiter_price(token_address)
        if not price:
            price = await self._get_birdeye_price(token_address)
        return price

    async def _get_jupiter_price(self, token_address: str) -> Optional[Decimal]:
        """Use Jupiter's free price API v2"""
        async with self.rate_limiter:
            try:
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
                print(f"Jupiter price error: {str(e)}")
                return None

    async def _get_birdeye_price(self, token_address: str) -> Optional[Decimal]:
        """Fallback to BirdEye's free tier API"""
        try:
            async with self.birdeye_session.get(
                "https://public-api.birdeye.so/public/price",
                params={"address": token_address}
            ) as resp:
                data = await resp.json()
                return Decimal(data["data"]["value"])
        except Exception as e:
            print(f"BirdEye price error: {str(e)}")
            return None

class TradingMonitor:
    def __init__(self, trader, poll_interval: int = 5, max_retries: int = 3):
        self.trader = trader
        self.monitor = PriceMonitor()
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.active_monitors = {}

    async def start_monitoring(
        self,
        token_address: str,
        entry_price: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal,
        max_duration: int = 1800  # 30 minutes default
    ):
        """Start monitoring with time-based exit"""
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
        config = self.active_monitors.get(token_address)
        if not config:
            return

        retries = 0
        while token_address in self.active_monitors:
            try:
                current_time = time.time()
                elapsed = current_time - config["start_time"]
                
                # Time-based exit check
                if elapsed > config["max_duration"]:
                    print(f"Time limit reached for {token_address}")
                    await self._safe_liquidate(token_address)
                    break

                current_price = await self.monitor.get_price(token_address)
                if not current_price:
                    retries += 1
                    if retries >= self.max_retries:
                        print(f"Max retries reached for {token_address}")
                        await self._safe_liquidate(token_address)
                        break
                    continue

                retries = 0  # Reset on successful price check
                await self._check_triggers(
                    token_address,
                    current_price,
                    config["tp"],
                    config["sl"]
                )

            except Exception as e:
                print(f"Monitoring error: {str(e)}")
            
            await asyncio.sleep(self.poll_interval)

    async def _check_triggers(
        self,
        token_address: str,
        current_price: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal
    ):
        """Evaluate TP/SL conditions"""
        if current_price >= take_profit:
            print(f"Take profit triggered @ {current_price}")
            await self._safe_liquidate(token_address)
        elif current_price <= stop_loss:
            print(f"Stop loss triggered @ {current_price}")
            await self._safe_liquidate(token_address)

    async def _safe_liquidate(self, token_address: str):
        """Handle position liquidation with cleanup"""
        try:
            await self.trader.execute_sell_all(token_address)
            self.stop_monitoring(token_address)
        except Exception as e:
            print(f"Liquidation failed: {str(e)}")

    def stop_monitoring(self, token_address: str):
        """Stop monitoring a token position"""
        if token_address in self.active_monitors:
            del self.active_monitors[token_address]




async def _get_execution_price(self, token_address: str):
        """Calculate actual entry price based on executed trade"""
        token_account = (await self.client.get_token_accounts_by_owner(
            self.wallet.pubkey(),
            mint=Pubkey.from_string(token_address)
        )).value[0]
        
        balance = await self.client.get_token_account_balance(token_account.pubkey)
        token_amount = Decimal(balance.value.amount) / Decimal(10**PUMPFUN_DECIMALS)
        return (TRADE_AMOUNT_SOL / token_amount).normalize()

