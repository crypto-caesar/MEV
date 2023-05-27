'''
a bot that uses Snowsight to monitor pending TXs, watch sync 
events to keep pools up-to-date, renew the subscription, and 
submit arbitrage TXs to the propagator. This uses global variable for
simplicity, but this should be removed. From a high level:

- Use a websocket subscription to observe new blocks and the latest 
    base fee
- Use a websocket subscription to receive new Sync events for 
    liquidity pools, then translates these sync events to reserve 
    amounts.
- Maintain a continuous subscription to the Snowsight service by 
    sending payments.
- Use the Snowsight mempool service to receive notifications for 
    pending and completed TXs.
    - Using these notifications, track and dynamically adjust to 
        fluctuating gas fees.
- Calculate + execute profitable arbitrage opportunities.
- Send TXs through the Snowsight propagator.
'''

async def renew_subscription():
    '''
    async function that executes only when a renewal
    is necessary. Retrieves data from the Snowsight contract,
    calculates the max payment, and submits a TX through the 
    Chainsight relay. This function is async, but the snowsight 
    payment blocks the event loop until the TX is confirmed.
    '''

    print("Starting subscription renewal loop")
    global nonce

    try:
        snowsight_contract = brownie.Contract(SNOWSIGHT_CONTRACT_ADDRESS)
    except:
        snowsight_contract = brownie.Contract.from_explorer(SNOWSIGHT_CONTRACT_ADDRESS)

    renewal_block = snowsight_contract.payments(alex_bot.address)[-1]

    while True:

        # renew credit if we're within 600 blocks (roughly 10 minutes) of expiration
        if renewal_block - newest_block <= 600:
            block_payment = min(
                snowsight_contract.calculateMaxPayment(),
                snowsight_contract.paymentPerBlock()
                * (
                    newest_block
                    + snowsight_contract.maximumPaymentBlocks()
                    - snowsight_contract.payments(alex_bot.address)[-1]
                ),
            )

            try:
                snowsight_contract.pay(
                    {
                        "from": alex_bot.address,
                        "value": block_payment,
                        "priority_fee": 0,
                    }
                )
                renewal_block = snowsight_contract.payments(
                    alex_bot.address
                )[-1]
            except:
                continue

        else:
            await asyncio.sleep(0)

async def watch_new_blocks():
    '''
    Watches the websocket for new blocks, updates the base fee
    for the last block, and prints a status update of the
    current maximum gas fees in the mempool
    '''

    print("Starting block watcher loop")

    global status_new_blocks
    global newest_block
    global newest_block_timestamp
    global last_base_fee
    global pending_tx
    global max_priority_fee
    global max_gas_price

    async for websocket in websockets.connect(uri=RPC_URI):

        # reset the start and status every time we connect or reconnect
        status_new_blocks = False

        try:
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["newHeads"],
                    }
                )
            )

            subscribe_result = await asyncio.wait_for(
                websocket.recv(),
                timeout=WEBSOCKET_TIMEOUT,
            )
            print(subscribe_result)

            while True:

                message = json.loads(
                    await asyncio.wait_for(
                        websocket.recv(),
                        timeout=WEBSOCKET_TIMEOUT,
                    )
                )

                status_new_blocks = True

                newest_block = int(
                    message
                    .get("params")
                    .get("result")
                    .get("number"),
                    16,
                )
                newest_block_timestamp = int(
                    message
                    .get("params")
                    .get("result")
                    .get("timestamp"),
                    16,
                )
                last_base_fee = int(
                    message
                    .get("params")
                    .get("result")
                    .get("baseFeePerGas"),
                    16,
                )

                print(
                    f"[{newest_block}] "
                    + f"base: {int(last_base_fee/(10**9))} / "
                    + f"t0 {int(max_gas_price/(10**9))} / "
                    + f"t2 {int(max_priority_fee/(10**9))} / "
                    + f"pending {len(pending_tx)}"
                )

        except Exception as e:
            print("reconnecting...")
            print(e)

async def watch_sync_events():
    '''
    asynch function responsible for keeping the shared list 
    pool_update_queue filled with sync events to be processed later
    '''

    print("Starting sync event watcher loop")

    global pool_update_queue
    global status_sync_events

    async for websocket in websockets.connect(uri=RPC_URI):

        # reset the status to False every time we
        # start a new websocket connection
        status_sync_events = False

        try:
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": [
                            "logs",
                            {
                                "topics": [
                                    web3.Web3()
                                    .keccak(
                                        text="Sync(uint112,uint112)",
                                    )
                                    .hex()
                                ],
                            },
                        ],
                    }
                )
            )
            subscribe_result = await asyncio.wait_for(
                websocket.recv(),
                timeout=WEBSOCKET_TIMEOUT,
            )
            print(subscribe_result)

            while True:

                # message dictionary keys available:
                # 'address',
                # 'topics',
                # 'data',
                # 'blockNumber',
                # 'transactionHash',
                # 'transactionIndex',
                # 'blockHash',
                # 'logIndex',
                # 'removed'

                message = json.loads(
                    await asyncio.wait_for(
                        websocket.recv(),
                        timeout=WEBSOCKET_TIMEOUT,
                    )
                )

                status_sync_events = True

                event_timestamp = time.monotonic()
                event_address = web3.Web3().toChecksumAddress(
                    message
                    .get("params")
                    .get("result")
                    .get("address")
                )
                event_block = int(
                    message
                    .get("params")
                    .get("result")
                    .get("blockNumber"),
                    16,
                )
                event_data = (
                    message
                        .get("params")
                        .get("result")
                        .get("data")
                )
                event_reserves = eth_abi.decode_single(
                    "(uint112,uint112)",
                    bytes.fromhex(
                        event_data[2:]
                    )
                )

                pool_update_queue.append(
                    [
                        event_timestamp,
                        event_address,
                        event_block,
                        event_reserves,
                    ],
                )

        except Exception as e:
            print("reconnecting...")
            print(e)

