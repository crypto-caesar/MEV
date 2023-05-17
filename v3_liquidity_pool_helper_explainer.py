''' V3 liquidity pool contract has several immutable properties that we care about: Factory address,
Pool Address, ABI, Fee, Tick spacing, Token0 Address, Token1 Address & mutable properties that we care about:
Current tick, Current liquidity, Current square root price, Initialized ticks, Liquidity changes across tick 
boundaries. All of these properties will be attributes of the class, so they will be defined in the abstract 
base class. The first class below is this abstract base class:'''

from abc import ABC, abstractmethod

class BaseV3LiquidityPool(ABC):
    def __init__(self, address):
        self.address = address

    def get_address(self):
        return self.address
        
    #define an abstract method in the base class which will cause instantiation to fail when that method is not overridden
    @abstractmethod
    def get_tick_spacing(self):
        pass
        
class V3LiquidityPool(BaseV3LiquidityPool):
    def get_tick_spacing(self):
        return 420

''' When running this, the V3LiquidityPool class has access to both the __init__ and
get_address methods. Even though both the interface (external methods and attributes) and the code are defined 
in the base class, the derived classes have automatic access to it. '''
