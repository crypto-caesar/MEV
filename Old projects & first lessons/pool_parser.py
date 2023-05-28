'''
NetworkX and graph theory -- feed Avalanche data into NetworkX
'''

import csv
import web3
import networkx as nx
import matplotlib.pyplot as plt
import itertools

WAVAX = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"

rows = []

for filename in [
    "sushiswap_pools.csv",
    "traderjoe_pools.csv",
    "pangolin_pools.csv",
]:
    with open(filename) as file:
        csv_reader = csv.reader(file)
        next(csv_reader)
        for row in csv_reader:
            rows.append(row)

print(f"Found {len(rows)} pools")

w3 = web3.Web3()

# csv contains a row with the following fields:
# index 0: pool_address
# index 1: token0_address
# index 2: token1_address

pools = [
    [
        w3.toChecksumAddress(row[0]),
        w3.toChecksumAddress(row[1]),
        w3.toChecksumAddress(row[2]),
    ]
    for row in rows
]

all_tokens = list(set([pool[1] for pool in pools] + [pool[2] for pool in pools]))

print(f"Found {len(all_tokens)} tokens")

G = nx.MultiGraph()

# build the graph with tokens as nodes, adding an edge
# between any two tokens held by a liquidity pool

for pool in pools:
    G.add_edge(pool[1], pool[2], lp_address=pool[0])

print(f"G ready: {len(G.nodes)} nodes, {len(G.edges)} edges")

all_wavax_pairs = [token for token in G.neighbors(WAVAX)]
print(f"Found {len(all_wavax_pairs)} tokens with a WAVAX pair")

print("*** Finding 'matched' token pools (2 or more) ***")

two_pool_pairs = []
for token in all_wavax_pairs:
    matched_pool_set = []
    if (token, WAVAX) in G.edges():
        _pools = G.get_edge_data(token, WAVAX)
        if len(_pools) >= 2:
            lps = []
            for pool in _pools.values():
                lp = pool["lp_address"]
                lps.append(lp)
            matched_pool_set.append(lps)
            for set in matched_pool_set:
                for pair in itertools.permutations(set, 2):
                    two_pool_pairs.append(pair)
print(f"found {len(two_pool_pairs)} unique two-pool arbitrage paths")
