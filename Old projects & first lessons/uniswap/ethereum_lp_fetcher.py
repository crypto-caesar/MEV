'''
This script replaces the multicall.py script which originally used CSV.
Here, we work with JSON and the exchanges are for UniV2 and Sushiswap 
as opposed to Avalanche DEXes. Instead of translating lists to CSV, 
we build a list of dictionaries and store that to JSON. 

'''

from brownie import network, Contract
import sys
import os
import json
import web3
from dotenv import load_dotenv
load_dotenv()

BROWNIE_NETWORK = "mainnet-local"

#os.environ["ETHERSCAN_TOKEN"] = "!EDITME"

def main():

    try:
        network.connect(BROWNIE_NETWORK)
    except:
        sys.exit(
            "Could not connect!"
        )

    exchanges = [
        {
            "name": "SushiSwap",
            "filename": "ethereum_sushiswap_lps.json",
            "factory_address": "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac",
        },
        {
            "name": "UniswapV2",
            "filename": "ethereum_uniswapv2_lps.json",
            "factory_address": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
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

        print(f"{name}")

        try:
            factory = Contract(address=factory_address)
        except:
            factory = Contract.from_explorer(
                address=factory_address,
                silent=True,
            )

        # Retrieve ABI for typical LP deployed by this factory
        try:
            LP_ABI = Contract(address=factory.allPairs(0)).abi
        except:
            LP_ABI = Contract.from_explorer(address=factory.allPairs(0)).abi

        lp_data = []

        # count the number of pairs tracked by the factory
        pool_count = int(factory.allPairsLength())
        print(f"Found {pool_count} pools")

        # retrieve pool addresses found from the factory
        print("• Fetching LP addresses and token data")
        for pool_id in range(pool_count):
            lp_dict = {}

            lp_address = factory.allPairs(pool_id)
            w3_pool = w3.eth.contract(
                address=lp_address,
                abi=LP_ABI,
            )
            token0 = w3_pool.functions.token0().call()
            token1 = w3_pool.functions.token1().call()

            lp_dict["pool_address"] = lp_address
            lp_dict["token0"] = token0
            lp_dict["token1"] = token1

            lp_data.append(lp_dict)

        print("• Saving pool data to JSON")
        with open(filename, "w") as file:
            json.dump(lp_data, file)


main()
