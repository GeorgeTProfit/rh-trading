---
aliases:
  - "Test Trade Rules"
  - "RH Chain Trading Rules v1"
tags:
  - strategy
  - rules
  - paper-trade
---

# Test Trade Rules — Robinhood Chain

*Drafted 2026-09-04. For use during paper-trading and first real trades with ~0.025 ETH.*

## ⚠️ Critical Safety Rules (Non-Negotiable)

1. **NEVER paste private keys or seed phrases anywhere** — not in chat, not in files, not in terminals
2. **Always check honeypot before buying:** https://trustswap.com/robinhood/honeypot-checker
3. **Max position size: $10 per trade** (or ~0.003 ETH). Never more.
4. **Stop loss: hard exit at -15%** on any trade. No exceptions.
5. **Daily loss limit: $25** (all ETH). If hit, STOP trading until next day.
6. **Minimum 2 hours between trades.** No revenge trading.

## 📊 Pre-Trade Checklist (Every Single Trade)

Run this sequence BEFORE clicking "confirm" in MetaMask:

### 1. Honeypot Check ✅
- Open: `https://trustswap.com/robinhood/honeypot-checker?token=0x{CONTRACT}`
- Verify: NOT a honeypot, can buy AND sell
- If unsure: DO NOT BUY

### 2. Liquidity Check ✅
- Minimum liquidity: $5,000 (scanner default)
- Ideal: $10,000+ (less slippage, safer)
- Check: https://dexscreener.com/robinhood/{CONTRACT}

### 3. Volume Check ✅
- 24h volume: $20,000+ (scanner default)
- 1h volume: increasing (buying pressure)
- Ratio: 1h vol / 24h vol > 0.1 (active trading)

### 4. Token Age Check ✅
- Token age: 1-48 hours (scanner default)
- Too fresh (<30min): high rug risk
- Too old (>7 days): already discovered, less upside

### 5. Market Cap Check ✅
- Market cap: $20,000 - $2,000,000
- Below $20k: too risky (illiquid)
- Above $2M: dilution risk, less upside

### 6. Price Action Check ✅
- 1h change: +5% to +50% (healthy uptrend)
- 24h change: +10% to +800% (growing, not peaked)
- NOT already +800% in 24h (too late to enter)

### 7. Buy/Sell Ratio Check ✅
- Healthy ratio: 45%-90% buys (some selling = normal)
- Too clean (>90% buys): possible wash trading
- Too many sells (<45% buys): selling pressure

## 🎯 Trade Setup Criteria (Strategy A: Momentum)

**Scanner auto-filters candidates meeting these:**
```json
{
  "min_liquidity_usd": 5000,
  "min_volume_24h_usd": 20000,
  "max_age_hours": 48,
  "min_market_cap": 20000,
  "max_market_cap": 2000000,
  "min_buy_sell_ratio": 0.45,
  "max_buy_sell_ratio": 0.90,
  "min_tx_count_1h": 10,
  "max_24h_pump_pct": 800,
  "score_threshold": 5.0
}
```

**When scanner alerts you:**
1. Score >= 5.0 = strong momentum candidate
2. Score 3-5 = watchlist (manual review needed)
3. Score < 3 = ignore (unless you see something the scanner misses)

## 📈 Trade Execution Rules

### Entry
- **Amount:** $5-$10 (never more than $10)
- **Timing:** Buy on pullback (not at peak of green candle)
- **Method:** Uniswap V4 on app.uniswap.org (select Robinhood Chain)
- **Slippage:** 5% (higher if token is very new)

### Exit
- **Take Profit 1:** +30% gain → sell 50% of position
- **Take Profit 2:** +60% gain → sell 30% of position  
- **Trailing Stop:** Hold 20% with trailing stop at -10%
- **Hard Stop:** -15% on entire position → EXIT IMMEDIATELY

### Position Management
- Max 1 open position at a time
- No averaging down (never add to losing trades)
- No leverage (no borrowed funds)
- Wait 2 hours minimum between trades

## 📝 Trade Journal Entry Template

For EVERY trade (win or loss), log this:

```markdown
## Trade #[NUMBER]

**Date:** YYYY-MM-DD HH:MM UTC
**Token:** $SYMBOL
**Contract:** 0x...
**Entry Price:** $X.XX
**Amount:** $Y.YY (ETH)
**Exit Price:** $X.XX
**Gross P&L:** +$Z.ZZ / -$Z.ZZ
**Fees:** -$F.FF (gas + swap fees)
**Net P&L:** +$N.NN / -$N.NN
**Hold Time:** Xh Ym
**Strategy:** Momentum
**Score:** X.X (scanner score)

**Reason for entry:**
> (Why did you buy? What was the signal?)

**What went right/wrong:**
> (What happened? Did the setup play out?)

**Lesson learned:**
> (What would you do differently?)

**Discipline check:**
- [ ] Did I follow my rules? (Yes/No)
- [ ] Did I hit my stop loss? (Yes/No)
- [ ] Did I take profits as planned? (Yes/No)
```

## 🚨 Red Flags (DO NOT TRADE IF...)

1. ❌ Honeypot check says can't sell
2. ❌ Liquidity <$5,000
3. ❌ Already pumped >800% in 24h
4. ❌ Market cap >$2M (diluted)
5. ❌ Buy/sell ratio >90% (wash trading)
6. ❌ Token age <30 minutes (too fresh)
7. ❌ You're feeling FOMO or revenge trading
8. ❌ You haven't checked the honeypot checker
9. ❌ You're trading after hitting daily loss limit
10. ❌ You can't articulate WHY you're buying

## 📅 Weekly Review Framework

At end of each week, review:

1. **Count trades:** Total wins, total losses
2. **Calculate win rate:** wins / total trades
3. **Calculate avg win/loss:** avg $ won vs avg $ lost
4. **Check discipline:** Did you follow rules? (If not, that's the fix)
5. **Analyze losses:** What went wrong? (Rug? Bad timing? FOMO?)
6. **Analyze wins:** What worked? (Good entry? Good exit?)
7. **Adjust:** If sample > 10 trades, tweak scanner thresholds

## 🎓 Learning Path

### Phase 1: Paper Trading (Weeks 1-2)
- Goal: 10 trades minimum
- Use scanner alerts but DON'T execute with real ETH
- Practice the checklist manually
- Log every trade in journal
- Target: 40%+ win rate

### Phase 2: Small Real Trades (Weeks 3-4)
- Fund wallet with 0.025 ETH (~$85)
- Start with $5 trades
- Follow rules strictly
- Target: Same win rate on real trades as paper trades

### Phase 3: Scale Up (Month 2+)
- Only scale if:
  - 20+ real trades logged
  - Positive P&L after fees
  - 100% rule compliance
  - Can explain every trade decision
- Scale to $10-$20 per trade max
- Reassess every 10 trades

## 🔗 Quick Links (Save These)

- **Dexscreener (RH Chain):** https://dexscreener.com/robinhood
- **Honeypot Check:** https://trustswap.com/robinhood/honeypot-checker
- **Block Explorer:** https://robinhoodchain.blockscout.com
- **Bridge:** https://across.to/
- **Uniswap:** https://app.uniswap.org/ (select Robinhood Chain)
- **Scanner:** `python3 ~/Obsidian/TradingVault/03-Tooling/auto_scanner.py`
- **Auto-Trade:** `python3 ~/Obsidian/TradingVault/03-Tooling/auto_trader.py`

## 💡 Pro Tips

1. **Scanner is your friend.** It filters out 90% of rugs automatically.
2. **Patience pays.** Most gains happen in first 2-4 hours, not first 2 minutes.
3. **Cut losses fast.** One -15% loss can wipe out 3 wins if you don't stop.
4. **Journal everything.** Data > feelings. Your journal will tell the truth.
5. **Start small.** $5 trades teach you the same lessons as $50 trades but safer.
6. **No FOMO.** If you miss a trade, another one will come. Always.
7. **Rest when tired.** Bad decisions happen when you're exhausted.
8. **Community > bots.** Join RH Chain Discord/Telegram for real-time info.
9. **Learn the charts.** Dexscreener 5m charts are your primary tool.
10. **Stay humble.** Even pros lose 60% of trades. Discipline beats talent.
