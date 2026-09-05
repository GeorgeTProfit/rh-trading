#!/usr/bin/env python3
"""Hardened ERC-20 -> WETH sell path for Robinhood Chain Uniswap v3.

Approval and swap are separate, exact-amount transactions. A sell is verified
only when the receipt succeeds, token balance decreases, and WETH increases.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from eth_account import Account
from web3 import Web3
from web3.exceptions import TimeExhausted

import rh_swap

ALLOWANCE_APPROVE_ABI = [
    {
        "type": "function", "name": "allowance", "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function", "name": "approve", "stateMutability": "nonpayable",
        "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


@dataclass(frozen=True)
class ApprovalPlan:
    token: str
    wallet: str
    spender: str
    amount: int
    required: bool
    calldata: str
    gas_estimate: int
    max_fee_per_gas: int


@dataclass(frozen=True)
class SellPlan:
    token: str
    wallet: str
    pool: str
    router: str
    fee: int
    token_amount: int
    quoted_weth_out: int
    min_weth_out: int
    token_decimals: int
    token_symbol: str
    slippage_bps: int
    calldata: str
    gas_estimate: int
    max_fee_per_gas: int
    approval_required: bool


def build_sell_calldata(token: str, wallet: str, fee: int,
                        *, token_amount: int, min_weth_out: int) -> str:
    return rh_swap.build_v3_exact_input_single(
        token, rh_swap.WETH, fee, wallet, token_amount, min_weth_out
    )


def token_allowance(w3: Web3, token: str, wallet: str,
                    spender: str = rh_swap.SWAP_ROUTER_02) -> int:
    contract = w3.eth.contract(
        address=w3.to_checksum_address(rh_swap.norm_addr(token)),
        abi=ALLOWANCE_APPROVE_ABI,
    )
    return int(contract.functions.allowance(
        w3.to_checksum_address(rh_swap.norm_addr(wallet)),
        w3.to_checksum_address(rh_swap.norm_addr(spender)),
    ).call())


def build_approval_plan(token: str, amount: int, *, wallet: str = rh_swap.TRENCHES_WALLET,
                        spender: str = rh_swap.SWAP_ROUTER_02,
                        max_gas_cost_wei: int = 1_500_000_000_000_000,
                        w3: Optional[Web3] = None) -> ApprovalPlan:
    w3 = w3 or rh_swap.make_web3()
    token = rh_swap.norm_addr(token)
    wallet = rh_swap.norm_addr(wallet)
    spender = rh_swap.norm_addr(spender)
    if amount <= 0:
        raise ValueError("approval amount must be positive")
    if rh_swap.is_denied(token, [rh_swap.RHC_DENIED]):
        raise ValueError(f"token is permanently denied: {token}")
    allowance = token_allowance(w3, token, wallet, spender)
    if allowance >= amount:
        return ApprovalPlan(token, wallet, spender, amount, False, "0x", 0, 0)
    contract = w3.eth.contract(
        address=w3.to_checksum_address(token), abi=ALLOWANCE_APPROVE_ABI
    )
    calldata = contract.functions.approve(
        w3.to_checksum_address(spender), amount
    )._encode_transaction_data()
    call = {
        "from": w3.to_checksum_address(wallet),
        "to": w3.to_checksum_address(token),
        "value": 0,
        "data": calldata,
    }
    w3.eth.call(call)
    gas_estimate = int(w3.eth.estimate_gas(call)) * 12_000 // 10_000
    max_fee = rh_swap._max_fee_per_gas(w3)
    if gas_estimate * max_fee > max_gas_cost_wei:
        raise RuntimeError("approval gas exceeds configured cap")
    return ApprovalPlan(token, wallet, spender, amount, True, calldata, gas_estimate, max_fee)


def execute_approval_plan(plan: ApprovalPlan, *, private_key: Optional[str] = None,
                          w3: Optional[Web3] = None, receipt_timeout: int = 90) -> Dict[str, Any]:
    if not plan.required:
        return {"outcome": "already_approved", "status": 1}
    w3 = w3 or rh_swap.make_web3()
    private_key = private_key or os.environ.get("RH_PRIVATE_KEY")
    if not private_key:
        return {"outcome": "not_sent", "error": "RH_PRIVATE_KEY is not set"}
    account = Account.from_key(private_key)
    if rh_swap.norm_addr(account.address) != plan.wallet:
        return {"outcome": "not_sent", "error": "private key does not match sell wallet"}
    tx = {
        "chainId": rh_swap.CHAIN_ID,
        "from": w3.to_checksum_address(plan.wallet),
        "to": w3.to_checksum_address(plan.token),
        "value": 0,
        "data": plan.calldata,
        "nonce": w3.eth.get_transaction_count(w3.to_checksum_address(plan.wallet), "pending"),
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
    result = rh_swap.classify_receipt(receipt)
    return {**result, "tx_hash": tx_hash, "gas_used": receipt.get("gasUsed"),
            "effective_gas_price": receipt.get("effectiveGasPrice", plan.max_fee_per_gas)}


def build_sell_plan(token: str, token_amount: int, *, wallet: str = rh_swap.TRENCHES_WALLET,
                    fee: int = 10_000, slippage_bps: int = 500,
                    max_gas_cost_wei: int = 1_500_000_000_000_000,
                    require_balance: bool = True,
                    w3: Optional[Web3] = None) -> SellPlan:
    w3 = w3 or rh_swap.make_web3()
    token = rh_swap.norm_addr(token)
    wallet = rh_swap.norm_addr(wallet)
    if token_amount <= 0:
        raise ValueError("token_amount must be positive")
    if rh_swap.is_denied(token, [rh_swap.RHC_DENIED]):
        raise ValueError(f"token is permanently denied: {token}")
    if int(w3.eth.chain_id) != rh_swap.CHAIN_ID:
        raise RuntimeError(f"wrong chain: expected {rh_swap.CHAIN_ID}, got {w3.eth.chain_id}")
    if require_balance and rh_swap.token_balance(w3, token, wallet) < token_amount:
        raise ValueError("sell amount exceeds wallet token balance")

    factory = w3.eth.contract(
        address=w3.to_checksum_address(rh_swap.V3_FACTORY), abi=rh_swap.FACTORY_ABI
    )
    pool = factory.functions.getPool(
        w3.to_checksum_address(token), w3.to_checksum_address(rh_swap.WETH), fee
    ).call()
    if rh_swap.norm_addr(pool) == rh_swap.ZERO_ADDRESS:
        raise ValueError(f"no Uniswap v3 token/WETH pool at fee {fee}")

    quoter = w3.eth.contract(
        address=w3.to_checksum_address(rh_swap.QUOTER_V2), abi=rh_swap.QUOTER_ABI
    )
    quote = quoter.functions.quoteExactInputSingle((
        w3.to_checksum_address(token), w3.to_checksum_address(rh_swap.WETH),
        token_amount, fee, 0,
    )).call({"from": w3.to_checksum_address(wallet)})
    quoted = int(quote[0])
    minimum = rh_swap.apply_slippage(quoted, slippage_bps)
    decimals, symbol = rh_swap._token_info(w3, token)
    calldata = build_sell_calldata(
        token, wallet, fee, token_amount=token_amount, min_weth_out=minimum
    )
    allowance = token_allowance(w3, token, wallet)
    approval_required = allowance < token_amount
    gas_estimate = 0
    max_fee = rh_swap._max_fee_per_gas(w3)
    if not approval_required:
        call = {
            "from": w3.to_checksum_address(wallet),
            "to": w3.to_checksum_address(rh_swap.SWAP_ROUTER_02),
            "value": 0,
            "data": calldata,
        }
        w3.eth.call(call)
        gas_estimate = int(w3.eth.estimate_gas(call)) * 12_000 // 10_000
        if gas_estimate * max_fee > max_gas_cost_wei:
            raise RuntimeError("sell gas exceeds configured cap")
    return SellPlan(
        token, wallet, rh_swap.norm_addr(pool), rh_swap.norm_addr(rh_swap.SWAP_ROUTER_02),
        fee, token_amount, quoted, minimum, decimals, symbol, slippage_bps,
        calldata, gas_estimate, max_fee, approval_required,
    )


def classify_sell_result(receipt: Dict[str, Any], *, token_before: int, token_after: int,
                         weth_before: int, weth_after: int) -> Dict[str, Any]:
    classified = rh_swap.classify_receipt(receipt)
    token_delta = token_after - token_before
    weth_delta = weth_after - weth_before
    result = {**classified, "token_balance_delta": token_delta, "weth_balance_delta": weth_delta}
    if classified["outcome"] != "ok":
        return result
    if token_delta >= 0:
        result["outcome"] = "receipt_ok_tokens_not_decreased"
    elif weth_delta <= 0:
        result["outcome"] = "receipt_ok_no_weth"
    return result


def execute_sell_plan(plan: SellPlan, *, private_key: Optional[str] = None,
                      w3: Optional[Web3] = None, receipt_timeout: int = 90) -> Dict[str, Any]:
    if plan.approval_required:
        return {"outcome": "not_sent", "error": "sell plan requires approval and fresh rebuild"}
    w3 = w3 or rh_swap.make_web3()
    private_key = private_key or os.environ.get("RH_PRIVATE_KEY")
    if not private_key:
        return {"outcome": "not_sent", "error": "RH_PRIVATE_KEY is not set"}
    account = Account.from_key(private_key)
    if rh_swap.norm_addr(account.address) != plan.wallet:
        return {"outcome": "not_sent", "error": "private key does not match sell wallet"}
    token_before = rh_swap.token_balance(w3, plan.token, plan.wallet)
    weth_before = rh_swap.token_balance(w3, rh_swap.WETH, plan.wallet)
    tx = {
        "chainId": rh_swap.CHAIN_ID,
        "from": w3.to_checksum_address(plan.wallet),
        "to": w3.to_checksum_address(plan.router),
        "value": 0,
        "data": plan.calldata,
        "nonce": w3.eth.get_transaction_count(w3.to_checksum_address(plan.wallet), "pending"),
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
    token_after = rh_swap.token_balance(w3, plan.token, plan.wallet)
    weth_after = rh_swap.token_balance(w3, rh_swap.WETH, plan.wallet)
    result = classify_sell_result(
        receipt, token_before=token_before, token_after=token_after,
        weth_before=weth_before, weth_after=weth_after,
    )
    return {**result, "tx_hash": tx_hash, "gas_used": receipt.get("gasUsed"),
            "effective_gas_price": receipt.get("effectiveGasPrice", plan.max_fee_per_gas)}
