# Base exception
class Alex_botError(Exception):
    """
    Base exception, intended as a generic exception and a base class for
    for all more-specific exceptions raised by various alex_bot modules
    """

    pass


class DeprecationError(ValueError):
    """
    Thrown when a feature, class, method, etc. is deprecated.
    """

    pass


# 1st level exceptions (derived from `Alex_botError`)
class ArbitrageError(Alex_botError):
    """
    Exception raised inside arbitrage helpers
    """

    pass


class BlockUnavailableError(Alex_botError):
    """
    Exception raised when a call for a specific block fails (trie node unavailable)
    """

    pass


class Erc20TokenError(Alex_botError):
    """
    Exception raised inside ERC-20 token helpers
    """

    pass


class EVMRevertError(Alex_botError):
    """
    Thrown when a simulated EVM contract operation would revert
    """

    pass


class LiquidityPoolError(Alex_botError):
    """
    Exception raised inside liquidity pool helpers
    """

    pass


class ManagerError(Alex_botError):
    """
    Exception raised inside manager helpers
    """

    pass


class TransactionError(Alex_botError):
    """
    Exception raised inside transaction simulation helpers
    """

    pass


# 2nd level exceptions for Arbitrage classes
class ArbCalculationError(ArbitrageError):
    """
    Thrown when an arbitrage calculation fails
    """

    pass


class InvalidSwapPathError(ArbitrageError):
    """
    Thrown in arbitrage helper constructors when the provided path is invalid
    """

    pass


class ZeroLiquidityError(ArbitrageError):
    """
    Thrown by the arbitrage helper if a pool in the path has no liquidity in the direction of the proposed swap
    """

    pass


# 2nd level exceptions for Uniswap Liquidity Pool classes
class BitmapWordUnavailableError(LiquidityPoolError):
    """
    Thrown by the ported V3 swap function when the bitmap word is not available.
    This should be caught by the helper to perform automatic fetching, and should
    not be raised to the calling function
    """

    pass


class ExternalUpdateError(LiquidityPoolError):
    """
    Thrown when an external update does not pass sanity checks
    """

    pass


class MissingTickWordError(LiquidityPoolError):
    """
    Thrown by the TickBitmap library when calling for an operation on a word that
    should be available, but is not
    """

    pass


class ZeroSwapError(LiquidityPoolError):
    """
    Thrown if a swap calculation resulted or would result in zero output
    """

    pass
