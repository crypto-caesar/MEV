'''
Flashbots — Two-Pool UniV2 Arbitrage Bot: an onchain + 
mempool backrunning arb bot for two-pool UniV2-style 
opportunities. Uses Flashbots relay for execution

This bot works with the ethereum_executor_2.vy smart 
contract, the ethereum_lp_fetcher.py helper, and the arbitrage 
builder (pool_parser_2.py) helper. 

Many code blocks are similar to the Snowsight arb bot, but 
have changes to suit the data returned by geth and the inputs 
required for the new smart contract and Flashbots relay.

Payload ABI Encoder code block is not a standalone function. 
It appears in each of the arb executor functions. Submitting 
payloads to the executor contract requires us to provide 
bytecode in the correct order. 

'''

import asyncio
import itertools
import web3
import json
import websockets
import eth_abi
import os
import sys
import time
import flashbots
import eth_abi
import eth_account
import csv
import concurrent.futures
from brownie import accounts, network, Contract
from alex_bot import *
from dotenv import load_dotenv
load_dotenv()

BROWNIE_NETWORK = "[edit]"
BROWNIE_ACCOUNT = "alex_bot"
FLASHBOTS_IDENTITY_ACCOUNT = "flashbots_id"

MULTICALL_FLUSH_INTERVAL = 250

FLASHBOTS_RELAY_URL = "https://relay.flashbots.net"
WEBSOCKET_URI = "ws://localhost:8546"

#ETHERSCAN_API_KEY = "[edit me]"

ARB_CONTRACT_ADDRESS = "[edit]"
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

ESTIMATED_GAS_USE = 450_000
TX_GAS_LIMIT = 750_000
MIN_PROFIT_ETH = 0.01 * 10**18
MIN_PRIORITY_FEE = 1 * 10**9  # 1 gwei priority fee

MINER_TIP = 0.95  # % of profit to bribe the miner

DRY_RUN = True
SIMULATE_ARB_LOCAL = True
SIMULATE_ARB_RELAY = True

USE_FLASHBOTS_RELAY = True

VERBOSE_BLOCKS = True
VERBOSE_MEMPOOL_GAS = False
VERBOSE_RESERVES = False

SIMULATE_DISCONNECTS = False

ARB_MEMPOOL_ENABLE = True
ARB_ONCHAIN_ENABLE = True

BLACKLISTED_TOKENS = {
    web3.Web3().toChecksumAddress(address)
    for address in [
        "0x9EA3b5b4EC044b70375236A281986106457b20EF",  # swap() disabled on UniswapV2
        "0x043942281890d4876D26BD98E2BB3F662635DFfb",  # ABI not published
    ]
}


async def main():
    try:
        await asyncio.gather(
            asyncio.create_task(watch_new_blocks()),
            asyncio.create_task(watch_sync_events()),
            asyncio.create_task(update_pools()),
            asyncio.create_task(watch_pending_transactions()),
        )
    except Exception as e:
        print(f"main: {e}")


