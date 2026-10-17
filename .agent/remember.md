# Session Memory: Technical Trading Quantitative Crypto System

## Architectural Upgrades Completed
1. **Uncapped Position Constraints & Senior Portfolio Heat Guard**:
   - Replaced artificial hard limits (`max_positions = 5`, `max_correlated = 2`) with dynamic institutional risk management in `risk_manager.py`:
     - **Aggregate Portfolio Heat Guard**: $\le 10.0\%$ of account equity at risk across all open positions.
     - **Cash Headroom Protection**: $\ge 10.0\%$ minimum cash buffer required before opening new positions.
     - **Sector Disaggregation**: Mapped crypto pairs into 5 independent sub-sectors (`Crypto_Core`, `Crypto_L1`, `Crypto_DeFi`, `Crypto_Meme`, `Crypto_Payments`) preventing false correlation lockouts.
2. **Dynamic Multi-Stage Profit Maximization**:
   - In `signal_engine.py`: Upgraded to `crypto-break-retest-v3-profit-max` with dual SMA20/EMA20 trend confirmation, 40% wick test, RSI bounds [38, 78], and asymmetric target setup ($+2.5 \times ATR$).
   - In `engine.py`: Replaced static take-profit with dynamic software-managed lifecycle:
     - **Breakeven Advance**: Once unrealized profit reaches $+1.2 \times ATR$, Stop Loss moves to $\text{Entry} + 0.1 \times ATR$ (risk-free trade).
     - **Dynamic Trailing Chandelier Stop**: Once unrealized profit reaches $+2.0 \times ATR$, Stop Loss ratchets to $\text{Highest High} - 1.5 \times ATR$.
     - **Climax Take-Profit**: At $+4.5 \times ATR$, full position is closed into explosive momentum.
3. **Watchlist Expansion**:
   - Expanded from 6 to 11 high-liquidity crypto assets: `BTC/USD`, `ETH/USD`, `SOL/USD`, `AVAX/USD`, `LINK/USD`, `DOGE/USD`, `LTC/USD`, `BCH/USD`, `UNI/USD`, `AAVE/USD`, `SHIB/USD`.
4. **CI/CD Workflow Update**:
   - Updated `.github/workflows/crypto-trading.yml` to run with `MAX_POSITIONS: "15"`, `MAX_CORRELATED_POSITIONS: "15"`, `MAX_POSITION_PCT: "15.0"`, `MIN_SIGNAL_SCORE: "0.55"`, `KELLY_FRACTION: "0.30"`, `MAX_PORTFOLIO_HEAT_PCT: "10.0"`, and `MIN_CASH_BUFFER_PCT: "10.0"`.
5. **Testing & FDE Masterclass Documentation**:
   - Verified 12 unit tests passing in `tests/test_crypto_system.py`.
   - Created full `/docs/fde-engagement/` documentation suite:
     - `01_DISCOVERY_AND_PRD.md`
     - `02_ADR_ARCHITECTURE.md`
     - `03_RUNBOOK_AND_WARROOM.md`
     - `04_FDE_MASTERCLASS_GUIDE.md`

## Current Working State
- **Account Status**: Clean cash (\$4,782.81 Paper Equity), 0 open positions.
- **Workflow Status**: GitHub Actions 6-hour cron active.
- **Git State**: Cleanly tested on disk. Ready for commit & push to `main`.
