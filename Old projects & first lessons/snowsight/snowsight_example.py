'''
a bot that watches the Snowsight websocket, filters out interesting 
TXs, keeps track of your current subscription credits and periodically
submits top-up payments to keep the subscription active:

- monitor the Avalanche mempool for swaps between WAVAX and CRA from 
users on the TraderJoe router. 
- Pass the input data for each Snowsight TX through the web3 library, 
decode it using the router  contract's ABI to get the raw inputs, and 
display them.

Note: not a good design practice to use global variables
'''

import asyncio
import json
import websockets
import os
import sys
import time
import requests
from brownie import accounts, network, Contract
from alex_bot import *
from pprint import pprint


def pay_sync():
    '''
    synchronous function that retrieves data from the Snowsight contract, calculates the maximum payment,
    and submits a payment to the Chainsight contract.
    '''

    snowsight_contract = brownie.Contract.from_explorer(SNOWSIGHT_CONTRACT_ADDRESS)

    block_payment = snowsight_contract.paymentPerBlock() * (
        brownie.chain.height
        + snowsight_contract.maximumPaymentBlocks()
        - snowsight_contract.payments(alex_bot.address)[-1]
    )

    snowsight_contract.pay(
        {
            "from": alex_bot,
            "value": min(
                block_payment,
                snowsight_contract.calculateMaxPayment(),
            ),
            "priority_fee": 0,
        }
    )


async def watch_pending_transactions():

    signed_message = alex_bot.sign_defunct_message(
        "Sign this message to authenticate your wallet with Snowsight."
    )

    async for websocket in websockets.connect(
        uri="ws://avax.chainsight.dev:8589",
        ping_timeout=None,
    ):

        try:
            await websocket.send(
                json.dumps({"signed_key": signed_message.signature.hex()})
            )
            resp = json.loads(await websocket.recv())
            print(resp)

            # if we're currently unauthenticted, pay the contract and restart the loop
            if "unauthenticated" in resp["status"]:
                pay_sync()
                continue

            if resp["status"] == "authenticated":
            #elif resp["status"] in ["trial", "standard", "premium"]:

                while True:

                    tx_message = json.loads(
                        await asyncio.wait_for(
                            websocket.recv(),
                            timeout=30,
                        )
                    )

                    # message keys:
                    # ['from', 'gas', 'gasPrice', 'maxFeePerGas', 'maxPriorityFeePerGas', 'hash', 'input', 'nonce', 'to', 'value', 'txType']

                    tx_to = tx_message["to"].lower()

                    # ignore the TX if it's not on our watchlist
                    if tx_to not in [address.lower() for address in ROUTERS.keys()]:
                        continue
                    else:
                        func, params = brownie.web3.eth.contract(
                            address=brownie.web3.toChecksumAddress(tx_to),
                            abi=ROUTERS[tx_to]["abi"],
                        ).decode_function_input(tx_message["input"])

                        # Print all TX with a 'path' argument, including the function name and inputs
                        if params.get("path") in [
                            [cra.address, wavax.address],
                            [wavax.address, cra.address],
                        ]:
                            print()
                            print("*** Pending CRA-WAVAX swap! ***")
                            print(func.fn_name)
                            print(params)

        except (websockets.WebSocketException, asyncio.exceptions.TimeoutError) as e:
            print(e)
            print("reconnecting...")
        except Exception as e:
            print(f"Exception in watch_pending_transactions: {e}")
            continue


async def main():
    await asyncio.create_task(watch_pending_transactions())
    # await asyncio.gather(
    #     asyncio.create_task(watch_pending_transactions()),
    # )


BROWNIE_NETWORK = "moralis-avax-main-websocket"
BROWNIE_ACCOUNT = "alex_bot"

# Contract addresses
SNOWSIGHT_CONTRACT_ADDRESS = "0x727Dc3C412cCb942c6b5f220190ebAB3eFE0Eb93"
CRA_CONTRACT_ADDRESS = "0xA32608e873F9DdEF944B24798db69d80Bbb4d1ed"
WAVAX_CONTRACT_ADDRESS = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"
TRADERJOE_LP_CRA_WAVAX_ADDRESS = "0x140cac5f0e05cbec857e65353839fddd0d8482c1"

# Change this to your Snowtrace API key!
SNOWTRACE_API_KEY = "[redacted]"

ROUTERS = {
    "0x60aE616a2155Ee3d9A68541Ba4544862310933d4".lower(): {
        "name": "TraderJoe",
        "abi": [],
    },
}

os.environ["SNOWTRACE_TOKEN"] = SNOWTRACE_API_KEY

try:
    network.connect(BROWNIE_NETWORK)
except:
    sys.exit(
        "Could not connect! Verify your Brownie network settings using 'brownie networks list'"
    )

try:
    # account object needs be accessible from other functions
    alex_bot = accounts.load(BROWNIE_ACCOUNT)
except:
    sys.exit(
        "Could not load account! Verify your Brownie account settings using 'brownie accounts list'"
    )

print("\nContracts loaded:")
cra = Erc20Token(address=CRA_CONTRACT_ADDRESS)
wavax = Erc20Token(address=WAVAX_CONTRACT_ADDRESS)

for address in ROUTERS.keys():
    ROUTERS[address]["abi"] = brownie.Contract.from_explorer(address).abi

traderjoe_lp_cra_wavax = LiquidityPool(
    address=TRADERJOE_LP_CRA_WAVAX_ADDRESS,
    name="TraderJoe",
    tokens=[cra, wavax],
)

lps = [
    traderjoe_lp_cra_wavax,
]


asyncio.run(main())
