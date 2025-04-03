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
from solana.rpc.types import TxOpts
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from monitor import TradingMonitor
import nest_asyncio
import re
from aiohttp import ClientError
from solana.rpc.core import RPCException
from decimal import ROUND_HALF_UP

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
TRADE_AMOUNT_SOL = Decimal(os.getenv("TRADE_AMOUNT_SOL", "0.1"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "500"))
SLIPPAGE_BPS_SELL = int(os.getenv("SLIPPAGE_BPS_SELL", "1000"))
SELL_RETRIES = int(os.getenv("SELL_RETRIES", "3"))
SELL_BACKOFF = int(os.getenv("SELL_BACKOFF", "5"))  # Seconds

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
        try:
            await self.client.close()
            await self.http_session.close()
            if self.monitor:
                await self.monitor.stop_all()
        except Exception as e:
            logger.error("Error during cleanup: %s", str(e), exc_info=True)
        logger.debug("Closed JupiterTrader resources")

    async def _get_execution_price(self, token_address: str) -> Decimal:
        try:
            logger.debug("Getting execution price for %s", token_address)
            token_account_resp = await self.client.get_token_accounts_by_owner(
                self.wallet.pubkey(), mint=Pubkey.from_string(token_address), commitment=Confirmed
            )
            if not token_account_resp.value:
                raise ValueError("Token account not found after confirmed buy")

            balance_resp = await self.client.get_token_account_balance(
                token_account_resp.value[0].pubkey, commitment=Confirmed
            )
            token_amount = Decimal(balance_resp.value.amount) / Decimal(10**PUMPFUN_DECIMALS)
            if token_amount == 0:
                raise ValueError("Zero tokens received after confirmed buy")

            return TRADE_AMOUNT_SOL / token_amount
        except RPCException as e:
            logger.error("RPC error fetching token balance: %s", str(e), exc_info=True)
            raise
        except Exception as e:
            logger.error("Price calculation failed: %s", str(e), exc_info=True)
            raise

    async def execute_buy(self, token_address: str) -> str:
        try:
            logger.info("Initiating buy order for %s", token_address)
            buy_tx = await self._execute_buy_transaction(token_address)
            if buy_tx:
                self.token_address = token_address
                await self._setup_position_monitoring(token_address)
                logger.info("Buy order successful for %s: %s", token_address, buy_tx)
                return buy_tx
            raise RuntimeError("Buy transaction returned no ID")
        except Exception as e:
            logger.critical("Buy execution failed: %s", str(e), exc_info=True)
            await self.monitor.stop_monitoring(token_address)
            raise

    async def _execute_buy_transaction(self, token_address: str) -> str:
        try:
            logger.debug("Fetching buy quote for %s", token_address)
            lamports = (TRADE_AMOUNT_SOL * Decimal("1e9")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            
            async with self.http_session.get(f"{JUPITER_API_URL}/quote", params={
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": token_address,
                "amount": str(int(lamports)),
                "slippageBps": str(SLIPPAGE_BPS)
            }) as quote:
                quote.raise_for_status()
                quote_data = await quote.json()
                if "outputAmount" not in quote_data or int(quote_data["outputAmount"]) <= 0:
                    raise ValueError("Invalid quote response: no output amount")

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
                if "swapTransaction" not in swap_data:
                    raise ValueError("Invalid swap response: no transaction data")

            transaction = VersionedTransaction.deserialize(base64.b64decode(swap_data["swapTransaction"]))
            transaction.sign([self.wallet])
            tx_sig = await self.client.send_transaction(transaction, opts=TxOpts(skip_preflight=True))
            await self.client.confirm_transaction(tx_sig.value, commitment=Confirmed)
            return tx_sig.value
        except ClientError as e:
            logger.error("HTTP error during buy: %s", str(e), exc_info=True)
            raise
        except RPCException as e:
            logger.error("RPC error during buy: %s", str(e), exc_info=True)
            raise
        except Exception as e:
            logger.error("Buy transaction failed: %s", str(e), exc_info=True)
            raise

    async def execute_sell_all(self, token_address: str) -> str:
        retries = SELL_RETRIES
        backoff = SELL_BACKOFF
        while retries > 0:
            try:
                logger.info("Initiating sell order for %s (attempt %d/%d)", token_address, SELL_RETRIES - retries + 1, SELL_RETRIES)
                token_account = await self.client.get_token_accounts_by_owner(
                    self.wallet.pubkey(), mint=Pubkey.from_string(token_address), commitment=Confirmed
                )
                if not token_account.value:
                    logger.warning("No tokens to sell for %s", token_address)
                    return "No tokens"

                balance = await self.client.get_token_account_balance(token_account.value[0].pubkey, commitment=Confirmed)
                raw_amount = int(balance.value.amount)
                if raw_amount == 0:
                    logger.warning("Zero balance for %s", token_address)
                    return "Zero balance"

                async with self.http_session.get(f"{JUPITER_API_URL}/quote", params={
                    "inputMint": token_address,
                    "outputMint": "So11111111111111111111111111111111111111112",
                    "amount": str(raw_amount),
                    "wrapAndUnwrapSol": True,
                    "slippageBps": str(SLIPPAGE_BPS_SELL)
                }) as quote:
                    quote.raise_for_status()
                    quote_data = await quote.json()
                    if "outputAmount" not in quote_data or int(quote_data["outputAmount"]) <= 0:
                        raise ValueError("Invalid quote response: no output amount")

                async with self.http_session.post(f"{JUPITER_API_URL}/swap", json={
                    "quoteResponse": quote_data,
                    "userPublicKey": str(self.wallet.pubkey()),
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": {"auto": True}
                }) as swap_tx:
                    swap_tx.raise_for_status()
                    swap_data = await swap_tx.json()
                    if "swapTransaction" not in swap_data:
                        raise ValueError("Invalid swap response: no transaction data")

                transaction = VersionedTransaction.deserialize(base64.b64decode(swap_data["swapTransaction"]))
                transaction.sign([self.wallet])
                tx_sig = await self.client.send_transaction(transaction, opts=TxOpts(skip_preflight=True))
                await self.client.confirm_transaction(tx_sig.value, commitment=Confirmed)
                logger.info("Sell successful for %s: %s", token_address, tx_sig.value)
                return tx_sig.value
            except (ClientError, RPCException) as e:
                retries -= 1
                if retries == 0:
                    logger.critical("Sell failed after retries for %s: %s", token_address, str(e), exc_info=True)
                    raise
                logger.warning("Sell attempt failed for %s: %s, retrying in %ds", token_address, str(e), backoff)
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.error("Unexpected sell error for %s: %s", token_address, str(e), exc_info=True)
                raise

    async def _setup_position_monitoring(self, token_address: str):
        try:
            entry_price_sol = await self._get_execution_price(token_address)
            try:
                sol_usd = Decimal(str(await asyncio.get_running_loop().run_in_executor(
                    None, 
                    lambda: sol_api.token.get_token_price(
                        api_key=os.getenv("MORALIS_API_KEY"),
                        params={"network": "mainnet", "address": "So11111111111111111111111111111111111111112"}
                    )["usdPrice"]
                )))
                logger.debug(f"Using Moralis SOL price: ${sol_usd}")
            except Exception as moralis_error:
                logger.warning(f"Moralis failed: {str(moralis_error)}, trying Birdeye")
                # Fallback to Birdeye
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://public-api.birdeye.so/public/price",
                        headers={"X-API-KEY": os.getenv("BIRDEYE_API_KEY")},
                        params={"address": "So11111111111111111111111111111111111111112"}
                    ) as resp:
                        resp.raise_for_status()
                        sol_usd = Decimal(str((await resp.json())["data"]["value"]))
                logger.debug(f"Using Birdeye SOL price: ${sol_usd}")
            
            entry_price_usd = entry_price_sol * sol_usd
            await self.monitor.start_monitoring(
                token_address=token_address,
                entry_price=entry_price_usd,
                tp_multiplier=Decimal("1.2"),
                sl_multiplier=Decimal("0.9"),
                max_duration=1800
            )
            logger.info(f"Monitoring set up for {token_address} with entry price ${entry_price_usd:.8f}")
        except ClientError as e:
            logger.error("HTTP error setting up monitoring for %s: %s", token_address, str(e), exc_info=True)
            await self.execute_sell_all(token_address)
            raise
        except Exception as e:
            logger.error("Monitoring setup failed for %s: %s", token_address, str(e), exc_info=True)
            await self.execute_sell_all(token_address)
            raise