async def update_pools():
    '''
    async funciton that updates the liquidity pool objects by 
    monitoring the shared pool_update_queue.
    '''

    print("Starting pool update loop")
    global pool_update_queue

    # create a working queue to be filled from the
    # global update queue, then purged after processing
    working_pool_update_queue = []

    while True:

        # yield to the event loop, check the timestamp
        # (index 0) of the most recent entry in
        # pool_update_queue, then process the entire queue
        # when the latest update was received greater than
        # some interval ago

        await asyncio.sleep(0)
        if pool_update_queue and (
            time.monotonic() - pool_update_queue[-1][0] > 0.1
        ):
            pass
        else:
            continue

        # pool_update_queue items have this format:
        # [
        #     index 0: event_timestamp,
        #     index 1: event_address,
        #     index 2: event_block,
        #     index 3: event_reserves,
        # }

        # deep copy the global update queue to a working
        # queue, then clear the global queue
        working_pool_update_queue = pool_update_queue[:]
        pool_update_queue.clear()

        # identify all relevant pools, eliminate duplicates
        updated_pool_addresses = list(
            set(
                [update[1] for update in working_pool_update_queue]
            )
        )
        for address in updated_pool_addresses:
            # process only the reserves as of the last
            # sync event
            reserves0, reserves1 = [
                update[3]
                for update in working_pool_update_queue
                if update[1] == address
            ][-1]

            for lp in [lp for lp in alex_bot_lps if lp.address == address]:
                lp.update_reserves(
                    external_token0_reserves=reserves0,
                    external_token1_reserves=reserves1,
                    print_ratios=False,
                    print_reserves=False,
                )

        working_pool_update_queue.clear()

        # update all arbitrage helpers, then set the
        # 'new_reserves' variable within LP objects
        # to False, which prevents the helper from
        # endlessly recalculating the same arbitrage on
        # each loop
        for arb in alex_bot_arbs:
            arb.update_reserves()
        for lp in alex_bot_lps:
            lp.new_reserves = False

async def process_arbs():
    '''
    runs continuously, monitoring the status of the sync and new 
    block watcher functions. If either turns False, the processor 
    function will go into a “paused” state. It will only continue 
    processing arb opportunities after both statuses have been True 
    and the function has been running for >= 1 minute.
    '''

    print("Starting arb processing loop")

    global nonce

    start = time.monotonic()

    while True:

        await asyncio.sleep(0)

        # reset the start timer if the block or sync status
        # is set to False (indicates that the websocket
        # disconnected, some events might be missed, and arb
        # pool states may be out of date)
        if status_new_blocks == False or status_sync_events == False:
            start = time.monotonic()
            continue

        # delay all processing unless the arb processing loop
        # has been running for more than 60s
        if time.monotonic() - start <= 60:
            continue

        # prepare a list of arbs to execute
        arbs_to_execute = []

        # calculate the estimated gas cost in Wei
        arb_gas_cost = ESTIMATED_GAS_USE * (max(max_gas_price, max_priority_fee) + 1)

        for arb in alex_bot_arbs:
            if arb.best["borrow_amount"] and arb.best["profit_amount"] >= MIN_PROFIT_MULTIPLIER * arb_gas_cost:
                # add the async task to a queue for
                # execution later
                arbs_to_execute.append(arb)

        if arbs_to_execute:
            nonce_start = nonce
            for i, arb in enumerate(arbs_to_execute):
                asyncio.create_task(
                    send_arb_via_relay(
                        arb,
                        nonce_start + i,
                    )
                )

async def send_arb_via_relay(arb, nonce):

    '''
    Arb executor function does not run infinitely like the other 
    async functions. It is designed to be added to the event loop 
    with a single arbitrage object and a nonce. When the event loop 
    gets around to it, each arb is sent through the Snowsight relay. 
    This looks very similar to the relay in snowsight_example.py.
    '''

    signed_message = alex_bot.sign_defunct_message(
        message="Sign this message to authenticate your wallet with Snowsight."
    )

    # prepare a TX to submit directly through the snowsight relay
    tx = (
        web3.Web3()
        .eth.contract(
            address=arb_contract.address,
            abi=arb_contract.abi,
        )
        .functions.start_flash_borrow_to_lp_swap(
            arb.borrow_pool.address,
            arb.best["borrow_pool_amounts"],
            arb.best["repay_amount"],
            arb.swap_pool_addresses,
            arb.best["swap_pool_amounts"],
        )
        .buildTransaction(
            {
                "from": alex_bot.address,
                "chainId": 43114,
                "gas": TX_GAS_LIMIT,
                "maxFeePerGas": int(max_priority_fee + 1),
                "maxPriorityFeePerGas": int(max_priority_fee + 1),
                "nonce": nonce,
            }
        )
    )

    # sign the TX with the bot's private key
    signed_tx = web3.Web3().eth.account.sign_transaction(
        tx,
        alex_bot.private_key,
    )

    # submit the raw TX directly to the chainsight propagator
    if not DRY_RUN:
        request = requests.post(
            url=SNOWSIGHT_RELAY,
            data=json.dumps(
                {
                    "signed_key": signed_message.signature.hex(),
                    "raw_tx": signed_tx.rawTransaction.hex(),
                }
            ),
        )
        print()
        print(f"*** {request.status_code} - {request.text} ***")
        print()
    else:
        print()
        print("*** ARB DRY RUN PLACEHOLDER ***")
        print()

    arb.best.update(
        {
            "borrow_amount": 0,
            "borrow_pool_amounts": [],
            "repay_amount": 0,
            "profit_amount": 0,
            "swap_pool_amounts": [],
        }
    )

