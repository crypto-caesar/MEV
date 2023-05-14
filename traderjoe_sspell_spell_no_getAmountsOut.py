'''
Functionally the same as traderjoe_spell_spell.py, but calculates the 
swap directly instead of using getAmountsOut (as it scales poorly). The only changes 
are in the arb loop:
# get quotes and execute SPELL -> sSPELL swaps only if we have a balance of SPELL
    if spell["balance"]:
and 
# get quotes and execute sSPELL -> SPELL swaps only if we have a balance of sSPELL
    if sspell["balance"]:
'''

import sys
import time
import datetime
import requests
import os
import json
from decimal import Decimal
from brownie import *
from dotenv import load_dotenv
load_dotenv()

# Contract addresses (verify on Snowtrace)
TRADERJOE_ROUTER_CONTRACT_ADDRESS = "0x60aE616a2155Ee3d9A68541Ba4544862310933d4"
TRADERJOE_POOL_CONTRACT_ADDRESS = "0x033C3Fc1fC13F803A233D262e24d1ec3fd4EFB48"
SPELL_CONTRACT_ADDRESS = "0xce1bffbd5374dac86a2893119683f4911a2f7814"
SSPELL_CONTRACT_ADDRESS = "0x3ee97d514bbef95a2f110e6b9b73824719030f7a"

SECOND = 1
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR

# SPELL -> sSPELL swap targets
# a zero value will trigger a swap when the ratio matches base_staking_rate exactly
# a negative value will trigger a swap when the rate is below base_staking_rate
# a positive value will trigger a swap when the rate is above base_staking_rate
THRESHOLD_SPELL_TO_SSPELL = Decimal("0.02")

# sSPELL -> SPELL swap targets
# a positive value will trigger a (sSPELL -> SPELL) swap when the ratio is above base_staking_rate
THRESHOLD_SSPELL_TO_SPELL = Decimal("0.05")

SLIPPAGE = Decimal("0.001")  # tolerated slippage in swap price (0.1%)

BASE_STAKING_RATE_FILENAME = ".abra_rate"

# Simulate swaps and approvals
DRY_RUN = True
# Quit after the first successful trade
ONE_SHOT = False
# How often to run the main loop (in seconds)
LOOP_TIME = 1.0


