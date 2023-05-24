'''
Uniswap V2 & V3 2-Pool Arbitrage Bot: searches and submits 2-pool 
cycle arbitrage transactions between V2 and V3 pools via Flashbots Auction.
It searches for 2-pool arb opportunities on Ethereum mainnet by cycling 
WETH through Uniswap-based AMM pools. Both types of pools are supported (V2 and V3, 
including Sushiswap), and in any combination. It is built on top of asyncio 
for coroutine-based concurrency, and makes heavy use of the websockets library to 
listen for events that track changes to pool states.

This is the 2-pool arbitrage builder component.
'''

import json
import web3
import networkx as nx
import itertools

WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

w3 = web3.Web3()

v2_lp_data = {}
for filename in [
    "ethereum_sushiswap_lps.json",
    "ethereum_uniswapv2_lps.json",
]:
    with open(filename) as file:
        for pool in json.load(file):
            v2_lp_data[pool.get("pool_address")] = {
                key: value
                for key, value in pool.items()
                if key not in ["pool_id"]
            }
print(f"Found {len(v2_lp_data)} V2 pools")

v3_lp_data = {}
for filename in [
    "ethereum_uniswapv3_lps.json",
]:
    with open(filename) as file:
        for pool in json.load(file):
            v3_lp_data[pool.get("pool_address")] = {
                key: value
                for key, value in pool.items()
                if key not in ["block_number"]
            }
print(f"Found {len(v3_lp_data)} V3 pools")

all_v2_pools = set(v2_lp_data.keys())
all_v3_pools = set(v3_lp_data.keys())

all_tokens = set(
    [lp.get("token0") for lp in v2_lp_data.values()]
    + [lp.get("token1") for lp in v2_lp_data.values()]
    + [lp.get("token0") for lp in v3_lp_data.values()]
    + [lp.get("token1") for lp in v3_lp_data.values()]
)

# build the graph with tokens as nodes, adding an edge
# between any two tokens held by a liquidity pool
G = nx.MultiGraph()
for pool in v2_lp_data.values():
    G.add_edge(
        pool.get("token0"),
        pool.get("token1"),
        lp_address=pool.get("pool_address"),
        pool_type="UniswapV2",
    )

for pool in v3_lp_data.values():
    G.add_edge(
        pool.get("token0"),
        pool.get("token1"),
        lp_address=pool.get("pool_address"),
        pool_type="UniswapV3",
    )

print(f"G ready: {len(G.nodes)} nodes, {len(G.edges)} edges")

all_tokens_with_weth_pool = list(G.neighbors(WETH_ADDRESS))
print(f"Found {len(all_tokens_with_weth_pool)} tokens with a WETH pair")

print("*** Finding two-pool arbitrage paths ***")
two_pool_arb_paths = {}

for token in all_tokens_with_weth_pool:

    pools = G.get_edge_data(token, WETH_ADDRESS).values()

    # skip tokens with only one pool
    if len(pools) < 2:
        continue

    for pool_a, pool_b in itertools.permutations(pools, 2):

        if pool_a.get("pool_type") == "UniswapV2":
            pool_a_dict = v2_lp_data.get(pool_a.get("lp_address"))
        elif pool_a.get("pool_type") == "UniswapV3":
            pool_a_dict = v3_lp_data.get(pool_a.get("lp_address"))
        else:
            raise Exception(f"could not identify pool {pool_a}")

        if pool_b.get("pool_type") == "UniswapV2":
            pool_b_dict = v2_lp_data.get(pool_b.get("lp_address"))
        elif pool_b.get("pool_type") == "UniswapV3":
            pool_b_dict = v3_lp_data.get(pool_b.get("lp_address"))
        else:
            raise Exception(f"could not identify pool {pool_b}")

        two_pool_arb_paths[id] = {
            "id": (
                id := w3.keccak(
                    hexstr="".join(
                        [
                            pool_a.get("lp_address")[2:],
                            pool_b.get("lp_address")[2:],
                        ]
                    )
                ).hex()
            ),
            "pools": {
                pool_a.get("lp_address"): pool_a_dict,
                pool_b.get("lp_address"): pool_b_dict,
            },
            "arb_types": ["cycle", "flash_borrow_lp_swap"],
            "path": [pool.get("lp_address") for pool in [pool_a, pool_b]],
        }
print(f"Found {len(two_pool_arb_paths)} unique two-pool arbitrage paths")

print("• Saving arb paths to JSON")
with open("ethereum_arbs_2pool_withv3.json", "w") as file:
    json.dump(two_pool_arb_paths, file, indent=2)
