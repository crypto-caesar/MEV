'''
A bot that monitors current price of tokens, monitors relevant liquidity pools, 
calculates possible arbitrage pathways and amounts, and sends execution calls to
deployed smart contract. This ties together lessons from smart contract arbitrage 
(flash borrows, using interfaces, Uniswap callback function, deploying a 2-pool 
swap contract, improving/optimizing its security and gas). Can be adjusted for
tokens/chains other than sSPELL/SPELL on TraderJoe.

[DATA STRUCTURES]
- Create objects to represent tokens
- Create objects to represent liquidity pools
- Create objects to represent arbitrage pathways

[MAIN LOOP]
- Calculate relevant token values
- Update all liquidity pools
- Check for possible arbitrage
- Call deployed smart contract for all arbitrage opportunities that exceed some threshold
'''

import sys
import time
import os
import json
from fractions import Fraction
from brownie import accounts, network, Contract
from MEV import *
from dotenv import load_dotenv
load_dotenv()

BROWNIE_NETWORK = "moralis-avax-main-websocket"
BROWNIE_ACCOUNT = "alex_bot"

UPDATE_METHOD = "polling"

DRY_RUN = False

# TODO: Address for the deployed flash arbitrage smart contract
ARB_CONTRACT_ADDRESS = "XXX"

SPELL_CHAINLINK_PRICE_FEED_ADDRESS = "0x4f3ddf9378a4865cf4f28be51e10aecb83b7daee"
WAVAX_CHAINLINK_PRICE_FEED_ADDRESS = "0x0a77230d17318075983913bc2145db16c7366156"

SPELL_CONTRACT_ADDRESS = "0xCE1bFFBD5374Dac86a2893119683F4911a2F7814"
SSPELL_CONTRACT_ADDRESS = "0x3Ee97d514BBef95a2f110e6B9b73824719030f7a"
WAVAX_CONTRACT_ADDRESS = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"

TRADERJOE_POOL_CONTRACT_ADDRESS = "0x033C3Fc1fC13F803A233D262e24d1ec3fd4EFB48"
SUSHISWAP_POOL_CONTRACT_ADDRESS = "0xE5cddBfd3A807691967e528f1d6b7f00b1919e6F"

TRADERJOE_FACTORY_CONTRACT_ADDRESS = "0x9Ad6C38BE94206cA50bb0d90783181662f0Cfa10"
SUSHISWAP_FACTORY_CONTRACT_ADDRESS = "0xc35DADB65012eC5796536bD9864eD8773aBc74C4"
PANGOLIN_FACTORY_CONTRACT_ADDRESS = "0xefa94DE7a4656D787667C749f7E1223D71E9FD88"

#SNOWTRACE_API_KEY = "XXXXX"
#os.environ["SNOWTRACE_TOKEN"] = SNOWTRACE_API_KEY

# How often to run the main loop (in seconds)
LOOP_TIME = 0.25

STAKING_RATE_FILENAME = ".abra_rate"

