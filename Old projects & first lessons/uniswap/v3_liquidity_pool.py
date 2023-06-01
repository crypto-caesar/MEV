''' The key reason to have an LP helper is to define a common interface that bot builders can use to 
retrieve and store data relevant to liquidity pools. The helper will be capable of interacting with 
the blockchain, so it needs access to a web3 object (either web3py or Brownie). The constructor pulls relevant 
data from the blockchain through the web3 object, then store the results internally. At startup, the LP 
helper should (1) create a Brownie object from the provided address, then (2) retrieve the relevant data 
from the LP contract. Updated with new features: attributes to keep track of the pool's factory address and (3) tick data.
For tick_data: a dictionary that stores liquidity information associated with a particular tick, keyed by that tick.
'''

from abc import ABC, abstractmethod
from brownie import Contract
from brownie.convert import to_address
from v3_lp_abi import V3_LP_ABI
from .tick_lens import TickLens

class BaseV3LiquidityPool(ABC):
		
    ''' constructor attempts to create a Brownie contract from storage, then from the explorer. If both fail, 
it will raise an exception and quit. If the contract creation succeeds, it will fetch token0, token1, fee, 
slot0, liquidity, tickSpacing. '''

    def __init__(self, address: str, lens: Contract = None):
        #added a block of code to build and store the lens contract (3):
        if lens:
            self.lens = lens
        else:
            try:
                self.lens = TickLens()
            except:
                raise
        self.address = to_address(address)

        try: #(1) 
            self._brownie_contract = Contract(address=address)
        except:
            try:
                self._brownie_contract = Contract.from_explorer(
                    address=address,
                    silent=True
                )
            except:
                #added in the case that contract isn't verified on Etherscan
                try:
                    self._brownie_contract = Contract.from_abi(
                        name="", address=address, abi=V3_LP_ABI
                        )
                except:
                    raise

        try: #(2)
            self.token0 = self._brownie_contract.token0()
            self.token1 = self._brownie_contract.token1()
            self.fee = self._brownie_contract.fee()
            self.slot0 = self._brownie_contract.slot0()
            self.liquidity = self._brownie_contract.liquidity()
            self.tick_spacing = self._brownie_contract.tickSpacing()
            self.sqrt_price_x96 = self.slot0[0]
            self.tick = self.slot0[1]
            self.factory = self._brownie_contract.factory()  # New added feature
            self.tick_data = {}  # New added feature
            self.tick_word, _ = self.get_tick_bitmap_position(self.tick)  # New added feature to automatically fetch the current “word” from TickLens at startup
            self.get_tick_data_at_word(self.tick_word)  # New added feature
        except:
            raise
    
    ''' Instead of silently updating state values, "update" will return a tuple with True/False 
to indicate whether any new values were found + make a dictionary of the new values. This helps us 
display updates inside a running bot, instead of making separate calls to the helper to retrieve and 
parse the values again. If we care about the values, we can use them. If not, they can be ignored
Update: I'd like to make a distinction between an update executed by the LP helper itself and 
an update of the LP helper via externally-provided data. So I rename update to auto_update, 
likely adding another method later called external_update that will accept external values 
instead of querying the blockchain.

'''

    def auto_update(self):
        updates = False
        try:
            if (slot0 := self._brownie_contract.slot0()) != self.slot0:
                updates = True
                self.slot0 = slot0
                self.sqrt_price_x96 = self.slot0[0]
                self.tick = self.slot0[1]
            if (
                liquidity := self._brownie_contract.liquidity()
            ) != self.liquidity:
                updates = True
                self.liquidity = liquidity

        except:
            raise
        else:
            return updates, {
                "slot0": self.slot0,
                "liquidity": self.liquidity,
                "sqrt_price_x96": self.sqrt_price_x96,
                "tick": self.tick,
            }

    '''
    With the "lens" block of code in "_init_", when the LP helper is built, 
    it will have a way to ask the TickLens about itself. Now define a function 
    that asks the TickLens for liquidity information about itself:
    Gets the initialized tick values at a specific word
    (a 32 byte number representing 256 ticks at the tickSpacing
    interval), then stores the liquidity values in the `self.tick_data`
    dictionary, using the tick index as the key.
    '''
    def get_tick_data_at_word(self, word_position: int):
    
        try:
            tick_data = self.lens._brownie_contract.getPopulatedTicksInWord(
                self.address, word_position
            )
        except:
            raise
        else:
            for (tick, liquidityNet, liquidityGross) in tick_data:
                self.tick_data[tick] = liquidityNet, liquidityGross
            return tick_data
    
    '''
    function that uses the TickBitmap library from univ3py to calculate 
    the word_position parameter. This function corrects internally for tick spacing!

    e.g. tick=600 is the 11th initialized tick for an LP with
    tickSpacing of 60, starting at 0.

    Calling `get_tick_bitmap_position(600)` returns (0,10), where:
        0 = wordPosition (zero-indexed)
        10 = bitPosition (zero-indexed)
    '''
    def get_tick_bitmap_position(self, tick) -> Tuple[int, int]:
        return TickBitmap.position(tick // self.tick_spacing)
        
    
    def calculate_tokens_out_from_tokens_in(
        self,
        token_in: Erc20Token,
        token_in_quantity: int = None,
    ):
        '''
        function implements the common interface `calculate_tokens_out_from_tokens_in`
        to calculate the number of tokens received from a given number of tokens deposited.

        The UniV3 liquidity pool function `swap` is adapted from
        https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol
        and used to calculate swap amounts, ticks crossed, liquidity changes at various ticks, etc.
        Credit to BowTiedDevil for the calculation of UniV3 swaps:
        https://github.com/BowTiedDevil/degenbot/blob/main/uniswap/v3/libraries/SwapMath.py
        
        The wrapper itself is simple. It does a few simple checks, then sends the appropriate
        arguments into swap, gets the results (amount0, amount1) and returns the appropriate one. 
        '''

        def swap(
            zeroForOne: bool,
            amountSpecified: int,
            sqrtPriceLimitX96: int,
        ) -> Tuple[int, int]:

            return amount0, amount1

        if token_in not in (self.token0, self.token1):
            raise Alex_botError("token_in not found!")

        # determine whether the swap is token0 -> token1
        zeroForOne = True if token_in == self.token0 else False

        # delegate calculations to the re-implemented `swap` function
        amount0, amount1 = swap(
            zeroForOne=zeroForOne,
            amountSpecified=token_in_quantity,
            sqrtPriceLimitX96=(
                TickMath.MIN_SQRT_RATIO + 1
                if zeroForOne
                else TickMath.MAX_SQRT_RATIO - 1
            ),
        )
        return -amount1 if zeroForOne else -amount0
    
class V3LiquidityPool(BaseV3LiquidityPool):
    pass