async def execute_multi_tx_arb(
    arb_dict: dict,
    gas_params: dict,
    mempool_tx,
):
    '''
    function is very similar to the standalone arb executor, 
    except it accepts an additional argument for a raw 
    mempool TXs observed from another address. This mempool 
    TX will be bundled first, followed by the payloads from 
    the arbitrage helper, then simulated and transmitted to 
    Flashbots if the profit threshold is met.
    '''

    if last_base_fee > max(
        gas_params.get("maxFeePerGas", 0),
        gas_params.get("gasPrice", 0),
    ):
        # skip all TX that are underpriced relative to the last-known base fee
        return

    print("\n*** MEMPOOL ARB ***")

    tx_params = {
        "from": alex_bot.address,
        "chainId": brownie.chain.id,
        "gas": TX_GAS_LIMIT,
        "nonce": alex_bot.nonce,
    }

    tx_params.update(gas_params)

    token0_borrow, token1_borrow = arb_dict.get("borrow_pool_amounts")
    if token0_borrow:
        borrow_token = alex_bot_lps.get(arb_dict.get("borrow_pool").address).token0
        repay_token = alex_bot_lps.get(arb_dict.get("borrow_pool").address).token1
        borrow_amount = token0_borrow
    elif token1_borrow:
        borrow_token = alex_bot_lps.get(arb_dict.get("borrow_pool").address).token1
        repay_token = alex_bot_lps.get(arb_dict.get("borrow_pool").address).token0
        borrow_amount = token1_borrow
    else:
        print("WTF?")
        return

    swap_token0, swap_token1 = arb_dict.get("swap_pool_amounts")[0]

    # generate payloads for the steps: transfer, swap, repay
    arb_payload = eth_abi.encode_abi(
        [  # start of types iterable
            "(address,bytes,uint256)[]",
        ],  # end of types iterable
        [  # start of values iterable
            [  # start of dynamic array
                (  # start of transfer payload
                    borrow_token.address,
                    # bytes calldata
                    web3.Web3().keccak(text="transfer(address,uint256)")[0:4]
                    + eth_abi.encode_abi(
                        [
                            "address",
                            "uint256",
                        ],
                        [
                            arb_dict.get("swap_pool_addresses")[0],
                            borrow_amount,
                        ],
                    ),
                    # msg.value
                    0,
                ),  # end of transfer payload
                (  # start of swap payload
                    arb_dict.get("swap_pool_addresses")[0],
                    web3.Web3().keccak(text="swap(uint256,uint256,address,bytes)")[0:4]
                    + eth_abi.encode_abi(
                        [
                            "uint256",
                            "uint256",
                            "address",
                            "bytes",
                        ],
                        [
                            swap_token0,
                            swap_token1,
                            arb_contract.address,
                            b"",
                        ],
                    ),
                    0,
                ),  # end of swap payload
                (  # start of repay payload
                    repay_token.address,
                    web3.Web3().keccak(text="transfer(address,uint256)")[0:4]
                    + eth_abi.encode_abi(
                        [
                            "address",
                            "uint256",
                        ],
                        [
                            arb_dict.get("borrow_pool").address,
                            arb_dict.get("repay_amount"),
                        ],
                    ),
                    0,
                ),  # end of repay payload
            ],  # end of dynamic array
        ],  # end of values iterable
    )

    # ABI encode the payload data to pass into the uniswap v2 callback
    packed_payload = web3.Web3().keccak(text="swap(uint256,uint256,address,bytes)")[
        0:4
    ] + eth_abi.encode_abi(
        [
            "uint256",
            "uint256",
            "address",
            "bytes",
        ],
        [
            *arb_dict.get("borrow_pool_amounts"),
            arb_contract.address,
            arb_payload,
        ],
    )

    if SIMULATE_ARB_RELAY:
        transactions_to_bundle = (
            w3.eth.contract(address=arb_contract.address, abi=arb_contract.abi)
            .functions.execute_packed_payload(
                arb_dict.get("borrow_pool").address,
                packed_payload,
                0,  # no bribe
            )
            .buildTransaction(tx_params),
        )

        # bundle the backrun (with the target mempool TX at start)
        bundle = []
        bundle.append({"signed_transaction": mempool_tx})

        for transaction in transactions_to_bundle:
            tx = eth_account.Account.from_key(alex_bot.private_key).sign_transaction(
                transaction
            )
            signed_tx = tx.rawTransaction
            bundle.append({"signed_transaction": signed_tx})

        try:
            simulation = w3.flashbots.simulate(bundle, newest_block)
        except Exception as e:
            print(f"Flashbots simulation error: {e}")
            return
        else:
            for result in simulation.get("results"):
                if result.get("error"):
                    # abort if any TX in the bundle failed simulation
                    # TODO: ignore for any bundle that is *allowed* to fail
                    return

            bundle_hash = simulation.get("bundleHash")
            print(f"bundleHash: {bundle_hash}")

            simulated_gas = simulation.get("totalGasUsed")
            max_gas = simulated_gas * max(
                gas_params.get("maxFeePerGas", 0),
                gas_params.get("gasPrice", 0),
            )
            arb_profit = arb_dict.get("profit_amount") - max_gas
            print("Flashbots simulation results:")
            print(f"Arb Profit = {arb_profit/(10**18):0.5f} ETH")

        if arb_profit > MIN_PROFIT_ETH and not DRY_RUN:

            # set the miner bribe
            bribe = int(MINER_TIP * arb_profit)

            transactions_to_bundle = (
                w3.eth.contract(address=arb_contract.address, abi=arb_contract.abi)
                .functions.execute_packed_payload(
                    arb_dict.get("borrow_pool").address,
                    packed_payload,
                    bribe,  # finalize the bribe
                )
                .buildTransaction(tx_params),
            )

            # bundle the backrun (with the target mempool TX at start)
            bundle = []
            bundle.append({"signed_transaction": mempool_tx})
            for transaction in transactions_to_bundle:
                tx = eth_account.Account.from_key(
                    alex_bot.private_key
                ).sign_transaction(transaction)
                signed_tx = tx.rawTransaction
                bundle.append({"signed_transaction": signed_tx})

            print(f"Sending bundle targeting block {newest_block + 1}")
            try:
                send_bundle_result = w3.flashbots.send_bundle(
                    bundle,
                    target_block_number=newest_block + 1,
                )
            except Exception as e:
                print(e)
            else:
                send_bundle_result.wait()
                print(send_bundle_result)

            try:
                receipts = send_bundle_result.receipts()
            except web3.exceptions.TransactionNotFound:
                print(f"Bundle not found in block {newest_block + 1}")
            else:
                print(f"Bundle was mined in block {receipts[0].blockNumber}\a")
                print(receipts)

                # zero out the arb
                arb_dict.update(
                    {
                        "borrow_amount": 0,
                        "borrow_pool_amounts": [],
                        "repay_amount": 0,
                        "profit_amount": 0,
                        "swap_pool_amounts": [],
                    }
                )
                sys.exit()

    else:
        print()
        print("*** ARB PLACEHOLDER ***")
        print()


