import json
from moralis import sol_api

# Replace 'YOUR_API_KEY' with your actual Moralis API key
api_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6ImViOTFjNDk1LTk1NjctNGFhOC04ZWM3LTY3MWFhZjUwYTU0NSIsIm9yZ0lkIjoiNDM4ODUwIiwidXNlcklkIjoiNDUxNDgyIiwidHlwZUlkIjoiYjQzNWZlMTYtYmVlMC00M2FhLTkwYmUtOTc0ZjdkYWU0NjU4IiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NDMzNTI0NTMsImV4cCI6NDg5OTExMjQ1M30.Eqqi6AInw85C9JZsOaGnnJm7znorrq3AO2Rmv0rLDNs"

# Replace 'TOKEN_ADDRESS' with the Solana token's mint address
token_address = "3WpYkeVUkQBzJW8ET6YjsqUnsP74HzWvS5NnhPxhpump"

# Define parameters for the API call
params = {
    "network": "mainnet",
    "address": token_address,
}

try:
    # Fetch token price
    result = sol_api.token.get_token_price(api_key=api_key, params=params)
    print(json.dumps(result, indent=4))
except Exception as e:
    print(f"An error occurred: {e}")
