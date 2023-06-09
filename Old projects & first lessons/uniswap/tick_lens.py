''' 
to retrieve tick info for the main v3_liquidity_pool helper, build a
helper to query the TickLens contract. 
The constructor assumes that the TickLens contract is deployed to mainnet at the 
standard address, though it allows an override.
'''


from abc import ABC, abstractmethod

from brownie import Contract
from brownie.convert import to_address


class TickLens(ABC):
    def __init__(
        self,
        address="0xbfd8137f7d1516D3ea5cA83523914859ec47F573"
    ):
        self.address = to_address(address)

        try:
            self._brownie_contract = Contract(address=address)
        except:
            try:
                self._brownie_contract = Contract.from_explorer(
                    address=address, silent=True
                )
            except:
                raise