async def watch_pending_transactions():
    
    '''
    async function watches the Snowsight mempool websocket for new 
    events, applies some basic filtering, and adds them to the global
    pending_tx queue. The subscription is set with the 'include_finalized' 
    option set to True, which delivers a 2nd message once a TX is 
    finalized and confirmed by the validator. This allows you to drop 
    the TX from the queue (keeps the gas calculations as accurate as 
    possible.
    '''

    print("Starting pending TX watcher loop")

    global pending_tx
    global max_gas_price
    global max_priority_fee
    global pending_tx
    global nonce

    signed_message = alex_bot.sign_defunct_message(
        "Sign this message to authenticate your wallet with Snowsight."
    )

    async for websocket in websockets.connect(
        uri=SNOWSIGHT_MEMPOOL,
        ping_timeout=None,
    ):

        try:
            await websocket.send(
                json.dumps(
                    {
                        "signed_key": signed_message.signature.hex(),
                        "include_finalized": True,
                    }
                ),
            )
            resp = json.loads(
                await asyncio.wait_for(websocket.recv(), timeout=WEBSOCKET_TIMEOUT),
            )
            print(resp)

            # if the service thinks we're connected or authenticated, sleep and retry later
            if (
                "already connected" in resp["status"]
                or "unauthenticated" in resp["status"]
            ):
                continue

            elif resp["status"] == "authenticated":

                # keep a log of pending transactions to prevent double-counting
                # list will be filled with entries in tuple format (transaction hash, timestamp)

                while True:

                    tx_message = json.loads(
                        await asyncio.wait_for(
                            websocket.recv(),
                            timeout=WEBSOCKET_TIMEOUT,
                        )
                    )
                    # message keys:
                    # 'from',
                    # 'gas',
                    # 'gasPrice',
                    # 'maxFeePerGas',
                    # 'maxPriorityFeePerGas',
                    # 'hash',
                    # 'input',
                    # 'nonce',
                    # 'to',
                    # 'value',
                    # 'txType'

                    # drop stale transactions from the queue
                    pending_tx = [
                        tx for tx in pending_tx if time.monotonic() - tx[1] <= 300
                    ]

                    tx_to = web3.Web3().toChecksumAddress(tx_message.get("to"))
                    tx_hash = tx_message.get("hash")
                    tx_value = int(tx_message.get("value"), 16)
                    tx_input = tx_message.get("input")
                    tx_from = web3.Web3().toChecksumAddress(tx_message.get("from"))
                    tx_nonce = int(tx_message.get("nonce"), 16)
                    tx_gas_fee = int(tx_message.get("gasPrice"), 16)
                    tx_max_fee = int(tx_message.get("maxFeePerGas"), 16)
                    tx_priority_fee = int(tx_message.get("maxPriorityFeePerGas"), 16)

                    # ignore basic AVAX transfers and transactions to certain addresses
                    if tx_input == "0x" or tx_to in [
                        web3.Web3().toChecksumAddress(address)
                        for address in [
                            "0xb0731d50c681c45856bfc3f7539d5f61d4be81d8",  # Anyswap Router
                            "0x82a85407bd612f52577909f4a58bfc6873f14da8",  # Crabada
                        ]
                    ]:
                        continue

                    # The Chainsight websocket sends duplicate
                    # notifications for confirmed transactions,
                    # so check if this TX has already been
                    # seen. If the TX is new, add it to the
                    # queue with a timestamp. If the TX is old,
                    # remove it from the queue and skip
                    # post-processing
                    if tx_hash not in [
                        tx[0] for tx in pending_tx
                    ]:
                        pending_tx.append(
                            [
                                tx_hash,
                                time.monotonic(),
                                tx_max_fee,
                                tx_priority_fee,
                                tx_gas_fee,
                            ]
                        )
                    else:
                        # find the original TX by matching the
                        # tx_hash, then delete it and return
                        # to the websocket
                        pos = [
                            tx[0] for tx in pending_tx
                        ].index(tx_hash)
                        del pending_tx[pos]
                        continue

                    # Catch any pending mempool TX that we've
                    # sent, then use the transmitted nonce to
                    # update the global nonce counter. This is
                    # faster than asking the RPC each time,
                    # since multiple transactions may need to
                    # be sent quickly with consecutive nonces
                    if tx_from == web3.Web3().toChecksumAddress(alex_bot.address):
                        print("self TX detected!")
                        if tx_nonce >= nonce:
                            print(f"old nonce: {tx_nonce}")
                            nonce = tx_nonce + 1
                            print(f"new nonce: {nonce}")

                    # calculate maximum fees for pending
                    # type0 and type2 transactions
                    max_gas_price = max(
                        [tx[4] for tx in pending_tx]
                    )
                    max_priority_fee = max(
                        [tx[3] for tx in pending_tx]
                    )


        except Exception as e:
            print("reconnecting...")
            print(e)

