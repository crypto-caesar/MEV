'''
a generic “limit order” bot that will wait for favorable ratios 
and executes one-time trades. 

WETH for MAGIC on Sushiswap on Arbitrum
'''

import sys, time, os
from decimal import Decimal
from fractions import Fraction
from brownie import accounts, network
from alex_bot import *
from dotenv import dotenv_values

# Contract addresses (verify on Arbiscan)
SUSHISWAP_ROUTER_CONTRACT_ADDRESS = "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506"
SUSHISWAP_POOL_CONTRACT_ADDRESS = "0xB7E50106A5bd3Cf21AF210A755F9C8740890A8c9"
WETH_CONTRACT_ADDRESS = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
MAGIC_CONTRACT_ADDRESS = "0x539bdE0d7Dbd336b79148AA742883198BBF60342"

SLIPPAGE = Decimal("0.001")  # tolerated slippage in swap price (0.1%)

# Simulate swaps and approvals
DRY_RUN = False
# Quit after the first successful trade
ONE_SHOT = False
# How often to run the main loop (in seconds)
LOOP_TIME = 0.25

CONFIG_FILE = "limit_bot.env"
BROWNIE_NETWORK = dotenv_values(CONFIG_FILE)["BROWNIE_NETWORK"]
BROWNIE_ACCOUNT = dotenv_values(CONFIG_FILE)["BROWNIE_ACCOUNT"]
os.environ["ARBISCAN_TOKEN"] = dotenv_values(CONFIG_FILE)["ARBISCAN_API_KEY"]


def main():

    try:
        network.connect(BROWNIE_NETWORK)
    except:
        sys.exit(
            "Could not connect! Verify your Brownie network settings using 'brownie networks list'"
        )

    try:
        alex_bot = accounts.load(BROWNIE_ACCOUNT)
    except:
        sys.exit(
            "Could not load account! Verify your Brownie account settings using 'brownie accounts list'"
        )

    magic = Erc20Token(
        address=MAGIC_CONTRACT_ADDRESS,
        user=alex_bot,
        # abi=ERC20
    )

    weth = Erc20Token(
        address=WETH_CONTRACT_ADDRESS,
        user=alex_bot,
        # abi=ERC20
    )

    tokens = [
        magic,
        weth,
    ]

    sushiswap_router = Router(
        address=SUSHISWAP_ROUTER_CONTRACT_ADDRESS,
        name="SushiSwap Router",
        user=alex_bot,
        abi=UNISWAPV2_ROUTER,
    )

    sushiswap_lp = LiquidityPool(
        address=SUSHISWAP_POOL_CONTRACT_ADDRESS,
        name="SushiSwap: MAGIC-WETH",
        router=sushiswap_router,
        abi=UNISWAPV2_LP_ABI,
        tokens=tokens,
        fee=Fraction(3, 1000),
    )

    lps = [
        sushiswap_lp,
    ]

    routers = [
        sushiswap_router,
    ]

    # Confirm approvals for all tokens on every router
    print()
    print("Approvals:")
    for router in routers:
        for token in tokens:
            if not token.get_approval(external_address=router.address) and not DRY_RUN:
                token.set_approval(external_address=router.address, value=-1)
            else:
                print(f"{token} on {router} OK")
    print()
    print("Swap Targets:")
    for lp in lps:
        lp.set_swap_target(
            token_in_qty=1,
            token_in=weth,
            token_out_qty=1500,
            token_out=magic,
        )

    balance_refresh = True
    
    #
    # Start of main loop
    #

    while True:

        try:
            if network.is_connected():
                pass
            else:
                print("Network connection lost! Reconnecting...")
                if network.connect(BROWNIE_NETWORK):
                    pass
                else:
                    time.sleep(5)
                    continue
        except:
            # restart loop
            continue

        loop_start = time.time()

        if balance_refresh:
            print()
            print("Account Balance:")
            for token in tokens:
                token.update_balance()
                print(f"• {token.normalized_balance} {token.symbol} ({token.name})")
                balance_refresh = False

        for lp in lps:
            lp.update_reserves(print_reserves=False)

            if lp.token0.balance and lp.token0_max_swap:
                token_in = lp.token0
                token_out = lp.token1
                # finds maximum token1 input at desired ratio
                token_in_qty = min(lp.token0.balance, lp.token0_max_swap)
                # calculate output from maximum input above
                token_out_qty = lp.calculate_tokens_out_from_tokens_in(
                    token_in=lp.token0,
                    token_in_quantity=token_in_qty,
                )
                print(
                    f"*** SWAP ON {str(lp.router).upper()} OF {token_in_qty / (10 ** token_in.decimals)} {token_in} FOR {token_out_qty / (10 ** token_out.decimals)} {token_out} ***"
                )
                if not DRY_RUN:
                    lp.router.token_swap(
                        token_in_quantity=token_in_qty,
                        token_in_address=token_in.address,
                        token_out_quantity=token_out_qty,
                        token_out_address=token_out.address,
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
                token_in_qty = min(lp.token1.balance, lp.token1_max_swap)
                # calculate output from maximum input above
                token_out_qty = lp.calculate_tokens_out_from_tokens_in(
                    token_in=lp.token1,
                    token_in_quantity=token_in_qty,
                )
                print(
                    f"*** EXECUTING SWAP ON {str(lp.router).upper()} OF {token_in_qty / (10 ** token_in.decimals)} {token_in} FOR {token_out_qty / (10 ** token_out.decimals)} {token_out} ***"
                )
                if not DRY_RUN:
                    lp.router.token_swap(
                        token_in_quantity=token_in_qty,
                        token_in_address=token_in.address,
                        token_out_quantity=token_out_qty,
                        token_out_address=token_out.address,
                        slippage=SLIPPAGE,
                    )
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")
                    break

        loop_end = time.time()

        # Control the loop timing more precisely by measuring start and end time and sleeping as needed
        if (loop_end - loop_start) >= LOOP_TIME:
            continue
        else:
            time.sleep(LOOP_TIME - (loop_end - loop_start))
            continue

    #
    # End of main loop
    #



# Only executes main loop if this file is called directly
if __name__ == "__main__":
    main()
