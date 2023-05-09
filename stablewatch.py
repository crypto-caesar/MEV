'''
Purpose: Stablecoin Watcher. A simple bot to watch swap rates between stablecoin pairs 
and print a message when the swap is profitable.

[SETUP]
Connect to the network
Load the user account
Load the router contract
Load the token contracts

[DATA STRUCTURES]
Prepare a data structure for each token
Prepare a data structure for each unique token pair

[MAIN PROGRAM]
Set up loop
Fetch and store swap rates
Print interesting results '''


# SETUP
import time
import datetime
from brownie import *

network.connect('avax-main')
user = accounts.load('test_account')

print("Loading Contracts:")
dai_contract = Contract.from_explorer('0xd586e7f844cea2f87f50152665bcbc2c279d8d70')
mim_contract = Contract.from_explorer('0x130966628846bfd36ff31a822705796e8cb8c18d')
usdc_contract = Contract.from_explorer('0xa7d7079b0fead91f3e65f86e8915cb59c1a4c664')
usdt_contract = Contract.from_explorer('0xc7198437980c041c805a1edcba50c1ce5db95118')
wavax_contract = Contract.from_explorer('0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7')
router_contract = Contract.from_explorer('0x60aE616a2155Ee3d9A68541Ba4544862310933d4')

# DATA STRUCTURES
dai = {
    "address": dai_contract.address,
    "symbol": dai_contract.symbol(),
    "decimals": dai_contract.decimals(),
}

mim = {
    "address": mim_contract.address,
    "symbol": mim_contract.symbol(),
    "decimals": mim_contract.decimals(),
}

usdc = {
    "address": usdc_contract.address,
    "symbol": usdc_contract.symbol(),
    "decimals": usdc_contract.decimals(),
}

usdt = {
    "address": usdt_contract.address,
    "symbol": usdt_contract.symbol(),
    "decimals": usdt_contract.decimals(),
}

token_pairs = [
    (dai, mim),
    (mim, dai),
    (dai, usdc),
    (usdc, dai),
    (usdt, dai),
    (dai, usdt),
    (usdc, usdt),
    (usdt, usdc),
    (usdt, mim),
    (mim, usdt),
    (usdc, mim),
    (mim, usdc),
]

# MAIN PROGRAM
while True:
    for pair in token_pairs:
        token_in = pair[0]
        token_out = pair[1]
        qty_out = (
            router_contract.getAmountsOut(
                1 * (10 ** token_in["decimals"]),
                [
                    token_in["address"],
                    wavax_contract.address,
                    token_out["address"]
                ],
            )[-1] / (10 ** token_out["decimals"])
        )
        if qty_out >= 1.01:
            print(
                f"{datetime.datetime.now().strftime('[%I:%M:%S %p]')} {token_in['symbol']} → {token_out['symbol']}: ({qty_out:.3f})"
            )
        time.sleep(0.1)
