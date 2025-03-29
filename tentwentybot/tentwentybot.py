import os
import json
import base64
import logging
import aiohttp
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

# Configure module-specific logger
logger = logging.getLogger('trader')
logger.setLevel(logging.INFO)

# Configure file handler from environment
log_file = os.getenv("TRADER_LOG_FILE", "trader.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
))
logger.addHandler(file_handler)

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
        logger.debug("Initialized JupiterTrader with RPC: %s", rpc_url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.close()
        await self.http_session.close()
        await self.monitor.close()
        logger.debug("Closed JupiterTrader resources")

    async def _get_execution_price(self, token_address: str) -> Decimal:
        """Calculate actual entry price based on executed trade"""
        try:
            logger.debug("Getting execution price for %s", token_address)
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

            return Decimal('0.1') / token_amount

        except Exception as e:
            logger.error("Price calculation failed for %s: %s", token_address, str(e), exc_info=True)
            raise RuntimeError(f"Price calculation failed: {str(e)}") from e

    async def execute_buy(self, token_address: str) -> str:
        """Execute SOL to token swap with integrated monitoring"""
        try:
            logger.info("Initiating buy order for %s", token_address)
            buy_tx = await self._execute_buy_transaction(token_address)
            await self._setup_position_monitoring(token_address)
            logger.info("Buy order executed successfully for %s: %s", token_address, buy_tx)
            return buy_tx

        except Exception as e:
            logger.critical("Buy execution failed for %s: %s", token_address, str(e), exc_info=True)
            await self.monitor.stop_monitoring(token_address)
            raise

    async def _execute_buy_transaction(self, token_address: str) -> str:
        """Core buy transaction logic"""
        try:
            logger.debug("Fetching buy quote for %s", token_address)
            async with self.http_session.get(
                f"{JUPITER_API_URL}/quote",
                params={
                    "inputMint": "So11111111111111111111111111111111111111112",
                    "outputMint": token_address,
                    "amount": str(int(Decimal('0.1') * 10**9)),
                    "slippageBps": "500"
                }
            ) as quote:
                quote.raise_for_status()
                quote_data = await quote.json()

            logger.debug("Creating swap transaction for %s", token_address)
            async with self.http_session.post(
                f"{JUPITER_API_URL}/swap",
                json={
                    "quoteResponse": quote_data,
                    "userPublicKey": str(self.wallet.pubkey()),
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": {"auto": True},
                    "wrapAndUnwrapSol": True
                }
            ) as swap_tx:
                swap_tx.raise_for_status()
                swap_data = await swap_tx.json()

            transaction = VersionedTransaction.deserialize(
                base64.b64decode(swap_data["swapTransaction"])
            )
            transaction.sign([self.wallet])
            result = await self.client.send_transaction(transaction)
            logger.debug("Transaction submitted for %s: %s", token_address, result.value)
            return result.value

        except Exception as e:
            logger.error("Buy transaction failed for %s: %s", token_address, str(e), exc_info=True)
            raise

    async def _setup_position_monitoring(self, token_address: str):
        """Initialize position tracking with TP/SL and time limit"""
        try:
            entry_price = await self._get_execution_price(token_address)
            logger.info("Setting up monitoring for %s at entry price %s", token_address, entry_price)
            await self.monitor.start_monitoring(
                token_address=token_address,
                entry_price=entry_price,
                take_profit=entry_price * Decimal('1.2'),
                stop_loss=entry_price * Decimal('0.9'),
                max_duration=1800
            )
        except Exception as e:
            logger.error("Failed to setup monitoring for %s: %s", token_address, str(e), exc_info=True)
            await self.execute_sell_all(token_address)
            raise

    async def execute_sell_all(self, token_address: str) -> str:
        """Execute token to SOL swap with error handling"""
        try:
            logger.info("Initiating sell order for %s", token_address)
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

            logger.debug("Fetching sell quote for %s", token_address)
            async with self.http_session.get(
                f"{JUPITER_API_URL}/quote",
                params={
                    "inputMint": token_address,
                    "outputMint": "So11111111111111111111111111111111111111112",
                    "amount": str(raw_amount),
                    "slippageBps": "1000"
                }
            ) as quote:
                quote.raise_for_status()
                quote_data = await quote.json()

            logger.debug("Creating sell transaction for %s", token_address)
            async with self.http_session.post(
                f"{JUPITER_API_URL}/swap",
                json={
                    "quoteResponse": quote_data,
                    "userPublicKey": str(self.wallet.pubkey()),
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": {"auto": True}
                }
            ) as swap_tx:
                swap_tx.raise_for_status()
                swap_data = await swap_tx.json()

            transaction = VersionedTransaction.deserialize(
                base64.b64decode(swap_data["swapTransaction"])
            )
            transaction.sign([self.wallet])
            result = await self.client.send_transaction(transaction)
            logger.info("Sell order executed for %s: %s", token_address, result.value)
            return result.value

        except Exception as e:
            logger.error("Sell transaction failed for %s: %s", token_address, str(e), exc_info=True)
            raise RuntimeError(f"Sell failed: {str(e)}") from e
        finally:
            logger.debug("Stopping monitoring for %s", token_address)
            self.monitor.stop_monitoring(token_address)


async def handle_telegram_command(update: Update, _):
    try:
        user_id = str(update.message.from_user.id)
        allowed_users = os.getenv("ALLOWED_USER_IDS").split(",")
        
        if user_id not in allowed_users:
            logger.warning("Unauthorized access attempt from user %s", user_id)
            await update.message.reply_text("⛔ Unauthorized")
            return

        command = update.message.text.strip()
        if not command.startswith("/trade "):
            return

        token_address = command.split()[-1]
        logger.info("Processing trade command for %s from user %s", token_address, user_id)
        wallet = Keypair.from_bytes(bytes(json.loads(os.getenv("WALLET_KEYPAIR"))))

        async with JupiterTrader(os.getenv("SOLANA_RPC_URL"), wallet) as trader:
            buy_tx = await trader.execute_buy(token_address)
            response_text = (
                f"✅ Buy executed: https://solscan.io/tx/{buy_tx}\n"
                f"⏱ Monitoring started (TP/SL/30min)"
            )
            await update.message.reply_text(response_text)
            logger.info("Successfully processed trade for %s", token_address)

    except Exception as e:
        logger.error("Telegram command handling failed: %s", str(e), exc_info=True)
        await update.message.reply_text(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    try:
        app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/trade "), handle_telegram_command))
        logger.info("🚀 Trading Bot Active")
        app.run_polling()
    except Exception as e:
        logger.critical("Bot startup failed: %s", str(e), exc_info=True)
        raise