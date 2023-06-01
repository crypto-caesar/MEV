'''
An atomic (single block) version of the first gOHM-wsOHM multi-step 
arbitrage bot, using a general-purpose bundle executor.

This lets us use the pair contract instead of the router. Goals:
    - Monitor the gOHM-wsOHM pair (done in avalanche_gohm_wsohm.py)
    - Determine when to execute a swap of either token, or migrate 
        some quantity (done in avalanche_gohm_wsohm.py)
    - Generate a payload for a direct token swap at the contract
    - Generate a payload for a token migration
    - Submit the payloads to the bundle executor
'''

import sys, time, os, json

from decimal import Decimal
from fractions import Fraction
from brownie import accounts, network, Contract
from alex_bot import *
from dotenv import load_dotenv
load_dotenv()

BROWNIE_NETWORK = "avax-main-fork-atblock"
BROWNIE_ACCOUNT = "alex_bot"

# Contract addresses (verify on Snowtrace)
TRADERJOE_POOL_CONTRACT_ADDRESS = "0x5D577C817bD4003a9b794c33eF45D0D6D4138bea"
EXECUTOR_CONTRACT_ADDRESS = "[set this after you've deployed your contract to mainnet]"
OLYMPUS_CONTRACT_ADDRESS = "0xb10209bfbb37d38ec1b5f0c964e489564e223ea7"
GOHM_CONTRACT_ADDRESS = "0x321E7092a180BB43555132ec53AaA65a5bF84251"
WSOHM_CONTRACT_ADDRESS = "0x8cd309e14575203535ef120b5b0ab4dded0c2073"

TEST_TOKEN = WSOHM_CONTRACT_ADDRESS
TEST_TOKEN_AMOUNT = 1 * 10 ** 18
TEST_TOKEN_FROM = OLYMPUS_CONTRACT_ADDRESS

#os.environ["SNOWTRACE_TOKEN"] = "[set this to your Snowtrace API key]"

# Simulate swaps and approvals
DRY_RUN = False
# Quit after the first successful trade
ONE_SHOT = False
# How often to run the main loop (in seconds)
LOOP_TIME = 5