async def handle_telegram_command(update: Update, _):
    try:
        user_id = str(update.message.from_user.id)
        allowed_ids = os.getenv("ALLOWED_USER_IDS", "").split(",")
        if not allowed_ids or allowed_ids == [""]:
            raise ValueError("ALLOWED_USER_IDS not configured in .env")
        user = update.message.from_user
        allowed_bots = os.getenv("AUTHORIZED_BOTS", "")

        is_authorized_user = user_id in allowed_ids
        is_authorized_bot = user.is_bot and user.username in allowed_bots
        if not (is_authorized_user or is_authorized_bot):
            logger.warning("Unauthorized access attempt from user %s", user_id)
            await update.message.reply_text("⛔ Unauthorized")
            return

        command = update.message.text.strip()
        if not command.startswith("/trade "):
            return

        token_address = command.split()[-1]
        if not re.match(r"^[1-9A-HJ-NP-Za-km-z]{44}$", token_address):
            await update.message.reply_text("❌ Invalid token address format (must be 44 chars)")
            return

        rpc_url = os.getenv("SOLANA_RPC_URL")
        wallet_keypair = os.getenv("WALLET_KEYPAIR")
        if not rpc_url or not wallet_keypair:
            raise ValueError("SOLANA_RPC_URL or WALLET_KEYPAIR not configured in .env")

        async with JupiterTrader(rpc_url, Keypair.from_bytes(bytes(json.loads(wallet_keypair)))) as trader:
            buy_tx = await trader.execute_buy(token_address)
            await update.message.reply_text(f"✅ Buy executed: https://solscan.io/tx/{buy_tx}")
    except ValueError as e:
        await update.message.reply_text(f"❌ Configuration error: {str(e)}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        logger.error("Telegram command error: %s", str(e), exc_info=True)

async def main():
    try:
        if not os.getenv("TELEGRAM_BOT_TOKEN"):
            raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")
        app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/trade "), handle_telegram_command))
        logger.info("🚀 Trading Bot Active")
        await app.run_polling()
    except ValueError as e:
        logger.critical("Startup failed: %s", str(e))
        raise
    except KeyboardInterrupt:
        logger.info("🛑 Bot shutting down gracefully...")
        await app.shutdown()
    except Exception as e:
        logger.critical("Bot startup failed: %s", str(e), exc_info=True)
        raise

if __name__ == "__main__":
    asyncio.run(main())