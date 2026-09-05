import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rh_sell

KEY = os.environ.get("RH_PRIVATE_KEY")
if not KEY:
    print("ERROR: RH_PRIVATE_KEY not set")
    print("Run: read -rs KEY && RH_PRIVATE_KEY=$KEY python3 verify_sell_path.py")
    sys.exit(1)

TOKEN = "0x90A71817bdA6DAC8c3A28bBfd877b02D667ae2f9"
AMT = 5_000_000_000
print("=== SELL PATH VERIFICATION ===")
print(f"Token: {TOKEN} (ROBINHOOD)  Amount: {AMT} raw units")

print("[1/5] Building sell plan...")
plan = rh_sell.build_sell_plan(TOKEN, AMT, fee=10000, slippage_bps=500, require_balance=True)
print(f"  Pool={plan.pool} Quoted={plan.quoted_weth_out} wei Gas={plan.gas_estimate} MaxFee={plan.max_fee_per_gas}")

if plan.approval_required:
    print("[2/5] Building approval plan...")
    aplan = rh_sell.build_approval_plan(TOKEN, AMT)
    print(f"  Spender={aplan.spender} Gas={aplan.gas_estimate}")
    print("[3/5] Broadcasting approval...")
    ar = rh_sell.execute_approval_plan(aplan, private_key=KEY)
    print(f"  {json.dumps(ar)}")
    if ar.get("outcome") not in ("ok", "already_approved"):
        print("ERROR: approval failed")
        sys.exit(1)
    print("[4/5] Rebuilding sell plan...")
    plan = rh_sell.build_sell_plan(TOKEN, AMT, fee=10000, slippage_bps=500, require_balance=True)
else:
    print("[2/5] No approval needed")
    print("[3/5] Skipped")
    print("[4/5] Plan OK")

print("[5/5] Broadcasting sell...")
result = rh_sell.execute_sell_plan(plan, private_key=KEY)
print(f"  {json.dumps(result)}")

td = result.get("token_balance_delta", 0)
wd = result.get("weth_balance_delta", 0)
st = result.get("status", 0)
print("=== VERDICT ===")
if result.get("outcome") == "ok" and st == 1 and td < 0 and wd > 0:
    print(f"PASS — tx={result.get('tx_hash','?')} token_delta={td} weth_delta={wd}")
else:
    print(f"FAIL — outcome={result.get('outcome','?')} status={st} token={td} weth={wd}")
