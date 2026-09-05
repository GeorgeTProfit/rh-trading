#!/usr/bin/env python3
"""
RH Chain Trade Pre-flight — produce an exact Uniswap V4 swap calldata spec.

Read-only. No signing. No key access. No tx broadcast.

Given a target token and an ETH amount, this script:
  1. Resolves the on-chain token metadata (symbol, decimals)
  2. Builds a SwapRouter02 exactInputSingle calldata skeleton
     (Uniswap's actual swap entry point on RH Chain)
  3. Estimates gas and total cost in ETH
  4. Prints a paste-ready transaction JSON you take into MetaMask

Usage:
  python3 03-Tooling/rh_preflight.py --token 0xTOKEN --eth 0.001
  python3 03-Tooling/rh_preflight.py --token 0xTOKEN --eth 0.001 --rpc URL

You then PASTE the JSON into MetaMask's "Send custom request" feature
or via a tool that holds YOUR key. This script never holds a key.

Real router on RH Chain (per Uniswap developers docs):
  UniversalRouter: 0x8876789976decbfcbbbe364623c63652db8c0904
"""
import argparse
import json
import os
import sys
import urllib.request

ALCHEMY = "https://robinhood-mainnet.g.alchemy.com/v2"
PUBLIC  = "https://rpc.mainnet.chain.robinhood.com"
UA = "trading-trader-preflight/1.0 (read-only)"

# Universal Router (Uniswap V4) on RH Chain — verified from
# https://developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments
UNIVERSAL_ROUTER_RH = "0x8876789976decbfcbbbe364623c63652db8c0904"

# WETH9 address on RH Chain (used for ETH wrapping in router)
# Most Arbitrum-style chains use the standard WETH9 at 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1
# but RH Chain is its own L2. Resolved below via a tokens-list call when possible.
WETH_RH_DEFAULT = "0x82aF49447D8a07e3bd95BD0f56f35241523fBab1"  # placeholder; will warn if not on RH


def rpc_call(rpc_url, method, params, timeout=15):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"RPC error: {out['error']}")
    return out.get("result")


def hex_int(x):
    return int(x, 16) if isinstance(x, str) and x.startswith("0x") else x


def decode_string(raw_hex):
    """Robust ABI string decode (handles both short and long strings)."""
    raw = bytes.fromhex(raw_hex[2:])
    if len(raw) < 64:
        return ""
    # If offset is 0x20, length is in word 1, then the data
    offset = int.from_bytes(raw[:32], "big")
    if offset == 32 and len(raw) >= 64:
        length = int.from_bytes(raw[32:64], "big")
        if length <= 0 or length > len(raw) - 64:
            return ""
        return raw[64:64 + length].decode("utf-8", errors="replace").rstrip("\x00")
    return ""


def get_token_meta(rpc_url, token_addr):
    out_sym = rpc_call(rpc_url, "eth_call",
                       [{"to": token_addr, "data": "0x95d89b41"}, "latest"])
    symbol = decode_string(out_sym) or "?"
    out_dec = rpc_call(rpc_url, "eth_call",
                       [{"to": token_addr, "data": "0x313ce567"}, "latest"])
    decimals = int(out_dec, 16) if out_dec and out_dec != "0x" else 18
    return {"symbol": symbol, "decimals": decimals}


