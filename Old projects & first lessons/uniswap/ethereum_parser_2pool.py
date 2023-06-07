'''
This script replaces the CSV two-pool arb builder in
ethereum_parser_2pool_univ3. It is similar, except it 
loads the LP information from the JSON file instead of 
CSV, and constructs the individual arbitrage paths as a 
dictionary instead of a list of addresses.

'''

import json
import web3
import networkx as nx
import itertools
import json

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

w3 = web3.Web3()

lp_data = []
for filename in [
    "ethereum_sushiswap_lps.json",
    "ethereum_uniswapv2_lps.json",
]:
    with open(filename) as file:
        lp_data.extend(json.load(file))

print(f"Found {len(lp_data)} pools with tokens")

all_pools = set([lp.get("pool_address") for lp in lp_data])
all_tokens = set(
    [lp.get("token0") for lp in lp_data] +
    [lp.get("token1") for lp in lp_data]
)

# build the graph with tokens as nodes, adding an edge
# between any two tokens held by a liquidity pool
G = nx.MultiGraph()
for pool in lp_data:
    G.add_edge(
        pool.get("token0"),
        pool.get("token1"),
        lp_address=pool.get("pool_address"),
    )

print(f"G ready: {len(G.nodes)} nodes, {len(G.edges)} edges")

all_weth_pairs = list(G.neighbors(WETH))
print(f"Found {len(all_weth_pairs)} tokens with a WETH pair")

two_pool_pairs_with_borrow_token = []
for token in all_weth_pairs:

    if (token, WETH) in G.edges():

        pool_edges = G.get_edge_data(token, WETH)

        # only process tokens where two or more LPs exist
        if len(pool_edges) >= 2:

            # get the LP address for all pools holding
            # this token pair from edge data,
            # then append to the two_pool_pairs list
            two_pool_pairs = []
            two_pool_pairs.append(
                [
                    pool["lp_address"]
                    for pool
                    in pool_edges.values()
                ]
            )

            for set in two_pool_pairs:
                for borrow_pool, swap_pool in itertools.permutations(set, 2):
                    # two_pool_pairs_with_borrow_token.append(list(pair) + [token])
                    arb_dict = {
                        "borrow_pool": borrow_pool,
                        "swap_pools": [swap_pool],
                        "borrow_token": token,
                        "repay_token": WETH,
                    }
                    two_pool_pairs_with_borrow_token.append(arb_dict)

print(f"Found {len(two_pool_pairs_with_borrow_token)} unique two-pool arbitrage paths")

print("• Saving pool data to JSON")
with open("ethereum_arbs_2pool.json", "w") as file:
    json.dump(two_pool_pairs_with_borrow_token, file)
