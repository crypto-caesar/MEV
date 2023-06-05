'''
Purpose: observe and decode multicall TXs that go through the router. A bot that:

- Connects to mainnet Ethereum via websocket
- Sets up an "eth_subscribe"  watcher to receive new pending transactions
- Decodes and prints the function and parameters associated with any observed UniswapV3 transaction
'''

# Import modules, establish a connection to our RPC via Brownie, and launch watch_pending_transactions coroutine

import asyncio
import brownie
import itertools
import json
import os
import sys
import web3
import websockets
from dotenv import load_dotenv
load_dotenv()


BROWNIE_NETWORK = "mainnet-local-ws"
WEBSOCKET_URI = "ws://localhost:8546"

'''
define an asynchronous coroutine called watch_pending_transactions that establishes
an RPC subscription to "newPendingTransactions", listens to the websocket for new messages, 
filters the messages to identify transactions to the UniV3 router contracts (Both Router and Router 2), 
then prints them
'''

async def watch_pending_transactions():

    v3_routers = {
        "0xE592427A0AEce92De3Edee1F18E0157C05861564": {
            "name": "UniswapV3: Router"
        },
        "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45": {
            "name": "UniswapV3: Router 2"
        },
    }

    for router_address in v3_routers.keys():
        try:
            router_contract = brownie.Contract(
                router_address
            )
        except:
            router_contract = brownie.Contract.from_explorer(
                router_address
            )
        else:
            v3_routers[router_address]["abi"] = router_contract.abi
            v3_routers[router_address]["web3_contract"] = w3.eth.contract(
                address=router_address,
                abi=router_contract.abi,
            )

        try:
            factory_address = w3.toChecksumAddress(router_contract.factory())
            factory_contract = brownie.Contract(factory_address)
        except:
            factory_contract = brownie.Contract.from_explorer(factory_address)
        else:
            v3_routers[router_address]["factory_address"] = factory_address
            v3_routers[router_address]["factory_contract"] = factory_contract

    print("Starting pending TX watcher loop")

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
            except websockets.WebSocketException as e:
                print("(pending_transactions inner) reconnecting...")
                print(e)
                break  # escape the loop to reconnect
            except Exception as e:
                print(e)
                break

            try:
                pending_tx = dict(
                    w3.eth.get_transaction(
                        message.get("params").get("result")
                    )
                )
            except:
                # ignore any transaction that cannot be found
                continue

            # skip post-processing unless the TX was sent to
            # an address on our watchlist
            if pending_tx.get("to") not in v3_routers.keys():
                continue
            else:
                try:
                    # decode the TX using the ABI
                    decoded_tx = (
                        v3_routers.get(
                            w3.toChecksumAddress(pending_tx.get("to"))
                        )
                        .get("web3_contract")
                        .decode_function_input(pending_tx.get("input"))
                    )
                except Exception as e:
                    continue
                else:
                    func, func_args = decoded_tx

            if func.fn_name == "multicall":
                print("MULTICALL")
                if func_args.get("deadline"):
                    print(f'deadline: {func_args.get("deadline")}')
                if func_args.get("data"):
                    for i, payload in enumerate(func_args.get("data")):
                        print(f"payload {i}: {payload.hex()}")
                        payload_func, payload_func_args = (
                            v3_routers.get(
                                w3.toChecksumAddress(pending_tx.get("to"))
                            )
                            .get("web3_contract")
                            .decode_function_input(payload)
                        )
                        print(f"\tpayload {i}: {payload_func.fn_name}")
                        print(f"\targs : {payload_func_args}")
                if func_args.get("previousBlockhash"):
                    print(
                        "previousBlockhash: "
                        + f'{func_args.get("previousBlockhash").hex()}'
                    )
            elif func.fn_name == "exactInputSingle":
                print(func.fn_name)
                print(func_args.get("params"))
            elif func.fn_name == "exactInput":
                print(func.fn_name)
                if (
                    v3_routers.get(
                        w3.toChecksumAddress(pending_tx.get("to"))
                    ).get("name")
                    == "UniswapV3: Router"
                ):
                    print("Decoding exactInput using Router ABI")
                    (
                        exactInputParams_path,
                        exactInputParams_recipient,
                        exactInputParams_deadline,
                        exactInputParams_amountIn,
                        exactInputParams_amountOutMinimum,
                    ) = func_args.get("params")
                elif (
                    v3_routers.get(
                        w3.toChecksumAddress(pending_tx.get("to"))
                    ).get("name")
                    == "UniswapV3: Router 2"
                ):
                    print("Decoding exactInput using Router2 ABI")
                    (
                        exactInputParams_path,
                        exactInputParams_recipient,
                        exactInputParams_amountIn,
                        exactInputParams_amountOutMinimum,
                    ) = func_args.get("params")

                # decode the path
                path_pos = 0
                exactInputParams_path_decoded = []
                # read alternating 20 and 3 byte chunks from the encoded path,
                # store each address (hex) and fee (int)
                for byte_length in itertools.cycle((20, 3)):
                    # stop at the end
                    if path_pos == len(exactInputParams_path):
                        break
                    elif (
                        byte_length == 20
                        and len(exactInputParams_path)
                        >= path_pos + byte_length
                    ):
                        address = exactInputParams_path[
                            path_pos : path_pos + byte_length
                        ].hex()
                        exactInputParams_path_decoded.append(address)
                    elif (
                        byte_length == 3
                        and len(exactInputParams_path)
                        >= path_pos + byte_length
                    ):
                        fee = int(
                            exactInputParams_path[
                                path_pos : path_pos + byte_length
                            ].hex(),
                            16,
                        )
                        exactInputParams_path_decoded.append(fee)
                    path_pos += byte_length

                print(f"\tpath = {exactInputParams_path_decoded}")
                print(f"\trecipient = {exactInputParams_recipient}")
                if exactInputParams_deadline:
                    print(f"\tdeadline = {exactInputParams_deadline}")
                print(f"\tamountIn = {exactInputParams_amountIn}")
                print(
                    f"\tamountOutMinimum = {exactInputParams_amountOutMinimum}"
                )
            elif func.fn_name == "exactOutputSingle":
                print(func.fn_name)
                print(func_args.get("params"))
            elif func.fn_name == "exactOutput":
                print(func.fn_name)
                print(func_args.get("params"))

                if (
                    v3_routers.get(
                        w3.toChecksumAddress(pending_tx.get("to"))
                    ).get("name")
                    == "UniswapV3: Router"
                ):
                    print("Decoding exactOutput using Router ABI")
                    (
                        exactOutputParams_path,
                        exactOutputParams_recipient,
                        exactOutputParams_deadline,
                        exactOutputParams_amountOut,
                        exactOutputParams_amountInMaximum,
                    ) = func_args.get("params")
                elif (
                    v3_routers.get(
                        w3.toChecksumAddress(pending_tx.get("to"))
                    ).get("name")
                    == "UniswapV3: Router 2"
                ):
                    print("Decoding exactOutput using Router2 ABI")
                    (
                        exactOutputParams_path,
                        exactOutputParams_recipient,
                        exactOutputParams_amountOut,
                        exactOutputParams_amountInMaximum,
                    ) = func_args.get("params")

                # decode the path
                path_pos = 0
                exactOutputParams_path_decoded = []
                # read alternating 20 and 3 byte chunks from the encoded path,
                # store each address (hex) and fee (int)
                for byte_length in itertools.cycle((20, 3)):
                    # stop at the end
                    if path_pos == len(exactOutputParams_path):
                        break
                    elif (
                        byte_length == 20
                        and len(exactOutputParams_path)
                        >= path_pos + byte_length
                    ):
                        address = exactOutputParams_path[
                            path_pos : path_pos + byte_length
                        ].hex()
                        exactOutputParams_path_decoded.append(address)
                    elif (
                        byte_length == 3
                        and len(exactOutputParams_path)
                        >= path_pos + byte_length
                    ):
                        fee = int(
                            exactOutputParams_path[
                                path_pos : path_pos + byte_length
                            ].hex(),
                            16,
                        )
                        exactOutputParams_path_decoded.append(fee)
                    path_pos += byte_length

                print(f"\tpath = {exactOutputParams_path_decoded}")
                print(f"\trecipient = {exactOutputParams_recipient}")
                if exactOutputParams_deadline:
                    print(f"\tdeadline = {exactOutputParams_deadline}")
                print(f"\tamountOut = {exactOutputParams_amountOut}")
                print(
                    f"\tamountamountInMaximum = {exactOutputParams_amountInMaximum}"
                )
            else:
                print(f"other function: {func.fn_name}")
                continue

# Create a reusable web3 object (no arguments to provider will default to localhost on default ports)
w3 = web3.Web3(web3.WebsocketProvider())

#os.environ["ETHERSCAN_TOKEN"] = ETHERSCAN_API_KEY

try:
    brownie.network.connect(BROWNIE_NETWORK)
except:
    sys.exit(
        "Could not connect! Verify your Brownie network settings using 'brownie networks list'"
    )


asyncio.run(watch_pending_transactions()) 
