'''
a bot that watches the Snowsight websocket, filters out interesting 
TXs, keeps track of your current subscription credits and periodically
submits top-up payments to keep the subscription active:

- monitor the Avalanche mempool for swaps between WAVAX and CRA from 
users on the TraderJoe router. 
- Pass the input data for each Snowsight TX through the web3 library, 
decode it using the router contract's ABI to get the raw inputs, and 
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
from degenbot import *
from pprint import pprint
from dotenv import load_dotenv
load_dotenv()


async def renew_subscription():
    '''
    async function that blocks only when a renewal
    is necessary. Retrieves data from the Snowsight contract,
    calculates the max payment, and submits a TX through the 
    Chainsight relay.

    This function is async but the snowsight payment blocks
    the event loop until the TX is confirmed
    '''

    print("Starting subscription renewal loop")
    global newest_block_timestamp
    global status_new_blocks
    global nonce

    _snowsight_tiers = {
        "trial": 0,
        "standard": 1,
        "premium": 2,
    }

    try:
        snowsight_contract = brownie.Contract(
            SNOWSIGHT_CONTRACT_ADDRESS,
        )
    except:
        snowsight_contract = brownie.Contract.from_explorer(
            SNOWSIGHT_CONTRACT_ADDRESS,
        )

    renewal_timestamp = snowsight_contract.payments(
        alex_bot.address,
        _snowsight_tiers[SNOWSIGHT_TIER],
    )[-1]

    while True:

        # delay until we're receiving new blocks (newest_block_timestamp needs to be accurate)
        if not status_new_blocks:
            await asyncio.sleep(1)
            continue

        if SNOWSIGHT_TIER in ["trial"]:
            # trial payment has a min and max payment of
            # 86400, so we can't renew early and must wait
            # for expiration
            if renewal_timestamp <= newest_block_timestamp:
                payment = snowsight_contract.calculateMaxPayment(
                    _snowsight_tiers[SNOWSIGHT_TIER]
                )
            else:
                print(
                    f"Renewal in {renewal_timestamp - newest_block_timestamp} seconds"
                )
                await asyncio.sleep(renewal_timestamp - newest_block_timestamp)
                continue

        if SNOWSIGHT_TIER in ["standard", "premium"]:
            # renew credit if we're within 600 seconds
            # of expiration for standard and premium
            if renewal_timestamp - newest_block_timestamp <= 600:
                payment = max(
                    snowsight_contract.calculatePaymentByTierAndTime(
                        _snowsight_tiers[SNOWSIGHT_TIER],
                        SNOWSIGHT_TIME,
                    ),
                    snowsight_contract.calculateMinPayment(
                        _snowsight_tiers[SNOWSIGHT_TIER],
                    ),
                )
            else:
                # sleep half of the remaining time
                print(
                    f"Renewal in {renewal_timestamp - newest_block_timestamp} seconds"
                )
                await asyncio.sleep((renewal_timestamp - newest_block_timestamp) / 2)

        try:
            snowsight_contract.pay(
                _snowsight_tiers[SNOWSIGHT_TIER],
                {
                    "from": alex_bot.address,
                    "value": payment,
                    "priority_fee": 0,
                },
            )
            renewal_timestamp = snowsight_contract.payments(
                alex_bot.address,
                _snowsight_tiers[SNOWSIGHT_TIER],
            )[-1]
            nonce = alex_bot.nonce
        except Exception as e:
            print(e)
            continue

async def watch_pending_transactions():
    '''
    async function to connect to the websocket, send authentication 
    message, and start receiving messages.
    '''

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

            #if resp["status"] == "authenticated":
            elif resp["status"] in ["trial", "standard", "premium"]:

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
    '''
    add our pending TX watcher to the event loop using 
    asyncio.create_task(), then capture the results using 
    asyncio.gather().
    '''
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
SNOWSIGHT_TIER = "standard"
SNOWSIGHT_TIME = 60 * 60 * 24 * 3 # subscription block in seconds

# SNOWTRACE_API_KEY = "[redacted]"

ROUTERS = {
    "0x60aE616a2155Ee3d9A68541Ba4544862310933d4".lower(): {
        "name": "TraderJoe",
        "abi": [],
    },
}

# os.environ["SNOWTRACE_TOKEN"] = SNOWTRACE_API_KEY

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
