'''
simple example to show how to monitor several websockets simultaneously
'''

import asyncio
import json
import websockets
import brownie

async def sync_events():
    async for websocket in websockets.connect(uri=brownie.web3.provider.endpoint_uri):
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
                                    brownie.web3.keccak(
                                        text="Sync(uint112,uint112)"
                                    ).hex()
                                ],
                            },
                        ],
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
                    tx_hash = json.loads(message)["params"]["result"]["transactionHash"]
                    print(f"Sync Event @ TX: {tx_hash}")
                except websockets.WebSocketException:
                    break  # escape the loop to reconnect
                except Exception as e:
                    print(e)

        except websockets.WebSocketException:
            print("reconnecting...")
            continue
        except Exception as e:
            print(e)
'''
opens a websocket connection, subscribes to a method 
newPendingTransactions, receives notifications from the node whenever 
a new pending TX is observed, then prints the hash for that TX
'''
async def pending_transactions():
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
                    print(tx_hash)
                except websockets.WebSocketException:
                    break  # escape the loop to reconnect
                except Exception as e:
                    print(e)

        except websockets.WebSocketException:
            print("reconnecting...")
            continue
        except Exception as e:
            print(e)

async def new_heads():
    async for websocket in websockets.connect(uri=brownie.web3.provider.endpoint_uri):
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

            subscribe_result = await websocket.recv()
            print(subscribe_result)

            while True:
                try:
                    message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=30,
                    )
                    block_number = int(
                        json.loads(message)["params"]["result"]["number"],
                        16,
                    )
                    print(f"New Block: {block_number}")
                except websockets.WebSocketException:
                    break  # escape the loop to reconnect
                except Exception as e:
                    print(e)

        except websockets.WebSocketException:
            print("reconnecting...")
            continue
        except Exception as e:
            print(e)

async def main():

    brownie.network.connect("moralis-ftm-main-websocket")

    await asyncio.gather(
        asyncio.create_task(new_heads()),        
    )


if __name__ == "__main__":
    asyncio.run(main())
