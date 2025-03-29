import os
import json
import base64
import logging
import aiohttp
import asyncio
from decimal import Decimal
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from monitor import TradingMonitor
import nest_asyncio
import re

nest_asyncio.apply()

# Configure logging
logger = logging.getLogger('trader')
logger.setLevel(logging.INFO)

log_file = os.getenv("TRADER_LOG_FILE", "trader.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

# Load environment variables
load_dotenv()
JUPITER_API_URL = "https://quote-api.jup.ag/v6"
PUMPFUN_DECIMALS = 6  
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "500"))  
SLIPPAGE_BPS_SELL = int(os.getenv("SLIPPAGE_BPS_SELL", "1000")) 

class JupiterTrader:
    def __init__(self, rpc_url: str, wallet: Keypair):
        self.client = AsyncClient(rpc_url)
        self.wallet = wallet
        self.http_session = aiohttp.ClientSession()
        self.monitor = TradingMonitor(self)
        self.token_address = None
        logger.debug("Initialized JupiterTrader with RPC: %s", rpc_url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.close()
        await self.http_session.close()
        if self.monitor:
            await self.monitor.stop_all()
        logger.debug("Closed JupiterTrader resources")

    async def _get_execution_price(self, token_address: str) -> Decimal:
        try:
            logger.debug("Getting execution price for %s", token_address)
            token_account = await self.client.get_token_accounts_by_owner(
                self.wallet.pubkey(),
                mint=Pubkey.from_string(token_address),
                commitment=Confirmed
            )
            if not token_account.value:
                raise ValueError("Token account not found")

            balance = await self.client.get_token_account_balance(token_account.value[0].pubkey, commitment=Confirmed)
            token_amount = Decimal(balance.value.amount) / Decimal(10**PUMPFUN_DECIMALS)

            if token_amount == 0:
                raise ValueError("Zero tokens received")

            return Decimal('0.1') / token_amount

        except Exception as e:
            logger.error("Price calculation failed: %s", str(e), exc_info=True)
            raise

    async def execute_buy(self, token_address: str) -> str:
        """Executes a buy order and sets up monitoring."""
        try:
            logger.info("Initiating buy order for %s", token_address)
            buy_tx = await self._execute_buy_transaction(token_address)
            if buy_tx:
                self.token_address = token_address  
                await self._setup_position_monitoring(token_address)
                logger.info("Buy order successful for %s: %s", token_address, buy_tx)
                return buy_tx
            raise RuntimeError("Buy transaction failed")

        except Exception as e:
            logger.critical("Buy execution failed: %s", str(e), exc_info=True)
            await self.monitor.stop_monitoring(token_address)
            raise

    async def _execute_buy_transaction(self, token_address: str) -> str:
        try:
            logger.debug("Fetching buy quote for %s", token_address)
            async with self.http_session.get(f"{JUPITER_API_URL}/quote", params={
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": token_address,
                "amount": str(int(Decimal('0.1') * 10**9)),
                "slippageBps": str(SLIPPAGE_BPS)
            }) as quote:
                quote.raise_for_status()
                quote_data = await quote.json()

            logger.debug("Creating swap transaction for %s", token_address)
            async with self.http_session.post(f"{JUPITER_API_URL}/swap", json={
                "quoteResponse": quote_data,
                "userPublicKey": str(self.wallet.pubkey()),
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {"auto": True},
                "wrapAndUnwrapSol": True
            }) as swap_tx:
                swap_tx.raise_for_status()
                swap_data = await swap_tx.json()

            transaction = VersionedTransaction.deserialize(base64.b64decode(swap_data["swapTransaction"]))
            transaction.sign([self.wallet])
            result = await self.client.send_transaction(transaction)
            return result.value

        except Exception as e:
            logger.error("Buy transaction failed: %s", str(e), exc_info=True)
            raise

    async def execute_sell_all(self, token_address: str) -> str:
        """Executes a sell order if a valid token balance exists."""
        try:
            logger.info("Initiating sell order for %s", token_address)
            token_account = await self.client.get_token_accounts_by_owner(self.wallet.pubkey(), mint=Pubkey.from_string(token_address), commitment=Confirmed)
            if not token_account.value:
                raise ValueError("No tokens to sell")

            balance = await self.client.get_token_account_balance(token_account.value[0].pubkey, commitment=Confirmed)
            raw_amount = int(balance.value.amount)
            if raw_amount == 0:
                raise ValueError("Token balance is zero")

            async with self.http_session.get(f"{JUPITER_API_URL}/quote", params={
                "inputMint": token_address,
                "outputMint": "So11111111111111111111111111111111111111112",
                "amount": str(raw_amount),
                "slippageBps": str(SLIPPAGE_BPS_SELL)
            }) as quote:
                quote.raise_for_status()
                quote_data = await quote.json()

            async with self.http_session.post(f"{JUPITER_API_URL}/swap", json={
                "quoteResponse": quote_data,
                "userPublicKey": str(self.wallet.pubkey()),
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {"auto": True}
            }) as swap_tx:
                swap_tx.raise_for_status()
                swap_data = await swap_tx.json()

            transaction = VersionedTransaction.deserialize(base64.b64decode(swap_data["swapTransaction"]))
            transaction.sign([self.wallet])
            result = await self.client.send_transaction(transaction)
            return result.value

        except Exception as e:
            logger.error("Sell transaction failed: %s", str(e), exc_info=True)
            raise

async def handle_telegram_command(update: Update, _):
    """Handles Telegram trade commands with validation."""
    try:
        user_id = str(update.message.from_user.id)
        allowed_ids = os.getenv("ALLOWED_USER_IDS", "").split(",")
        user = update.message.from_user
        allowed_bots = os.getenv("AUTHORIZED_BOTS", "")

        is_authorized_user = str(user_id) in allowed_ids
        is_authorized_bot = user.is_bot and user.username in allowed_bots
        if not (is_authorized_user or is_authorized_bot):
            logger.warning("Unauthorized access attempt from user %s", user_id)
            await update.message.reply_text("⛔ Unauthorized")
            return

        command = update.message.text.strip()
        if not command.startswith("/trade "):
            return

        token_address = command.split()[-1]
        if not re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", token_address):
            await update.message.reply_text("❌ Invalid token address format.")
            return

        async with JupiterTrader(os.getenv("SOLANA_RPC_URL"), Keypair.from_bytes(bytes(json.loads(os.getenv("WALLET_KEYPAIR"))))) as trader:
            buy_tx = await trader.execute_buy(token_address)
            await update.message.reply_text(f"✅ Buy executed: https://solscan.io/tx/{buy_tx}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def main():
    try:
        app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/trade "), handle_telegram_command))
        logger.info("🚀 Trading Bot Active")
        await app.run_polling()
    except KeyboardInterrupt:
        logger.info("🛑 Bot shutting down gracefully...")
        await app.shutdown()   
    except Exception as e:
        logger.critical("Bot startup failed: %s", str(e), exc_info=True)
        raise

if __name__ == "__main__":
    asyncio.run(main())
