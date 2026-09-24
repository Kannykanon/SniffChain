// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @title HoneypotSimulator
/// @notice Round-trip (buy -> sell) simulator for Uniswap v4 pools and V2 pairs.
/// @dev Designed to be executed through eth_call with a state override that gives this
///      contract a USDC balance (a native-balance override is enough on Arc, since the
///      USDC ERC-20 at 0x3600...0000 reads the native balance). Nothing is ever committed.
///      It is also deployable as-is so other Arc builders can call it the same way.
///      No immutables: runtime bytecode must work when injected via a code override.

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
}

struct PoolKey {
    address currency0;
    address currency1;
    uint24 fee;
    int24 tickSpacing;
    address hooks;
}

struct SwapParams {
    bool zeroForOne;
    int256 amountSpecified;
    uint160 sqrtPriceLimitX96;
}

interface IUniswapV2Pair {
    function getReserves() external view returns (uint112, uint112, uint32);
    function token0() external view returns (address);
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
}

interface IPoolManager {
    function unlock(bytes calldata data) external returns (bytes memory);
    function swap(PoolKey memory key, SwapParams memory params, bytes calldata hookData) external returns (int256);
    function sync(address currency) external;
    function settle() external payable returns (uint256);
    function take(address currency, address to, uint256 amount) external;
}

