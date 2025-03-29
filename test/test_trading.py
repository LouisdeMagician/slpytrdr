import unittest
from asyncio import run
from tentwentybot import JupiterTrader
import os
from solders.keypair import Keypair

class TestTrading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wallet = Keypair.from_file("devnet-wallet.json")
        cls.trader = JupiterTrader(os.getenv("SOLANA_RPC_URL"), cls.wallet)
        cls.token_address = "<YOUR_DEVNET_TOKEN>"

    def test_buy(self):
        buy_tx = run(self.trader.execute_buy(self.token_address))
        print(f"Buy TX: https://explorer.solana.com/tx/{buy_tx}?cluster=devnet")
        self.assertIsInstance(buy_tx, str)

    def test_sell(self):
        sell_tx = run(self.trader.execute_sell_all(self.token_address))
        print(f"Sell TX: https://explorer.solana.com/tx/{sell_tx}?cluster=devnet")
        self.assertIsInstance(sell_tx, str)



async def test_buy():
    wallet = Keypair.from_file("devnet-wallet.json")
    trader = JupiterTrader(os.getenv("SOLANA_RPC_URL"), wallet)
    
    token_address = "<YOUR_DEVNET_TOKEN>"
    buy_tx = await trader.execute_buy(token_address)
    print(f"Test Buy TX: https://explorer.solana.com/tx/{buy_tx}?cluster=devnet")

run(test_buy())


async def test_sell():
    wallet = Keypair.from_file("devnet-wallet.json")
    trader = JupiterTrader(os.getenv("SOLANA_RPC_URL"), wallet)
    
    token_address = "<YOUR_DEVNET_TOKEN>"
    sell_tx = await trader.execute_sell_all(token_address)
    print(f"Test Sell TX: https://explorer.solana.com/tx/{sell_tx}?cluster=devnet")

run(test_sell())

if __name__ == "__main__":
    unittest.main()