async def main():

    '''
    main async loop that create tasks for all of the asynchronous 
    coroutines, then executes them.
    '''

    print("Starting main loops")
    await asyncio.gather(
        asyncio.create_task(process_arbs()),
        asyncio.create_task(renew_subscription()),
        asyncio.create_task(update_pools()),
        asyncio.create_task(watch_new_blocks()),
        asyncio.create_task(watch_pending_transactions()),
        asyncio.create_task(watch_sync_events()),
    )

'''
Setups and imports. This is a large section of code that demonstrates
how to use the pre-built arbitrage helper class

'''

import asyncio
import web3
import json
import websockets
import os
import sys
import time
import requests
import eth_abi
from brownie import accounts, network, Contract
from alex_bot import *
from pprint import pprint
from dotenv import load_dotenv
load_dotenv()

BROWNIE_NETWORK = "moralis-avax-main"
BROWNIE_ACCOUNT = "alex_bot"

# SNOWTRACE_API_KEY = "[redacted]"

SNOWSIGHT_RELAY = "http://tx-propagator.snowsight.chainsight.dev:8081"
SNOWSIGHT_MEMPOOL = "ws://mempool-stream.snowsight.chainsight.dev:8589"

#RPC_URI = (
#    "wss://speedy-nodes-nyc.moralis.io/[redacted]/avalanche/mainnet/ws"
#)

WEBSOCKET_TIMEOUT = 30

ARB_CONTRACT_ADDRESS = "0x286E197B66Fd0f07F73844a66C9de2A0990d55D9"
SNOWSIGHT_CONTRACT_ADDRESS = "0xD9B1ee4AE46d4fe51Eeaf644107f53A37F93352f"

WAVAX_CHAINLINK_PRICE_FEED_ADDRESS = "0x0a77230d17318075983913bc2145db16c7366156"

CRA_CONTRACT_ADDRESS = "0xA32608e873F9DdEF944B24798db69d80Bbb4d1ed"
WAVAX_CONTRACT_ADDRESS = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"
DAI_CONTRACT_ADDRESS = "0xd586e7f844cea2f87f50152665bcbc2c279d8d70"
GOHM_CONTRACT_ADDRESS = "0x321e7092a180bb43555132ec53aaa65a5bf84251"
LINK_CONTRACT_ADDRESS = "0x5947bb275c521040051d82396192181b413227a3"
SPELL_CONTRACT_ADDRESS = "0xCE1bFFBD5374Dac86a2893119683F4911a2F7814"
WBTC_CONTRACT_ADDRESS = "0x50b7545627a5162f82a992c33b87adc75187b218"
WETH_CONTRACT_ADDRESS = "0x49d5c2bdffac6ce2bfdb6640f4f80f226bc10bab"
TUS_CONTRACT_ADDRESS = "0xf693248f96fe03422fea95ac0afbbbc4a8fdd172"
SAVAX_CONTRACT_ADDRESS = "0x2b2C81e08f1Af8835a78Bb2A90AE924ACE0eA4bE"

TRADERJOE_LP_PIGGY_WAVAX_ADDRESS = "0x2440885843d8e9f16a4b64933354d1CfBCf7F180"
TRADERJOE_LP_PSHARE_WAVAX_ADDRESS = "0x40128a19F97cb09f13cc370909fC82E69Bccabb1"
TRADERJOE_LP_PIGGY_PSHARE_ADDRESS = "0x1b94828cdaecb3bf6e9569867de702168d363f6d"

TRADERJOE_LP_DAI_WAVAX_ADDRESS = "0x87dee1cc9ffd464b79e058ba20387c1984aed86a"
PANGOLIN_LP_DAI_WAVAX_ADDRESS = "0xbA09679Ab223C6bdaf44D45Ba2d7279959289AB0"
SUSHISWAP_LP_DAI_WAVAX_ADDRESS = "0x55cf10bfbc6a9deaeb3c7ec0dd96d3c1179cb948"

TRADERJOE_LP_SAVAX_WAVAX_ADDRESS = "0x4b946c91c2b1a7d7c40fb3c130cdfbaf8389094d"
PANGOLIN_LP_SAVAX_WAVAX_ADDRESS = "0x4e9a38f05c38106c1cf5c145df24959ec50ff70d"

TRADERJOE_LP_GOHM_WAVAX_ADDRESS = "0xb674f93952f02f2538214d4572aa47f262e990ff"
PANGOLIN_LP_GOHM_WAVAX_ADDRESS = "0xb68f4e8261a4276336698f5b11dc46396cf07a22"
SUSHISWAP_LP_GOHM_WAVAX_ADDRESS = "0xf642f80655c63f687eaf12838ceaf2909d31ef52"

