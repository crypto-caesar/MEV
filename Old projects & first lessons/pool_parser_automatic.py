'''
Generalize + automate the arb helper generation from the NetworkX 
pathways (eliminates the need to manually enter arb paths).

Goal: identify arbitrage pathways on Avalanche of the form:
    1. Identify an LP with WAVAX and some other token
    2. Flash borrow the non-WAVAX token
    3. Swap the non-WAVAX token for WAVAX into a different LP holding the same token pair
    4. Repay the flash borrow with WAVAX
    5. Keep the difference

To automate this, first identify all tokens held by pools on TJ, 
Sushi, and Pangolin. Using NetworkX, loop through each neighbor of 
the WAVAX node to identify these “pair” tokens. Then count the 
number of edges between each pair token and WAVAX. If there are two 
or more, this indicates that two or more LPs exist. If this is true, 
save the found liquidity pools and the associated tokens to a list. 
Using `itertools`, build all possible unique paths between these 
pools, saving them to a list with their associated tokens. After 
this, repeat the process for the next pair token until all have been 
discovered. At the end, there is a list of lists. Each inner list 
contains two liquidity pool addresses and two token addresses. Then 
save this to a CSV file, since this will be all the information needed 
to automate the helper object build.

'''

import csv
import web3
import networkx as nx
import matplotlib.pyplot as plt
import itertools

PAIR_TOKENS = [
    WAVAX := "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
    GOHM := "0x321E7092a180BB43555132ec53AaA65a5bF84251",
    SPELL := "0xCE1bFFBD5374Dac86a2893119683F4911a2F7814",
    TUS := "0xf693248f96fe03422fea95ac0afbbbc4a8fdd172",
    CRA := "0xA32608e873F9DdEF944B24798db69d80Bbb4d1ed",
    DCAU := "0x100Cc3a819Dd3e8573fD2E46D1E66ee866068f30",
    FRAX := "0xD24C2Ad096400B6FBcd2ad8B24E7acBc21A1da64",
    MIM := "0x130966628846BFd36ff31a822705796e8cb8C18D",
    LINK := "0x5947BB275c521040051D82396192181b413227A3",
    DAI := "0xd586E7F844cEa2F87f50152665BCbc2C279D8d70",
    USDTE := "0xc7198437980c041c805A1EDcbA50c1Ce5db95118",
    USDT := "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
    USDCE := "0xA7D7079b0FEaD91F3e65f86E8915Cb59c1a4C664",
    USDC := "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    BUSDE := "0x19860CCB0A68fd4213aB9D8266F7bBf05A8dDe98",
    WBTC := "0x50b7545627a5162F82A992c33b87aDc75187B218",
    SHIB := "0x02D980A0D7AF3fb7Cf7Df8cB35d9eDBCF355f665",
    FRAX := "0xD24C2Ad096400B6FBcd2ad8B24E7acBc21A1da64",
    WETH := "0x49D5c2BdFfac6CE2BFdB6640F4F80f226bc10bAB",
    PIGGY := "0x1a877B68bdA77d78EEa607443CcDE667B31B0CdF",
    PSHARE := "0xA5e2cFe48fe8C4ABD682CA2B10fCAaFE34b8774c",
    SAVAX := "0x2b2C81e08f1Af8835a78Bb2A90AE924ACE0eA4bE",
    BAT := "0x98443B96EA4b0858FDF3219Cd13e98C7A4690588",
    CRVE := "0x249848BeCA43aC405b8102Ec90Dd5F22CA513c06",
    FXS := "0x214DB107654fF987AD859F34125307783fC8e387",
    RAI := "0x97Cd1CFE2ed5712660bb6c14053C0EcB031Bff7d",
]

BLACKLISTED_TOKENS = [
    "0xf2f13f0B7008ab2FA4A2418F4ccC3684E49D20Eb",  # UST Proxy
]

rows = []
for filename in ["sushiswap_pools.csv", "traderjoe_pools.csv", "pangolin_pools.csv"]:
    with open(filename) as file:
        csv_reader = csv.reader(file)
        next(csv_reader)
        for row in csv_reader:
            rows.append(row)

print(f"Found {len(rows)} pools")

# sanitize the addresses by converting to checksum
# csv contains a row with the following fields:
# index 0: pool_address
# index 1: token0_address
# index 2: token1_address
w3 = web3.Web3()
all_pools_with_tokens = [
    [
        w3.toChecksumAddress(row[0]),
        w3.toChecksumAddress(row[1]),
        w3.toChecksumAddress(row[2]),
    ]
    for row in rows
]

all_tokens = set(
    [pool[1] for pool in all_pools_with_tokens]
    + [pool[2] for pool in all_pools_with_tokens]
) - set(BLACKLISTED_TOKENS)


print(f"Found {len(all_tokens)} tokens")

# build the graph with tokens as nodes, adding an edge
# between any two tokens held by a liquidity pool
G = nx.MultiGraph()
for pool in all_pools_with_tokens:
    G.add_edge(pool[1], pool[2], lp_address=pool[0])

print(f"G ready: {len(G.nodes)} nodes, {len(G.edges)} edges")

all_wavax_pairs = [token for token in G.neighbors(WAVAX)]
print(f"Found {len(all_wavax_pairs)} tokens with a WAVAX pair")

two_pool_pairs_with_tokens = []

for token in PAIR_TOKENS:

    if (token, WAVAX) in G.edges():
        pool_edges = G.get_edge_data(token, WAVAX)

        # only process tokens where two or more LPs exist
        if len(pool_edges) >= 2:

            # get the LP address for all pools holding this token pair from edge data,
            # then append to the two_pool_pairs list
            two_pool_pairs = []
            two_pool_pairs.append([pool["lp_address"] for pool in pool_edges.values()])

            # generate a list of pool permutations (direction matters),
            # adding the borrowed token to the end
            for pool_pair in two_pool_pairs:
                for pair in itertools.permutations(pool_pair, 2):
                    two_pool_pairs_with_tokens.append(list(pair) + [token] + [WAVAX])

print(f"Found {len(two_pool_pairs_with_tokens)} unique two-pool arbitrage paths")

print("• Saving pool data to CSV")
with open("avalanche_arbs_limited.csv", "w") as file:
    csv_writer = csv.writer(file)
    csv_writer.writerow(["borrow_pool", "swap_pool", "tokenA", "tokenB"])
    csv_writer.writerows(two_pool_pairs_with_tokens)
