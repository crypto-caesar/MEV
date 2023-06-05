'''
LP Fetchers for UniswapV2 for use in the 
Uniswap V2 & V3 2-Pool Arbitrage Bot: a bot that searches and 
submits 2-pool cycle arbitrage transactions between V2 and V3 pools 
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

# number of pools to process at a time before flushing to disk
CHUNK_SIZE = 1000

try:
    network.connect(BROWNIE_NETWORK)
except:
    sys.exit("Could not connect!")

exchanges = [
    {
        "name": "SushiSwap",
        "filename": "ethereum_sushiswap_lps.json",
        "factory_address": "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac",
        "factory_deployment_block": 10794229,
    },
    {
        "name": "Uniswap V2",
        "filename": "ethereum_uniswapv2_lps.json",
        "factory_address": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "factory_deployment_block": 10000835,
    },
]

w3 = web3.Web3(web3.WebsocketProvider())

current_block = w3.eth.get_block_number()

for name, factory_address, filename, deployment_block in [
    (
        exchange["name"],
        exchange["factory_address"],
        exchange["filename"],
        exchange["factory_deployment_block"],
    )
    for exchange in exchanges
]:

    print(f"DEX: {name}")

    try:
        factory_contract = Contract(factory_address)
    except:
        try:
            factory_contract = Contract.from_explorer(factory_address)
        except:
            factory_contract = None
    finally:
        if factory_contract is None:
            sys.exit("FACTORY COULD NOT BE LOADED")

    try:
        with open(filename) as file:
            lp_data = json.load(file)
    except FileNotFoundError:
        lp_data = []

    if lp_data:
        previous_pool_count = len(lp_data)
        print(f"Found previously-fetched data: {previous_pool_count} pools")
        previous_block = lp_data[-1].get("block_number")
        print(f"Found pool data up to block {previous_block}")
    else:
        previous_pool_count = 0
        previous_block = deployment_block

    for i in range(previous_block + 1, current_block + 1, BLOCK_SPAN):
        if i + BLOCK_SPAN > current_block:
            end_block = current_block
        else:
            end_block = i + BLOCK_SPAN

        if pool_created_events := factory_contract.events.PairCreated.getLogs(
            fromBlock=i, toBlock=end_block
        ):
            for event in pool_created_events:
                lp_data.append(
                    {
                        "pool_address": event.args.get("pair"),
                        "token0": event.args.get("token0"),
                        "token1": event.args.get("token1"),
                        "block_number": event.get("blockNumber"),
                        "pool_id": event.args.get(""),
                        "type": "UniswapV2",
                    }
                )
        with open(filename, "w") as file:
            json.dump(lp_data, file, indent=2)
