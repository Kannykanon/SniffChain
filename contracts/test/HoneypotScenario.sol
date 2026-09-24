// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {HoneypotSimulator, PoolKey} from "../HoneypotSimulator.sol";

/// @dev Test-only. Everything runs inside one eth_call on real Arc state: create a token, open a
///      v4 pool against USDC, add liquidity, then run HoneypotSimulator against it.

/// ERC-20 whose sells (transfers into the PoolManager) revert above `maxSell`, except for the owner.
contract TestToken {
    mapping(address => uint256) public balanceOf;
    address public immutable owner;
    address public immutable pm;
    uint256 public immutable maxSell;

    constructor(address pm_, uint256 maxSell_, uint256 supply) {
        owner = msg.sender;
        pm = pm_;
        maxSell = maxSell_;
        balanceOf[msg.sender] = supply;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        if (to == pm && msg.sender != owner) require(amount <= maxSell, "sell blocked");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

struct ModifyLiquidityParams {
    int24 tickLower;
    int24 tickUpper;
    int256 liquidityDelta;
    bytes32 salt;
}

interface IPoolManagerSetup {
    function initialize(PoolKey memory key, uint160 sqrtPriceX96) external returns (int24);
    function unlock(bytes calldata data) external returns (bytes memory);
    function modifyLiquidity(PoolKey memory key, ModifyLiquidityParams memory params, bytes calldata hookData)
        external
        returns (int256, int256);
    function sync(address currency) external;
    function settle() external payable returns (uint256);
}

contract HoneypotScenario {
    address private constant USDC = 0x3600000000000000000000000000000000000000;
    address private _pm;

    /// @param sqrtPriceTokenIs0 / sqrtPriceTokenIs1  start price for either currency ordering
    /// @param liquidity full-range liquidity to add
    function run(
        address pm,
        address simulator,
        uint256 maxSell,
        uint160 sqrtPriceTokenIs0,
        uint160 sqrtPriceTokenIs1,
        uint128 liquidity,
        uint256 buyUsdc
    ) external returns (HoneypotSimulator.Leg memory buy, HoneypotSimulator.Leg memory sell) {
        _pm = pm;
        TestToken token = new TestToken(pm, maxSell, 1e30);
        bool tokenIs0 = address(token) < USDC;
        PoolKey memory key = PoolKey(
            tokenIs0 ? address(token) : USDC, tokenIs0 ? USDC : address(token), 3000, 60, address(0));
        IPoolManagerSetup(pm).initialize(key, tokenIs0 ? sqrtPriceTokenIs0 : sqrtPriceTokenIs1);
        IPoolManagerSetup(pm).unlock(abi.encode(key, liquidity));
        return HoneypotSimulator(payable(simulator)).simulate(pm, key, address(token), buyUsdc);
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == _pm);
        (PoolKey memory key, uint128 liquidity) = abi.decode(data, (PoolKey, uint128));
        (int256 delta,) = IPoolManagerSetup(_pm).modifyLiquidity(
            key, ModifyLiquidityParams(-887220, 887220, int256(uint256(liquidity)), 0), "");
        _settle(key.currency0, uint256(uint128(-int128(delta >> 128))));
        _settle(key.currency1, uint256(uint128(-int128(delta))));
        return "";
    }

    function _settle(address currency, uint256 amount) private {
        IPoolManagerSetup(_pm).sync(currency);
        (bool ok,) = currency.call(abi.encodeWithSelector(0xa9059cbb, _pm, amount));
        require(ok, "settle transfer");
        IPoolManagerSetup(_pm).settle();
    }
}
