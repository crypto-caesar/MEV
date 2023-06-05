''' 
test for uniswap_lp_cycle.py: script that simulates the arb, generates 
the amounts, then compares the values to the arb calculation inside the helper
'''

import brownie
import alex_bot as bot
import os
import web3
from dotenv import load_dotenv
load_dotenv()

#ETHERSCAN_API_KEY = "[redacted]"
#os.environ["ETHERSCAN_TOKEN"] = ETHERSCAN_API_KEY

brownie.network.connect("mainnet-fork")

wbtc = bot.Erc20Token(
    address="0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
)
weth = bot.Erc20Token(
    address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
)

w3 = web3.Web3()

bot_account = brownie.accounts[0]

arb_contract = brownie.project.load().ethereum_executor_v3.deploy(
    {"from": bot_account, "value": 10 * 10**18},
)

weth_balance = weth._contract.balanceOf(arb_contract)

arbs = []

arbs.append(
    bot.arbitrage.UniswapLpCycle.from_addresses(
        input_token_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        swap_pool_addresses=[
            ("0xbb2b8038a1640196fbe3e38816f3e67cba72d940", "V2"),
            ("0x4585FE77225b41b697C938B018E2Ac67Ac5a20c0", "V3"),
        ],
        max_input=weth_balance,
    )
)

arbs.append(
    bot.arbitrage.UniswapLpCycle.from_addresses(
        input_token_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        swap_pool_addresses=[
            ("0x4585FE77225b41b697C938B018E2Ac67Ac5a20c0", "V3"),
            ("0xbb2b8038a1640196fbe3e38816f3e67cba72d940", "V2"),
        ],
        max_input=weth_balance,
    )
)

arbs.append(
    bot.arbitrage.UniswapLpCycle.from_addresses(
        input_token_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        swap_pool_addresses=[
            ("0xbb2b8038a1640196fbe3e38816f3e67cba72d940", "V2"),
            ("0xCBCdF9626bC03E24f779434178A73a0B4bad62eD", "V3"),
        ],
        max_input=weth_balance,
    )
)

arbs.append(
    bot.arbitrage.UniswapLpCycle.from_addresses(
        input_token_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        swap_pool_addresses=[
            ("0xCBCdF9626bC03E24f779434178A73a0B4bad62eD", "V3"),
            ("0xbb2b8038a1640196fbe3e38816f3e67cba72d940", "V2"),
        ],
        max_input=weth_balance,
    )
)

arbs.append(
    bot.arbitrage.UniswapLpCycle.from_addresses(
        input_token_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        swap_pool_addresses=[
            ("0xCBCdF9626bC03E24f779434178A73a0B4bad62eD", "V3"),
            ("0x4585FE77225b41b697C938B018E2Ac67Ac5a20c0", "V3"),
        ],
        max_input=weth_balance,
    )
)

arbs.append(
    bot.arbitrage.UniswapLpCycle.from_addresses(
        input_token_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        swap_pool_addresses=[
            ("0x4585FE77225b41b697C938B018E2Ac67Ac5a20c0", "V3"),
            ("0xCBCdF9626bC03E24f779434178A73a0B4bad62eD", "V3"),
        ],
        max_input=weth_balance,
    )
)

for arb in arbs:
    arb.auto_update()
    status, (swap_input, arb_profit) = arb.calculate_arbitrage()

    arb_payloads = arb.generate_payloads(
        from_address=arb_contract.address
    )

    tx = arb_contract.execute_payloads(
        arb_payloads,
        {"from": bot_account}
    )

    print(tx.info())
