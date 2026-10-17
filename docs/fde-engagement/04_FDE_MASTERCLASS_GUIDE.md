# 04. FDE Masterclass Guide: Institutional Crypto Trading Systems

> **Author**: Harshal Borse (`barshal-horse`)  
> **Persona**: Principal Forward Deployed Engineer & Quantitative Systems Architect  
> **Standard**: Production High-Throughput Quantitative Trading & Risk Architecture

---

## 1. ASCII End-to-End Request Lifecycle Diagram

The following diagram tracks a single candle tick through signal generation, ML inference, risk allocation, order routing, and the software-managed exit lifecycle, complete with millisecond latency budgets:

```
[ WebSocket / REST Ticks ]
           │
           │  (T+0ms) Ingestion Hop (~5ms)
           ▼
┌────────────────────────────────────────┐
│ CandleAggregator (5-Minute Bucket)     │
│ - Timeframe alignment                 │
│ - OHLCV bucket closure detection      │
└──────────────────┬─────────────────────┘
                   │
                   │  (T+5ms) Bar Closure Event (~15ms)
                   ▼
┌────────────────────────────────────────┐
│ SignalEngine v3 (Profit-Max Engine)    │
│ - EMA20 / EMA50 / SMA20 Proximity      │
│ - Breakout & Retest Confirmation       │
│ - Volume Expansion Ratio (Vol / VolMA) │
│ - ATR14 & RSI Bounds [38, 78]          │
└──────────────────┬─────────────────────┘
                   │
                   │  (T+20ms) Signal Object Created (~50ms)
                   ▼
┌────────────────────────────────────────┐
│ Machine Learning Refiner (FinBERT/PPO) │
│ - Headline NLP Sentiment Scoring       │
│ - PPO Action Filter & Policy Eval      │
│ - Composite Alpha Score Calculation    │
└──────────────────┬─────────────────────┘
                   │
                   │  (T+70ms) Alpha Candidate List (~10ms)
                   ▼
┌────────────────────────────────────────┐
│ Portfolio Risk Manager (Heat Guard)    │
│ - Aggregate Portfolio Heat <= 10.0%    │
│ - Cash Reserve Headroom >= 10.0%       │
│ - Volatility Parity / Fractional Sizing│
│ - Crypto Sector Correlation Guard      │
└──────────────────┬─────────────────────┘
                   │
                   │  (T+80ms) Sizing Approved (~120ms)
                   ▼
┌────────────────────────────────────────┐
│ Alpaca Broker Adapter (Execution)      │
│ - Sub-penny Slippage Guard             │
│ - Spot Crypto Market Order (Long-only) │
│ - Order Submission Confirmation        │
└──────────────────┬─────────────────────┘
                   │
                   │  (T+200ms) Order Filled on Ledger (~10ms)
                   ▼
┌────────────────────────────────────────┐
│ Dynamic Software Exit Engine           │
│ - Step 1: PnL >= +1.2 ATR -> Breakeven │
│ - Step 2: PnL >= +2.0 ATR -> Chandelier│
│ - Step 3: PnL >= +4.5 ATR -> Climax TP │
└──────────────────┬─────────────────────┘
                   │
                   │  (T+210ms) Persistent Logging (~15ms)
                   ▼
┌────────────────────────────────────────┐
│ SQLite Journal & WAL State             │
│ - Audit Trail (Realized PnL, Slippage) │
│ - GitHub Actions Artifact Persistence  │
└────────────────────────────────────────┘
  Total End-to-End Latency Budget: ~225ms
```

---

## 2. Pattern Dictionary: High-Frequency & Quantitative Design Patterns

### 1. Volatility Parity Sizing (Inverse-ATR Sizing)
- **Concept**: Instead of risking a fixed dollar amount or fixed percentage of equity on each asset, we size positions such that each position's dollar risk per unit of market volatility is identical.
- **Formula**:
  $$\text{Unit Size} = \frac{\text{Equity} \times \text{Risk Fraction}}{\text{ATR}_{14}}$$
- **Why it Wins**: A 5% move in Bitcoin represents a totally different regime than a 5% move in Shiba Inu. Volatility parity ensures high-volatility assets don't blow up the portfolio while low-volatility assets aren't starved of capital.

### 2. Aggregate Portfolio Heat (Value-at-Risk Budget)
- **Concept**: The sum of all active downside exposures across the entire book:
  $$\text{Portfolio Heat} = \sum_{i=1}^{N} \frac{\text{Quantity}_i \times (\text{Entry}_i - \text{StopLoss}_i)}{\text{Equity}} \le 10.0\%$$
- **Why it Wins**: Traditional bots limit "position count" (e.g. max 5 positions). That is amateurish: 5 positions with 5% risk equals 25% account heat (fatal ruin risk), while 15 positions with 0.5% risk equals 7.5% heat (institutional grade). Portfolio Heat uncouples position count from catastrophic failure risk.

### 3. Chandelier Trailing Stop
- **Concept**: A volatility-pegged trailing exit developed by Chuck LeBeau:
  $$\text{StopPrice} = \text{HighestHigh}(N) - k \times \text{ATR}$$
- **Why it Wins**: Normal percentage trailing stops (e.g. "trail by 2%") get chopped out on normal random noise during high-volatility regimes. Chandelier stops expand their distance when volatility surges and contract during quiet trends, preventing premature stop-outs of massive multi-day runners.

