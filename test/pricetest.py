import json
from moralis import sol_api
from decimal import Decimal


# Replace 'YOUR_API_KEY' with your actual Moralis API key
api_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6ImViOTFjNDk1LTk1NjctNGFhOC04ZWM3LTY3MWFhZjUwYTU0NSIsIm9yZ0lkIjoiNDM4ODUwIiwidXNlcklkIjoiNDUxNDgyIiwidHlwZUlkIjoiYjQzNWZlMTYtYmVlMC00M2FhLTkwYmUtOTc0ZjdkYWU0NjU4IiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NDMzNTI0NTMsImV4cCI6NDg5OTExMjQ1M30.Eqqi6AInw85C9JZsOaGnnJm7znorrq3AO2Rmv0rLDNs"

# Replace 'TOKEN_ADDRESS' with the Solana token's mint address
token_address = "EoMCnTbqt2metzoCXcGFyQf5WjSFZXX6sraBH9tFpump"
#"AZoRLqw9XHiZShASgM8Lh4LQRBNqHS4wSmtLzmRcpump"
#"7pazHXKLhNDFYbrDfQminAcvCotpiaCSJ6xcZnwTpump"

# Define parameters for the API call
params = {
    "network": "mainnet",
    "address": token_address,
}

try:
    # Fetch token price
    result = sol_api.token.get_token_price(api_key=api_key, params=params)
    print(json.dumps(result, indent=4))
    price = Decimal(result["usdPrice"])
    print(price)
except Exception as e:
    print(f"An error occurred: {e}")