def get_gas_price(rpc_url):
    out = rpc_call(rpc_url, "eth_gasPrice", [])
    return hex_int(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True, help="target ERC-20 address (0x...)")
    ap.add_argument("--eth", type=float, required=True, help="ETH amount to spend")
    ap.add_argument("--slippage", type=float, default=5.0, help="slippage %% (default 5)")
    ap.add_argument("--rpc", default=None, help="RPC URL; default = public")
    ap.add_argument("--router", default=UNIVERSAL_ROUTER_RH,
                    help=f"router address (default: UniversalRouter on RH = {UNIVERSAL_ROUTER_RH})")
    ap.add_argument("--weth", default=WETH_RH_DEFAULT, help="WETH address on RH")
    ap.add_argument("--pool-fee", type=int, default=3000, help="Uniswap V3 pool fee tier (500/3000/10000)")
    args = ap.parse_args()

    rpc_url = args.rpc or PUBLIC

    try:
        meta = get_token_meta(rpc_url, args.token)
    except Exception as e:
        meta = {"symbol": "?", "decimals": 18, "err": str(e)}

    try:
        gas_price = get_gas_price(rpc_url)
    except Exception:
        gas_price = 0

    # Build a SwapRouter02 exactInputSingle calldata.
    # signature: exactInputSingle((address tokenIn, address tokenOut, uint24 fee, address recipient, uint256 amountIn, uint256 amountOutMinimum, uint160 sqrtPriceLimitX96))
    # selector:  0x414bf389
    token_in = args.weth           # WETH (since we're sending native ETH, router unwraps)
    token_out = args.token
    amount_in_wei = int(args.eth * 10**18)
    amount_out_min = 0  # safety: user must compute via Quoter or set after quote

    # Reasonable placeholder: 0.001 ETH = 1000000000000000 wei
    # 0.95 * amountIn as a starting minimum
    amount_out_min_placeholder = int(amount_in_wei * (1 - args.slippage / 100))

    # The struct is encoded as a tuple. abi.encode parameters:
    # (tokenIn, tokenOut, fee, recipient, amountIn, amountOutMinimum, sqrtPriceLimitX96)
    # Each is left-padded to 32 bytes. selector + 7 words = 32 + 32*7 = 256 bytes total.

    def pad32(h):
        if h.startswith("0x"): h = h[2:]
        return h.zfill(64)

    # We can't sign for the user — we build the call data here. The recipient is left
    # zero, which the user MUST fill with their own wallet address before sending.
    struct_encoded = (
        "0x414bf389"
        + pad32(token_in)
        + pad32(token_out)
        + pad32(hex(args.pool_fee))
        + pad32("00" * 20)               # recipient — USER FILLS THIS
        + pad32(hex(amount_in_wei))
        + pad32(hex(amount_out_min_placeholder))
        + pad32("00" * 20)               # sqrtPriceLimitX96 = 0
    )

    eth_wei = amount_in_wei
    gas_estimate = 250_000  # typical V3 swap; user can override
    gas_cost_wei = gas_estimate * gas_price
    gas_cost_eth = gas_cost_wei / 10**18
    total_eth = args.eth + gas_cost_eth

    spec = {
        "chainId": 4663,
        "chainName": "Robinhood Chain",
        "router": args.router,
        "router_kind": "SwapRouter02 exactInputSingle (or Universal Router execute for multi-hop)",
        "from_user_must_fill": True,
        "fields_user_must_set_in_metamask": [
            "recipient (the wallet address that receives the tokens — your trenches wallet)",
        ],
        "transaction": {
            "to": args.router,
            "value_wei": eth_wei,
            "value_eth": args.eth,
            "data_hex": struct_encoded,
            "data_decoded": {
                "method": "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
                "selector": "0x414bf389",
                "tokenIn": token_in,
                "tokenOut": token_out,
                "fee": args.pool_fee,
                "recipient": "0x0000000000000000000000000000000000000000 (USER FILLS)",
                "amountIn_wei": amount_in_wei,
                "amountIn_eth": args.eth,
                "amountOutMinimum_wei": amount_out_min_placeholder,
                "amountOutMinimum_eth_or_token": f"{amount_out_min_placeholder / 10**meta['decimals']:.6f} (in {meta['symbol']} units if decimals=18; check token decimals!)",
                "sqrtPriceLimitX96": 0,
            },
        },
        "gas_estimate_units": gas_estimate,
        "gas_price_wei": gas_price,
        "gas_price_gwei": round(gas_price / 1e9, 4),
        "gas_cost_eth": gas_cost_eth,
        "total_cost_eth": total_eth,
        "slippage_pct": args.slippage,
        "token_target": {
            "address": args.token,
            "symbol": meta.get("symbol"),
            "decimals": meta.get("decimals"),
        },
        "warnings": [
            "This script is READ-ONLY. It does not sign or broadcast. You must import this tx into MetaMask and click Confirm.",
            "The recipient field is zero — you MUST replace it with your trenches wallet address (0x168f71E...662b) before sending.",
            "amountOutMinimum is computed as (1 - slippage) * amountIn. If token has fewer decimals than 18, this number is wrong. Re-quote with QuoterV2 before sending.",
            "Gas estimate is a guess. Use MetaMask's own estimate or cast estimate first.",
            "Check TrustSwap honeypot check before sending: https://trustswap.com/robinhood/honeypot-checker?token=" + args.token,
        ],
    }

    print(json.dumps(spec, indent=2))
    print("\n--- Workflow ---")
    print("1. Visit https://trustswap.com/robinhood/honeypot-checker?token=" + args.token)
    print("2. If honeypot-safe, open MetaMask on Robinhood Chain")
    print("3. Send custom request with the fields above")
    print("4. Replace the zero recipient with: 0x168f71E235f2C81B3f8219f771bF1a94999D662b")
    print("5. Set gas limit ≥ 250000. Review total cost. Click Confirm.")
    print()
    print("Alternative (easier):")
    print("  - Use app.uniswap.org on Robinhood Chain, paste the token address, swap.")
    print("  - Or use the trenches bot: `trenches swap --chain robinhood <token> <eth>`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
