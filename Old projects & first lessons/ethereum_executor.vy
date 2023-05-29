# @version >=0.3.4

OWNER: immutable(address)
WETH_ADDRESS: immutable(address)
MAX_PAYLOADS: constant(uint256) = 8
MAX_PAYLOAD_BYTES: constant(uint256) = 1024
ENCODED_PAYLOAD_LENGTH: constant(uint256) = 9536
CALLBACK_CALLDATA_LENGTH: constant(uint256) = ENCODED_PAYLOAD_LENGTH + 32*3 + 32*2

# length calculation (bytes):
# 32: address
# 32: uint256
# 32: uint256
# 32: marker for calldata
# 32: offset for calldata
# 9536: value for calldata
# total = 9696

struct payload:
    target: address
    calldata: Bytes[MAX_PAYLOAD_BYTES]
    value: uint256

@external
@payable
def __init__(_weth_address: address):
    '''
    set the initial state and load some helpful variables. 
    If we intend to hold WETH in the contract, send it 
    along using msg.value and the constructor will wrap it 
    to WETH
    '''
    OWNER = msg.sender
    WETH_ADDRESS = _weth_address

    # wrap the initial Ether deposit to WETH
    if msg.value > 0:
        raw_call(
            WETH_ADDRESS,
            method_id('deposit()'),
            value=msg.value
        )

@external
@payable
def __default__():
    '''
    Vyper creates a 'fallback' function that will revert by 
    default (helps protect users who send ETH to the 
    contract by mistake). However since we're wrapping and 
    unwrapping ETH, we need to be able to receive ETH. If this 
    step is skipped, WETH deposits will work, but WETH 
    withdrawals will not.
    '''
    if len(msg.data) == 0:
        return


@external
def send_bribe_UNSAFE(amount: uint256):
    '''
    unsafe external function for testing.
    Will delete later
    '''
    assert msg.sender == OWNER, "!OWNER"
    self.bribe(amount)


@internal
def bribe(amount: uint256):
    '''
    contract first performs a check of the internal balance 
    (both Ether and WETH). If the internal balance exceeds 
    the requested amount, it reverts immediately. Otherwise 
    the contract will pay the miner directly after topping 
    up its Ether balance as needed by calling withdraw() 
    at the WETH contract
    '''
    weth_balance: uint256 = extract32(
        raw_call(
            WETH_ADDRESS,
            _abi_encode(
                self,
                method_id = method_id('balanceOf(address)')
            ),
            max_outsize=32
        ),
        0,
        output_type=uint256)

    assert amount <= self.balance + weth_balance, "BRIBE EXCEEDS BALANCE"

    if self.balance >= amount:
        send(block.coinbase, amount)
    else:
        raw_call(
            WETH_ADDRESS,
            _abi_encode(
                amount - self.balance,
                method_id = method_id('withdraw(uint256)')
            ),
        )
        send(block.coinbase, amount)


@external
@payable
def execute(
    payloads: DynArray[payload, MAX_PAYLOADS],
    bribe_amount: uint256,
    return_on_first_failure: bool = False,  # optional argument
    execute_all_payloads: bool = False,  # optional argument
):
    '''
    Specifies the bribe on any particular external function. 
    It simply calls bribe() after finishing execution of 
    submitted payloads
    '''

    assert msg.sender == OWNER, "!OWNER"

    if return_on_first_failure:
        assert not execute_all_payloads, "CONFLICTING REVERT OPTIONS"

    if execute_all_payloads:
        assert not return_on_first_failure, "CONFLICTING REVERT OPTIONS"

    success: bool = False
    response: Bytes[32] = b""

    total_value: uint256 = 0
    for _payload in payloads:
        total_value += _payload.value
    assert total_value <= msg.value + self.balance, "INSUFFICIENT VALUE"

    if not execute_all_payloads and not return_on_first_failure:
        # default behavior, reverts on any payload failure
        for _payload in payloads:
            raw_call(
                _payload.target,
                _payload.calldata,
                value=_payload.value,
            )
    elif return_on_first_failure:
        # custom behavior, will execute payloads until
        # the first failed call and break the loop without
        # reverting the previous successful transfers
        for _payload in payloads:
            success, response = raw_call(
                _payload.target,
                _payload.calldata,
                max_outside=32,
                value=_payload.value,
                revert_on_failure=False
            )
            if not success:
                break
    elif execute_all_payloads:
        # custom behavior, will execute all payloads
        # regardless of success
        for _payload in payloads:
            success, response = raw_call(
                _payload.target,
                _payload.calldata,
                max_outside=32,
                value=_payload.value,
                revert_on_failure=False
            )

    # transfer the bribe
    if bribe_amount > 0:
        self.bribe(bribe_amount)
        return