async def execute_standalone_arb(
    arb_dict: dict,
    gas_params: dict,
):
    '''
    function takes the gas parameters and a dictionary of 
    arbitrage values from the arb helper object, build a 
    payload, evaluate the potential profit by submitting it 
    to the Flashbots relay for simulation, and then transmit 
    the bundle for inclusion if it meets a profit threshold 
    (measured in ETH).
    '''

    tx_params = {
        "from": alex_bot.address,
        "chainId": brownie.chain.id,
        "gas": TX_GAS_LIMIT,
        "nonce": alex_bot.nonce,
    }

    tx_params.update(gas_params)

    token0_borrow, token1_borrow = arb_dict.get("borrow_pool_amounts")
    if token0_borrow:
        borrow_token = alex_bot_lps.get(arb_dict.get("borrow_pool").address).token0
        repay_token = alex_bot_lps.get(arb_dict.get("borrow_pool").address).token1
        borrow_amount = token0_borrow
    elif token1_borrow:
        borrow_token = alex_bot_lps.get(arb_dict.get("borrow_pool").address).token1
        repay_token = alex_bot_lps.get(arb_dict.get("borrow_pool").address).token0
        borrow_amount = token1_borrow
    else:
        print("WTF?")
        return

    swap_token0, swap_token1 = arb_dict.get("swap_pool_amounts")[0]

    # generate payloads for the steps: transfer, swap, repay
    arb_payload = eth_abi.encode_abi(
        [  # start of types iterable
            "(address,bytes,uint256)[]",
        ],  # end of types iterable
        [  # start of values iterable
            [  # start of dynamic array
                (  # start of transfer payload
                    borrow_token.address,
                    # bytes calldata
                    web3.Web3().keccak(text="transfer(address,uint256)")[0:4]
                    + eth_abi.encode_abi(
                        [
                            "address",
                            "uint256",
                        ],
                        [
                            arb_dict.get("swap_pool_addresses")[0],
                            borrow_amount,
                        ],
                    ),
                    # msg.value
                    0,
                ),  # end of transfer payload
                (  # start of swap payload
                    arb_dict.get("swap_pool_addresses")[0],
                    web3.Web3().keccak(text="swap(uint256,uint256,address,bytes)")[0:4]
                    + eth_abi.encode_abi(
                        [
                            "uint256",
                            "uint256",
                            "address",
                            "bytes",
                        ],
                        [
                            swap_token0,
                            swap_token1,
                            arb_contract.address,
                            b"",
                        ],
                    ),
                    0,
                ),  # end of swap payload
                (  # start of repay payload
                    repay_token.address,
                    web3.Web3().keccak(text="transfer(address,uint256)")[0:4]
                    + eth_abi.encode_abi(
                        [
                            "address",
                            "uint256",
                        ],
                        [
                            arb_dict.get("borrow_pool").address,
                            arb_dict.get("repay_amount"),
                        ],
                    ),
                    0,
                ),  # end of repay payload
            ],  # end of dynamic array
        ],  # end of values iterable
    )

    # ABI encode the payload data to pass into the uniswap v2 callback
    packed_payload = web3.Web3().keccak(text="swap(uint256,uint256,address,bytes)")[
        0:4
    ] + eth_abi.encode_abi(
        [
            "uint256",
            "uint256",
            "address",
            "bytes",
        ],
        [
            *arb_dict.get("borrow_pool_amounts"),
            arb_contract.address,
            arb_payload,
        ],
    )

    print("\n*** ONCHAIN ARB ***")

    if SIMULATE_ARB_LOCAL:
        try:
            print(f"block: {newest_block}")
            print(f'address: {arb_dict.get("borrow_pool").address}')
            print(f'amounts: {arb_dict.get("borrow_pool_amounts")}')
            arb_contract.execute_packed_payload.call(
                arb_dict.get("borrow_pool").address,
                packed_payload,
                0,  # miner bribe
                {"from": alex_bot},
            )
        except Exception as e:
            print(f"Local simulation failed: {e}")
        else:
            print("Local simulation successful")

    if USE_FLASHBOTS_RELAY:
        transactions_to_bundle = (
            w3.eth.contract(address=arb_contract.address, abi=arb_contract.abi)
            .functions.execute_packed_payload(
                arb_dict.get("borrow_pool").address,
                packed_payload,
                0,  # lowest possible bribe
            )
            .buildTransaction(tx_params),
        )

        # bundle the transaction (assumes no mempool TX)
        bundle = []
        for transaction in transactions_to_bundle:
            # sign the web3 transaction and append the raw transaction in the bundle
            tx = eth_account.Account.from_key(alex_bot.private_key).sign_transaction(
                transaction
            )
            signed_tx = tx.rawTransaction
            bundle.append({"signed_transaction": signed_tx})

        try:
            simulation = w3.flashbots.simulate(bundle, newest_block)
        except Exception as e:
            print(f"Flashbots simulation error: {e}")
            return
        else:
            for result in simulation.get("results"):
                if result.get("error"):
                    # abort if any TX in the bundle failed simulation
                    # TODO: ignore for any bundle that is *allowed* to fail
                    return

            bundle_hash = simulation.get("bundleHash")
            print(f"bundleHash: {bundle_hash}")

            simulated_gas = simulation.get("totalGasUsed")
            max_gas = simulated_gas * max(
                gas_params.get("maxFeePerGas", 0),
                gas_params.get("gasPrice", 0),
            )
            arb_profit = arb_dict.get("profit_amount") - max_gas
            print("Flashbots simulation results:")
            print(f"Arb Profit = {arb_profit/(10**18):0.5f} ETH")

        if arb_profit > MIN_PROFIT_ETH and not DRY_RUN:

            # set the miner bribe
            bribe = int(MINER_TIP * arb_profit)

            transactions_to_bundle = (
                w3.eth.contract(address=arb_contract.address, abi=arb_contract.abi)
                .functions.execute_packed_payload(
                    arb_dict.get("borrow_pool").address,
                    packed_payload,
                    bribe,  # finalize the bribe
                )
                .buildTransaction(tx_params),
            )

            # bundle the transaction
            bundle = []
            for transaction in transactions_to_bundle:
                tx = eth_account.Account.from_key(
                    alex_bot.private_key
                ).sign_transaction(transaction)
                signed_tx = tx.rawTransaction
                bundle.append({"signed_transaction": signed_tx})

            # submit the bundle to Flashbots relay, targeting the next block
            print(f"Sending bundle targeting block {newest_block + 1}")
            try:
                send_bundle_result = w3.flashbots.send_bundle(
                    bundle,
                    target_block_number=newest_block + 1,
                )
            except Exception as e:
                print(e)
            else:
                print(send_bundle_result)

            try:
                receipts = send_bundle_result.receipts()
            except web3.exceptions.TransactionNotFound:
                print(f"Bundle not found in block {newest_block + 1}")
            else:
                print(f"Bundle was mined in block {receipts[0].blockNumber}\a")
                print(receipts)

                # zero out the arb
                arb_dict.update(
                    {
                        "borrow_amount": 0,
                        "borrow_pool_amounts": [],
                        "repay_amount": 0,
                        "profit_amount": 0,
                        "swap_pool_amounts": [],
                    }
                )
                sys.exit()

    else:
        print()
        print("*** ARB PLACEHOLDER ***")
        print()


async def process_onchain_arbs():

    '''
    After all pool updates are done (ensuring the LP state 
    is accurate within the helper objects), the pool updater 
    function will add this task to the event loop. It runs, 
    re-evaluating each arbitrage helper that sees an updated 
    LP along its path. Finally it will submit the most 
    profitable arbitrage to a new task.
    
    '''

    # abort if the block or sync status is set to False, which indicates that the websocket disconnected,
    # some events might have been missed, and pool states are inaccurate
    if status_new_blocks == False or status_sync_events == False:
        print("Aborting arb processing! Blocks and Sync Event statuses are not active")
        return

    for arb in alex_bot_borrow_arbs:
        arb.update_reserves()

    # identify all profitable arbs (ignoring gas fees)
    arbs_to_process = []
    for arb in alex_bot_borrow_arbs:
        if arb.best.get("borrow_amount"):
            arbs_to_process.append(arb)

    if arbs_to_process:
        # identify the most profitable arb
        best_arb_profit = 0
        best_arb = None
        for arb in arbs_to_process:
            if arb.best.get("profit_amount") > best_arb_profit:
                best_arb_profit = arb.best.get("profit_amount")
                best_arb = arb

        gas_params = {
            # Maximum EIP-1559 base fee increase per block is 12.5%
            "maxFeePerGas": int(1.15 * last_base_fee),
            "maxPriorityFeePerGas": max(MIN_PRIORITY_FEE, last_priority_fee),
        }

        asyncio.create_task(
            execute_standalone_arb(
                arb_dict=best_arb.best,
                gas_params=gas_params,
            )
        )


