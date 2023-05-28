'''
modify snowsight_arb.py to exclusively capture mempool TX
backrunning arbitrage. This will watch the same token pair,
CRA-WAVAX. We can make the configuration more generic and 
search for arbitrary token pairs in the future. Changes:

- Subscription renewal
- Pool Updater
- Arbitrage Transaction Submitter
- New Block Watcher
- Pending TX Watcher
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
from dotenv import load_dotenv
load_dotenv()

ROUTERS = {
    web3.Web3().toChecksumAddress("0x60aE616a2155Ee3d9A68541Ba4544862310933d4"): {
        "name": "TraderJoe",
        "abi": [],
    },
    web3.Web3().toChecksumAddress("0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506"): {
        "name": "SushiSwap",
        "abi": [],
    },
    web3.Web3().toChecksumAddress("0xE54Ca86531e17Ef3616d22Ca28b0D458b6C89106"): {
        "name": "Pangolin",
        "abi": [],
    },
}

BROWNIE_NETWORK = "moralis-avax-main"
BROWNIE_ACCOUNT = "alex_bot"

#SNOWTRACE_API_KEY = "[redacted]"

SNOWSIGHT_RELAY = "http://tx-propagator.snowsight.chainsight.dev:8081"
SNOWSIGHT_MEMPOOL = "ws://mempool-stream.snowsight.chainsight.dev:8589"

#RPC_URI = (
#    "wss://speedy-nodes-nyc.moralis.io/[redacted]/avalanche/mainnet/ws"
#)

WEBSOCKET_TIMEOUT = 60

ARB_CONTRACT_ADDRESS = "0x286E197B66Fd0f07F73844a66C9de2A0990d55D9"
SNOWSIGHT_CONTRACT_ADDRESS = "0x727Dc3C412cCb942c6b5f220190ebAB3eFE0Eb93"

CRA_CONTRACT_ADDRESS = "0xA32608e873F9DdEF944B24798db69d80Bbb4d1ed"
WAVAX_CONTRACT_ADDRESS = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"
TUS_CONTRACT_ADDRESS = "0xf693248f96fe03422fea95ac0afbbbc4a8fdd172"

TRADERJOE_LP_CRA_WAVAX_ADDRESS = "0x140cac5f0e05cbec857e65353839fddd0d8482c1"
PANGOLIN_LP_CRA_WAVAX_ADDRESS = "0x960fa242468746c59bc32513e2e1e1c24fdfaf3f"
TRADERJOE_LP_CRA_TUS_ADDRESS = "0x21889033414f652f0fd0e0f60a3fc0221d870ee4"
TRADERJOE_LP_TUS_WAVAX_ADDRESS = "0x565d20bd591b00ead0c927e4b6d7dd8a33b0b319"
PANGOLIN_LP_TUS_WAVAX_ADDRESS = "0xbced3b6d759b9ca8fc7706e46aa81627b2e9eae8"
TRADERJOE_LP_TUS_WAVAX_ADDRESS = "0x565d20bd591b00ead0c927e4b6d7dd8a33b0b319"
PANGOLIN_LP_TUS_WAVAX_ADDRESS = "0xbced3b6d759b9ca8fc7706e46aa81627b2e9eae8"

ESTIMATED_GAS_USE = 325_000
TX_GAS_LIMIT = 750_000
MIN_PROFIT_MULTIPLIER = 1.25  # minimum profit compared to the expected fee

SNOWSIGHT_TIER = "premium"
SNOWSIGHT_TIME = 60 * 60 * 24 * 3  # subscription block in seconds

DRY_RUN = False

VERBOSE_BLOCKS = False
VERBOSE_MEMPOOL_GAS = False
VERBOSE_TIMING = False


async def renew_subscription():
    '''
    async function that blocks only when a renewal is necessary.
    Retrieves data from the Snowsight contract, calculates the max payment,
    and submits a TX through the Chainsight relay. This function is 
    async but the snowsight payment blocks the event loop until
    the TX is confirmed.
    '''

    print("Starting subscription renewal loop")
    global newest_block_timestamp
    global status_new_blocks

    _snowsight_tiers = {
        "trial": 0,
        "standard": 1,
        "premium": 2,
    }

    try:
        snowsight_contract = brownie.Contract(SNOWSIGHT_CONTRACT_ADDRESS)
    except:
        snowsight_contract = brownie.Contract.from_explorer(SNOWSIGHT_CONTRACT_ADDRESS)

    renewal_timestamp = snowsight_contract.payments(
        alex_bot.address,
        _snowsight_tiers[SNOWSIGHT_TIER],
    )[-1]

    while True:

        await asyncio.sleep(0)

        # delay until we're receiving new blocks
        # (maintains newest_block_timestamp)
        if not status_new_blocks:
            continue

        # renew credit if we're within 600 seconds of expiration
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
            except Exception as e:
                print(e)
                continue

        else:
            await asyncio.sleep(0)

async def watch_new_blocks():

    '''
    Watches the websocket for new blocks, updates the base fee
    for the last block, and prints a status update of the
    current maximum gas fees in the mempool

    Very lightly modified, now it maintains a global variable 
    newest_block_timestamp. This timestamp is read by the 
    subscription renewal function.
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

                if VERBOSE_TIMING:
                    print("starting watch_new_blocks")
                    start = time.monotonic()

                status_new_blocks = True

                # message dictionary keys available:
                # 'parentHash',
                # 'sha3Uncles',
                # 'miner',
                # 'stateRoot',
                # 'transactionsRoot',
                # 'receiptsRoot',
                # 'logsBloom',
                # 'difficulty',
                # 'number',
                # 'gasLimit',
                # 'gasUsed',
                # 'timestamp',
                # 'extraData',
                # 'mixHash',
                # 'nonce',
                # 'extDataHash',
                # 'baseFeePerGas',
                # 'extDataGasUsed',
                # 'blockGasCost',
                # 'hash'

                newest_block = int(
                    message.get("params").get("result").get("number"),
                    16,
                )
                newest_block_timestamp = int(
                    message.get("params").get("result").get("timestamp"),
                    16,
                )
                last_base_fee = int(
                    message.get("params").get("result").get("baseFeePerGas"),
                    16,
                )

                if VERBOSE_BLOCKS:
                    print(
                        f"[{newest_block}] "
                        + f"base: {int(last_base_fee/(10**9))} / "
                        + f"type0: {int(max_gas_price/(10**9))} / "
                        + f"type2: {int(max_priority_fee/(10**9))} / "
                        + f"pending: {len(pending_tx)}"
                    )

                if VERBOSE_TIMING:
                    print(
                        f"watch_new_blocks completed in {time.monotonic() - start:0.4f}s"
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
    very similar to the previous version, except it no longer keeps 
    the arbitrage helper objects updated after every new sync event. 
    This is handled by the pending TX watcher.
    '''

    print("Starting pool update loop")
    global pool_update_queue

    while True:

        # yield to the event loop, check the timestamp
        # (index 0) of the most recent entry in
        # pool_update_queue, then process the entire queue
        # when the latest update was received greater than
        # some interval ago

        await asyncio.sleep(0)

        if pool_update_queue and (
            time.monotonic() - pool_update_queue[-1][0] > 0.05
        ):
            pass
        else:
            continue

        if VERBOSE_TIMING:
            print("starting update_pools")
            start = time.monotonic()

        # pool_update_queue items have this format:
        # [
        #     index 0: event_timestamp,
        #     index 1: event_address,
        #     index 2: event_block,
        #     index 3: event_reserves,
        # }

        # identify all relevant pool addresses from the queue, eliminating duplicates
        updated_pool_addresses = list(set([update[1] for update in pool_update_queue]))
        for address in updated_pool_addresses:
            # process only the reserves for the newest sync event
            reserves0, reserves1 = [
                update[3] for update in pool_update_queue if update[1] == address
            ][-1]
            # TODO: could this be improved? loops within loops will become a bottleneck
            for lp in [lp for lp in alex_bot_lps if lp.address == address]:
                lp.update_reserves(
                    external_token0_reserves=reserves0,
                    external_token1_reserves=reserves1,
                    silent=False,
                    print_ratios=False,
                    print_reserves=True,
                )

        pool_update_queue.clear()

        if VERBOSE_TIMING:
            print(f"update_pools completed in {time.monotonic() - start:0.4f}s")

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

async def send_mempool_arb_via_relay(
    arb,
    nonce: int,
    gas_params: dict,
):
    
    '''
    major difference here is that the arbitrage relay function 
    accepts a dictionary of gas parameters, instead of calculating 
    it based on the mempool min/average/max.
    '''

    if VERBOSE_TIMING:
        start = time.monotonic()
        print("starting send_mempool_arb_via_relay")

    signed_message = alex_bot.sign_defunct_message(
        message="Sign this message to authenticate your wallet with Snowsight."
    )

    print(
        f"OPPORTUNITY at block {newest_block}: profit {arb.best['profit_amount']/(10**18):.4f} {arb.repay_token}"
    )

    print(f"Borrow Pool: {arb.borrow_pool.address}")
    print(f"LP Path: {arb.swap_pool_addresses}")
    print(f"Borrow Amounts: {arb.best['borrow_pool_amounts']}")
    print(f"Repay Amount: {arb.best['repay_amount']}")
    print(f"Swap Amounts: {arb.best['swap_pool_amounts']}")

    tx_params = {
        "from": alex_bot.address,
        "chainId": 43114,
        "gas": TX_GAS_LIMIT,
        "nonce": nonce,
    }

    tx_params.update(gas_params)

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
        .buildTransaction(tx_params)
    )

    # sign the TX with the bot's private key
    signed_tx = web3.Web3().eth.account.sign_transaction(
        tx,
        alex_bot.private_key,
    )

    # send the TX depending on simulation flags and results
    if not DRY_RUN:
        # submit the raw TX to the Chainsight propagator
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
        print("*** ARB PLACEHOLDER ***")
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

    if VERBOSE_TIMING:
        print(f"send_arb_via_relay completed in {time.monotonic() - start:0.4f}s")

async def watch_pending_transactions():

    '''
    This function now performs several new tasks:
    - Watches the mempool for TXs going to a router address watchlist 
        and with certain token addresses
    - For any TX meeting our watchlist criteria, uses web3 & ABI to 
        decode the function inputs
    - For all predictable swap functions, translate the function 
        inputs to swap amounts (either in or out)
    - Using these swap amounts, simulate the future state of the 
        associated token pool
    - Calculate arbitrage opportunity using this future state
    - If an arbitrage opportunity is found meeting a minimum threshold, 
        submit it to the TX propagator relay
    
    See each section for details on the changes.
    '''

    '''
    Main Watcher Section — very similar to snowsight_arb.py. The 
    only modifications are to the websocket status monitor, 
    updated to reflect the new subscription tiers (trial/standard/
    premium).
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

    async for websocket in websockets.connect(uri=SNOWSIGHT_MEMPOOL):

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
                await asyncio.wait_for(
                    websocket.recv(),
                    timeout=WEBSOCKET_TIMEOUT,
                ),
            )
            print(resp)

            # if the service thinks we're connected or authenticated, sleep and retry later
            if resp["status"] in ["already connected", "unauthenticated"]:
                continue

            elif resp["status"] in ["trial", "standard", "premium"]:

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
                        tx for tx in pending_tx if time.monotonic() - tx[1] <= 60
                    ]

                    tx_timestamp = time.monotonic()
                    tx_hash = tx_message.get("hash")
                    tx_to = web3.Web3().toChecksumAddress(tx_message.get("to"))
                    tx_from = web3.Web3().toChecksumAddress(tx_message.get("from"))
                    tx_value = int(tx_message.get("value"), 16)
                    tx_input = tx_message.get("input")
                    tx_type = tx_message.get("txType")
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

                    # The Chainsight websocket sends duplicate notifications for confirmed
                    # transactions, so check if this TX has already been seen.
                    #
                    # If the TX is new, add it to the queue with a timestamp.
                    # If the TX is old, remove it from the queue and skip post-processing
                    if tx_hash not in [tx[0] for tx in pending_tx]:
                        pending_tx.append(
                            [
                                tx_hash,
                                tx_timestamp,
                                tx_max_fee,
                                tx_priority_fee,
                                tx_gas_fee,
                            ]
                        )
                    else:
                        # find the position by matching tx_hash, then delete
                        pos = [tx[0] for tx in pending_tx].index(tx_hash)
                        del pending_tx[pos]
                        continue

                    # catch any pending mempool that we've sent, then use the transmitted nonce to update
                    # the global nonce counter. This is faster than asking the RPC each time, since multiple transactions may need to be
                    # sent quickly with consecutive nonce
                    if tx_from == web3.Web3().toChecksumAddress(alex_bot.address):
                        tx_nonce = int(tx_message.get("nonce"), 16)
                        print("self TX detected!")
                        if tx_nonce >= nonce:
                            print(f"old nonce: {tx_nonce}")
                            nonce = tx_nonce + 1
                            print(f"new nonce: {nonce}")

                    # tx items have this format
                    # index 0: tx_hash
                    # index 1: tx_timestamp
                    # index 2: tx_max_fee (type 2)
                    # index 3: tx_priority_fee (type 2)
                    # index 4: tx_gas_fee (type 0)

                    # calculate maximum fees for pending type0 and type2 transactions
                    max_gas_price = max([tx[4] for tx in pending_tx])
                    max_priority_fee = max([tx[3] for tx in pending_tx])

                    '''
                    Transaction Filter section: Here we start to filter 
                    down the mempool TX, “ignoring” anything except swaps 
                    send through the router contracts of TraderJoe, SushiSwap, 
                    and Pangolin. 
                    '''
                    # ignore the TX unless it was sent to an address on our watchlist
                    if tx_to not in ROUTERS.keys():
                        continue
                    else:
                        func, params = (
                            web3.Web3()
                            .eth.contract(
                                address=web3.Web3().toChecksumAddress(tx_to),
                                abi=ROUTERS.get(tx_to).get("abi"),
                            )
                            .decode_function_input(tx_message.get("input"))
                        )

                    # convert addresses to checksummed (comparison is case sensitive)
                    if params.get("path"):
                        tx_path = [
                            web3.Web3().toChecksumAddress(address)
                            for address in params.get("path")
                        ]
                    else:
                        # not a swap we can exploit (yet), skip
                        continue

                    '''
                    CRA-WAVAX Decoder section: begins by filtering only for TXs where
                    tx_path equals  [cra.address, wavax.address] or 
                    [wavax.address, cra.address] exactly. For now we only process 
                    swaps of the type CRA→WAVAX or WAVAX→CRA. 
                    '''
                    if tx_path in [
                        [cra.address, wavax.address],
                        [wavax.address, cra.address],
                    ]:

                        print()
                        print("*** Pending CRA-WAVAX swap! ***")
                        print(f"DEX: {ROUTERS[tx_to]['name']}")
                        print(f"TX: {tx_hash}")
                        print(func.fn_name)

                        if ROUTERS[tx_to]["name"] == "TraderJoe":
                            lp = traderjoe_lp_cra_wavax
                            future_lp = traderjoe_lp_cra_wavax_future
                        elif ROUTERS[tx_to]["name"] == "Pangolin":
                            lp = pangolin_lp_cra_wavax
                            future_lp = pangolin_lp_cra_wavax_future

                        if func.fn_name == "swapExactTokensForAVAX":
                            mempool_tx_token_in = cra
                            mempool_tx_token_out = wavax
                            mempool_tx_token_in_quantity = params.get("amountIn")
                            print(
                                f"In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                            )
                            print(
                                f"Out: {params['amountOutMin']/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                            )
                        elif func.fn_name == "swapTokensForExactAVAX":
                            mempool_tx_token_in = cra
                            mempool_tx_token_out = wavax
                            mempool_tx_token_in_quantity = (
                                lp.calculate_tokens_in_from_tokens_out(
                                    token_in=cra,
                                    token_out_quantity=params.get("amountOut"),
                                )
                            )
                            print(
                                f"In: {params['amountInMax']/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                            )
                            print(
                                f"Min. In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                            )
                            print(
                                f"Out: {params['amountOut']/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                            )
                        elif func.fn_name == "swapAVAXForExactTokens":
                            mempool_tx_token_in = wavax
                            mempool_tx_token_out = cra
                            mempool_tx_token_in_quantity = (
                                lp.calculate_tokens_in_from_tokens_out(
                                    token_in=wavax,
                                    token_out_quantity=params.get("amountOut"),
                                )
                            )
                            print(
                                f"In: {tx_value/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                            )
                            print(
                                f"Min. In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                            )
                            print(
                                f"Out: {params['amountOut']/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                            )
                        elif func.fn_name == "swapExactAVAXForTokens":
                            mempool_tx_token_in = wavax
                            mempool_tx_token_out = cra
                            mempool_tx_token_in_quantity = tx_value
                            print(
                                f"In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                            )
                            print(
                                f"Out: {params['amountOutMin']/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                            )
                        elif func.fn_name == "swapExactTokensForTokens":
                            mempool_tx_token_in_quantity = params.get("amountIn")
                            if params.get("path")[0] == CRA_CONTRACT_ADDRESS:
                                mempool_tx_token_in = cra
                                mempool_tx_token_out = wavax
                            elif params.get("path")[0] == WAVAX_CONTRACT_ADDRESS:
                                mempool_tx_token_in = wavax
                                mempool_tx_token_out = cra
                            else:
                                print()
                                print("WTF HAPPENED?")
                                print()
                            print(
                                f"In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                            )
                            print(
                                f"Out: {params['amountOutMin']/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                            )
                        else:
                            print("ignored")
                            continue

                        '''
                        Gas Processor section — Since we are attempting to backrun, 
                        we need to match the gas of the mempool TX exactly. A TX can 
                        be either type 0 (legacy) or type 2 (EIP-1559), so we use the 
                        TX keys to rebuild the gas settings exactly and build a
                        gas_params dictionary that the arb submission function will use.
                        '''
                        if tx_type == "0x2":
                            arb_gas_cost = ESTIMATED_GAS_USE * tx_max_fee
                            gas_params = {
                                "maxFeePerGas": tx_max_fee,
                                "maxPriorityFeePerGas": tx_priority_fee,
                            }
                            if VERBOSE_MEMPOOL_GAS:
                                print(f"Max Fee (Type 2): {tx_max_fee}")
                                print(f"Priority Fee: {tx_priority_fee}")
                        elif tx_type == "0x0":
                            arb_gas_cost = ESTIMATED_GAS_USE * tx_gas_fee
                            gas_params = {"gasPrice": tx_gas_fee}
                            if VERBOSE_MEMPOOL_GAS:
                                print(f"Gas Price (Type 0): {tx_gas_fee}")
                        '''
                        Swap Simulator section— Here is the first time we the use 
                        the future LP objects. These are “dummy” helpers, intended 
                        to be updated once for the purpose of an arbitrage calculation, 
                        then ignored after. They use the “external” update method, 
                        which relies on an outside source to provide new pool reserves 
                        instead of fetching them directly. Hard-coded token0 as CRA and 
                        token1 as WAVAX. Once this technique is extended to multiple 
                        token types, this will have to be rewritten.
                        '''
                        if mempool_tx_token_in == cra:
                            print("Simulating CRA → WAVAX")
                        elif mempool_tx_token_in == wavax:
                            print("Simulating WAVAX → CRA")

                        # set the future LP reserves before adjusting for the pending TX
                        traderjoe_lp_cra_wavax_future.update_reserves(
                            external_token0_reserves=traderjoe_lp_cra_wavax.reserves_token0,
                            external_token1_reserves=traderjoe_lp_cra_wavax.reserves_token1,
                            silent=True,
                            print_ratios=False,
                            print_reserves=False,
                        )
                        pangolin_lp_cra_wavax_future.update_reserves(
                            external_token0_reserves=pangolin_lp_cra_wavax.reserves_token0,
                            external_token1_reserves=pangolin_lp_cra_wavax.reserves_token1,
                            silent=True,
                            print_ratios=False,
                            print_reserves=False,
                        )

                        # token0 = CRA
                        # token1 = WAVAX
                        # TODO FIX HARDCODED TOKEN0/TOKEN1

                        # predict the pool state after the pending swap confirms
                        if mempool_tx_token_in == cra:
                            future_reserves = (
                                lp.reserves_token0 + mempool_tx_token_in_quantity,
                                lp.reserves_token1
                                - lp.calculate_tokens_out_from_tokens_in(
                                    token_in=mempool_tx_token_in,
                                    token_in_quantity=mempool_tx_token_in_quantity,
                                ),
                            )
                        elif mempool_tx_token_in == wavax:
                            future_reserves = (
                                lp.reserves_token0
                                - lp.calculate_tokens_out_from_tokens_in(
                                    token_in=mempool_tx_token_in,
                                    token_in_quantity=mempool_tx_token_in_quantity,
                                ),
                                lp.reserves_token1 + mempool_tx_token_in_quantity,
                            )

                        future_lp.update_reserves(
                            external_token0_reserves=future_reserves[0],
                            external_token1_reserves=future_reserves[1],
                            silent=False,
                            print_reserves=True,
                            print_ratios=False,
                        )

                        '''
                        Arbitrage Calculator section — Finally, cycle through 
                        the arbitrage helpers for these future states and 
                        execute only the most profitable arb.
                        '''
                        arbs_to_execute = []

                        for arb in alex_bot_future_borrow_arbs:
                            arb.update_reserves()
                            if (
                                arb.best["borrow_amount"]
                                and arb.best["profit_amount"]
                                >= MIN_PROFIT_MULTIPLIER * arb_gas_cost
                            ):

                                print()
                                print(
                                    f"MEMPOOL OPPORTUNITY: profit {arb.best['profit_amount']/(10**18):.4f} {arb.repay_token}"
                                )
                                print()

                                # add the arb to a queue for execution later
                                arbs_to_execute.append(arb)

                        # loop through all profitable arbs to identify the
                        # best one, then submit it
                        if arbs_to_execute:

                            best_arb_profit = 0
                            best_arb = None

                            for arb in arbs_to_execute:
                                if arb.best["profit_amount"] > best_arb_profit:
                                    best_arb_profit = arb.best["profit_amount"]
                                    best_arb = arb

                            await send_mempool_arb(
                                best_arb,
                                # nonce_start + i,
                                nonce,
                                gas_params=gas_params,
        except Exception as e:
            print("reconnecting...")
            print(e)


async def main():

    print("sleeping...")
    await asyncio.sleep(10)

    print("updating all pools")
    for pool in alex_bot_lps:
        pool.update_reserves(
            override_update_method="polling",
            print_ratios=False,
        )

    print("Starting main loops")
    await asyncio.gather(
        asyncio.create_task(renew_subscription()),
        asyncio.create_task(update_pools()),
        asyncio.create_task(watch_new_blocks()),
        asyncio.create_task(watch_pending_transactions()),
        asyncio.create_task(watch_sync_events()),
    )


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

newest_block = brownie.chain.height

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

try:
    LP_ABI = brownie.Contract(TRADERJOE_LP_CRA_WAVAX_ADDRESS).abi
except:
    LP_ABI = brownie.Contract.from_explorer(TRADERJOE_LP_CRA_WAVAX_ADDRESS).abi

lp_addresses = [
    TRADERJOE_LP_CRA_WAVAX_ADDRESS,
    PANGOLIN_LP_CRA_WAVAX_ADDRESS,
    TRADERJOE_LP_TUS_WAVAX_ADDRESS,
    PANGOLIN_LP_TUS_WAVAX_ADDRESS,
    TRADERJOE_LP_CRA_TUS_ADDRESS,
]

# FUTURE LP HELPERS
traderjoe_lp_cra_wavax_future = LiquidityPool(
    address=TRADERJOE_LP_CRA_WAVAX_ADDRESS,
    name="TraderJoe: CRA-WAVAX (FUTURE)",
    tokens=[wavax, cra],
    update_method="external",
)

pangolin_lp_cra_wavax_future = LiquidityPool(
    address=PANGOLIN_LP_CRA_WAVAX_ADDRESS,
    name="Pangolin: CRA-WAVAX (FUTURE)",
    tokens=[wavax, cra],
    update_method="external",
)

# REGULAR LP HELPERS
traderjoe_lp_cra_wavax = LiquidityPool(
    address=TRADERJOE_LP_CRA_WAVAX_ADDRESS,
    name="TraderJoe: CRA-WAVAX",
    tokens=[wavax, cra],
    update_method="external",
)

pangolin_lp_cra_wavax = LiquidityPool(
    address=PANGOLIN_LP_CRA_WAVAX_ADDRESS,
    name="Pangolin: CRA-WAVAX",
    tokens=[wavax, cra],
    update_method="external",
)

traderjoe_lp_cra_tus = LiquidityPool(
    address=TRADERJOE_LP_CRA_TUS_ADDRESS,
    name="TraderJoe: CRA-TUS",
    tokens=[tus, cra],
    update_method="external",
)

traderjoe_lp_tus_wavax = LiquidityPool(
    address=TRADERJOE_LP_TUS_WAVAX_ADDRESS,
    name="TraderJoe: TUS-WAVAX",
    tokens=[wavax, tus],
    update_method="external",
)

pangolin_lp_tus_wavax = LiquidityPool(
    address=PANGOLIN_LP_TUS_WAVAX_ADDRESS,
    name="Pangolin: TUS-WAVAX",
    tokens=[wavax, tus],
    update_method="external",
)

alex_bot_lps = [
    traderjoe_lp_cra_wavax,
    traderjoe_lp_tus_wavax,
    traderjoe_lp_cra_tus,
    pangolin_lp_cra_wavax,
    pangolin_lp_tus_wavax,
]

alex_bot_future_lps = [
    # CRA-WAVAX arb
    traderjoe_lp_cra_wavax_future,
    pangolin_lp_cra_wavax_future,
]

alex_bot_future_borrow_arbs = [
    # CRA-WAVAX arbs (mempool)
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_cra_wavax_future,
        borrow_token=cra,
        repay_token=wavax,
        swap_pools=[
            pangolin_lp_cra_wavax_future,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_cra_wavax_future,
        borrow_token=cra,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_wavax_future,
        ],
        update_method="external",
    ),
    # CRA-TUS-WAVAX arb
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_cra_wavax_future,
        borrow_token=cra,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_tus,
            traderjoe_lp_tus_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=traderjoe_lp_cra_wavax_future,
        borrow_token=cra,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_tus,
            pangolin_lp_tus_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_cra_wavax_future,
        borrow_token=cra,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_tus,
            pangolin_lp_tus_wavax,
        ],
        update_method="external",
    ),
    FlashBorrowToLpSwapNew(
        borrow_pool=pangolin_lp_cra_wavax_future,
        borrow_token=cra,
        repay_token=wavax,
        swap_pools=[
            traderjoe_lp_cra_tus,
            traderjoe_lp_tus_wavax,
        ],
        update_method="external",
    ),
]

# Arb helpers have update_method set to "external", so the initial arbitrage dictionary is
# unpopulated. Call update_reserves() to perform the first arbitrage calculation immediately
for arb in alex_bot_future_borrow_arbs:
    arb.update_reserves()

for address in ROUTERS.keys():
    try:
        ROUTERS[address]["abi"] = brownie.Contract(address).abi
    except:
        ROUTERS[address]["abi"] = brownie.Contract.from_explorer(address).abi

last_base_fee = brownie.chain.base_fee
pool_update_queue = []
pending_tx = []
max_priority_fee = 0
max_gas_price = 0
nonce = alex_bot.nonce
status_new_blocks = False
status_sync_events = False

asyncio.run(main())
