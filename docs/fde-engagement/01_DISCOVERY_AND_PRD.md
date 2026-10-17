# 01. Discovery & Product Requirements Document (PRD)

## Project Overview
**Client/Workspace**: Autonomous Quantitative Cryptocurrency Trading System (`barshal-horse`)  
**Deployment Target**: Alpaca Paper / Live Trading API via GitHub Actions (Singleton 6-hour cron, 4x daily)  
**Lead Engineer**: Harshal Borse (Forward Deployed Engineer)

---

## 1. Executive Summary & Problem Statement
The automated trading system previously experienced low trade velocity and premature profit-taking due to:
1. **Artificial Position Capping**: Fixed thresholds (`max_positions = 5`, `max_correlated = 2`) constrained crypto trading since all cryptocurrencies were lumped into a single correlation group. Once two positions were opened, all other candidate signals were rejected.
2. **Premature Profit Capping**: Fixed static Take-Profit ($+2.0 \times ATR$) artificially truncated runners during explosive crypto bull impulses.
3. **Restrictive Watchlist**: A narrow 6-symbol watchlist limited market breadth, reducing breakout occurrences during low-volatility regimes.
4. **Constrained Sizing**: Fixed percentage allocations failed to dynamically scale with volatility parity and portfolio risk budget.

### The Objective
Transform the system into an **Institutional Senior Trading Engine** that:
- Removes arbitrary position caps while strictly guarding aggregate portfolio risk via **Portfolio Heat** ($\le 10\%$ equity at risk).
- Enforces **Cash Headroom Protection** ($\ge 10\%$ cash buffer).
- Implements **Volatility Parity & Dynamic ATR-Based Trailing Exits** (breakeven lock at $+1.2 \times ATR$, trailing Chandelier ratchet at $+2.0 \times ATR$, climax take-profit at $+4.5 \times ATR$).
- Expands universe coverage across 11 high-liquidity crypto assets on Alpaca.

---

## 2. Functional Requirements
| ID | Requirement | Acceptance Criteria |
|---|---|---|
| **FR-01** | Multi-Pair Scanning | Continuously scan 11 crypto pairs (`BTC`, `ETH`, `SOL`, `AVAX`, `LINK`, `DOGE`, `LTC`, `BCH`, `UNI`, `AAVE`, `SHIB`). |
| **FR-02** | Composite Alpha Scoring | Combine trend strength, breakout-retest structure, volume momentum, and FinBERT ML sentiment into a single composite rank ($[0, 1]$). |
| **FR-03** | Portfolio Heat Guard | Cap total aggregate dollar stop-loss exposure across all open positions at $\le 10.0\%$ of account equity. |
| **FR-04** | Cash Headroom Invariant | Reject new order allocation if total post-trade open position value exceeds $90\%$ of equity (retaining $\ge 10\%$ cash buffer). |
| **FR-05** | Dynamic Breakeven Ratchet | When an open position achieves $+1.2 \times ATR$ in unrealized profit, advance Stop-Loss to Entry $+ 0.1 \times ATR$ (guaranteeing a risk-free trade). |
| **FR-06** | Trailing Chandelier Stop | When an open position achieves $+2.0 \times ATR$, activate trailing stop ratcheted at $\text{Highest High} - 1.5 \times ATR$. |
| **FR-07** | Spot Crypto Non-Shorting | Enforce hard rejection of short sell entries on spot crypto pairs. |
| **FR-08** | Automated CI/CD Execution | Deploy on GitHub Actions with a 6-hour schedule (4x daily) utilizing repository secrets for Alpaca authentication and persisting SQLite database states. |

---

## 3. Non-Functional Requirements & Performance SLAs
- **Execution Latency**: Bar close to order submission $< 250\text{ ms}$.
- **Fault Tolerance**: Fail-closed architecture on network drop or broker timeout; graceful degradation if FinBERT sentiment is unreachable.
- **Zero-Credential Resilience**: Deterministic fallback and offline test suite capability without live credentials.
