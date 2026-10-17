# 02. Architectural Decision Records (ADR)

## ADR-001: Transition from Fixed Position Counts to Dynamic Portfolio Heat

### Status
**Accepted & Implemented**

### Context
In naive trading bots, risk is controlled by fixed integers (e.g. `max_positions = 5`). In cryptocurrency markets with asymmetric volatility, an arbitrary count is mathematically flawed:
- Holding 5 low-volatility large caps (`BTC`, `ETH`) with tight stops may risk only $1.5\%$ of equity.
- Holding 2 high-beta meme tokens (`DOGE`, `SHIB`) with wide ATR stops may risk $8.0\%$ of equity.
Furthermore, grouping all crypto assets under a single correlation bucket with `max_correlated = 2` locked out $80\%$ of genuine high-confidence alpha opportunities.

### Decision
1. Eliminate arbitrary position count bottlenecks (raising `max_positions` to 15).
2. Segment crypto into distinct sub-sectors (`Crypto_Core`, `Crypto_L1`, `Crypto_DeFi`, `Crypto_Meme`, `Crypto_Payments`).
3. Implement **Aggregate Portfolio Heat Guard**:
   $$\text{Portfolio Heat (\%)} = \frac{\sum_{i=1}^{N} \text{Qty}_i \times (\text{Entry}_i - \text{SL}_i)}{\text{Equity}} \times 100 \le 10.0\%$$
4. Enforce **Minimum Cash Buffer**:
   $$\text{Cash Buffer (\%)} = \frac{\text{Equity} - \sum_{i=1}^{N} \text{Market Value}_i}{\text{Equity}} \times 100 \ge 10.0\%$$

### Consequences
- **Positive**: The engine can open 8–12 concurrent positions when signals are strong, volatility is moderate, and individual risks are small.
- **Negative**: Requires real-time synchronization of open position quantities and stop levels across broker calls.

---

## ADR-002: Dynamic Multi-Stage Exit Engine (Breakeven + Chandelier Trailing)

### Status
**Accepted & Implemented**

### Context
Fixed take-profit targets ($+2.0 \times ATR$) cause severe **negative skew** in trend-following crypto portfolios. Bull runs frequently move $5\times$ to $10\times ATR$, while fixed targets cut winners prematurely and retain full downside risk on losers.

### Decision
Replace fixed bracket take-profit with a 3-tier dynamic exit lifecycle:
1. **Tier 1: Breakeven Advance ($+1.2 \times ATR$)**:
   When price $\ge \text{Entry} + 1.2 \times ATR$, move stop loss to $\text{Entry} + 0.1 \times ATR$. The trade is now mathematically risk-free and locked with small profit.
2. **Tier 2: Trailing Chandelier Stop ($\ge +2.0 \times ATR$)**:
   Activate a trailing stop pegged to the highest high:
   $$\text{SL}_{\text{trail}} = \max\left(\text{SL}_{\text{current}}, \text{Highest High} - 1.5 \times ATR\right)$$
   This captures runners as long as the trend expands.
3. **Tier 3: Climax Windfall Exit ($+4.5 \times ATR$)**:
   Lock in parabolic, blow-off tops before mean-reversion crashes.

---

## ADR-003: Sub-Sector Correlation Disaggregation

### Status
**Accepted & Implemented**

### Context
Treating `BTC`, `ETH`, `SOL`, `LINK`, and `DOGE` as identical correlated assets suppressed diversification. While all crypto correlates during macro drawdowns, idiosyncratic rallies occur across sub-sectors (e.g. DeFi summer, Meme cycles, L1 rotations).

### Decision
Disaggregate into 5 crypto sub-sectors:
- `Crypto_Core`: `BTC/USD`, `ETH/USD`
- `Crypto_L1`: `SOL/USD`, `AVAX/USD`
- `Crypto_DeFi`: `LINK/USD`, `UNI/USD`, `AAVE/USD`
- `Crypto_Meme`: `DOGE/USD`, `SHIB/USD`
- `Crypto_Payments`: `LTC/USD`, `BCH/USD`
Each sector allows independent allocation up to the portfolio heat boundary.
