'''
A helper class to manage the tedious parts of flash borrow arbitrage.
It has the following structure:
    - An internal `LiquidityPool` object called `borrow_pool` that 
        represents the LP where our flash borrow begins. The borrowed 
        token is represented by an `Erc20Token` object called 
        `borrow_token`.
    - An internal list of `LiquidityPool` objects called `pools` that
        allow the helper to loop through and update the reserves for 
        all pools involved in the proposed swap path.
    - An internal dictionary called `best` which contains many 
        key-value pairs to represent the best-available arbitrage 
        through this LP path:
            - `borrow_amount` = the integer value of the optimal borrow
            - `borrow_token` = the `Erc20Token` object representing the 
                borrowed token inside `borrow_pool`
            - `borrow_pool_amounts` = a list of the requested token output
                for the borrowing pool. This is sent to `swap()` by the 
                deployed smart contract.
            - `repay_amount` = amount repaid to the borrowing pool
            - `profit_amount` = the difference in the LP swap output 
                and the `repay_amount`
            - `profit_token` = the `Erc20Token` object representing 
                the profit from the opportunity
            - `swap_pools` = a list of `LiquidityPool` objects 
            - `swap_pool_amounts` = a list of lists, containing 
                integer values to be passed to the `swap()` function 
                at each pool in `swap_pools`

The helper has a few useful methods:
    - `update_reserves()` = loops through all of the internal pool 
        objects (`borrow_pool` and each pool in the `pools` list). 
        If any of the internal reserves change during a check, 
        it will set an internal variable `recalculate` to `True`, 
        which will execute a call to the next function.
    - `_calculate_arbitrage()` = an internal function which will 
        perform a SciPy optimization and determine the best possible 
        arbitrage at the current pool states.
    - `calculate_multipool_tokens_out_from_tokens_in()` = returns 
        the output of a multi-LP token swap through any pool path 
        (after checking that the pool path actually exists).
    - `_build_multipool_amounts_out()` = generates `swap_pool_amounts`
'''

from fractions import Fraction
from typing import List, Optional

from brownie import Contract  # type: ignore
from scipy import optimize  # type: ignore

from MEV.token import Erc20Token
from MEV.uniswap.v2.liquidity_pool import LiquidityPool