contract HoneypotSimulator {
    uint160 private constant MIN_SQRT_PRICE_PLUS_ONE = 4295128740;
    uint160 private constant MAX_SQRT_PRICE_MINUS_ONE = 1461446703485210103287273052203988822378723970341;

    struct Leg {
        bool ok;
        bytes err;       // raw revert data when !ok
        uint256 paid;    // input actually owed to the pool (partial fills pay less than requested)
        uint256 quoted;  // output the pool credited us (after hook deltas)
        uint256 received; // output that actually landed in our balance (after transfer taxes)
        uint256 gasUsed;
    }

    /// @dev Only set for the duration of simulate(); guards unlockCallback.
    address private _pm;

    receive() external payable {}

    /// @param pm      Uniswap v4 PoolManager
    /// @param key     Pool to trade through; must contain `token` and `quote`
    /// @param token   Token under test
    /// @param amountIn Amount of the quote currency (USDC) to buy with, in its own units
    function simulate(address pm, PoolKey calldata key, address token, uint256 amountIn)
        external
        returns (Leg memory buy, Leg memory sell)
    {
        require(key.currency0 == token || key.currency1 == token, "token not in pool");
        _pm = pm;
        bool tokenIs0 = key.currency0 == token;

        // Buy: quote -> token. If token is currency0 we are swapping 1 -> 0.
        buy = _leg(key, !tokenIs0, amountIn);
        if (buy.ok && buy.received > 0) {
            // Sell exactly what we actually hold.
            sell = _leg(key, tokenIs0, buy.received);
        }
        _pm = address(0);
    }

    function _leg(PoolKey calldata key, bool zeroForOne, uint256 amountIn) private returns (Leg memory l) {
        uint256 g = gasleft();
        try IPoolManager(_pm).unlock(abi.encode(key, zeroForOne, amountIn)) returns (bytes memory ret) {
            (l.paid, l.quoted, l.received) = abi.decode(ret, (uint256, uint256, uint256));
            l.ok = true;
        } catch (bytes memory err) {
            l.err = err;
        }
        l.gasUsed = g - gasleft();
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == _pm, "not pool manager");
        (PoolKey memory key, bool zeroForOne, uint256 amountIn) = abi.decode(data, (PoolKey, bool, uint256));

        int256 delta = IPoolManager(_pm).swap(
            key,
            SwapParams(zeroForOne, -int256(amountIn), zeroForOne ? MIN_SQRT_PRICE_PLUS_ONE : MAX_SQRT_PRICE_MINUS_ONE),
            ""
        );
        int128 d0 = int128(delta >> 128);
        int128 d1 = int128(delta);
        (int128 dIn, int128 dOut) = zeroForOne ? (d0, d1) : (d1, d0);
        (address cIn, address cOut) = zeroForOne ? (key.currency0, key.currency1) : (key.currency1, key.currency0);
        require(dIn <= 0 && dOut >= 0, "unexpected delta sign");

        uint256 paid = uint256(uint128(-dIn));
        uint256 quoted = uint256(uint128(dOut));
        _pay(cIn, paid);

        uint256 before = _balanceOf(cOut);
        if (quoted > 0) IPoolManager(_pm).take(cOut, address(this), quoted);
        uint256 received = _balanceOf(cOut) - before;

        return abi.encode(paid, quoted, received);
    }

    function _pay(address currency, uint256 amount) private {
        if (amount == 0) return;
        if (currency == address(0)) {
            IPoolManager(_pm).settle{value: amount}();
            return;
        }
        IPoolManager(_pm).sync(currency);
        _transfer(currency, _pm, amount);
        IPoolManager(_pm).settle();
    }

    // ---------------------------------------------------------------- Uniswap V2 (and forks)

    /// @param feeBps the pair's swap fee (30 for Uniswap V2)
    function simulateV2(address pair, address token, address quote, uint256 amountIn, uint256 feeBps)
        external
        returns (Leg memory buy, Leg memory sell)
    {
        buy = _v2Leg(pair, quote, token, amountIn, feeBps);
        if (buy.ok && buy.received > 0) {
            sell = _v2Leg(pair, token, quote, buy.received, feeBps);
        }
    }

    function _v2Leg(address pair, address tIn, address tOut, uint256 amountIn, uint256 feeBps)
        private
        returns (Leg memory l)
    {
        uint256 g = gasleft();
        // External self-call so a revert anywhere in the swap is caught with its reason.
        try this.v2Swap(pair, tIn, tOut, amountIn, feeBps) returns (uint256 paid, uint256 quoted, uint256 received) {
            (l.ok, l.paid, l.quoted, l.received) = (true, paid, quoted, received);
        } catch (bytes memory err) {
            l.err = err;
        }
        l.gasUsed = g - gasleft();
    }

    /// @return paid what the pair actually received (a transfer tax makes this < amountIn)
    /// @return quoted output the pair's reserves imply for `paid`
    /// @return received output that actually landed here (a transfer tax makes this < quoted)
    function v2Swap(address pair, address tIn, address tOut, uint256 amountIn, uint256 feeBps)
        external
        returns (uint256 paid, uint256 quoted, uint256 received)
    {
        require(msg.sender == address(this), "self only");
        (uint256 r0, uint256 r1,) = IUniswapV2Pair(pair).getReserves();
        bool inIs0 = IUniswapV2Pair(pair).token0() == tIn;
        (uint256 rIn, uint256 rOut) = inIs0 ? (r0, r1) : (r1, r0);

        _transfer(tIn, pair, amountIn);
        // Cap at amountIn: a pair can hold tokens beyond its reserves (stray transfers), which isn't ours.
        paid = IERC20(tIn).balanceOf(pair) - rIn;
        if (paid > amountIn) paid = amountIn;
        uint256 inWithFee = paid * (10_000 - feeBps);
        quoted = inWithFee * rOut / (rIn * 10_000 + inWithFee);

        uint256 before = IERC20(tOut).balanceOf(address(this));
        IUniswapV2Pair(pair).swap(inIs0 ? 0 : quoted, inIs0 ? quoted : 0, address(this), "");
        received = IERC20(tOut).balanceOf(address(this)) - before;
    }

    // ---------------------------------------------------------------- shared

    /// Low-level transfer: tolerates tokens that return nothing, and bubbles up the token's own
    /// revert reason ("blacklisted", "trading not enabled", ...).
    function _transfer(address token, address to, uint256 amount) private {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSelector(0xa9059cbb, to, amount));
        if (!ok) {
            assembly { revert(add(ret, 32), mload(ret)) }
        }
        require(ret.length == 0 || abi.decode(ret, (bool)), "token transfer returned false");
    }

    function _balanceOf(address currency) private view returns (uint256) {
        return currency == address(0) ? address(this).balance : IERC20(currency).balanceOf(address(this));
    }
}
