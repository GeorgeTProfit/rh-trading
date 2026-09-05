#!/usr/bin/env python3
"""Event-level swap data fetcher for Robinhood Chain.

Pulls Uniswap v3 Swap logs from a public RPC, decodes them into typed rows,
and writes them as JSONL. v4 support is intentionally out of scope for v1.

Design constraints:
- No private keys. No transactions. Read-only.
- Operates against an injected `rpc` callable so tests can use a fake.
- Chunked to avoid the chain's getLogs block range limits.
- Reorg-safe: logs with `removed: true` are filtered out before decode.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def _s256(v: int) -> str:
    """Encode a signed int as 32-byte two's-complement hex (no 0x prefix)."""
    return hex(v if v >= 0 else (1 << 256) + v)[2:].rjust(64, "0")


def _hex_topic_address(address: str) -> str:
    a = address.lower().replace("0x", "")
    if len(a) != 40:
        raise ValueError(f"bad address: {address}")
    return "0x" + ("0" * 24) + a


def fetch_logs_in_range(
    rpc: Any,
    from_block: int,
    to_block: int,
    *,
    chunk: int = 1_000,
    address: Optional[str] = None,
    topic0: str = V3_SWAP_TOPIC,
    fetch: Optional[Callable[[Any, int, int], List[Dict[str, Any]]]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    """Walk a block range in chunks, collecting Swap logs.

    `fetch` is injected so tests can supply deterministic fakes.
    If `fetch` is None, the default calls `rpc.call("eth_getLogs", params)`.
    `progress` is called as `progress(current, total)` after each chunk.
    """
    if from_block > to_block:
        return []
    if fetch is None:
        def fetch(rpc_, fr, to):
            params: Dict[str, Any] = {
                "fromBlock": hex(fr),
                "toBlock": hex(to),
                "topics": [topic0],
            }
            if address is not None:
                params["address"] = address
            return list(rpc_.call("eth_getLogs", [params]))
    out: List[Dict[str, Any]] = []
    cursor = from_block
    while cursor <= to_block:
        end = min(cursor + chunk - 1, to_block)
        out.extend(fetch(rpc, cursor, end))
        if progress is not None:
            progress(end - from_block + 1, to_block - from_block + 1)
        cursor = end + 1
    return out


def filter_logs(raw_logs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop removed logs (reorgs). Preserves input order."""
    return [r for r in raw_logs if not r.get("removed", False)]


def decode_swap_log(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Decode one Uniswap v3 Swap log into a typed row."""
    data = raw["data"]
    if data.startswith("0x"):
        data = data[2:]
    expected = 5 * 64
    if len(data) != expected:
        raise ValueError(f"unexpected Swap data length: {len(data)}")
    amount0_u = int(data[0:64], 16)
    amount0 = amount0_u - (1 << 256) if amount0_u >= (1 << 255) else amount0_u
    amount1_u = int(data[64:128], 16)
    amount1 = amount1_u - (1 << 256) if amount1_u >= (1 << 255) else amount1_u
    sqrt_price_x96 = int(data[128:192], 16)
    liquidity = int(data[192:256], 16)
    tick_u = int(data[256:320], 16)
    tick = tick_u - (1 << 256) if tick_u >= (1 << 255) else tick_u
    if tick < -(1 << 23) or tick >= (1 << 23):
        raise ValueError("invalid tick encoding")
    topics = raw["topics"]
    if len(topics) < 3:
        raise ValueError("Swap log must have sender+recipient topics")
    sender = "0x" + topics[1][-40:]
    recipient = "0x" + topics[2][-40:]
    return {
        "pool": raw["address"].lower(),
        "block_number": int(raw["blockNumber"], 16),
        "block_hash": raw["blockHash"],
        "tx_hash": raw["transactionHash"],
        "transaction_index": int(raw.get("transactionIndex", "0x0"), 16),
        "log_index": int(raw["logIndex"], 16),
        "sender": sender.lower(),
        "recipient": recipient.lower(),
        "amount0": amount0,
        "amount1": amount1,
        "sqrt_price_x96": sqrt_price_x96,
        "liquidity": liquidity,
        "tick": tick,
    }


def classify_direction(
    amount0: int, amount1: int, symbol0: str = "token0", symbol1: str = "token1"
) -> Tuple[str, int, int]:
    """Return (side, base_amount_in, quote_amount_out).

    `side` is "buy" if the swap acquired token1, "sell" otherwise.
    `base_amount_in` and `quote_amount_out` are absolute values.
    """
    if amount0 > 0 and amount1 < 0:
        # Pool receives token0 and sends token1: user buys token1.
        return ("buy", amount0, abs(amount1))
    if amount0 < 0 and amount1 > 0:
        # Pool sends token0 and receives token1: user sells token1.
        return ("sell", amount1, abs(amount0))
    raise ValueError(f"non-directional swap: amount0={amount0} amount1={amount1}")


def fetch_pool_token_addresses(rpc: Any, pool: str) -> Tuple[str, str]:
    """Call pool.token0() and pool.token1() via the RPC."""
    selector_token0 = "0x0dfe1681"  # token0()
    selector_token1 = "0xd21220a7"  # token1()
    t0 = rpc.call("eth_call", [{"to": pool, "data": selector_token0}, "latest"])
    t1 = rpc.call("eth_call", [{"to": pool, "data": selector_token1}, "latest"])
    if isinstance(t0, str) and t0.startswith("0x"):
        return ("0x" + t0[-40:]).lower(), ("0x" + t1[-40:]).lower()
    raise ValueError(f"bad eth_call response: {t0!r}")


def collect_pool_swaps(
    rpc: Any,
    pool: str,
    from_block: int,
    to_block: int,
    *,
    chunk: int = 1_000,
) -> List[Dict[str, Any]]:
    """Fetch, filter, and decode all v3 Swap logs for one pool."""
    raw = fetch_logs_in_range(rpc, from_block, to_block, chunk=chunk, address=pool)
    raw = filter_logs(raw)
    out: List[Dict[str, Any]] = []
    for r in raw:
        try:
            out.append(decode_swap_log(r))
        except ValueError:
            continue
    return out


def write_jsonl(rows: Iterable[Dict[str, Any]], path: str) -> int:
    """Write rows as JSONL. Returns the count written."""
    n = 0
    with open(path, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            n += 1
    return n


def _default_rpc_call(url: str):
    """Return a function that talks to the given RPC URL using stdlib urllib."""
    import json as _json
    import urllib.request

    def call(method, params):
        body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        # The public Robinhood Chain RPC rate-limits arbitrary UAs with HTTP 429
        # but accepts `curl/8.0` without rate limits. Alchemy handles any UA.
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "curl/8.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data["result"]
    return call


def default_rpc_url() -> str:
    """Prefer Alchemy if the env key is set, else the public Robinhood Chain RPC.

    The public RPC handles large eth_getLogs ranges without a chunk-size cap.
    Alchemy's free tier is capped at 10 blocks per request on this chain.
    """
    import os
    key = os.environ.get("RH_ALCHEMY_KEY")
    if key:
        return f"https://robinhood-mainnet.g.alchemy.com/v2/{key}"
    return "https://rpc.mainnet.chain.robinhood.com"


# The public Robinhood Chain RPC accepts large eth_getLogs ranges (we tested
# 110k blocks in <1s). Alchemy Free tier is capped at 10 blocks. The default
# of 1000 works on the public RPC and stays well under Alchemy's 10k limit if
# the key is later set.
DEFAULT_CHUNK = 1000


def collect_mainnet_swaps(
    pool: str,
    from_block: int,
    to_block: int,
    *,
    rpc_url: Optional[str] = None,
    chunk: int = DEFAULT_CHUNK,
    progress: bool = True,
) -> List[Dict[str, Any]]:
    """Convenience: read the live public Robinhood Chain RPC for one pool."""
    if rpc_url is None:
        rpc_url = default_rpc_url()
    rpc = _default_rpc_call(rpc_url)

    class _Rpc:
        def __init__(self, c):
            self.c = c
        def call(self, m, p):
            return self.c(m, p)

    r = _Rpc(rpc)
    raw = fetch_logs_in_range(r, from_block, to_block, chunk=chunk, address=pool)
    raw = filter_logs(raw)
    out: List[Dict[str, Any]] = []
    for entry in raw:
        try:
            out.append(decode_swap_log(entry))
        except ValueError:
            continue
    if progress:
        print(f"  raw logs: {len(raw)}, decoded: {len(out)}, dropped: {len(raw) - len(out)}")
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Pull v3 Swap logs for a pool on Robinhood Chain")
    ap.add_argument("--pool", required=True)
    ap.add_argument("--from", dest="from_block", type=int, required=True)
    ap.add_argument("--to", dest="to_block", type=int, required=True)
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    rows = collect_mainnet_swaps(
        args.pool, args.from_block, args.to_block, chunk=args.chunk
    )
    n = write_jsonl(rows, args.out)
    print(f"  wrote {n} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
