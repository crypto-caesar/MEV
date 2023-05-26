'''
Builds off of "pending_transactions" function from websocket_subscribe.py:

- addded get_transaction(), allows us to look up the function 
inputs for a given TX. Brownie automatically creates a "web3" object.
- added spookyswap_router=. Instead of just printing that we found a 
TX, we decode it, then print the function and its parameters
- modified the main loop with spookyswap router inputs
- fetched the TX data using web3 to determine the encoded structure 
of the TX, then filtered for the Spookyswap router address
'''

import asyncio
import json
import websockets
import brownie


async def pending_transactions():

    spookyswap_router = brownie.Contract.from_explorer(
        "0xF491e7B69E4244ad4002BC14e878a34207E38c29"
    )

    async for websocket in websockets.connect(uri=brownie.web3.provider.endpoint_uri):
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
            subscribe_result = await websocket.recv()
            print(subscribe_result)

            while True:
                try:
                    message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=30,
                    )

                    tx_hash = json.loads(message)["params"]["result"]
                    try:
                        tx_data = brownie.web3.eth.get_transaction(tx_hash)
                    except:
                        continue

                    if tx_data and (
                        (tx_data["to"]).lower() ==
                        spookyswap_router.address.lower()
                    ):
                        func_object, func_params = brownie.web3.eth.contract(
                            address=spookyswap_router.address, abi=spookyswap_router.abi
                        ).decode_function_input(tx_data["input"])
                        print()
                        print(func_object.fn_name)
                        print(func_params)

                except websockets.WebSocketException:
                    print("(pending_transactions inner) reconnecting...")
                    break  # escape the loop to reconnect
                except Exception as e:
                    print(e)
                    continue

        except websockets.WebSocketException:
            print("(pending_transactions outer) reconnecting...")
            continue
        except Exception as e:
            print(e)

async def main():

    brownie.network.connect("moralis-ftm-main-websocket")

    await asyncio.gather(
        asyncio.create_task(pending_transactions()),
    )


if __name__ == "__main__":
    asyncio.run(main())