def main():

    global traderjoe_router
    global traderjoe_lp
    global spell
    global sspell
    global user

    try:
        network.connect("avax-main")
    except:
        sys.exit(
            "Could not connect to Avalanche! Verify that brownie lists the Avalanche Mainnet using 'brownie networks list'"
        )

    try:
        user = accounts.load("degenbot")
    except:
        sys.exit(
            "Could not load account! Verify that your account is listed using 'brownie accounts list' and that you are using the correct password. If you have not added an account, run 'brownie accounts new' now."
        )

    print("\nContracts loaded:")
    spell_contract = contract_load(SPELL_CONTRACT_ADDRESS, "Avalanche Token: SPELL")
    sspell_contract = contract_load(SSPELL_CONTRACT_ADDRESS, "Avalanche Token: sSPELL")

    traderjoe_router = contract_load(
        TRADERJOE_ROUTER_CONTRACT_ADDRESS, "TraderJoe: Router"
    )
    traderjoe_lp = contract_load(
        TRADERJOE_POOL_CONTRACT_ADDRESS, "TraderJoe LP: SPELL-sSPELL"
    )

    spell = {
        "address": SPELL_CONTRACT_ADDRESS,
        "contract": spell_contract,
        "name": None,
        "symbol": None,
        "balance": None,
        "decimals": None,
    }

    sspell = {
        "address": SSPELL_CONTRACT_ADDRESS,
        "contract": sspell_contract,
        "name": None,
        "symbol": None,
        "balance": None,
        "decimals": None,
    }

    spell["symbol"] = get_token_symbol(spell["contract"])
    spell["name"] = get_token_name(spell["contract"])
    spell["balance"] = get_token_balance(spell_contract, user)
    spell["decimals"] = get_token_decimals(spell_contract)

    sspell["symbol"] = get_token_symbol(sspell["contract"])
    sspell["name"] = get_token_name(sspell["contract"])
    sspell["balance"] = get_token_balance(sspell_contract, user)
    sspell["decimals"] = get_token_decimals(sspell_contract)

    if (spell["balance"] == 0) and (sspell["balance"] == 0):
        sys.exit("No tokens found!")

    # Confirm approvals for tokens
    print("\nChecking Approvals:")

    if get_approval(spell["contract"], traderjoe_router, user):
        print(f"• {spell['symbol']} OK")
    else:
        token_approve(spell["contract"], traderjoe_router)

    if get_approval(sspell["contract"], traderjoe_router, user):
        print(f"• {sspell['symbol']} OK")
    else:
        token_approve(sspell["contract"], traderjoe_router)

    try:
        with open(BASE_STAKING_RATE_FILENAME, "r") as file:
            base_staking_rate = Decimal(file.read().strip())
            print(f"\nEthereum L1 Staking Rate: {base_staking_rate}")
    except FileNotFoundError:
        sys.exit(
            "Cannot load the base Abracadabra SPELL/sSPELL staking rate. Run `python3 abra_rate.py` and try again."
        )

    network.priority_fee("5 gwei")
    balance_refresh = True

    #
    # Start of arbitrage loop
    #
    while True:

        loop_start = time.time()

        try:
            with open(BASE_STAKING_RATE_FILENAME, "r") as file:
                if (result := Decimal(file.read().strip())) != base_staking_rate:
                    base_staking_rate = result
                    print(f"Updated staking rate: {base_staking_rate}")
        except FileNotFoundError:
            sys.exit(
                "Cannot load the base Abracadabra SPELL/sSPELL staking rate. Run `python3 ethereum_abracadabra_rate_watcher.py` and try again."
            )

        if balance_refresh:
            time.sleep(10)
            spell["balance"] = get_token_balance(spell_contract, user)
            sspell["balance"] = get_token_balance(sspell_contract, user)
            print("\nAccount Balance:")
            print(
                f"• Token #1: {int(spell['balance']/(10**spell['decimals']))} {spell['symbol']} ({spell['name']})"
            )
            print(
                f"• Token #2: {int(sspell['balance']/(10**sspell['decimals']))} {sspell['symbol']} ({sspell['name']})"
            )
            print()
            balance_refresh = False

        # get quotes and execute SPELL -> sSPELL swaps only if we have a balance of SPELL
        if spell["balance"]:

            try:
                # token0 (x) is sSPELL
                # token1 (y) is SPELL
                x0, y0 = traderjoe_lp.getReserves.call()[0:2]
            except:
                continue

            # finds maximum SPELL input at desired sSPELL/SPELL ratio "C"
            if spell_in := get_tokens_in_for_ratio_out(
                pool_reserves_token0=x0,
                pool_reserves_token1=y0,
                # sSPELL (token0) out
                token0_out=True,
                token0_per_token1=Decimal(
                    str(1 / (base_staking_rate * (1 + THRESHOLD_SPELL_TO_SSPELL)))
                ),
                fee=Decimal("0.003"),
            ):

                if spell_in > spell["balance"]:
                    spell_in = spell["balance"]

                # calculate sSPELL output from SPELL input calculated above (used by token_swap to set amountOutMin)
                sspell_out = get_tokens_out_for_tokens_in(
                    pool_reserves_token0=x0,
                    pool_reserves_token1=y0,
                    quantity_token1_in=spell_in,
                    fee=Decimal("0.003"),
                )

                print(
                    f"*** EXECUTING SWAP FOR {spell_in // (10 ** spell['decimals'])} SPELL ***"
                )
                if token_swap(
                    token_in_quantity=spell_in,
                    token_in_address=spell["address"],
                    token_out_quantity=sspell_out,
                    token_out_address=sspell["address"],
                    router=traderjoe_router,
                ):
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")

        # get quotes and execute sSPELL -> SPELL swaps only if we have a balance of sSPELL
        if sspell["balance"]:

            try:
                # token0 (x) is sSPELL
                # token1 (y) is SPELL
                x0, y0 = traderjoe_lp.getReserves.call()[0:2]
            except:
                continue

            # finds maximum sSPELL input at desired sSPELL/SPELL ratio "C"
            if sspell_in := get_tokens_in_for_ratio_out(
                pool_reserves_token0=x0,
                pool_reserves_token1=y0,
                # SPELL (token1) out
                token1_out=True,
                token0_per_token1=Decimal(
                    str(1 / (base_staking_rate * (1 + THRESHOLD_SSPELL_TO_SPELL)))
                ),
                fee=Decimal("0.003"),
            ):

                if sspell_in > sspell["balance"]:
                    sspell_in = sspell["balance"]

                # calculate SPELL output from sSPELL input calculated above (used by token_swap to set amountOutMin)
                spell_out = get_tokens_out_for_tokens_in(
                    pool_reserves_token0=x0,
                    pool_reserves_token1=y0,
                    quantity_token0_in=sspell_in,
                    fee=Decimal("0.003"),
                )

                print(
                    f"*** EXECUTING SWAP FOR {sspell_in // (10 ** sspell['decimals'])} sSPELL ***"
                )
                if token_swap(
                    token_in_quantity=sspell_in,
                    token_in_address=sspell["address"],
                    token_out_quantity=spell_out,
                    token_out_address=spell["address"],
                    router=traderjoe_router,
                ):
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")

        loop_end = time.time()

        # Control the loop timing more precisely by measuring start and end time and sleeping as needed
        if (loop_end - loop_start) >= LOOP_TIME:
            continue
        else:
            time.sleep(LOOP_TIME - (loop_end - loop_start))
            continue
    #
    # End of arbitrage loop
    #