TRADERJOE_LP_WETH_WAVAX_ADDRESS = "0xFE15c2695F1F920da45C30AAE47d11dE51007AF9"
SUSHISWAP_LP_WETH_WAVAX_ADDRESS = "0x2FdE1c280a623950b10b6483B9a0C23549c9B515"
PANGOLIN_LP_WETH_WAVAX_ADDRESS = "0x7c05d54fc5CB6e4Ad87c6f5db3b807C94bB89c52"

TRADERJOE_LP_SPELL_WAVAX_ADDRESS = "0x62cf16BF2BC053E7102E2AC1DEE5029b94008d99"
SUSHISWAP_LP_SPELL_WAVAX_ADDRESS = "0x7782ea0303cd51f65eE1eB52770185BbC937C8F8"
PANGOLIN_LP_SPELL_WAVAX_ADDRESS = "0xD4CBC976E1a1A2bf6F4FeA86DEB3308d68638211"

TRADERJOE_LP_WBTC_WAVAX_ADDRESS = "0xd5a37dc5c9a396a03dd1136fc76a1a02b1c88ffa"
SUSHISWAP_LP_WBTC_WAVAX_ADDRESS = "0xe1051d42f623a1794d347aebeb2612bed2ffc667"
PANGOLIN_LP_WBTC_WAVAX_ADDRESS = "0x5764b8d8039c6e32f1e5d8de8da05ddf974ef5d3"

TRADERJOE_LP_LINK_WAVAX_ADDRESS = "0x6f3a0c89f611ef5dc9d96650324ac633d02265d3"
PANGOLIN_LP_LINK_WAVAX_ADDRESS = "0x5875c368cddd5fb9bf2f410666ca5aad236dabd4"

TRADERJOE_LP_CRA_WAVAX_ADDRESS = "0x140cac5f0e05cbec857e65353839fddd0d8482c1"
PANGOLIN_LP_CRA_WAVAX_ADDRESS = "0x960fa242468746c59bc32513e2e1e1c24fdfaf3f"
SUSHISWAP_LP_CRA_WAVAX_ADDRESS = "0xc873c6f57f7304a840b2341778e6ff0df6a0b7b9"
TRADERJOE_LP_CRA_TUS_ADDRESS = "0x21889033414f652f0fd0e0f60a3fc0221d870ee4"
TRADERJOE_LP_TUS_WAVAX_ADDRESS = "0x565d20bd591b00ead0c927e4b6d7dd8a33b0b319"
PANGOLIN_LP_TUS_WAVAX_ADDRESS = "0xbced3b6d759b9ca8fc7706e46aa81627b2e9eae8"

TRADERJOE_LP_SAVAX_WAVAX_ADDRESS = "0x4b946c91c2b1a7d7c40fb3c130cdfbaf8389094d"
PANGOLIN_LP_SAVAX_WAVAX_ADDRESS = "0x4e9a38f05c38106c1cf5c145df24959ec50ff70d"

ESTIMATED_GAS_USE = 300_000
TX_GAS_LIMIT = 750_000
MIN_PROFIT_MULTIPLIER = 1.25  # minimum profit compared to the expected fee

# set this to False when you want to broadcast real TX
DRY_RUN = True

#os.environ["SNOWTRACE_TOKEN"] = SNOWTRACE_API_KEY

try:
    network.connect(BROWNIE_NETWORK)
except:
    sys.exit(
        "Could not connect! Verify your Brownie network settings using 'brownie networks list'"
    )

try:
    alex_bot = accounts.load(BROWNIE_ACCOUNT)
except:
    sys.exit(
        "Could not load account! Verify your Brownie account settings using 'brownie accounts list'"
    )

arb_contract = Contract.from_abi(
    name="",
    address=ARB_CONTRACT_ADDRESS,
    abi=json.loads(
        """
        [{"stateMutability": "nonpayable", "type": "constructor", "inputs": [], "outputs": []}, {"stateMutability": "nonpayable", "type": "function", "name": "withdraw", "inputs": [{"name": "token_address", "type": "address"}, {"name": "token_amount", "type": "uint256"}], "outputs": []}, {"stateMutability": "nonpayable", "type": "function", "name": "start_flash_borrow_to_lp_swap", "inputs": [{"name": "flash_borrow_pool_address", "type": "address"}, {"name": "flash_borrow_token_amounts", "type": "uint256[]"}, {"name": "flash_repay_token_amount", "type": "uint256"}, {"name": "swap_pool_addresses", "type": "address[]"}, {"name": "swap_pool_amounts", "type": "uint256[][]"}], "outputs": []}, {"stateMutability": "nonpayable", "type": "function", "name": "start_transfer_to_lp_swap", "inputs": [{"name": "swap_token_address", "type": "address"}, {"name": "swap_token_amount", "type": "uint256"}, {"name": "swap_pool_addresses", "type": "address[]"}, {"name": "swap_pool_amounts", "type": "uint256[][]"}], "outputs": []}, {"stateMutability": "nonpayable", "type": "function", "name": "joeCall", "inputs": [{"name": "_sender", "type": "address"}, {"name": "_amount0", "type": "uint256"}, {"name": "_amount1", "type": "uint256"}, {"name": "_data", "type": "bytes"}], "outputs": []}, {"stateMutability": "nonpayable", "type": "function", "name": "uniswapV2Call", "inputs": [{"name": "_sender", "type": "address"}, {"name": "_amount0", "type": "uint256"}, {"name": "_amount1", "type": "uint256"}, {"name": "_data", "type": "bytes"}], "outputs": []}, {"stateMutability": "nonpayable", "type": "function", "name": "pangolinCall", "inputs": [{"name": "_sender", "type": "address"}, {"name": "_amount0", "type": "uint256"}, {"name": "_amount1", "type": "uint256"}, {"name": "_data", "type": "bytes"}], "outputs": []}]
        """
    ),
)