def refresh_pools_sync():

    global status_pools_updated

    print("Refreshing all pools...")

    status_pools_updated = False

    brownie_objects = [pool._contract for pool in alex_bot_lps.values()]

    with brownie.multicall():
        results = []
        for i, obj in enumerate(brownie_objects):
            if i % MULTICALL_FLUSH_INTERVAL == 0 and i != 0:
                brownie.multicall.flush()
            try:
                results.append([obj.address, obj.getReserves()[0:2]])
            except Exception as e:
                print(e)

    for lp_address, (reserves0, reserves1) in results:
        if reserves0 and reserves1:
            alex_bot_lps[lp_address].update_reserves(
                external_token0_reserves=reserves0,
                external_token1_reserves=reserves1,
                silent=False,
                print_ratios=False,
                print_reserves=False,
            )

    status_pools_updated = True


async def refresh_pools():

    loop = asyncio.get_running_loop()
    thread_pool = (
        concurrent.futures.ThreadPoolExecutor()
    )  # use this for I/O bound synchronous tasks

    try:
        await loop.run_in_executor(thread_pool, refresh_pools_sync)
    except Exception as e:
        print(f"refresh_pools: {e}")

    # pool reserves are current, add the update_pools() task back to the event loop
    asyncio.create_task(update_pools())


async def update_pools():

    print("Starting pool update loop")
    global pool_update_queue

    # TODO: set the interval dynamically
    MIN_INTERVAL = 0.05

    # store the subscription ID that was active on startup. If the sync watcher disconnects,
    # it will no longer match which triggers the coroutine to be stopped and rescheduled
    subscription = status_sync_events_subscription

    try:

        while True:

            loop_start = time.monotonic()

            # values will match until the sync event subscription disconnects
            if (
                subscription != status_sync_events_subscription
                or not status_pools_updated
            ):
                # Abort processing if the sync event subscription was interrupted or
                # if the batch updater has not completed.
                # Clear pool_update_queue, cancel the task, then restart the loop
                pool_update_queue.clear()
                asyncio.create_task(refresh_pools())
                asyncio.current_task().cancel()
                await asyncio.sleep(0)

            # if the update_queue is empty, sleep half MIN_INTERVAL then restart
            if pool_update_queue == []:
                await asyncio.sleep(MIN_INTERVAL / 2)
                continue

            # if the most recent item in the queue is older than MIN_INTERVAL, process the whole stack
            if loop_start - pool_update_queue[-1][0] >= MIN_INTERVAL:
                pass
            # otherwise sleep until the oldest item is MIN_INTERVAL old, then restart the loop
            else:
                await asyncio.sleep(
                    MIN_INTERVAL - (pool_update_queue[-1][0] - loop_start)
                )
                continue

            # pool_update_queue items have this format:
            # [
            #     index 0: event_timestamp,
            #     index 1: event_address,
            #     index 2: event_block,
            #     index 3: event_reserves,
            # }

            # identify all relevant pool addresses from the queue, eliminating duplicates
            for address in set([update[1] for update in pool_update_queue]):
                # Only process events from addresses with an associated LP helper,
                # otherwise ignore
                if lp := alex_bot_lps.get(address):
                    # process only the reserves for the newest sync event
                    reserves0, reserves1 = [
                        update[3]
                        for update in pool_update_queue
                        if update[1] == address
                    ][-1]
                    # update the reserves of the LP helper
                    lp.update_reserves(
                        external_token0_reserves=reserves0,
                        external_token1_reserves=reserves1,
                        silent=False,
                        print_ratios=False,
                        print_reserves=False,
                    )

            # clear the queue, all events have been processed
            pool_update_queue.clear()

            if ARB_ONCHAIN_ENABLE:
                asyncio.create_task(process_onchain_arbs())

    # this task can cancel itself, so handle the exception here to avoid propagating it back to
    # asyncio.gather() in main()
    except asyncio.exceptions.CancelledError:
        pass


async def watch_new_blocks():
    """
    Watches the websocket for new blocks, updates the base fee
    for the last block, and prints a status update of the
    current maximum gas fees in the mempool
    """

    print("Starting block watcher loop")

    global status_new_blocks
    global newest_block
    global newest_block_timestamp
    global last_base_fee
    global last_priority_fee
    global status_new_blocks_subscription
    global all_pending_tx

    # async for websocket in websockets.unix_connect("../geth.ipc"):
    async for websocket in websockets.connect(uri=WEBSOCKET_URI):

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

            subscribe_result = json.loads(await websocket.recv())

            print(subscribe_result)

            status_new_blocks = True
            status_new_blocks_subscription = subscribe_result.get("result")

            while True:

                message = json.loads(await websocket.recv())

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

                last_priority_fee = brownie.web3.eth.max_priority_fee

                block_transactions = [
                    hash.hex()
                    for hash in w3.eth.get_block(newest_block).get("transactions")
                ]

                for hash in block_transactions:
                    if hash in all_pending_tx:
                        all_pending_tx.remove(hash)
                        # print(f"tx confirmed: {hash}")

                if VERBOSE_BLOCKS:
                    print(
                        f"[{newest_block}] "
                        + f"base: {int(last_base_fee/(10**9))} priority: {int(last_priority_fee/(10**9))} - "
                        f"pending: {len(all_pending_tx)}"
                    )

        except Exception as e:
            print("watch_new_blocks reconnecting...")
            print(e)