### 4. Breakeven Ratchet
- **Concept**: When a position reaches $+1.2 \times ATR$ in unrealized profit, the software ratchets the Stop Loss to $\text{Entry} + 0.1 \times ATR$.
- **Why it Wins**: Converts a risky trade into a "free trade" (zero downside risk, small profit locked). This preserves psychological capital and protects portfolio equity from round-tripping winning trades into full losses.

### 5. WAL (Write-Ahead Logging) SQLite Journaling
- **Concept**: SQLite's write-ahead log separates reader locks from writer locks, allowing concurrent readers (Streamlit UI, scanner thread, test suites) to access data without blocking trade execution writes.

---

## 3. Failure Mode & Chaos Matrix

| Scenario / Chaos Injection | System Behavior & Failure Mechanism | Recovery & Fail-Closed Guarantee |
|---|---|---|
| **Alpaca API 429 Rate Limiting** | Broker calls fail with HTTP 429 during market surges. | Exponential backoff with random jitter ($10\text{s} \times 1.5^n \pm \text{jitter}$). Order queue does NOT double-submit. |
| **Partial Order Fill** | Alpaca fills 0.42 BTC of a requested 1.0 BTC order. | `_check_software_exits()` queries live broker positions before firing exit orders to ensure exact available quantity is sold, preventing negative balances. |
| **Broker Rejects Bracket Orders on Crypto** | Alpaca spot crypto API rejects orders containing attached stop-loss or take-profit legs. | System detects `is_crypto_symbol(symbol)`, routes entry as a standalone market order, and executes stops via local high-frequency software monitoring (`_check_software_exits`). |
| **FinBERT HuggingFace Down / Cold** | Memory exhausted or PyTorch inference error during news parsing. | Caught cleanly via `try/except`; system logs `FinBERT warm-up failed` and falls back to pure technical breakout-retest alpha without crashing the trading daemon. |
| **Simultaneous Multi-Symbol Breakout** | 10 pairs trigger BUY signals on the exact same 5-minute candle. | Candidates are sorted by `composite_score` descending. The loop enters trades sequentially until `can_open_position` triggers a heat cap ($\ge 10\%$) or cash buffer breach ($\le 10\%$), cleanly blocking the rest. |

---

## 4. Flashcard Cheat Sheet: Staff-Level Q&A

**Q1: Why uncap `max_positions` instead of keeping a strict limit of 5?**  
*Answer*: Fixed counts are an arbitrary heuristic. By replacing fixed counts with **Aggregate Portfolio Heat** ($\le 10\%$) and **Cash Headroom** ($\ge 10\%$), the engine dynamically maximizes capital efficiency. It can open 10+ trades during diversified, low-risk setups while automatically contracting to 2 or 3 trades during high-volatility regimes.

**Q2: What is the mathematical flaw with static take-profit targets in crypto?**  
*Answer*: Cryptocurrency trends exhibit heavy-tailed, power-law distributions. Static targets ($+2 \times ATR$) cap the right tail of the return distribution, guaranteeing negative skew where rare catastrophic drawdowns outweigh capped winners. Dynamic trailing stops preserve positive skew.

**Q3: How does the Breakeven Ratchet prevent trade degradation?**  
*Answer*: At $+1.2 \times ATR$, the stop is elevated to $\text{Entry} + 0.1 \times ATR$. This eliminates downside downside variance and locks in trading fees, turning the position into a costless option on trend continuation.

**Q4: How does the system handle spot crypto's prohibition of short selling on Alpaca?**  
*Answer*: The `PortfolioRiskManager` enforces a hard rule on line 60: any signal with `action == "SELL"` on a crypto asset is immediately rejected with `REJECT: SPOT_CRYPTO_NO_SHORT`, preventing broker order rejections.

**Q5: Why use an ATR-based Chandelier trail instead of a fixed percentage trail?**  
*Answer*: Assets possess different volatility signatures across different market regimes. A 2% trail on BTC might survive noise, whereas a 2% trail on SOL or DOGE will get stopped out within minutes. The Chandelier stop scales dynamically with instantaneous asset variance.

**Q6: What prevents the trading bot from depleting all cash on high-conviction signals?**  
*Answer*: The `min_cash_buffer_pct` invariant (10%). Before opening any position, the risk manager computes total invested equity. If post-trade cash would drop below 10% of total equity, the trade is rejected.

**Q7: How does the engine survive GitHub Actions 6-hour runner terminations?**  
*Answer*: The engine runs for 340 minutes, writes all state, active orders, and PnL to SQLite in WAL mode, uploads the database as a workflow artifact, and terminates cleanly with exit code 0 before GitHub kills the job. The next scheduled run re-seeds from DB state.

**Q8: What is Volatility Parity?**  
*Answer*: Sizing positions inversely proportional to asset volatility ($\text{Size} \propto \frac{1}{\text{ATR}}$) so that each trade contributes an identical dollar risk budget to the portfolio.

**Q9: Why are crypto assets split into 5 sub-sectors instead of 1 global crypto bucket?**  
*Answer*: Cryptocurrency sub-sectors exhibit distinct beta and rotation characteristics (e.g. DeFi vs Memes vs Core L1s). Disaggregating allows the bot to capture sector rotation without falsely treating all pairs as 100% correlated.

**Q10: What ensures zero-credential resilience for test suites?**  
*Answer*: Mock brokers, SQLite in-memory/isolated files, and deterministic price series in `tests/test_crypto_system.py` allow full test coverage without network calls or live API keys.