cra = Erc20Token(address=CRA_CONTRACT_ADDRESS)
wavax = Erc20Token(address=WAVAX_CONTRACT_ADDRESS)
tus = Erc20Token(address=TUS_CONTRACT_ADDRESS)
spell = Erc20Token(address=SPELL_CONTRACT_ADDRESS)
gohm = Erc20Token(address=GOHM_CONTRACT_ADDRESS)
link = Erc20Token(address=LINK_CONTRACT_ADDRESS)
weth = Erc20Token(address=WETH_CONTRACT_ADDRESS)
dai = Erc20Token(address=DAI_CONTRACT_ADDRESS)
wbtc = Erc20Token(address=WBTC_CONTRACT_ADDRESS)
savax = Erc20Token(address=SAVAX_CONTRACT_ADDRESS)

try:
    LP_ABI = brownie.Contract(TRADERJOE_LP_CRA_WAVAX_ADDRESS).abi
except:
    LP_ABI = brownie.Contract.from_explorer(TRADERJOE_LP_CRA_WAVAX_ADDRESS).abi

lp_addresses = [
    TRADERJOE_LP_CRA_WAVAX_ADDRESS,
    PANGOLIN_LP_CRA_WAVAX_ADDRESS,
    SUSHISWAP_LP_CRA_WAVAX_ADDRESS,
    TRADERJOE_LP_TUS_WAVAX_ADDRESS,
    PANGOLIN_LP_TUS_WAVAX_ADDRESS,
    TRADERJOE_LP_CRA_TUS_ADDRESS,
    TRADERJOE_LP_DAI_WAVAX_ADDRESS,
    PANGOLIN_LP_DAI_WAVAX_ADDRESS,
    SUSHISWAP_LP_DAI_WAVAX_ADDRESS,
    TRADERJOE_LP_GOHM_WAVAX_ADDRESS,
    PANGOLIN_LP_GOHM_WAVAX_ADDRESS,
    TRADERJOE_LP_WETH_WAVAX_ADDRESS,
    SUSHISWAP_LP_WETH_WAVAX_ADDRESS,
    PANGOLIN_LP_WETH_WAVAX_ADDRESS,
    TRADERJOE_LP_SPELL_WAVAX_ADDRESS,
    SUSHISWAP_LP_SPELL_WAVAX_ADDRESS,
    PANGOLIN_LP_SPELL_WAVAX_ADDRESS,
    TRADERJOE_LP_WBTC_WAVAX_ADDRESS,
    SUSHISWAP_LP_WBTC_WAVAX_ADDRESS,
    PANGOLIN_LP_WBTC_WAVAX_ADDRESS,
    TRADERJOE_LP_LINK_WAVAX_ADDRESS,
    PANGOLIN_LP_LINK_WAVAX_ADDRESS,
    TRADERJOE_LP_SAVAX_WAVAX_ADDRESS,
    PANGOLIN_LP_SAVAX_WAVAX_ADDRESS,
]

brownie_lps = [
    brownie.Contract.from_abi(name="", address=address, abi=LP_ABI)
    for address in lp_addresses
]