async def watch_pending_transactions():

    print("Starting pending TX watcher loop")

    global all_pending_tx

    async for websocket in websockets.connect(uri=WEBSOCKET_URI):

        try:
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["newPendingTransactions"],
                    }
                )
            )
        except websockets.WebSocketException:
            print("(pending_transactions) reconnecting...")
            continue
        except Exception as e:
            print(e)
            continue
        else:
            subscribe_result = json.loads(await websocket.recv())
            print(subscribe_result)

        while True:

            try:
                message = json.loads(await websocket.recv())
            except websockets.WebSocketException:
                print("(pending_transactions inner) reconnecting...")
                break  # escape the loop to reconnect
            except Exception as e:
                print(e)
                break

            try:
                pending_tx = dict(
                    w3.eth.get_transaction(message.get("params").get("result"))
                )
            except:
                # ignore any transactions that cannot be found for any reason
                continue
            else:
                # add the pending TX to the set if new, else skip and continue
                if pending_tx.get("hash").hex() not in all_pending_tx:
                    all_pending_tx.add(pending_tx.get("hash").hex())
                else:
                    continue

            if (
                not status_new_blocks
                or not status_sync_events
                or not status_pools_updated
            ):
                continue

            # ignore the TX unless it was sent to an address on our watchlist
            if pending_tx.get("to") not in ROUTERS.keys():
                continue
            else:
                try:
                    # decode the TX using the ABI
                    decoded_tx = (
                        ROUTERS.get(w3.toChecksumAddress(pending_tx.get("to")))
                        .get("web3_contract")
                        .decode_function_input(pending_tx.get("input"))
                    )
                    # fetch the raw TX bytes (can be included in a bundle later)
                    pending_tx_raw = w3.eth.get_raw_transaction(pending_tx.get("hash"))
                except Exception as e:
                    # print(f"error decoding function: {e}")
                    # print(f"tx: {pending_tx.get('hash').hex()}")
                    continue
                else:
                    func, params = decoded_tx

            # params.get('path') returns None if not found so check it first,
            # then compare all tokens in the path to the list of known tokens
            # that we are monitoring
            if params.get("path") and set(params.get("path")) == set(
                [
                    w3.toChecksumAddress(token_address)
                    for token_address in params.get("path")
                ]
            ).intersection(alex_bot_tokens.keys()):
                print(func.fn_name)

                # assume TX is valid, test later to confirm (eliminates bad simulation inputs)
                valid_swap = True

                # prepare a list of token helper objects for all addresses found in the path
                mempool_tx_token_objects = []
                for address in params.get("path"):
                    mempool_tx_token_objects.append(alex_bot_tokens.get(address))

                mempool_tx_token_object_pairs = [
                    token_object_pair
                    for token_object_pair in itertools.pairwise(
                        mempool_tx_token_objects
                    )
                ]

                mempool_tx_lp_objects = []
                for token0, token1 in mempool_tx_token_object_pairs:
                    print(f"{token0} → {token1}")
                    # find all LP objects representing the token pairs involved in the swap
                    # (e.g. WETH -> DAI -> USDC involves WETH/DAI and DAI/USDC)
                    lp_objects = [
                        lp
                        for lp in alex_bot_lps.values()
                        if lp.factory
                        == ROUTERS.get(w3.toChecksumAddress(pending_tx.get("to"))).get(
                            "factory_address"
                        )
                        if set((token0.address, token1.address))
                        == set(
                            (
                                lp.token0.address,
                                lp.token1.address,
                            )
                        )
                    ]  # BUGFIX: previously took [0] index here, which errors if the list is empty

                    if len(lp_objects) > 1:
                        print(f"found duplicate LPs:")
                        for lp in lp_objects:
                            print(lp)
                    if lp_objects:
                        mempool_tx_lp_objects.append(lp_objects[0])
                    else:
                        # if we couldn't find an LP object for this intermediate pool,
                        # attempt to generate it from the factory
                        try:
                            # create a contract object to query the factory, then
                            # call getPair() on the token addresses to get the LP address
                            lp_address = (
                                ROUTERS.get(w3.toChecksumAddress(pending_tx.get("to")))
                                .get("factory_contract")
                                .getPair(token0.address, token1.address)
                            )
                            # attempt to create an LP helper
                            lp = LiquidityPool(
                                address=lp_address,
                                tokens=[token0, token1],
                                update_method="external",
                                silent=True,
                            )
                        except Exception as e:
                            print(f"Trouble with LP {lp_address}: {e}")
                        else:
                            # add the LP helper to the known LP dictionary,
                            # and the working list of LP objects for this swap
                            alex_bot_lps[pool_address] = lp
                            mempool_tx_lp_objects.append(lp)
                            print(f"Added missing LP: {lp}")

                # if we couldn't find any objects, skip the TX
                if not mempool_tx_lp_objects:
                    continue

                # proceed only if the bot has an LP object for each intermediate step in the swap path
                if len(mempool_tx_lp_objects) != len(params.get("path")) - 1:
                    print(
                        f"Skipping: found {len(mempool_tx_lp_objects)} LP objects, needed {len(params.get('path')) - 1}"
                    )
                    continue

                # identify all arbitrage helpers that track any LPs along the pending TX swap path
                mempool_tx_arbs = []
                for arb in alex_bot_borrow_arbs:
                    if arb.borrow_pool in mempool_tx_lp_objects:
                        # if the borrow pool is a match, add to the list and continue (no need to process swap_pools)
                        mempool_tx_arbs.append(arb)
                        continue
                    # otherwise process swap_pools and mark any positive match
                    for pool in arb.swap_pools:
                        if pool in mempool_tx_lp_objects:
                            mempool_tx_arbs.append(arb)
                            break

                mempool_tx_token_in = mempool_tx_token_objects[0]
                mempool_tx_token_out = mempool_tx_token_objects[-1]

                if (
                    func.fn_name
                    in (
                        "swapExactTokensForETH",
                        "swapExactTokensForETHSupportingFeeOnTransferTokens",
                    )
                    and mempool_tx_token_out.address == WETH_ADDRESS
                ):
                    mempool_tx_token_in_quantity = params.get("amountIn")
                    print(
                        f"In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Out: {params.get('amountOutMin')/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                    )
                    print(f"DEX: {ROUTERS[pending_tx.get('to')]['name']}")

                elif (
                    func.fn_name
                    in (
                        "swapExactETHForTokens",
                        "swapExactETHForTokensSupportingFeeOnTransferTokens",
                    )
                    and mempool_tx_token_in.address == WETH_ADDRESS
                ):
                    mempool_tx_token_in_quantity = pending_tx.get("value")
                    print(
                        f"In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Out: {params.get('amountOutMin')/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                    )
                    print(f"DEX: {ROUTERS[pending_tx.get('to')]['name']}")

                elif func.fn_name in [
                    "swapExactTokensForTokens",
                    "swapExactTokensForTokensSupportingFeeOnTransferTokens",
                ]:
                    mempool_tx_token_in_quantity = params.get("amountIn")
                    print(
                        f"In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Out: {params.get('amountOutMin')/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                    )

                elif (
                    func.fn_name in ("swapTokensForExactETH")
                    and mempool_tx_token_out.address == WETH_ADDRESS
                ):
                    # an index used for finding token addresses in the TX path
                    token_out_position = -1

                    # work backward from the end (using a negative step list copy), calculating token inputs required to receive amountOut from final pool
                    for pool in mempool_tx_lp_objects[::-1]:
                        token_out = alex_bot_tokens.get(
                            params.get("path")[token_out_position]
                        )
                        token_in = alex_bot_tokens.get(
                            params.get("path")[token_out_position - 1]
                        )

                        # use the transaction amountOut parameter for the first calculation
                        if token_out_position == -1:
                            token_out_quantity = params.get("amountOut")

                        # check if the requested amount out exceeds the available pool reserves. If so, set valid_swap to False and break
                        _lp = mempool_tx_lp_objects[token_out_position]

                        if token_out == _lp.token0:
                            if token_out_quantity > _lp.reserves_token0:
                                valid_swap = False
                                break
                        elif token_out == _lp.token1:
                            if token_out_quantity > _lp.reserves_token1:
                                valid_swap = False
                                break

                        # print(f"Calculating input for pool {pool}")

                        token_in_quantity = mempool_tx_lp_objects[
                            token_out_position
                        ].calculate_tokens_in_from_tokens_out(
                            token_in=token_in,
                            token_out_quantity=token_out_quantity,
                        )

                        # feed the result into the next loop, unless we're at the beginning of the path
                        if token_out_position == -len(mempool_tx_lp_objects):
                            mempool_tx_token_in_quantity = token_in_quantity
                            if mempool_tx_token_in_quantity > params.get("amountInMax"):
                                valid_swap = False
                            break
                        else:
                            # move the index back
                            token_out_position -= 1
                            # set the output for the next pool equal to the input of this pool
                            token_out_quantity = token_in_quantity

                    if not valid_swap:
                        continue

                    print(
                        f"In: {params.get('amountInMax')/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Min. In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Out: {params.get('amountOut')/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                    )
                    print(f"DEX: {ROUTERS[pending_tx.get('to')].get('name')}")

                elif (
                    func.fn_name in ("swapETHForExactTokens")
                    and mempool_tx_token_in.address == WETH_ADDRESS
                ):
                    # an index used for finding token addresses in the TX path
                    token_out_position = -1

                    # work backward (using a negative step list copy), calculating token inputs required to receive amountOut from final pool
                    for pool in mempool_tx_lp_objects[::-1]:
                        token_out = alex_bot_tokens.get(
                            params.get("path")[token_out_position]
                        )
                        token_in = alex_bot_tokens.get(
                            params.get("path")[token_out_position - 1]
                        )

                        # use the quantity from the mempool TX
                        if token_out_position == -1:
                            token_out_quantity = params.get("amountOut")

                        # check if the requested amount out exceeds the available pool reserves. If so, set valid_swap to False and break
                        _lp = mempool_tx_lp_objects[token_out_position]

                        if token_out == _lp.token0:
                            if token_out_quantity > _lp.reserves_token0:
                                valid_swap = False
                                break
                        elif token_out == _lp.token1:
                            if token_out_quantity > _lp.reserves_token1:
                                valid_swap = False
                                break

                        # print(f"Calculating input for pool {pool}")

                        token_in_quantity = mempool_tx_lp_objects[
                            token_out_position
                        ].calculate_tokens_in_from_tokens_out(
                            token_in=token_in,
                            token_out_quantity=token_out_quantity,
                        )

                        # Feed the result into the next loop, unless we've reached the beginning of the path.
                        # If we're at the beginning, set the required min input and break the loop
                        if token_out_position == -len(mempool_tx_lp_objects):
                            mempool_tx_token_in_quantity = token_in_quantity
                            if mempool_tx_token_in_quantity > pending_tx.get("value"):
                                valid_swap = False
                            break
                        else:
                            # move the index back
                            token_out_position -= 1
                            # set the output for the next pool equal to the input of this pool
                            token_out_quantity = token_in_quantity

                    if not valid_swap:
                        continue

                    print(
                        f"In: {pending_tx.get('value')/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Min. In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Out: {params.get('amountOut')/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                    )
                    print(f"DEX: {ROUTERS[pending_tx.get('to')].get('name')}")

                elif func.fn_name == "swapTokensForExactTokens":
                    # an index used for finding token addresses in the TX path
                    token_out_position = -1

                    # work backward (using a negative step list copy), calculating token inputs required to receive amountOut from final pool
                    for pool in mempool_tx_lp_objects[::-1]:
                        token_out = alex_bot_tokens.get(
                            params.get("path")[token_out_position]
                        )
                        token_in = alex_bot_tokens.get(
                            params.get("path")[token_out_position - 1]
                        )

                        # use the quantity from the mempool TX
                        if token_out_position == -1:
                            token_out_quantity = params.get("amountOut")

                        # check if the requested amount out exceeds the available pool reserves. If so, set valid_swap to False and break
                        _lp = mempool_tx_lp_objects[token_out_position]

                        if token_out == _lp.token0:
                            if token_out_quantity > _lp.reserves_token0:
                                valid_swap = False
                                break
                        elif token_out == _lp.token1:
                            if token_out_quantity > _lp.reserves_token1:
                                valid_swap = False
                                break

                        token_in_quantity = mempool_tx_lp_objects[
                            token_out_position
                        ].calculate_tokens_in_from_tokens_out(
                            token_in=token_in,
                            token_out_quantity=token_out_quantity,
                        )

                        # Feed the result into the next loop, unless we've reached the beginning of the path.
                        # If we're at the beginning, set the required min input and break the loop
                        if token_out_position == -len(mempool_tx_lp_objects):
                            mempool_tx_token_in_quantity = token_in_quantity
                            if mempool_tx_token_in_quantity > params.get("amountInMax"):
                                valid_swap = False
                            break
                        else:
                            # move the index back
                            token_out_position -= 1
                            # set the output for the next pool equal to the input of this pool
                            token_out_quantity = token_in_quantity

                    if not valid_swap:
                        continue

                    print(
                        f"In: {params.get('amountInMax')/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Min. In: {mempool_tx_token_in_quantity/(10**mempool_tx_token_in.decimals):.4f} {mempool_tx_token_in}"
                    )
                    print(
                        f"Out: {params.get('amountOut')/(10**mempool_tx_token_out.decimals):.4f} {mempool_tx_token_out}"
                    )
                    print(f"DEX: {ROUTERS[pending_tx.get('to')].get('name')}")

                else:
                    print(f"ignored: {func.fn_name}")
                    continue

                if pending_tx.get("type") == "0x2":
                    gas_params = {
                        "maxFeePerGas": pending_tx.get("maxFeePerGas"),
                        "maxPriorityFeePerGas": pending_tx.get("maxPriorityFeePerGas"),
                    }
                    if VERBOSE_MEMPOOL_GAS:
                        print(f"Max Fee (Type 2): {pending_tx.get('maxFeePerGas')}")
                        print(f"Priority Fee: {pending_tx.get('maxPriorityFeePerGas')}")
                elif pending_tx.get("type") == "0x0":
                    gas_params = {"gasPrice": pending_tx.get("gasPrice")}
                    if VERBOSE_MEMPOOL_GAS:
                        print(f"Gas Price (Type 0): {pending_tx.get('gasPrice')}")

                # predict the pool states if the pending swap executes at current pool reserves
                mempool_tx_future_pool_overrides = []
                for i, pool in enumerate(mempool_tx_lp_objects):
                    if i == 0:
                        token_in_quantity = mempool_tx_token_in_quantity
                        if mempool_tx_token_in == pool.token0:
                            token_in = pool.token0
                        elif mempool_tx_token_in == pool.token1:
                            token_in = pool.token1
                        else:
                            print("WTF? Could not identify input token")

                    token_out_quantity = pool.calculate_tokens_out_from_tokens_in(
                        token_in=token_in,
                        token_in_quantity=token_in_quantity,
                    )

                    if token_in == pool.token0:
                        future_reserves_token0 = (
                            pool.reserves_token0 + token_in_quantity
                        )
                        future_reserves_token1 = (
                            pool.reserves_token1 - token_out_quantity
                        )
                        # set the input token for the next swap
                        token_in = pool.token1
                    elif token_in == pool.token1:
                        future_reserves_token0 = (
                            pool.reserves_token0 - token_out_quantity
                        )
                        future_reserves_token1 = (
                            pool.reserves_token1 + token_in_quantity
                        )
                        # set the input token for the next swap
                        token_in = pool.token0
                    else:
                        print("WTF? Could not identify input token")
                        continue

                    # set the input quantity for the next swap
                    token_in_quantity = token_out_quantity
                    mempool_tx_future_pool_overrides.append(
                        [
                            pool,
                            (future_reserves_token0, future_reserves_token1),
                        ]
                    )

                    if VERBOSE_RESERVES:
                        print(f"Simulating swap through pool: {pool}")
                        print(f"[{pool} (CURRENT)]")
                        print(f"{pool.token0}: {pool.reserves_token0}")
                        print(f"{pool.token1}: {pool.reserves_token1}")
                        print(f"[{pool} (FUTURE)]")
                        print(f"{pool.token0}: {future_reserves_token0}")
                        print(f"{pool.token1}: {future_reserves_token1}")

                arbs_to_execute = []
                for arb in mempool_tx_arbs:
                    # BUGFIX: added try/except here, update_reserves will raise exceptions on malformed inputs
                    try:
                        arb.update_reserves(
                            silent=False,
                            print_reserves=False,
                            print_ratios=False,
                            override_future=True,
                            pool_overrides=mempool_tx_future_pool_overrides,
                        )
                    except Exception as e:
                        print(e)
                        continue

                    if arb.best_future.get("borrow_amount") and arb.best_future.get(
                        "profit_amount"
                    ):
                        # add the arb to a queue for execution later
                        arbs_to_execute.append(arb)

                # loop through all profitable arbs to identify the best one, then submit it
                if arbs_to_execute:
                    best_arb_profit = 0
                    best_arb = None

                    for arb in arbs_to_execute:
                        if arb.best_future.get("profit_amount") > best_arb_profit:
                            best_arb_profit = arb.best_future.get("profit_amount")
                            best_arb = arb

                    if ARB_MEMPOOL_ENABLE and best_arb is not None:
                        await execute_multi_tx_arb(
                            arb_dict=best_arb.best_future,
                            gas_params=gas_params,
                            mempool_tx=pending_tx_raw,
                        )


async def watch_sync_events():

    print("Starting sync event watcher loop")

    global pool_update_queue
    global status_sync_events
    global status_sync_events_subscription

    async for websocket in websockets.connect(uri=WEBSOCKET_URI):

        # reset the status to False every time we start a new websocket connection
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
                                    w3.keccak(
                                        text="Sync(uint112,uint112)",
                                    ).hex()
                                ],
                            },
                        ],
                    }
                )
            )
            subscribe_result = json.loads(await websocket.recv())
            print(subscribe_result)

            status_sync_events_subscription = subscribe_result.get("result")

            if not status_sync_events:
                # reset status and timetamp
                status_sync_events = True

            while True:

                message = json.loads(
                    await websocket.recv(),
                )

                event_timestamp = time.monotonic()
                event_address = w3.toChecksumAddress(
                    message.get("params").get("result").get("address")
                )
                event_block = int(
                    message.get("params").get("result").get("blockNumber"),
                    16,
                )
                event_data = message.get("params").get("result").get("data")
                event_reserves = eth_abi.decode_single(
                    "(uint112,uint112)",
                    bytes.fromhex(
                        event_data[2:],
                    ),
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
            print("watch_sync_events reconnecting...")
            print(e)


if not DRY_RUN:
    print(
        "\n"
        "\n***************************************"
        "\n*** DRY RUN DISABLED - BOT IS LIVE! ***"
        "\n***************************************"
        "\n"
    )
    time.sleep(5)

# Create a reusable web3 object (no arguments to provider will default to localhost on default ports)
w3 = web3.Web3(web3.WebsocketProvider())

ROUTERS = {
    w3.toChecksumAddress("0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F"): {
        "name": "Sushiswap"
    },
    w3.toChecksumAddress("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"): {
        "name": "Uniswap"
    },
}

os.environ["ETHERSCAN_TOKEN"] = ETHERSCAN_API_KEY

try:
    network.connect(BROWNIE_NETWORK)
except:
    sys.exit(
        "Could not connect! Verify your Brownie network settings using 'brownie networks list'"
    )

try:
    alex_bot = accounts.load(BROWNIE_ACCOUNT)
    flashbots_id = accounts.load(FLASHBOTS_IDENTITY_ACCOUNT)
except:
    sys.exit(
        "Could not load account! Verify your Brownie account settings using 'brownie accounts list'"
    )

# inject the flashbots middleware into the w3 object
flashbots.flashbot(
    w3,
    eth_account.Account.from_key(flashbots_id.private_key),
    FLASHBOTS_RELAY_URL,
)

arb_contract = Contract.from_abi(
    name="",
    address=ARB_CONTRACT_ADDRESS,
    abi=json.loads(
        """
        [edit me]
        """
    ),
)

pools_with_tokens = []
for filename in ["ethereum_sushiswap_lps.csv", "ethereum_uniswapv2_lps.csv"]:
    with open(filename) as file:
        csv_reader = csv.reader(file)
        # ignore the header
        next(csv_reader)
        for row in csv_reader:
            pools_with_tokens.append(row)


# load all arb pathways and the borrow token from CSV
arb_paths = []
for filename in ["ethereum_2pool_arbs.csv"]:
    with open(filename) as file:
        csv_reader = csv.reader(file)
        # ignore the header
        next(csv_reader)
        for row in csv_reader:
            arb_paths.append(row)

# Identify all unique pool addresses
unique_pools = {
    address
    for *pools, token in arb_paths
    for address in pools
    if token not in BLACKLISTED_TOKENS
}

# Identify all unique token addresses (ignoring blacklisted tokens)
unique_tokens = {
    token for *pools, token in arb_paths if token not in BLACKLISTED_TOKENS
}
# Add WETH to ensure that it's present for pending transaction checks
unique_tokens.add(WETH_ADDRESS)

# build a list of Erc20Token helper objects for all tokens found in the CSV paths
token_objects = []
with concurrent.futures.ThreadPoolExecutor() as thread_pool:
    futures_addresses = {
        thread_pool.submit(
            Erc20Token, address=token_address, abi=ERC20, silent=True
        ): token_address
        for token_address in unique_tokens
    }
    for future in concurrent.futures.as_completed(futures_addresses):
        _address = futures_addresses[future]
        try:
            time.sleep(1)
            result = future.result()
        except Exception as e:
            print(f"Problem creating token @ {_address}: {e}")
        else:
            token_objects.append(result)

print(f"Built {len(token_objects)} token helpers")

# Build a dictionary of token objects, populated from the pair_tokens list.
# Dictionary contains token helper objects, keyed by token address
alex_bot_tokens = {
    w3.toChecksumAddress(token.address): token for token in token_objects
}

arb_lps = []
with concurrent.futures.ThreadPoolExecutor() as thread_pool:
    futures = []
    for pool_address in unique_pools:
        # identify tokens for this pool
        pool_token0 = [
            w3.toChecksumAddress(token0)
            for pool, token0, token1 in pools_with_tokens
            if pool == pool_address
        ][0]
        pool_token1 = [
            w3.toChecksumAddress(token1)
            for pool, token0, token1 in pools_with_tokens
            if pool == pool_address
        ][0]

        token0 = alex_bot_tokens.get(pool_token0)
        token1 = alex_bot_tokens.get(pool_token1)

        if token0 and token1:

            futures.append(
                thread_pool.submit(
                    LiquidityPool,
                    address=pool_address,
                    tokens=[token0, token1],
                    update_method="external",
                    silent=True,
                )
            )

    for future in concurrent.futures.as_completed(futures):
        try:
            time.sleep(1)
            result = future.result()
        except Exception as e:
            print(f"problem {future}: {e}")
        else:
            # ignore LPs with zero reserves
            if result.reserves_token0 and result.reserves_token1:
                arb_lps.append(result)

print(f"Built {len(arb_lps)} liquidity pool helpers")

# build a dictionary of LP objects, keyed by address
alex_bot_lps = {lp.address: lp for lp in arb_lps}

alex_bot_borrow_arbs = []
for borrow_pool, *swap_pools, borrow_token in arb_paths:
    borrow_lp_obj = alex_bot_lps.get(w3.toChecksumAddress(borrow_pool))

    swap_lp_objs = [
        obj
        for pool_address in swap_pools
        if (obj := alex_bot_lps.get(w3.toChecksumAddress(pool_address))) is not None
    ]

    if len(swap_lp_objs) != len(swap_pools) or not borrow_lp_obj:
        continue

    borrow_pool_tokens = [borrow_lp_obj.token0, borrow_lp_obj.token1]

    if borrow_pool_tokens[0].address == WETH_ADDRESS:
        borrow_token = borrow_pool_tokens[1]
        repay_token = borrow_pool_tokens[0]
    elif borrow_pool_tokens[1].address == WETH_ADDRESS:
        borrow_token = borrow_pool_tokens[0]
        repay_token = borrow_pool_tokens[1]
    else:
        print(f"weird stuff going on with tokens: {borrow_pool_tokens}")

    try:
        alex_bot_borrow_arbs.append(
            FlashBorrowToLpSwapWithFuture(
                borrow_pool=alex_bot_lps.get(borrow_pool),
                borrow_token=borrow_token,
                repay_token=repay_token,
                swap_pools=swap_lp_objs,
                update_method="external",
            )
        )
    except Exception as e:
        print(
            f"trouble building arb: borrow pool = {borrow_pool}, swap pools = {swap_pools}"
        )
        print(e)

print(f"Built {len(alex_bot_borrow_arbs)} arb helpers")

for router_address in ROUTERS.keys():
    try:
        router_contract = brownie.Contract(router_address)
    except:
        router_contract = brownie.Contract.from_explorer(router_address)
    else:
        ROUTERS[router_address]["abi"] = router_contract.abi
        ROUTERS[router_address]["web3_contract"] = w3.eth.contract(
            address=router_address,
            abi=router_contract.abi,
        )

    try:
        factory_address = w3.toChecksumAddress(router_contract.factory())
        factory_contract = brownie.Contract(factory_address)
    except:
        factory_contract = brownie.Contract.from_explorer(factory_address)
    else:
        ROUTERS[router_address]["factory_address"] = factory_address
        ROUTERS[router_address]["factory_contract"] = factory_contract

last_base_fee = brownie.chain.base_fee
newest_block = brownie.chain.height
newest_block_timestamp = time.time()
pool_update_queue = []
status_new_blocks = False
status_new_blocks_subscription = None
status_sync_events = False
status_sync_events_subscription = None
status_pools_updated = False
all_pending_tx = set()

asyncio.run(main())