# ignore arbitrage opportunities below this nominal USD value
MIN_PROFIT_USD = 1.00

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

    print("\nContracts loaded:")

    arb_contract = Contract.from_abi(
        name="",
        address=ARB_CONTRACT_ADDRESS,
        abi=json.loads(
            """
            [ PASTE ABI HERE ]
            """
            # use 'vyper -f json [contract filename]' and paste
        ),
    )

    spell = Erc20Token(
        address=SPELL_CONTRACT_ADDRESS,
        oracle_address=SPELL_CHAINLINK_PRICE_FEED_ADDRESS,
    )

    sspell = Erc20Token(
        address=SSPELL_CONTRACT_ADDRESS,
    )

    wavax = Erc20Token(
        address=WAVAX_CONTRACT_ADDRESS,
        oracle_address=WAVAX_CHAINLINK_PRICE_FEED_ADDRESS,
    )

    sushiswap_lp_sspell_spell = LiquidityPool(
        address=SUSHISWAP_POOL_CONTRACT_ADDRESS,
        name="SushiSwap",
        tokens=[sspell, spell],
        update_method=UPDATE_METHOD,
        fee=Fraction(3, 1000),
    )

    traderjoe_lp_sspell_spell = LiquidityPool(
        address=TRADERJOE_POOL_CONTRACT_ADDRESS,
        name="TraderJoe",
        tokens=[sspell, spell],
        update_method=UPDATE_METHOD,
        fee=Fraction(3, 1000),
    )

    sushi_sspell_to_traderjoe = FlashBorrowToLpSwap(
        borrow_pool=sushiswap_lp_sspell_spell,
        borrow_token=sspell,
        swap_factory_address=TRADERJOE_FACTORY_CONTRACT_ADDRESS,
        swap_token_addresses=[
            SSPELL_CONTRACT_ADDRESS,
            SPELL_CONTRACT_ADDRESS,
        ],
        update_method=UPDATE_METHOD,
    )

    sushi_spell_to_traderjoe = FlashBorrowToLpSwap(
        borrow_pool=sushiswap_lp_sspell_spell,
        borrow_token=spell,
        swap_factory_address=TRADERJOE_FACTORY_CONTRACT_ADDRESS,
        swap_token_addresses=[
            SPELL_CONTRACT_ADDRESS,
            SSPELL_CONTRACT_ADDRESS,
        ],
        update_method=UPDATE_METHOD,
    )

    traderjoe_sspell_to_sushi = FlashBorrowToLpSwap(
        borrow_pool=traderjoe_lp_sspell_spell,
        borrow_token=sspell,
        swap_factory_address=SUSHISWAP_FACTORY_CONTRACT_ADDRESS,
        swap_token_addresses=[
            SSPELL_CONTRACT_ADDRESS,
            SPELL_CONTRACT_ADDRESS,
        ],
        update_method=UPDATE_METHOD,
    )

    traderjoe_spell_to_sushi = FlashBorrowToLpSwap(
        borrow_pool=traderjoe_lp_sspell_spell,
        borrow_token=spell,
        swap_factory_address=SUSHISWAP_FACTORY_CONTRACT_ADDRESS,
        swap_token_addresses=[
            SPELL_CONTRACT_ADDRESS,
            SSPELL_CONTRACT_ADDRESS,
        ],
        update_method=UPDATE_METHOD,
    )

    arbs = [
        sushi_spell_to_traderjoe,
        traderjoe_spell_to_sushi,
    ]

    try:
        with open(STAKING_RATE_FILENAME, "r") as file:
            base_staking_rate = float(file.read().strip())
            print(f"\nEthereum L1 Staking Rate: {base_staking_rate}")
    except FileNotFoundError:
        sys.exit(
            "Cannot load the base Abracadabra SPELL/sSPELL staking rate. Run `python3 abra_rate.py` and try again."
        )

spell.update_price()
wavax.update_price()
sspell.price = base_staking_rate * spell.price

#
# Start of arbitrage loop
#
while True:

    loop_start = time.time()
    
    #Update liquidity pools. Populate the arbs list in the Data Structures section above.
    for arb in arbs:

        arb.update_reserves(
            print_reserves=False,
            print_ratios=False,
            silent=False,
        )
        
    # Check for possible arbitrage
    # This will check the best dictionary using the "borrow_amount" key inside each arbitrage 
    # helper object. If it finds a positive value, it will print all of the relevant info.
    if arb.best["borrow_amount"]:

        arb_profit_usd = (
            arb.best["profit_amount"]
            / (10 ** arb.best["profit_token"].decimals)
            * arb.best["profit_token"].price
        )

        print(
            f"Borrow {arb.best['borrow_amount']/(10**arb.best['borrow_token'].decimals):.2f} {arb.best['borrow_token']} on {arb.borrow_pool}, Profit {arb.best['profit_amount']/(10 ** arb.best['profit_token'].decimals):.2f} {arb.best['profit_token']} (${arb_profit_usd:.2f})"
        )

        print(f"LP Path: {arb.swap_pool_addresses}")
        print(f"Borrow Amount: {arb.best['borrow_amount']}")
        print(f"Borrow Amounts: {arb.best['borrow_pool_amounts']}")
        print(f"Repay Amount: {arb.best['repay_amount']}")
        print(f"Swap Amounts: {arb.best['swap_pool_amounts']}")
        print()
    
    # Execute the swap
    if arb_profit_usd >= MIN_PROFIT_USD and not DRY_RUN:

        print("executing arb")
        try:
            arb_contract.execute(
                arb.borrow_pool.address,
                arb.best["borrow_pool_amounts"],
                arb.best["repay_amount"],
                arb.swap_pool_addresses,
                arb.best["swap_pool_amounts"],
                {"from": alex_bot.address},
            )
        except Exception as e:
            print(e)
        finally:
            break
        
        # Refresh price info, loop timing
        try:
            wavax.update_price()
            spell.update_price()
            sspell.price = base_staking_rate * spell.price
        except Exception as e:
            print(f"(update_price) Exception: {e}")

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
