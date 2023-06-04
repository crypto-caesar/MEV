'''
use getReserves() instead of making calls directly to the chain with getAmountsOut.
Using only pool quantities and accounting for all fees, provide an accurate
estimate of the swap rate without calling getAmountsOut().
'''

import sys
import time
import os
from brownie import *
from decimal import Decimal
from dotenv import load_dotenv
load_dotenv()

# Contract addresses
TRADERJOE_ROUTER_CONTRACT_ADDRESS = "0x60aE616a2155Ee3d9A68541Ba4544862310933d4"
TOKEN_POOL_CONTRACT_ADDRESS = "0x033C3Fc1fC13F803A233D262e24d1ec3fd4EFB48"

def get_tokens_out_from_tokens_in(
    pool_reserves_token0,
    pool_reserves_token1,
    quantity_token0_in=0,
    quantity_token1_in=0,
    fee=0,
):
    # fails if two input tokens are passed, or if both are 0
    assert not (quantity_token0_in and quantity_token1_in)
    assert quantity_token0_in or quantity_token1_in

    if quantity_token0_in:
        return (pool_reserves_token1 * quantity_token0_in * (1 - fee)) // (
            pool_reserves_token0 + quantity_token0_in * (1 - fee)
        )

    if quantity_token1_in:
        return (pool_reserves_token0 * quantity_token1_in * (1 - fee)) // (
            pool_reserves_token1 + quantity_token1_in * (1 - fee)
        )

def get_tokens_in_for_ratio_out(
    pool_reserves_token0,
    pool_reserves_token1,
    token0_per_token1,
    token0_out=False,
    token1_out=False,
    fee=Decimal("0.0"),
):
    assert not (token0_out and token1_out)

    # token1 input, token0 output
    if token0_out:
        # dy = x0/C - y0/(1-fee)
        # C = ratio of token0 (dx) to token1 (dy)
        dy = int(
            pool_reserves_token0 / token0_per_token1 - pool_reserves_token1 / (1 - fee)
        )
        if dy > 0:
            return dy
        else:
            return 0

    # token0 input, token1 output
    if token1_out:
        # dx = y0*C - x0/(1-fee)
        # C = ratio of token0 (dxy) to token1 (dy)
        dx = int(
            pool_reserves_token1 * token0_per_token1 - pool_reserves_token0 / (1 - fee)
        )
        if dx > 0:
            return dx
        else:
            return 0

def contract_load(address, alias):
    # Attempts to load the saved contract by alias.
    # If not found, fetch from network explorer and set alias.
    try:
        contract = Contract(alias)
    except ValueError:
        contract = Contract.from_explorer(address)
        contract.set_alias(alias)
    finally:
        print(f"• {alias}")
        return contract

def get_swap_rate(token_in_quantity, token_in_address, token_out_address, contract):
    try:
        return contract.getAmountsOut(
            token_in_quantity, [token_in_address, token_out_address]
        )
    except Exception as e:
        print(f"Exception in get_swap_rate: {e}")
        return False

try:
    network.connect("avax-main")
except:
    sys.exit(
        "Could not connect to Avalanche! Verify that brownie lists the Avalanche Mainnet using 'brownie networks list'"
    )

print("\nContracts loaded:")
lp = contract_load(TOKEN_POOL_CONTRACT_ADDRESS, "TraderJoe LP: SPELL-sSPELL")
router = contract_load(TRADERJOE_ROUTER_CONTRACT_ADDRESS, "TraderJoe: Router")

token0 = Contract.from_explorer(lp.token0.call())
token1 = Contract.from_explorer(lp.token1.call())

print()
print(f"token0 = {token0.symbol.call()}")
print(f"token1 = {token1.symbol.call()}")

print()
print("*** Getting Pool Reserves *** ")
x0, y0 = lp.getReserves.call()[0:2]
print(f"token0: \t\t\t{x0}")
print(f"token1: \t\t\t{y0}")

print()
print("*** Calculating hypothetical swap: 500,000 SPELL to sSPELL @ 0.3% fee ***")
quote = router.getAmountsOut(500_000 * (10 ** 18), [token1.address, token0.address])[-1]
tokens_out = get_tokens_out_from_tokens_in(
    pool_reserves_token0=x0,
    pool_reserves_token1=y0,
    quantity_token1_in=500_000 * (10 ** 18),
    fee=Decimal("0.003"),
)
print()
print(f"Calculated Tokens Out: \t\t{tokens_out}")
print(f"Router Quoted getAmountsOut: \t{quote}")
print(f"Difference: \t\t\t{quote - tokens_out}")

print()
print("*** Calculating hypothetical swap: 500,000 sSPELL to SPELL @ 0.3% fee ***")
quote = router.getAmountsOut(
    500_000 * (10 ** 18),
    [token0.address, token1.address],
)[-1]
tokens_out = get_tokens_out_from_tokens_in(
    pool_reserves_token0=x0,
    pool_reserves_token1=y0,
    quantity_token0_in=500_000 * (10 ** 18),
    fee=Decimal("0.003"),
)
print()
print(f"Calculated Tokens Out: \t\t{tokens_out}")
print(f"Router Quoted getAmountsOut: \t{quote}")
print(f"Difference: \t\t\t{quote - tokens_out}")
