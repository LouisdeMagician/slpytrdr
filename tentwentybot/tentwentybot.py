
import asyncio
import os
import time
from decimal import Decimal
from dotenv import load_dotenv
import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from jupiter_python import JupiterAPI

load_dotenv()

# Trading parameters
TRADE_AMOUNT_SOL = Decimal('0.1')
STOP_LOSS_PERCENT = Decimal('0.10')  # 10%
TAKE_PROFIT_PERCENT = Decimal('0.20')  # 20%
MAX_TRADE_DURATION = 1800  # 30 minutes
MIN_LIQUIDITY = Decimal('1000')
PUMPFUN_DECIMALS = 6

class AdvancedTrader:
    def __init__(self, rpc_url: str, wallet: Keypair):
        self.client = AsyncClient(rpc_url)
        self.jupiter = JupiterAPI(self.client)
        self.wallet = wallet
        self.active_trades = {}

    async def _get_execution_price(self, token_address: str):
        """Calculate actual entry price based on executed trade"""
        token_account = (await self.client.get_token_accounts_by_owner(
            self.wallet.pubkey(),
            mint=Pubkey.from_string(token_address)
        )).value[0]
        
        balance = await self.client.get_token_account_balance(token_account.pubkey)
        token_amount = Decimal(balance.value.amount) / Decimal(10**PUMPFUN_DECIMALS)
        return (TRADE_AMOUNT_SOL / token_amount).normalize()

    async def _execute_buy(self, token_address: str) -> tuple[bool, Decimal]:
        """Core buy logic with proper error handling"""
        try:
            print(f"🛒 Attempting to buy {TRADE_AMOUNT_SOL} SOL of {token_address}")
            
            # Get quote and execute swap
            quote = await self.jupiter.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint=token_address,
                amount=int(TRADE_AMOUNT_SOL * 1e9),
                slippage_bps=1500,
                only_direct_routes=True  # Important for new Pump.fun tokens
            )
            
            # Build and send transaction
            swap_tx = await self.jupiter.get_swap_transaction(
                quote,
                self.wallet.pubkey(),
                fee_bps=10000  # 0.01 SOL priority fee
            )
            swap_tx.sign(self.wallet)
            tx_sig = await self.client.send_transaction(swap_tx)
            
            # Confirm transaction
            if await self._confirm_transaction(tx_sig):
                # Get actual purchased amount
                token_account = (await self.client.get_token_accounts_by_owner(
                    self.wallet.pubkey(),
                    mint=Pubkey.from_string(token_address)
                ).value[0]
                
                balance = await self.client.get_token_account_balance(token_account.pubkey)
                bought_amount = Decimal(balance.value.amount) / Decimal(10**PUMPFUN_DECIMALS)
                
                print(f"✅ Bought {bought_amount:.2f} tokens")
                return True, bought_amount
            return False, Decimal(0)
            
        except Exception as e:
            print(f"🚨 Buy failed: {str(e)}")
            return False, Decimal(0)


    async def execute_trade_cycle(self, token_address: str):
        """Full trade lifecycle with enhanced TP/SL"""
        entry_price = await self._get_execution_price(token_address)
        stop_loss = entry_price * (1 - STOP_LOSS_PERCENT)
        take_profit = entry_price * (1 + TAKE_PROFIT_PERCENT)
        
        print(f"🏁 Trade initiated | Entry: ${entry_price:.6f}")
        print(f"🔻 Stop Loss: ${stop_loss:.6f} | 🚀 Take Profit: ${take_profit:.6f}")

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                "wss://public-api.birdeye.so/ws",
                headers={"X-API-KEY": os.getenv("BIRDEYE_API_KEY")}
            ) as ws:
                while True:
                    try:
                        await ws.send_json({
                            "type": "subscribe",
                            "address": token_address,
                            "channel": "price"
                        })
                        
                        start_time = time.time()
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = msg.json()
                                if data.get('channel') == 'price':
                                    current_price = Decimal(data['data']['price'])
                                    elapsed = time.time() - start_time

                                    # Exit conditions
                                    if current_price <= stop_loss:
                                        print("🔴 Stop loss triggered!")
                                        await self._liquidate_position(token_address)
                                        return "SL Exit"
                                    
                                    if current_price >= take_profit:
                                        print("🟢 Take profit hit!")
                                        await self._liquidate_position(token_address)
                                        return "TP Exit"
                                    
                                    if elapsed > MAX_TRADE_DURATION:
                                        print("⏰ Time-based exit")
                                        await self._liquidate_position(token_address)
                                        return "Time Exit"
                                    
                                    # Price alert system
                                    diff = ((current_price - entry_price) / entry_price * 100).quantize(Decimal('0.01'))
                                    print(f"📈 Price: ${current_price:.6f} | Δ: {diff}%")
                    except Exception as e:
                        print(f"WebSocket error: {e}. Reconnecting in 5s...")
                        await asyncio.sleep(5)

    async def _liquidate_position(self, token_address: str):
        """Execute full liquidation with slippage control"""
        token_account = (await self.client.get_token_accounts_by_owner(
            self.wallet.pubkey(),
            mint=Pubkey.from_string(token_address))
        ).value[0]
        
        balance = await self.client.get_token_account_balance(token_account.pubkey)
        raw_amount = int(balance.value.amount)
        
        # Dynamic slippage based on token volatility
        slippage = self._calculate_dynamic_slippage(token_address)
        quote = await self.jupiter.get_quote(
            input_mint=token_address,
            output_mint="So11111111111111111111111111111111111111112",
            amount=raw_amount,
            slippage_bps=int(slippage * 100)
        
        swap_tx = await self.jupiter.get_swap_transaction(quote, self.wallet.pubkey())
        swap_tx.sign(self.wallet)
        return await self.client.send_transaction(swap_tx)

    def _calculate_dynamic_slippage(self, token_address: str) -> Decimal:
        """Adjust slippage based on recent price volatility"""
        # Implement your volatility analysis here
        return Decimal('0.5')  # Base 15% slippage for memecoins

class PriceValidator:
    @staticmethod
    async def validate_token(token_address: str):
        """Comprehensive token verification"""
        async with aiohttp.ClientSession() as session:
            # Liquidity check
            liquidity = await PriceValidator._get_liquidity(session, token_address)
            if liquidity < MIN_LIQUIDITY:
                return False, f"Liquidity ${liquidity} < ${MIN_LIQUIDITY}"
            
            return True, "Valid token"

    @staticmethod
    async def _get_liquidity(session, token_address: str) -> Decimal:
        url = f"https://public-api.birdeye.so/defi/price?address={token_address}"
        async with session.get(url, headers={"X-API-KEY": os.getenv("BIRDEYE_API_KEY")}) as resp:
            data = (await resp.json())["data"]
            return Decimal(data["liquidity"])


async def handle_telegram_command(update: Update, _):
    user_id = str(update.message.from_user.id)
    allowed_users = os.getenv("ALLOWED_USER_IDS").split(",")
    
    if user_id not in allowed_users:
        await update.message.reply_text("⛔ Unauthorized access")
        return

    command = update.message.text.strip()
    if command.endswith("pump"):
        token_address = command.split()[-1]
        
        # Validate token
        is_valid, message = await PriceValidator.validate_token(token_address)
        if not is_valid:
            await update.message.reply_text(f"❌ Invalid token: {message}")
            return
            
        # Execute trade
        trader = AdvancedTrader(
            os.getenv("SOLANA_RPC_URL"),
            Keypair.from_bytes(bytes(json.loads(os.getenv("WALLET_KEYPAIR")))
        )
        result = await trader.execute_trade_cycle(token_address)
        await update.message.reply_text(f"Trade completed: {result}")

if __name__ == "__main__":
    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_command))
    print("🚀 Advanced Trading Bot Active")
    app.run_polling()