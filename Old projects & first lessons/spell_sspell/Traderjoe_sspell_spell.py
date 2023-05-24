'''
Purpose: Bot to check the rates of spell and sspell; programmatically execute swaps if the conversion
criteria are met
'''

import sys
import time
import datetime
import requests
import os
from brownie import *
from dotenv import load_dotenv
load_dotenv()

# GLOBAL CONSTANTS

# Contract addresses (verify on Snowtrace)
TRADERJOE_ROUTER_CONTRACT_ADDRESS = "0x60aE616a2155Ee3d9A68541Ba4544862310933d4"
SPELL_CONTRACT_ADDRESS = "0xce1bffbd5374dac86a2893119683f4911a2f7814"
SSPELL_CONTRACT_ADDRESS = "0x3ee97d514bbef95a2f110e6b9b73824719030f7a"

#SNOWTRACE_API_KEY = ""
#os.environ["SNOWTRACE_TOKEN"] = SNOWTRACE_API_KEY

# HELPER VALUES
SECOND = 1
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR
PERCENT = 0.01

# Swap Thresholds and slippage

# a zero value will trigger a swap when the ratio matches base_staking_rate
# a negative value will trigger a swap when the rate is below base_staking_rate
# a positive value will trigger a swap when the rate is above base_staking_rate
THRESHOLD_SPELL_TO_SSPELL = 0.2 * PERCENT

# sSPELL -> SPELL swap targets
# a positive value will trigger a (sSPELL -> SPELL) swap when the ratio is above base_staking_rate
THRESHOLD_SSPELL_TO_SPELL = 1.2 * PERCENT

# tolerated slippage in swap price 
SLIPPAGE = 0.1 * PERCENT

STAKING_RATE_FILENAME = ".abra_rate"

#Bot options

# Simulate swaps and approvals
DRY_RUN = False

# Quit after the first successful trade
ONE_SHOT = False

# How often to run the main loop (in seconds)
LOOP_TIME = 1.0

#MAIN