traderjoe_lp_cra_wavax = LiquidityPool(
    address=TRADERJOE_LP_CRA_WAVAX_ADDRESS,
    name="TraderJoe: CRA-WAVAX",
    tokens=[wavax, cra],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_cra_wavax = LiquidityPool(
    address=PANGOLIN_LP_CRA_WAVAX_ADDRESS,
    name="Pangolin: CRA-WAVAX",
    tokens=[wavax, cra],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_tus_wavax = LiquidityPool(
    address=TRADERJOE_LP_TUS_WAVAX_ADDRESS,
    name="Pangolin: TUS-WAVAX",
    tokens=[wavax, tus],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_tus_wavax = LiquidityPool(
    address=PANGOLIN_LP_TUS_WAVAX_ADDRESS,
    name="Pangolin: TUS-WAVAX",
    tokens=[wavax, tus],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_cra_tus = LiquidityPool(
    address=TRADERJOE_LP_CRA_TUS_ADDRESS,
    name="Pangolin: CRA-TUS",
    tokens=[cra, tus],
    update_method="external",
    fee=Fraction(3, 1000),
)

sushiswap_lp_spell_wavax = LiquidityPool(
    address=SUSHISWAP_LP_SPELL_WAVAX_ADDRESS,
    name="SushiSwap: SPELL-WAVAX",
    tokens=[wavax, spell],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_spell_wavax = LiquidityPool(
    address=TRADERJOE_LP_SPELL_WAVAX_ADDRESS,
    name="TraderJoe: SPELL-WAVAX",
    tokens=[wavax, spell],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_spell_wavax = LiquidityPool(
    address=PANGOLIN_LP_SPELL_WAVAX_ADDRESS,
    name="Pangolin: SPELL-WAVAX",
    tokens=[wavax, spell],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_gohm_wavax = LiquidityPool(
    address=TRADERJOE_LP_GOHM_WAVAX_ADDRESS,
    name="TraderJoe: gOHM-WAVAX",
    tokens=[wavax, gohm],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_gohm_wavax = LiquidityPool(
    address=PANGOLIN_LP_GOHM_WAVAX_ADDRESS,
    name="Pangolin: gOHM-WAVAX",
    tokens=[wavax, gohm],
    update_method="external",
    fee=Fraction(3, 1000),
)


sushiswap_lp_weth_wavax = LiquidityPool(
    address=SUSHISWAP_LP_WETH_WAVAX_ADDRESS,
    name="SushiSwap: WETH-WAVAX",
    tokens=[wavax, weth],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_weth_wavax = LiquidityPool(
    address=TRADERJOE_LP_WETH_WAVAX_ADDRESS,
    name="TraderJoe: WETH-WAVAX",
    tokens=[wavax, weth],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_weth_wavax = LiquidityPool(
    address=PANGOLIN_LP_WETH_WAVAX_ADDRESS,
    name="Pangolin: WETH-WAVAX",
    tokens=[wavax, weth],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_link_wavax = LiquidityPool(
    address=TRADERJOE_LP_LINK_WAVAX_ADDRESS,
    name="TraderJoe: LINK-WAVAX",
    tokens=[wavax, link],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_link_wavax = LiquidityPool(
    address=PANGOLIN_LP_LINK_WAVAX_ADDRESS,
    name="Pangolin: LINK-WAVAX",
    tokens=[wavax, link],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_dai_wavax = LiquidityPool(
    address=TRADERJOE_LP_DAI_WAVAX_ADDRESS,
    name="TraderJoe: DAI-WAVAX",
    tokens=[wavax, dai],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_dai_wavax = LiquidityPool(
    address=PANGOLIN_LP_DAI_WAVAX_ADDRESS,
    name="Pangolin: DAI-WAVAX",
    tokens=[wavax, dai],
    update_method="external",
    fee=Fraction(3, 1000),
)

sushiswap_lp_dai_wavax = LiquidityPool(
    address=SUSHISWAP_LP_DAI_WAVAX_ADDRESS,
    name="SushiSwap: DAI-WAVAX",
    tokens=[wavax, dai],
    update_method="external",
    fee=Fraction(3, 1000),
)

sushiswap_lp_wbtc_wavax = LiquidityPool(
    address=SUSHISWAP_LP_WBTC_WAVAX_ADDRESS,
    name="SushiSwap: WBTC-WAVAX",
    tokens=[wavax, wbtc],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_wbtc_wavax = LiquidityPool(
    address=TRADERJOE_LP_WBTC_WAVAX_ADDRESS,
    name="TraderJoe: WBTC-WAVAX",
    tokens=[wavax, wbtc],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_wbtc_wavax = LiquidityPool(
    address=PANGOLIN_LP_WBTC_WAVAX_ADDRESS,
    name="Pangolin: WBTC-WAVAX",
    tokens=[wavax, wbtc],
    update_method="external",
    fee=Fraction(3, 1000),
)

traderjoe_lp_savax_wavax = LiquidityPool(
    address=TRADERJOE_LP_SAVAX_WAVAX_ADDRESS,
    name="TraderJoe: sAVAX-WAVAX",
    tokens=[wavax, savax],
    update_method="external",
    fee=Fraction(3, 1000),
)

pangolin_lp_savax_wavax = LiquidityPool(
    address=PANGOLIN_LP_SAVAX_WAVAX_ADDRESS,
    name="Pangolin: sAVAX-WAVAX",
    tokens=[wavax, savax],
    update_method="external",
    fee=Fraction(3, 1000),
)

alex_bot_lps = [
    # CRA-WAVAX arb
    traderjoe_lp_cra_wavax,
    pangolin_lp_cra_wavax,
    # TUS-WAVAX arb
    traderjoe_lp_tus_wavax,
    pangolin_lp_tus_wavax,
    # TUS-CRA-WAVAX arb
    traderjoe_lp_cra_tus,
    # SPELL-WAVAX arb
    sushiswap_lp_spell_wavax,
    traderjoe_lp_spell_wavax,
    pangolin_lp_spell_wavax,
    # gOHM-WAVAX arb
    traderjoe_lp_gohm_wavax,
    pangolin_lp_gohm_wavax,
    # WETH-WAVAX arb
    traderjoe_lp_weth_wavax,
    sushiswap_lp_weth_wavax,
    pangolin_lp_weth_wavax,
    # LINK-WAVAX arb
    traderjoe_lp_link_wavax,
    pangolin_lp_link_wavax,
    # DAI-WAVAX arb
    traderjoe_lp_dai_wavax,
    sushiswap_lp_dai_wavax,
    pangolin_lp_dai_wavax,
    # WBTC-WAVAX arb
    traderjoe_lp_wbtc_wavax,
    sushiswap_lp_wbtc_wavax,
    pangolin_lp_wbtc_wavax,
    # SAVAX-WAVAX arb
    traderjoe_lp_savax_wavax,
    pangolin_lp_savax_wavax,
]

alex_bot_arbs = [
    # CRA-WAVAX arb
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_cra_wavax,
        borrow_token=cra,
        repay_token=wavax,
        swap_pools=[
            pangolin_lp_cra_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_cra_wavax,
        borrow_token=cra,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_wavax,
        ],
        update_method="external",
    ),
    # TUS-WAVAX arb
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_tus_wavax,
        borrow_token=tus,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_tus_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_tus_wavax,
        borrow_token=tus,
        repay_token=wavax,
        swap_pools=[
            pangolin_lp_tus_wavax,
        ],
        update_method="external",
    ),
    # TUS-CRA-WAVAX arb
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_tus_wavax,
        borrow_token=tus,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_tus,
            traderjoe_lp_cra_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_tus_wavax,
        borrow_token=tus,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_tus,
            pangolin_lp_cra_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_tus_wavax,
        borrow_token=tus,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_tus,
            pangolin_lp_cra_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_tus_wavax,
        borrow_token=tus,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_tus,
            traderjoe_lp_cra_wavax,
        ],
        update_method="external",
    ),
    # SPELL-WAVAX arbs
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_spell_wavax,
        borrow_token=spell,
        repay_token=wavax,
        swap_pools=[pangolin_lp_spell_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_spell_wavax,
        borrow_token=spell,
        repay_token=wavax,
        swap_pools=[sushiswap_lp_spell_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_spell_wavax,
        borrow_token=spell,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_spell_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_spell_wavax,
        borrow_token=spell,
        repay_token=wavax,
        swap_pools=[sushiswap_lp_spell_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=sushiswap_lp_spell_wavax,
        borrow_token=spell,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_spell_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=sushiswap_lp_spell_wavax,
        borrow_token=spell,
        repay_token=wavax,
        swap_pools=[pangolin_lp_spell_wavax],
        update_method="external",
    ),
    # WETH-WAVAX arbs
    FlashBorrowToLpSwapNew(
        borrow_pool=sushiswap_lp_weth_wavax,
        borrow_token=weth,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_weth_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=sushiswap_lp_weth_wavax,
        borrow_token=weth,
        repay_token=wavax,
        swap_pools=[pangolin_lp_weth_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_weth_wavax,
        borrow_token=weth,
        repay_token=wavax,
        swap_pools=[sushiswap_lp_weth_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_weth_wavax,
        borrow_token=weth,
        repay_token=wavax,
        swap_pools=[pangolin_lp_weth_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_weth_wavax,
        borrow_token=weth,
        repay_token=wavax,
        swap_pools=[sushiswap_lp_weth_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_weth_wavax,
        borrow_token=weth,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_weth_wavax],
        update_method="external",
    ),
    # LINK-WAVAX arbs
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_link_wavax,
        borrow_token=link,
        repay_token=wavax,
        swap_pools=[pangolin_lp_link_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_link_wavax,
        borrow_token=link,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_link_wavax],
        update_method="external",
    ),
    # DAI-WAVAX arbs
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_dai_wavax,
        borrow_token=dai,
        repay_token=wavax,
        swap_pools=[pangolin_lp_dai_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_dai_wavax,
        borrow_token=dai,
        repay_token=wavax,
        swap_pools=[sushiswap_lp_dai_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_dai_wavax,
        borrow_token=dai,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_dai_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_dai_wavax,
        borrow_token=dai,
        repay_token=wavax,
        swap_pools=[sushiswap_lp_dai_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=sushiswap_lp_dai_wavax,
        borrow_token=dai,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_dai_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=sushiswap_lp_dai_wavax,
        borrow_token=dai,
        repay_token=wavax,
        swap_pools=[pangolin_lp_dai_wavax],
        update_method="external",
    ),
    # WBTC-WAVAX arbs
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_wbtc_wavax,
        borrow_token=wbtc,
        repay_token=wavax,
        swap_pools=[pangolin_lp_wbtc_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_wbtc_wavax,
        borrow_token=wbtc,
        repay_token=wavax,
        swap_pools=[sushiswap_lp_wbtc_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_wbtc_wavax,
        borrow_token=wbtc,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_wbtc_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_wbtc_wavax,
        borrow_token=wbtc,
        repay_token=wavax,
        swap_pools=[sushiswap_lp_wbtc_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=sushiswap_lp_wbtc_wavax,
        borrow_token=wbtc,
        repay_token=wavax,
        swap_pools=[traderjoe_lp_wbtc_wavax],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=sushiswap_lp_wbtc_wavax,
        borrow_token=wbtc,
        repay_token=wavax,
        swap_pools=[pangolin_lp_wbtc_wavax],
        update_method="external",
    ),
    # SAVAX-WAVAX arb
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_savax_wavax,
        borrow_token=savax,
        repay_token=wavax,
        swap_pools=[
            pangolin_lp_savax_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_savax_wavax,
        borrow_token=savax,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_savax_wavax,
        ],
        update_method="external",
    ),
]

# Arb helpers have update_method set to "external", so
# the initial arbitrage dictionary is unpopulated.
# Call update_reserves() to perform the first arbitrage
# calculation immediately
for arb in alex_bot_arbs:
    arb.update_reserves()

last_base_fee = brownie.chain.base_fee
newest_block = brownie.chain.height
newest_block_timestamp = time.time()
pool_update_queue = []
pending_tx = []
max_priority_fee = 0
max_gas_price = 0
nonce = alex_bot.nonce
status_new_blocks = False
status_sync_events = False

# run the main function which starts the bot
asyncio.run(main())