class FlashBorrowToLpSwap:
    def __init__(
        self,
        borrow_pool: LiquidityPool,
        borrow_token: Erc20Token,
        swap_factory_address: str,
        swap_token_addresses: List[str],
        swap_router_fee=Fraction(3, 1000),
        name: str = "",
        update_method="polling",
    ):
        if borrow_token.address != swap_token_addresses[0]:
            raise ValueError(
                "Token addresses must begin with the borrowed token"
            )
        # assert (
        #     borrow_token.address == swap_token_addresses[0]
        # ), "Token addresses must begin with the borrowed token"

        if borrow_pool.token0 == borrow_token:
            if borrow_pool.token1.address != swap_token_addresses[-1]:
                raise ValueError(
                    "Token addresses must end with the repaid token"
                )
            # assert (
            #     borrow_pool.token1.address == swap_token_addresses[-1]
            # ), "Token addresses must end with the repaid token"
        else:
            if borrow_pool.token0.address != swap_token_addresses[-1]:
                raise ValueError(
                    "Token addresses must end with the repaid token"
                )
            # assert (
            #     borrow_pool.token0.address == swap_token_addresses[-1]
            # ), "Token addresses must end with the repaid token"

        # build a list of all tokens involved in this swapping path
        self.tokens = []
        for address in swap_token_addresses:
            self.tokens.append(Erc20Token(address=address))

        if name:
            self.name = name
        else:
            self.name = "-".join([token.symbol for token in self.tokens])

        self.token_path = [token.address for token in self.tokens]

        # build the list of intermediate pool pairs for the given multi-token path.
        # Pool list length will be 1 less than the token path length, e.g. a token1->token2->token3
        # path will result in a pool list consisting of token1/token2 and token2/token3
        self.swap_pools = []
        try:
            _factory = Contract(swap_factory_address)
        except Exception as e:
            print(e)
            _factory = Contract.from_explorer(swap_factory_address)

        for i in range(len(self.token_path) - 1):
            self.swap_pools.append(
                LiquidityPool(
                    address=_factory.getPair(
                        self.token_path[i], self.token_path[i + 1]
                    ),
                    name=" - ".join(
                        [self.tokens[i].symbol, self.tokens[i + 1].symbol]
                    ),
                    tokens=[self.tokens[i], self.tokens[i + 1]],
                    update_method=update_method,
                    fee=swap_router_fee,
                )
            )
            print(
                f"Loaded LP: {self.tokens[i].symbol} - {self.tokens[i+1].symbol}"
            )

        self.swap_pool_addresses = [pool.address for pool in self.swap_pools]

        self.borrow_pool = borrow_pool
        self.borrow_token = borrow_token

        if self.borrow_token == self.borrow_pool.token0:
            self.repay_token = self.borrow_pool.token1
        elif self.borrow_token == self.borrow_pool.token1:
            self.repay_token = self.borrow_pool.token0

        self.best = {
            "init": True,
            "strategy": "flash borrow swap",
            "borrow_amount": 0,
            "borrow_token": self.borrow_token,
            "borrow_pool_amounts": [],
            "repay_amount": 0,
            "profit_amount": 0,
            "profit_token": self.repay_token,
            "swap_pools": self.swap_pools,
            "swap_pool_amounts": [],
        }

    def __str__(self):
        return self.name

    def update_reserves(
        self,
        silent: bool = False,
        print_reserves: bool = True,
        print_ratios: bool = True,
    ) -> bool:
        """
        Checks each liquidity pool for updates by passing a call to .update_reserves(), which returns False if there are no updates.
        Will calculate arbitrage amounts only after checking all pools and finding an update, or on startup (via the 'init' dictionary key)
        """
        recalculate = False

        # calculate initial arbitrage after the object is instantiated, otherwise proceed with normal checks
        if self.best["init"] == True:
            self.best["init"] = False
            recalculate = True

        # flag for recalculation if the borrowing pool has been updated
        if self.borrow_pool.update_reserves(
            silent=silent,
            print_reserves=print_reserves,
            print_ratios=print_ratios,
        ):
            recalculate = True

        # flag for recalculation if any of the pools along the swap path have been updated
        for pool in self.swap_pools:
            if pool.update_reserves(
                silent=silent,
                print_reserves=print_reserves,
                print_ratios=print_ratios,
            ):
                recalculate = True

        if recalculate:
            self._calculate_arbitrage()
            return True
        else:
            return False

    def _calculate_arbitrage(self):
        # set up the boundaries for the Brent optimizer based on which token is being borrowed
        if self.borrow_token.address == self.borrow_pool.token0.address:
            bounds = (
                1,
                float(self.borrow_pool.reserves_token0),
            )
            bracket = (
                0.01 * self.borrow_pool.reserves_token0,
                0.05 * self.borrow_pool.reserves_token0,
            )
        else:
            bounds = (
                1,
                float(self.borrow_pool.reserves_token1),
            )
            bracket = (
                0.01 * self.borrow_pool.reserves_token1,
                0.05 * self.borrow_pool.reserves_token1,
            )

        opt = optimize.minimize_scalar(
            lambda x: -float(
                self.calculate_multipool_tokens_out_from_tokens_in(
                    token_in=self.borrow_token,
                    token_in_quantity=x,
                )
                - self.borrow_pool.calculate_tokens_in_from_tokens_out(
                    token_in=self.repay_token,
                    token_out_quantity=x,
                )
            ),
            method="bounded",
            bounds=bounds,
            bracket=bracket,
        )

        best_borrow = int(opt.x)

        if self.borrow_token.address == self.borrow_pool.token0.address:
            borrow_amounts = [best_borrow, 0]
        elif self.borrow_token.address == self.borrow_pool.token1.address:
            borrow_amounts = [0, best_borrow]
        else:
            print("wtf?")
            raise Exception

        best_repay = self.borrow_pool.calculate_tokens_in_from_tokens_out(
            token_in=self.repay_token,
            token_out_quantity=best_borrow,
        )
        best_profit = -int(opt.fun)

        # only save opportunities with rational, positive values
        if best_borrow > 0 and best_profit > 0:
            self.best.update(
                {
                    "borrow_amount": best_borrow,
                    "borrow_pool_amounts": borrow_amounts,
                    "repay_amount": best_repay,
                    "profit_amount": best_profit,
                    "swap_pool_amounts": self._build_multipool_amounts_out(
                        token_in=self.borrow_token,
                        token_in_quantity=best_borrow,
                    ),
                }
            )
        else:
            self.best.update(
                {
                    "borrow_amount": 0,
                    "borrow_pool_amounts": [],
                    "repay_amount": 0,
                    "profit_amount": 0,
                    "swap_pool_amounts": [],
                }
            )

    def calculate_multipool_tokens_out_from_tokens_in(
        self,
        token_in: Erc20Token,
        token_in_quantity: int,
    ) -> int:
        """
        Calculates the expected token OUTPUT from the last pool for a given token INPUT to the first pool at current pool reserves.
        Uses the self.token0 and self.token1 pointers to determine which token is being swapped in
        and uses the appropriate formula
        """

        number_of_pools = len(self.swap_pools)

        for i in range(number_of_pools):
            # determine the output token for pool0
            if token_in.address == self.swap_pools[i].token0.address:
                token_out = self.swap_pools[i].token1
            elif token_in.address == self.swap_pools[i].token1.address:
                token_out = self.swap_pools[i].token0
            else:
                print("wtf?")
                raise Exception

            # calculate the swap output through pool[i]
            token_out_quantity = self.swap_pools[
                i
            ].calculate_tokens_out_from_tokens_in(
                token_in=token_in,
                token_in_quantity=token_in_quantity,
            )

            if i == number_of_pools - 1:
                break
            else:
                # otherwise, use the output as input on the next loop
                token_in = token_out
                token_in_quantity = token_out_quantity

        return token_out_quantity

    def _build_multipool_amounts_out(
        self,
        token_in: Erc20Token,
        token_in_quantity: int,
        silent: bool = False,
    ) -> List[list]:
        number_of_pools = len(self.swap_pools)

        pools_amounts_out = []

        for i in range(number_of_pools):
            # determine the output token for pool0
            if token_in.address == self.swap_pools[i].token0.address:
                token_out = self.swap_pools[i].token1
            elif token_in.address == self.swap_pools[i].token1.address:
                token_out = self.swap_pools[i].token0
            else:
                print("wtf?")
                raise Exception

            # calculate the swap output through pool[i]
            token_out_quantity = self.swap_pools[
                i
            ].calculate_tokens_out_from_tokens_in(
                token_in=token_in,
                token_in_quantity=token_in_quantity,
            )

            if token_in.address == self.swap_pools[i].token0.address:
                pools_amounts_out.append([0, token_out_quantity])
            elif token_in.address == self.swap_pools[i].token1.address:
                pools_amounts_out.append([token_out_quantity, 0])

            if i == number_of_pools - 1:
                # if we've reached the last pool, return the amounts_out list
                break
            else:
                # otherwise, use the output as input on the next loop
                token_in = token_out
                token_in_quantity = token_out_quantity

        return pools_amounts_out
