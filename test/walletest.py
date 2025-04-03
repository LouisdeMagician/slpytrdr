from mnemonic import Mnemonic
from solders.keypair import Keypair
from bip32 import BIP32
import json
import base58

# Your Jupiter Wallet seed phrase
seed_phrase = "pluck buddy wrap jeans scrub cactus ski twist jar bone attack common"

# Convert seed phrase to seed bytes
mnemo = Mnemonic("english")
seed = mnemo.to_seed(seed_phrase)

# Derive the private key using BIP-44 path m/44'/501'/0'/0'
bip32 = BIP32.from_seed(seed)
derivation_path = "m/44'/501'/0'/0'"  # Solana standard path for first account
private_key = bip32.get_privkey_from_path(derivation_path)[:32]  # Truncate to 32 bytes

# Create the keypair
keypair = Keypair.from_seed(private_key)
keypair_bytes = bytes(keypair)
keypair_json = json.dumps(list(keypair_bytes))

# Print details
print("Public Key:", str(keypair.pubkey()))
print("Private Key (Base58):", base58.b58encode(keypair.secret()).decode())  # Encode bytes to Base58
print("Keypair (for .env):", keypair_json)