def main():

    try:
        network.connect(BROWNIE_NETWORK)
    except:
        sys.exit(
            "Could not connect! Verify your Brownie network settings using 'brownie networks list'"
        )

    if network.main.CONFIG.network_type == "live":
        try:
            network.priority_fee("0.1 gwei")
            alex_bot = accounts.load("alex_bot")
            bundle_executor = Contract.from_abi(
                name="Bundle Executor",
                address=EXECUTOR_CONTRACT_ADDRESS,
                abi=json.loads(
                    """
                    [{"stateMutability": "nonpayable", "type": "constructor", "inputs": [], "outputs": []}, {"stateMutability": "payable", "type": "function", "name": "execute", "inputs": [{"name": "payloads", "type": "(address,bytes,uint256)[]"}], "outputs": []}, {"stateMutability": "payable", "type": "function", "name": "execute", "inputs": [{"name": "payloads", "type": "(address,bytes,uint256)[]"}, {"name": "return_on_first_failure", "type": "bool"}], "outputs": []}, {"stateMutability": "payable", "type": "function", "name": "execute", "inputs": [{"name": "payloads", "type": "(address,bytes,uint256)[]"}, {"name": "return_on_first_failure", "type": "bool"}, {"name": "execute_all_payloads", "type": "bool"}], "outputs": []}]
                    """
                ),
            )
        except:
            sys.exit(
                "Could not load account! Verify your Brownie account settings using 'brownie accounts list'"
            )
    else:
        alex_bot = accounts[0]

        bundle_executor = brownie.project.load().executor.deploy(
            {"from": alex_bot},
        )

        Contract.from_explorer(TEST_TOKEN).transfer(
            bundle_executor.address,
            TEST_TOKEN_AMOUNT,
            {"from": TEST_TOKEN_FROM},
        )

    print()
    print("Contracts:")

    gohm = Erc20Token(
        address=GOHM_CONTRACT_ADDRESS,
        user=bundle_executor,
    )

    wsohm = Erc20Token(
        address=WSOHM_CONTRACT_ADDRESS,
        user=bundle_executor,
        abi=ERC20,
    )

    tj_lp = LiquidityPool(
        address=TRADERJOE_POOL_CONTRACT_ADDRESS,
        name="TraderJoe: gOHM-wsOHM",
        tokens=[gohm, wsohm],
        fee=Fraction(3, 1000),
    )

    olympus_migrator = Contract.from_explorer(
        OLYMPUS_CONTRACT_ADDRESS)

    tokens = [gohm, wsohm]

    if not DRY_RUN:
        if (gohm.balance == 0) and (wsohm.balance == 0):
            sys.exit("No tokens found!")

    print()
    print("Swap Targets:")
    tj_lp.set_swap_target(
        token_in_qty=1,
        token_in=gohm,
        token_out_qty=1.01,
        token_out=wsohm,
    )

    tj_lp.set_swap_target(
        token_in_qty=1,
        token_in=wsohm,
        token_out_qty=1.01,
        token_out=gohm,
    )

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

        tj_lp.update_reserves(print_reserves=False)

        # token0: gOHM
        # token1: wsOHM

        if gohm.balance and tj_lp.token0_max_swap:

            print("executing gOHM -> wsOHM swap")

            token_in = gohm

            # finds maximum token0 input at desired ratio
            # (either the full token balance of our contract,
            # or the maximum possible swap at target ratio)
            token_in_qty = min(
                gohm.balance,
                tj_lp.token0_max_swap,
            )
            # calculate token1 output from the calculated input above
            token_out_qty = tj_lp.calculate_tokens_out_from_tokens_in(
                token_in=gohm,
                token_in_quantity=token_in_qty,
            )

            if not DRY_RUN:

                transfer_payload = token_in._contract.transfer.encode_input(
                    tj_lp.address,
                    token_in_qty,
                )

                swap_payload = tj_lp._contract.swap.encode_input(
                    0,  # gOHM deposit
                    token_out_qty,  # wsOHM withdrawal
                    bundle_executor.address,
                    b"",
                )

                try:
                    # attempt the arbitrage
                    tx = bundle_executor.execute(
                        [
                            # transfer payload
                            (
                                token_in.address,
                                transfer_payload,
                                0,
                            ),
                            # swap payload
                            (
                                tj_lp.address,
                                swap_payload,
                                0,
                            ),
                        ],
                        {"from": alex_bot.address},
                    )
                    print(tx.info())
                except Exception as e:
                    print(e)
                else:
                    # this block executes if the swap was successful
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")
                finally:
                    # this block always executes
                    # (return to the top of the main loop)
                    continue

        if wsohm.balance and tj_lp.token1_max_swap:

            print("executing wsOHM -> gOHM swap")

            token_in = wsohm

            # finds maximum token1 input at desired ratio
            token_in_qty = min(
                wsohm.balance,
                tj_lp.token1_max_swap,
            )
            # calculate output from maximum input above
            token_out_qty = tj_lp.calculate_tokens_out_from_tokens_in(
                token_in=wsohm,
                token_in_quantity=token_in_qty,
            )

            if not DRY_RUN:

                transfer_payload = token_in._contract.transfer.encode_input(
                    tj_lp.address,
                    token_in_qty,
                )

                swap_payload = tj_lp._contract.swap.encode_input(
                    token_out_qty,  # gOHM withdrawal
                    0,  # wsOHM deposit
                    bundle_executor.address,
                    b"",
                )

                try:
                    # attempt the arbitrage
                    tx = bundle_executor.execute(
                        [
                            # transfer payload
                            (
                                token_in.address,
                                transfer_payload,
                                0,
                            ),
                            # swap payload
                            (
                                tj_lp.address,
                                swap_payload,
                                0,
                            ),
                        ],
                        {"from": alex_bot.address},
                    )
                    print(tx.info())
                except Exception as e:
                    print(e)
                else:
                    # executes if the swap was successful
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")
                finally:
                    # this block always executes
                    # (return to the top of the main loop)
                    continue

        # holding wsOHM only and the ratio favors a swap
        # from gOHM to wsOHM, first migrate wsOHM->gOHM using
        # the olympus contract, then swap normally
        if gohm.balance == 0 and wsohm.balance and tj_lp.token0_max_swap:

            print("executing wsOHM migrate -> gOHM -> wsOHM swap")

            migrate_amount = min(
                tj_lp.token0_max_swap,
                wsohm.balance,
            )
            approve_payload = wsohm._contract.approve.encode_input(
                olympus_migrator.address,
                migrate_amount,
            )

            token_in = gohm

            token_in_qty = migrate_amount
            # calculate token1 output from the calculated input above
            token_out_qty = tj_lp.calculate_tokens_out_from_tokens_in(
                token_in=gohm,
                token_in_quantity=token_in_qty,
            )

            migrate_payload = olympus_migrator.migrate.encode_input(
                migrate_amount,
            )

            transfer_payload = gohm._contract.transfer.encode_input(
                tj_lp.address,
                token_in_qty,
            )

            swap_payload = tj_lp._contract.swap.encode_input(
                0,  # gOHM deposit
                token_out_qty,  # wsOHM withdrawal
                bundle_executor.address,
                b"",
            )

            if not DRY_RUN:
                try:
                    # attempt the arbitrage
                    tx = bundle_executor.execute(
                        [
                            # approve payload
                            (
                                wsohm.address,
                                approve_payload,
                                0,
                            ),
                            # migrate payload
                            (
                                olympus_migrator.address,
                                migrate_payload,
                                0,
                            ),
                            # transfer payload
                            (
                                token_in.address,
                                transfer_payload,
                                0,
                            ),
                            # swap payload
                            (
                                tj_lp.address,
                                swap_payload,
                                0,
                            ),
                        ],
                        {"from": alex_bot.address},
                    )
                    print(tx.info())
                except Exception as e:
                    print(e)
                else:
                    # executes if the swap was successful
                    balance_refresh = True
                    if ONE_SHOT:
                        sys.exit("single shot complete!")
                finally:
                    # this block always executes
                    # (return to the top of the main loop)
                    continue

        loop_end = time.time()

        # Control the loop timing more precisely by
        # measuring start and end time and sleeping as needed
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
