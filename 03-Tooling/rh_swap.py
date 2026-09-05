#!/usr/bin/env python3
"""Safe, quoted Uniswap v3 swaps on Robinhood Chain.

This module is intentionally narrow: native ETH -> WETH -> ERC-20 via the
verified Robinhood Chain SwapRouter02. It verifies the pool, obtains a fresh
QuoterV2 quote, enforces minAmountOut, simulates the exact transaction,
estimates gas, signs in-process, and verifies receipt + token balance delta.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

from eth_abi import encode
from eth_account import Account
from web3 import HTTPProvider, Web3
from web3.exceptions import TimeExhausted

CHAIN_ID = 4663
PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"
ALCHEMY_BASE = "https://robinhood-mainnet.g.alchemy.com/v2"

WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
QUOTER_V2 = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
SWAP_ROUTER_02 = "0xcaf681a66d020601342297493863e78c959e5cb2"
TRENCHES_WALLET = "0xEc52530D2828A20853AAd5727d28B56149E86D4b"

RHC_DENIED = "0x7f04da8cc451dddfbf80d6fa3aae3ee0642f8ab9"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
EXACT_INPUT_SINGLE_SELECTOR = "04e45aaf"
MULTICALL_DEADLINE_SELECTOR = "5ae401dc"

FACTORY_ABI = [{
    "type": "function",
    "name": "getPool",
    "stateMutability": "view",
    "inputs": [
        {"name": "tokenA", "type": "address"},
        {"name": "tokenB", "type": "address"},
        {"name": "fee", "type": "uint24"},
    ],
    "outputs": [{"name": "pool", "type": "address"}],
}]

QUOTER_ABI = [{
    "type": "function",
    "name": "quoteExactInputSingle",
    "stateMutability": "nonpayable",
    "inputs": [{
        "name": "params",
        "type": "tuple",
        "components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "fee", "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
    }],
    "outputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "sqrtPriceX96After", "type": "uint160"},
        {"name": "initializedTicksCrossed", "type": "uint32"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
}]

ERC20_ABI = [
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "decimals",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "type": "function",
        "name": "symbol",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]


@dataclass(frozen=True)
class SwapPlan:
    chain_id: int
    wallet: str
    token: str
    pool: str
    router: str
    fee: int
    amount_in_wei: int
    quoted_out: int
    amount_out_min: int
    token_decimals: int
    token_symbol: str
    slippage_bps: int
    deadline: int
    calldata: str
    gas_estimate: int
    max_fee_per_gas: int
    max_gas_cost_wei: int


def rpc_url() -> str:
    key = os.environ.get("RH_ALCHEMY_KEY")
    return f"{ALCHEMY_BASE}/{key}" if key else PUBLIC_RPC


def make_web3(url: Optional[str] = None) -> Web3:
    return Web3(HTTPProvider(url or rpc_url(), request_kwargs={"timeout": 20}))


def norm_addr(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("address must be a string")
    value = value.strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError(f"invalid 20-byte EVM address: {value!r}")
    return value.lower()


def is_denied(token: str, denied: Iterable[str]) -> bool:
    token_norm = norm_addr(token)
    return token_norm in {norm_addr(item) for item in denied}


def apply_slippage(quoted_out: int, slippage_bps: int) -> int:
    if quoted_out <= 0:
        raise ValueError("quote must be positive")
    if not 0 <= slippage_bps <= 5000:
        raise ValueError("slippage_bps must be between 0 and 5000")
    result = quoted_out * (10_000 - slippage_bps) // 10_000
    if result <= 0:
        raise ValueError("amountOutMinimum became zero")
    return result


def _pad32(value: Any) -> str:
    if isinstance(value, int):
        if value < 0:
            raise ValueError("negative ABI integer")
        raw = hex(value)[2:]
    else:
        raw = str(value)
        if raw.startswith("0x"):
            raw = raw[2:]
    return raw.zfill(64)


def build_v3_exact_input_single(
    token_in: str,
    token_out: str,
    fee: int,
    recipient: str,
    amount_in_wei: int,
    amount_out_min_wei: int,
) -> str:
    token_in = norm_addr(token_in)
    token_out = norm_addr(token_out)
    recipient = norm_addr(recipient)
    if not 0 <= fee <= 0xFFFFFF:
        raise ValueError("fee is outside uint24")
    if amount_in_wei <= 0 or amount_out_min_wei <= 0:
        raise ValueError("swap amounts must be positive")
    return (
        "0x"
        + EXACT_INPUT_SINGLE_SELECTOR
        + _pad32(token_in)
        + _pad32(token_out)
        + _pad32(fee)
        + _pad32(recipient)
        + _pad32(amount_in_wei)
        + _pad32(amount_out_min_wei)
        + _pad32(0)
    )


def wrap_deadline(inner_calldata: str, deadline: int) -> str:
    if deadline <= int(time.time()):
        raise ValueError("deadline must be in the future")
    payload = bytes.fromhex(inner_calldata[2:])
    return "0x" + MULTICALL_DEADLINE_SELECTOR + encode(
        ["uint256", "bytes[]"], [deadline, [payload]]
    ).hex()


def classify_receipt(receipt: Dict[str, Any], stdout: str = "") -> Dict[str, Any]:
    if "status 0 (failed)" in stdout.lower():
        return {"outcome": "reverted", "status": 0}
    status = receipt.get("status")
    if isinstance(status, str):
        try:
            status = int(status, 16) if status.startswith("0x") else int(status)
        except ValueError:
            status = None
    if status == 1:
        return {"outcome": "ok", "status": 1}
    if status == 0:
        return {"outcome": "reverted", "status": 0}
    return {"outcome": "unknown_pending", "status": status}


def _checksum(w3: Web3, address: str) -> str:
    return w3.to_checksum_address(norm_addr(address))


def _token_info(w3: Web3, token: str) -> tuple[int, str]:
    contract = w3.eth.contract(address=_checksum(w3, token), abi=ERC20_ABI)
    decimals = int(contract.functions.decimals().call())
    if not 0 <= decimals <= 36:
        raise ValueError(f"invalid token decimals: {decimals}")
    try:
        symbol = str(contract.functions.symbol().call())
    except Exception:
        symbol = "?"
    return decimals, symbol


def token_balance(w3: Web3, token: str, wallet: str) -> int:
    contract = w3.eth.contract(address=_checksum(w3, token), abi=ERC20_ABI)
    return int(contract.functions.balanceOf(_checksum(w3, wallet)).call())


def _max_fee_per_gas(w3: Web3) -> int:
    block = w3.eth.get_block("latest")
    base_fee = int(block.get("baseFeePerGas", w3.eth.gas_price))
    try:
        tip = int(w3.eth.max_priority_fee)
    except Exception:
        tip = 1_000_000_000
    tip = max(tip, 100_000_000)
    return base_fee * 2 + tip


def build_plan(
    token: str,
    amount_in_wei: int,
    *,
    wallet: str = TRENCHES_WALLET,
    fee: int = 10_000,
    slippage_bps: int = 500,
    deadline_seconds: int = 180,
    gas_multiplier_bps: int = 12_000,
    max_gas_cost_wei: int = 1_500_000_000_000_000,
    w3: Optional[Web3] = None,
) -> SwapPlan:
    w3 = w3 or make_web3()
    token = norm_addr(token)
    wallet = norm_addr(wallet)
    if is_denied(token, [RHC_DENIED]):
        raise ValueError(f"token is permanently denied: {token}")
    if amount_in_wei <= 0:
        raise ValueError("amount_in_wei must be positive")
    if w3.eth.chain_id != CHAIN_ID:
        raise RuntimeError(f"wrong chain: expected {CHAIN_ID}, got {w3.eth.chain_id}")
    if not w3.eth.get_code(_checksum(w3, token)):
        raise ValueError(f"token has no contract code: {token}")

    factory = w3.eth.contract(address=_checksum(w3, V3_FACTORY), abi=FACTORY_ABI)
    pool = factory.functions.getPool(
        _checksum(w3, WETH), _checksum(w3, token), fee
    ).call()
    if norm_addr(pool) == ZERO_ADDRESS:
        raise ValueError(f"no Uniswap v3 WETH/token pool at fee {fee}")

    quoter = w3.eth.contract(address=_checksum(w3, QUOTER_V2), abi=QUOTER_ABI)
    quote = quoter.functions.quoteExactInputSingle((
        _checksum(w3, WETH),
        _checksum(w3, token),
        amount_in_wei,
        fee,
        0,
    )).call({"from": _checksum(w3, wallet)})
    quoted_out = int(quote[0])
    amount_out_min = apply_slippage(quoted_out, slippage_bps)
    decimals, symbol = _token_info(w3, token)

    inner = build_v3_exact_input_single(
        WETH, token, fee, wallet, amount_in_wei, amount_out_min
    )
    deadline = int(time.time()) + deadline_seconds
    calldata = wrap_deadline(inner, deadline)
    tx_call = {
        "from": _checksum(w3, wallet),
        "to": _checksum(w3, SWAP_ROUTER_02),
        "value": amount_in_wei,
        "data": calldata,
    }
    # Both calls must succeed before a plan can be signed.
    w3.eth.call(tx_call)
    raw_gas = int(w3.eth.estimate_gas(tx_call))
    gas_estimate = raw_gas * gas_multiplier_bps // 10_000
    max_fee_per_gas = _max_fee_per_gas(w3)
    worst_case_gas = gas_estimate * max_fee_per_gas
    if worst_case_gas > max_gas_cost_wei:
        raise RuntimeError(
            f"worst-case gas {worst_case_gas} wei exceeds cap {max_gas_cost_wei} wei"
        )

    return SwapPlan(
        chain_id=CHAIN_ID,
        wallet=wallet,
        token=token,
        pool=norm_addr(pool),
        router=norm_addr(SWAP_ROUTER_02),
        fee=fee,
        amount_in_wei=amount_in_wei,
        quoted_out=quoted_out,
        amount_out_min=amount_out_min,
        token_decimals=decimals,
        token_symbol=symbol,
        slippage_bps=slippage_bps,
        deadline=deadline,
        calldata=calldata,
        gas_estimate=gas_estimate,
        max_fee_per_gas=max_fee_per_gas,
        max_gas_cost_wei=max_gas_cost_wei,
    )


def execute_plan(
    plan: SwapPlan,
    *,
    private_key: Optional[str] = None,
    w3: Optional[Web3] = None,
    receipt_timeout: int = 90,
) -> Dict[str, Any]:
    w3 = w3 or make_web3()
    private_key = private_key or os.environ.get("RH_PRIVATE_KEY")
    if not private_key:
        return {"outcome": "not_sent", "error": "RH_PRIVATE_KEY is not set"}
    account = Account.from_key(private_key)
    if norm_addr(account.address) != norm_addr(plan.wallet):
        return {
            "outcome": "not_sent",
            "error": "RH_PRIVATE_KEY does not match the configured wallet",
        }
    before = token_balance(w3, plan.token, plan.wallet)
    tx = {
        "chainId": CHAIN_ID,
        "from": _checksum(w3, plan.wallet),
        "to": _checksum(w3, plan.router),
        "value": plan.amount_in_wei,
        "data": plan.calldata,
        "nonce": w3.eth.get_transaction_count(_checksum(w3, plan.wallet), "pending"),
        "gas": plan.gas_estimate,
        "maxFeePerGas": plan.max_fee_per_gas,
        "maxPriorityFeePerGas": max(100_000_000, plan.max_fee_per_gas // 10),
        "type": 2,
    }
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    try:
        receipt = dict(w3.eth.wait_for_transaction_receipt(tx_hash, timeout=receipt_timeout))
    except TimeExhausted:
        return {"outcome": "unknown_pending", "tx_hash": tx_hash}
    classified = classify_receipt(receipt)
    result: Dict[str, Any] = {
        **classified,
        "tx_hash": tx_hash,
        "block_number": receipt.get("blockNumber"),
        "gas_used": receipt.get("gasUsed"),
        "effective_gas_price": receipt.get("effectiveGasPrice", plan.max_fee_per_gas),
    }
    if classified["outcome"] != "ok":
        return result
    after = token_balance(w3, plan.token, plan.wallet)
    delta = after - before
    result["token_balance_delta"] = delta
    if delta <= 0:
        result["outcome"] = "receipt_ok_no_tokens"
        result["error"] = "receipt succeeded but token balance did not increase"
    return result


def display_plan(plan: SwapPlan) -> Dict[str, Any]:
    data = asdict(plan)
    scale = Decimal(10) ** plan.token_decimals
    data["amount_in_eth"] = str(Decimal(plan.amount_in_wei) / Decimal(10**18))
    data["quoted_out_display"] = str(Decimal(plan.quoted_out) / scale)
    data["amount_out_min_display"] = str(Decimal(plan.amount_out_min) / scale)
    data["max_gas_cost_eth"] = str(
        Decimal(plan.gas_estimate * plan.max_fee_per_gas) / Decimal(10**18)
    )
    data.pop("calldata")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--amount-eth", required=True)
    parser.add_argument("--fee", type=int, default=10_000)
    parser.add_argument("--slippage-bps", type=int, default=500)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    amount_eth = Decimal(args.amount_eth)
    amount_wei = int(amount_eth * Decimal(10**18))
    try:
        plan = build_plan(
            args.token,
            amount_wei,
            fee=args.fee,
            slippage_bps=args.slippage_bps,
        )
    except Exception as exc:
        print(json.dumps({"outcome": "preflight_failed", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"outcome": "simulated", "plan": display_plan(plan)}, indent=2))
    if not args.live:
        return 0
    result = execute_plan(plan)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("outcome") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