def account_get_balance(account):
    try:
        return account.balance()
    except Exception as e:
        print(f"Exception in account_get_balance: {e}")


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


def get_approval(token, router, user):
    try:
        return token.allowance.call(user, router.address)
    except Exception as e:
        print(f"Exception in get_approval: {e}")
        return False


def get_token_name(token):
    try:
        return token.name.call()
    except Exception as e:
        print(f"Exception in get_token_name: {e}")
        raise


def get_token_symbol(token):
    try:
        return token.symbol.call()
    except Exception as e:
        print(f"Exception in get_token_symbol: {e}")
        raise


def get_token_balance(token, user):
    try:
        return token.balanceOf.call(user)
    except Exception as e:
        print(f"Exception in get_token_balance: {e}")
        raise


def get_token_decimals(token):
    try:
        return token.decimals.call()
    except Exception as e:
        print(f"Exception in get_token_decimals: {e}")
        raise


def token_approve(token, router, value="unlimited"):
    if DRY_RUN:
        return True

    if value == "unlimited":
        try:
            token.approve(
                router,
                2 ** 256 - 1,
                {"from": user},
            )
            return True
        except Exception as e:
            print(f"Exception in token_approve: {e}")
            raise
    else:
        try:
            token.approve(
                router,
                value,
                {"from": user},
            )
            return True
        except Exception as e:
            print(f"Exception in token_approve: {e}")
            raise


def token_swap(
    token_in_quantity,
    token_in_address,
    token_out_quantity,
    token_out_address,
    router,
):
    if DRY_RUN:
        return True

    try:
        router.swapExactTokensForTokens(
            token_in_quantity,
            int(token_out_quantity * (1 - SLIPPAGE)),
            [token_in_address, token_out_address],
            user.address,
            1000 * int(time.time() + 60 * SECOND),
            {"from": user},
        )
        return True
    except Exception as e:
        print(f"Exception: {e}")
        return False


def get_tokens_in_for_ratio_out(
    pool_reserves_token0,
    pool_reserves_token1,
    token0_out=False,
    token1_out=False,
    token0_per_token1=0,
    fee=Decimal("0.0"),
):
    assert not (token0_out and token1_out)
    assert token0_per_token1

    # token1 input, token0 output
    if token0_out:
        # dy = x0/C - y0/(1-FEE)
        dy = int(
            pool_reserves_token0 / token0_per_token1 - pool_reserves_token1 / (1 - fee)
        )
        if dy > 0:
            return dy
        else:
            return 0

    # token0 input, token1 output
    if token1_out:
        # dx = y0*C - x0/(1-FEE)
        dx = int(
            pool_reserves_token1 * token0_per_token1 - pool_reserves_token0 / (1 - fee)
        )
        if dx > 0:
            return dx
        else:
            return 0


# TODO: make fully generic with one return, instead of labeling 0/1, use in/out
def get_tokens_out_for_tokens_in(
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


# Only executes main loop if this file is called directly
if __name__ == "__main__":
    main()
