'''
Purpose: Automated Swap. A more complex bot that automates token approval, continuously monitors swap rates 
between two stablecoins, and attempts to swap at a profit via the router.

[SETUP]
Connect to the network
Load the user account
Load the router contract
Load the token contracts
Load the dotenv snowtrace api token

[DATA STRUCTURES]
Prepare a data structure for each token

[MAIN PROGRAM]
Get allowance and set approvals as needed
Set up loop
Fetch, store, and print swap rates
Print interesting results
Execute a single swap if our swap threshold is met'''

#SETUP
import time
import datetime
import sys
from brownie import *
from dotenv import load_dotenv
load_dotenv()

network.connect('avax-main')
user = accounts.load('alex_bot')

print("Loading Contracts:")
dai_contract = Contract.from_explorer('0xd586e7f844cea2f87f50152665bcbc2c279d8d70')
mim_contract = Contract.from_explorer('0x130966628846bfd36ff31a822705796e8cb8c18d')
wavax_contract = Contract.from_explorer('0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7')
router_contract = Contract.from_explorer('0x60aE616a2155Ee3d9A68541Ba4544862310933d4')

#DATA STRUCTURES
dai = {
    "address": dai_contract.address,
    "symbol": dai_contract.symbol(),
    "decimals": dai_contract.decimals(),
    "balance": dai_contract.balanceOf(user.address),
}

mim = {
    "address": mim_contract.address,
    "symbol": mim_contract.symbol(),
    "decimals": mim_contract.decimals(),
    "balance": mim_contract.balanceOf(user.address),
}

print(dai)
print(mim)

if mim["balance"] == 0:
    sys.exit("MIM balance is zero, aborting...")

#MAIN PROGRAM
if mim_contract.allowance(user.address, router_contract.address) < mim["balance"]:
    mim_contract.approve(
        router_contract.address,
        mim["balance"],
        {'from':user.address},
    )
    
last_ratio = 0.0

while True:
    try:
        qty_out = router_contract.getAmountsOut(
            mim["balance"],
            [
                mim["address"],
                wavax_contract.address,
                dai["address"]
            ],
        )[-1]
    except:
        print("Some error occurred, retrying...")
        continue

    ratio = round(qty_out / mim["balance"], 3)
    if ratio != last_ratio:
        print(
            f"{datetime.datetime.now().strftime('[%I:%M:%S %p]')} MIM → DAI: ({ratio:.3f})"
        )
        last_ratio = ratio

    if ratio >= 1.01:
        print("*** EXECUTING SWAP ***")
        try:
            router_contract.swapExactTokensForTokens(
                mim["balance"],
                int(0.995 * qty_out),
                [
                    mim["address"],
                    wavax_contract.address,
                    dai["address"]
                ],
                user.address,
                1000 * int(time.time() + 60),
                {"from": user},
            )
            print("Swap success!")
        except:
            print("Swap failed, better luck next time!")
        finally:
            break

    time.sleep(0.5)