def main():

    global spell_contract
    global sspell_contract
    global traderjoe_router_contract
    global traderjoe_lp_contract
    global spell
    global sspell

    try:
        network.connect("avax-main")
        # Avalanche supports EIP-1559 transactions, so we set the priority fee
        # and allow the base fee to adjust as needed
        network.priority_fee('5 gwei')
        # Can set a limit on maximum fee, if desired
        #network.max_fee('200 gwei')
    except:
        sys.exit(
            "Could not connect to Avalanche! Verify that brownie lists the Avalanche Mainnet using 'brownie networks list'"
        )

    try:
        global user
        user = accounts.load("alex_bot")
    except:
        sys.exit(
            "Could not load account! Verify that your account is listed using 'brownie accounts list' and that you are using the correct password. If you have not added an account, run 'brownie accounts' now."
        )

    print("\nContracts loaded:")
    spell_contract = contract_load(SPELL_CONTRACT_ADDRESS, "Avalanche Token: SPELL")
    sspell_contract = contract_load(SSPELL_CONTRACT_ADDRESS, "Avalanche Token: sSPELL")
    router_contract = contract_load(
        TRADERJOE_ROUTER_CONTRACT_ADDRESS, "TraderJoe: Router"
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

    if get_approval(spell["contract"], router_contract, user):
        print(f"• {spell['symbol']} OK")
    else:
        token_approve(spell["contract"], router_contract)

    if get_approval(sspell["contract"], router_contract, user):
        print(f"• {sspell['symbol']} OK")
    else:
        token_approve(sspell["contract"], router_contract)
    
    try:
        with open(STAKING_RATE_FILENAME, "r") as file:
        base_staking_rate = float(file.read().strip())
        print(f"\nEthereum L1 Staking Rate: {base_staking_rate}")
    except FileNotFoundError:
        sys.exit(
        "Cannot load the base Abracadabra SPELL/sSPELL staking rate. Run `python3 abra_rate.py` and try again."
    )

    balance_refresh = True

    #
    # Start of arbitrage loop
    #
    while True:

        loop_start = time.time()

        try:
            with open(STAKING_RATE_FILENAME, "r") as file:
                if (result := float(file.read().strip())) != base_staking_rate:
                    base_staking_rate = result
                    print(f"Updated staking rate: {base_staking_rate}")
        except FileNotFoundError:
            sys.exit(
                "Cannot load the base Abracadabra SPELL/sSPELL staking rate. Run `python3 abra_rate.py` and try again."
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
            last_ratio_spell_to_sspell = 0
            last_ratio_sspell_to_spell = 0

        # get quotes and execute SPELL -> sSPELL swaps only if we have a balance of SPELL
        if spell["balance"]:

            if result := get_swap_rate(
                token_in_quantity=spell["balance"],
                token_in_address=spell["address"],
                token_out_address=sspell["address"],
                router=router_contract,
            ):
                spell_in, sspell_out = result
                ratio_spell_to_sspell = round(sspell_out / spell_in, 4)

                # print and save any updated swap values since last loop
                if ratio_spell_to_sspell != last_ratio_spell_to_sspell:
                    print(
                        f"{datetime.datetime.now().strftime('[%I:%M:%S %p]')} {spell['symbol']} → {sspell['symbol']}: ({ratio_spell_to_sspell:.4f}/{1 / (base_staking_rate * (1 + THRESHOLD_SPELL_TO_SSPELL)):.4f})"
                    )
                    last_ratio_spell_to_sspell = ratio_spell_to_sspell
            else:
                # abandon the for loop to avoid re-using stale data
                break

            # execute SPELL -> sSPELL arb if trigger is satisfied
            if ratio_spell_to_sspell >= 1 / (
                base_staking_rate * (1 + THRESHOLD_SPELL_TO_SSPELL)
            ):
                print(
                    f"*** EXECUTING SWAP OF {int(spell_in / (10**spell['decimals']))} {spell['symbol']} AT BLOCK {chain.height} ***"
                )
                if token_swap(
                    token_in_quantity=spell_in,
                    token_in_address=spell["address"],
                    token_out_quantity=sspell_out,
                    token_out_address=sspell["address"],
                    router=router_contract,
                ):
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")

        # get quotes and execute sSPELL -> SPELL swaps only if we have a balance of sSPELL
        if sspell["balance"]:

            if result := get_swap_rate(
                token_in_quantity=sspell["balance"],
                token_in_address=sspell["address"],
                token_out_address=spell["address"],
                router=router_contract,
            ):
                sspell_in, spell_out = result
                ratio_sspell_to_spell = round(spell_out / sspell_in, 4)

                # print and save any updated swap values since last loop
                if ratio_sspell_to_spell != last_ratio_sspell_to_spell:
                    print(
                        f"{datetime.datetime.now().strftime('[%I:%M:%S %p]')} {sspell['symbol']} → {spell['symbol']}: ({ratio_sspell_to_spell:.4f}/{base_staking_rate * (1 + THRESHOLD_SSPELL_TO_SPELL):.4f})"
                    )
                    last_ratio_sspell_to_spell = ratio_sspell_to_spell
            else:
                # abandon the for loop to avoid re-using stale data
                break

            # execute sSPELL -> SPELL arb if trigger is satisfied
            if ratio_sspell_to_spell >= base_staking_rate * (
                1 + THRESHOLD_SSPELL_TO_SPELL
            ):
                print(
                    f"*** EXECUTING SWAP OF {int(sspell_in/(10**sspell['decimals']))} {sspell['symbol']} AT BLOCK {chain.height} ***"
                )
                if token_swap(
                    token_in_quantity=sspell_in,
                    token_in_address=sspell["address"],
                    token_out_quantity=spell_out,
                    token_out_address=spell["address"],
                    router=router_contract,
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

# FUNCTION DEFINITIONS
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


def get_swap_rate(token_in_quantity, token_in_address, token_out_address, router):
    try:
        return router.getAmountsOut(
            token_in_quantity, [token_in_address, token_out_address]
        )
    except Exception as e:
        print(f"Exception in get_swap_rate: {e}")
        return False


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
            int(1000 * (time.time()) + 30 * SECOND),
            {"from": user},
        )
        return True
    except Exception as e:
        print(f"Exception: {e}")
        return False
    
# Only executes main loop if this file is called directly
if __name__ == "__main__":
    main()
