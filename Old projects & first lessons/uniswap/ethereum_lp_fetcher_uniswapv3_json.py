'''
LP Fetchers for UniswapV3 for use in the 
Uniswap V2 & V3 2-Pool Arbitrage Bot: a bot that searches and 
submits 2-pool cycle arbitrage TXs between V2 and V3 pools 
via Flashbots Auction -- ethereum_parser_2pool_univ3.py
'''

from brownie import network, Contract
import sys
import os
import json
import web3
from dotenv import load_dotenv
load_dotenv()

BROWNIE_NETWORK = "mainnet-local"
#os.environ["ETHERSCAN_TOKEN"] = "[redacted]"

# maximum blocks to process with getLogs
BLOCK_SPAN = 50_000

FACTORY_DEPLOYMENT_BLOCK = 12369621

try:
    network.connect(BROWNIE_NETWORK)
except:
    sys.exit("Could not connect!")

exchanges = [
    {
        "name": "Uniswap V3",
        "filename": "ethereum_uniswapv3_lps.json",
        "factory_address": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
]

w3 = web3.Web3(web3.WebsocketProvider())

for name, factory_address, filename in [
    (
        exchange["name"],
        exchange["factory_address"],
        exchange["filename"],
    )
    for exchange in exchanges
]:

    print(f"DEX: {name}")

    try:
        factory = Contract(factory_address)
    except:
        try:
            factory = Contract.from_explorer(factory_address)
        except:
            factory = None
    finally:
        if factory is None:
            sys.exit("FACTORY COULD NOT BE LOADED")

    try:
        with open(filename) as file:
            lp_data = json.load(file)
    except FileNotFoundError:
        lp_data = []

    if lp_data:
        previous_block = lp_data[-1].get("block_number")
        print(f"Found pool data up to block {previous_block}")
    else:
        previous_block = FACTORY_DEPLOYMENT_BLOCK

    factory_contract = w3.eth.contract(
        address=factory.address, abi=factory.abi
    )

    current_block = w3.eth.get_block_number()
    previously_found_pools = len(lp_data)
    print(f"previously found {previously_found_pools} pools")

    for i in range(previous_block + 1, current_block + 1, BLOCK_SPAN):
        if i + BLOCK_SPAN > current_block:
            end_block = current_block
        else:
            end_block = i + BLOCK_SPAN

        if pool_created_events := factory_contract.events.PoolCreated.getLogs(
            fromBlock=i, toBlock=end_block
        ):
            for event in pool_created_events:
                lp_data.append(
                    {
                        "pool_address": event.args.pool,
                        "fee": event.args.fee,
                        "token0": event.args.token0,
                        "token1": event.args.token1,
                        "block_number": event.blockNumber,
                        "type": "UniswapV3",
                    }
                )
        with open(filename, "w") as file:
            json.dump(lp_data, file, indent=2)

    print(f"Saved {len(lp_data) - previously_found_pools} new pools")
