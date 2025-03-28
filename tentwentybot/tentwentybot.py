import asyncio
import os
import json
import base64
import aiohttp
from decimal import Decimal
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from monitor import TradingMonitor

# Load environment variables
load_dotenv()
JUPITER_API_URL = "https://quote-api.jup.ag/v6"
PUMPFUN_DECIMALS = 6  # All Pump.fun tokens use 6 decimals

class JupiterTrader:
    def __init__(self, rpc_url: str, wallet: Keypair):
        self.client = AsyncClient(rpc_url)
        self.wallet = wallet
        self.http_session = aiohttp.ClientSession()
        self.monitor = TradingMonitor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.close()
        await self.http_session.close()

    async def _get_execution_price(self, token_address: str) -> Decimal:
        """Calculate actual entry price based on executed trade"""
        try:
            # Get token account balance
            token_account = await self.client.get_token_accounts_by_owner(
                self.wallet.pubkey(),
                mint=Pubkey.from_string(token_address),
                commitment=Confirmed
            )
            if not token_account.value:
                raise ValueError("Token account not found")

            balance = await self.client.get_token_account_balance(
                token_account.value[0].pubkey,
                commitment=Confirmed
            )
            token_amount = Decimal(balance.value.amount) / Decimal(10**PUMPFUN_DECIMALS)
            
            if token_amount == 0:
                raise ValueError("Zero tokens received")
            
            return Decimal('0.1') / token_amount  # TRADE_AMOUNT_SOL / token_amount

        except Exception as e:
            raise RuntimeError(f"Price calculation failed: {str(e)}") from e

    async def execute_buy(self, token_address: str) -> str:
        """Execute SOL to token swap with integrated monitoring"""
        try:
            # Execute buy transaction
            buy_tx = await self._execute_buy_transaction(token_address)
            
            # Setup monitoring
            await self._setup_position_monitoring(token_address)
            
            return buy_tx

        except Exception as e:
            await self.monitor.stop_monitoring(token_address)
            raise

    async def _execute_buy_transaction(self, token_address: str) -> str:
        """Core buy transaction logic"""
        quote = await self.http_session.get(
            f"{JUPITER_API_URL}/quote",
            params={
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": token_address,
                "amount": str(int(Decimal('0.1') * 10**9),
                "slippageBps": "500"
            }
        )
        quote.raise_for_status()
        quote_data = await quote.json()

        swap_tx = await self.http_session.post(
            f"{JUPITER_API_URL}/swap",
            json={
                "quoteResponse": quote_data,
                "userPublicKey": str(self.wallet.pubkey()),
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {"auto": True},
                "wrapAndUnwrapSol": True
            }
        )
        swap_tx.raise_for_status()
        swap_data = await swap_tx.json()

        transaction = VersionedTransaction.deserialize(
            base64.b64decode(swap_data["swapTransaction"])
        )
        transaction.sign([self.wallet])
        return (await self.client.send_transaction(transaction)).value

    async def _setup_position_monitoring(self, token_address: str):
        """Initialize position tracking with TP/SL and time limit"""
        try:
            entry_price = await self._get_execution_price(token_address)
            await self.monitor.start_monitoring(
                token_address=token_address,
                entry_price=entry_price,
                take_profit=entry_price * Decimal('1.2'),
                stop_loss=entry_price * Decimal('0.9'),
                max_duration=1800  # 30 minutes
            )
        except Exception as e:
            print(f"Monitoring setup failed: {str(e)}")
            await self.execute_sell_all(token_address)
            raise

    async def execute_sell_all(self, token_address: str) -> str:
        """Execute token to SOL swap with error handling"""
        try:
            token_account = await self.client.get_token_accounts_by_owner(
                self.wallet.pubkey(),
                mint=Pubkey.from_string(token_address),
                commitment=Confirmed
            )
            if not token_account.value:
                raise ValueError("No tokens to sell")

            balance = await self.client.get_token_account_balance(
                token_account.value[0].pubkey,
                commitment=Confirmed
            )
            raw_amount = int(balance.value.amount)

            quote = await self.http_session.get(
                f"{JUPITER_API_URL}/quote",
                params={
                    "inputMint": token_address,
                    "outputMint": "So11111111111111111111111111111111111111112",
                    "amount": str(raw_amount),
                    "slippageBps": "1000"
                }
            )
            quote.raise_for_status()
            quote_data = await quote.json()

            swap_tx = await self.http_session.post(
                f"{JUPITER_API_URL}/swap",
                json={
                    "quoteResponse": quote_data,
                    "userPublicKey": str(self.wallet.pubkey()),
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": {"auto": True}
                }
            )
            swap_tx.raise_for_status()
            swap_data = await swap_tx.json()

            transaction = VersionedTransaction.deserialize(
                base64.b64decode(swap_data["swapTransaction"])
            )
            transaction.sign([self.wallet])
            return (await self.client.send_transaction(transaction)).value

        except Exception as e:
            raise RuntimeError(f"Sell failed: {str(e)}") from e
        finally:
            self.monitor.stop_monitoring(token_address)

async def handle_telegram_command(update: Update, _):
    try:
        if str(update.message.from_user.id) not in os.getenv("ALLOWED_USER_IDS").split(","):
            await update.message.reply_text("⛔ Unauthorized")
            return

        command = update.message.text.strip()
        if not command.startswith("/trade "):
            return

        token_address = command.split()[-1]
        wallet = Keypair.from_bytes(bytes(json.loads(os.getenv("WALLET_KEYPAIR")))

        async with JupiterTrader(os.getenv("SOLANA_RPC_URL"), wallet) as trader:
            buy_tx = await trader.execute_buy(token_address)
            await update.message.reply_text(
                f"✅ Buy executed: https://solscan.io/tx/{buy_tx}\n"
                f"⏱ Monitoring started (TP/SL/30min)"
            )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_command))
    print("🚀 Trading Bot Active with Auto-Liquidation")
    app.run_polling()