'''
a traditional bot that executes multi-step arbitrage across 
2+ blocks.

It monitors the gOHM-wsOHM pool for favorable ratios, executing 
a swap whenever possible. Limitations:
    - It uses the router (non-zero security risk)
    - Migrating and swapping are separate actions.
'''

import sys, time, os
from decimal import Decimal
from fractions import Fraction
from brownie import accounts, network, Contract
from alex_bot import *
from dotenv import load_dotenv
load_dotenv()

# BROWNIE_NETWORK = "avax-main"
# BROWNIE_ACCOUNT = "alex_bot"

SNOWTRACE_TOKEN = os.getenv("SNOWTRACE_TOKEN")

# Contract addresses (verify on Snowtrace)
TRADERJOE_ROUTER_CONTRACT_ADDRESS = "0x60aE616a2155Ee3d9A68541Ba4544862310933d4"
TRADERJOE_POOL_CONTRACT_ADDRESS = "0x5D577C817bD4003a9b794c33eF45D0D6D4138bea"
OLYMPUS_CONTRACT_ADDRESS = "0xb10209bfbb37d38ec1b5f0c964e489564e223ea7"
GOHM_CONTRACT_ADDRESS = "0x321E7092a180BB43555132ec53AaA65a5bF84251"
WSOHM_CONTRACT_ADDRESS = "0x8cd309e14575203535ef120b5b0ab4dded0c2073"

SECOND = 1
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR

# tolerated slippage in swap price (0.05%)
SLIPPAGE = Decimal("0.000005")
# Simulate swaps and approvals
DRY_RUN = False
# Quit after the first successful trade
ONE_SHOT = False
# How often to run the main loop (in seconds)
LOOP_TIME = 5

def main():

    """ try:
        network.connect(BROWNIE_NETWORK)
    except:
        sys.exit(
            "Could not connect! Verify your Brownie network settings using 'brownie networks list'"
        ) """

    try:
        #alex_bot = accounts.load(BROWNIE_ACCOUNT)
        alex_bot = accounts[0]
    except:
        sys.exit(
            "Could not load account! Verify your Brownie account settings using 'brownie accounts list'"
        )

    print()
    print("Contracts:")
    gohm = Erc20Token(address=GOHM_CONTRACT_ADDRESS, user=alex_bot)
    wsohm = Erc20Token(address=WSOHM_CONTRACT_ADDRESS, user=alex_bot, min_abi=True)

    traderjoe_router = Router(
        address=TRADERJOE_ROUTER_CONTRACT_ADDRESS,
        name="TraderJoe Router",
        user=alex_bot,
    )

    traderjoe_lp = LiquidityPool(
        address=TRADERJOE_POOL_CONTRACT_ADDRESS,
        name="TraderJoe: gOHM-wsOHM",
        router=traderjoe_router,
        tokens=[gohm, wsohm],
        fee=Fraction(3, 1000),
    )

    olympus_migrator = Contract.from_explorer(
        OLYMPUS_CONTRACT_ADDRESS
    )

    tokens = [gohm, wsohm]
    lps = [traderjoe_lp]

    if not DRY_RUN:
        if (gohm.balance == 0) and (wsohm.balance == 0):
            sys.exit("No tokens found!")

    # Confirm approvals for tokens
    print()
    print("Approvals:")
    for router in (traderjoe_router,):
        if (
            not gohm.get_approval(
                external_address=router.address,
            )
            and not DRY_RUN
        ):
            gohm.set_approval(
                external_address=router.address,
                value=-1,
            )
        else:
            print(f"• {gohm} on {router} OK")

        if (
            not wsohm.get_approval(
                external_address=router.address,
            )
            and not DRY_RUN
        ):
            wsohm.set_approval(
                external_address=router.address,
                value=-1,
            )
        else:
            print(f"• {wsohm} on {router} OK")
    print()

    print()
    print("Swap Targets:")
    traderjoe_lp.set_swap_target(
        token_in_qty=1,
        token_in=gohm,
        token_out_qty=1.01,
        token_out=wsohm,
    )

    traderjoe_lp.set_swap_target(
        token_in_qty=1,
        token_in=wsohm,
        token_out_qty=1.01,
        token_out=gohm,
    )

    network.priority_fee("0.1 gwei")
    balance_refresh = True

    #
    # Start of arbitrage loop
    #
    while True:

        loop_start = time.time()

        if balance_refresh:
            gohm.update_balance()
            wsohm.update_balance()

            print()
            print("Account Balance:")
            for token in tokens:
                print(
                    f"• {token.normalized_balance} "
                    f"{token.symbol} "
                    f"({token.name})"
                )
            print()
            balance_refresh = False

        for lp in lps:
            lp.update_reserves(print_reserves=False)

            if lp.token0.balance and lp.token0_max_swap:
                token_in = lp.token0
                token_out = lp.token1
                # finds maximum token1 input at desired ratio
                token_in_qty = min(
                    lp.token0.balance,
                    lp.token0_max_swap,
                )
                # calculate output from maximum input above
                token_out_qty = lp.calculate_tokens_out_from_tokens_in(
                    token_in=lp.token0,
                    token_in_quantity=token_in_qty,
                )
                print(
                    f"*** SWAP ON {str(lp.router).upper()} "
                    f"OF {token_in_qty / (10 ** token_in.decimals)} {token_in} "
                    f"FOR {token_out_qty / (10 ** token_out.decimals)} {token_out} ***"
                )
                if not DRY_RUN:
                    lp.router.token_swap(
                        token_in_quantity=token_in_qty,
                        token_in_address=token_in.address,
                        token_out_quantity=token_out_qty,
                        token_out_address=token_out.address,
                        deadline=60 * SECOND,
                        slippage=SLIPPAGE,
                    )
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")
                    break

            if lp.token1.balance and lp.token1_max_swap:
                token_in = lp.token1
                token_out = lp.token0
                # finds maximum token1 input at desired ratio
                token_in_qty = min(
                    lp.token1.balance,
                    lp.token1_max_swap,
                )
                # calculate output from maximum input above
                token_out_qty = lp.calculate_tokens_out_from_tokens_in(
                    token_in=lp.token1,
                    token_in_quantity=token_in_qty,
                )
                print(
                    f"*** EXECUTING SWAP ON {str(lp.router).upper()} "
                    f"OF {token_in_qty / (10 ** token_in.decimals)} {token_in} "
                    f"FOR {token_out_qty / (10 ** token_out.decimals)} {token_out} ***"
                )
                if not DRY_RUN:
                    lp.router.token_swap(
                        token_in_quantity=token_in_qty,
                        token_in_address=token_in.address,
                        token_out_quantity=token_out_qty,
                        token_out_address=token_out.address,
                        deadline=60 * SECOND,
                        slippage=SLIPPAGE,
                    )
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")
                    break

            # holding wsOHM only and the ratio favors a
            # swap from gOHM to wsOHM, migrate that exact
            # amount of wsOHM -> gOHM via the Olympus contract
            if lp.token0.balance == 0 and lp.token1.balance and lp.token0_max_swap:
                olympus_migrator.migrate(
                    min(lp.token0_max_swap, lp.token1.balance),
                    {"from": alex_bot},
                )
                balance_refresh = True
                break

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

# Only executes main loop if this file is called directly
if __name__ == "__main__":
    main()
