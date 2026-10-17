# 03. Runbook & War Room Operational Guide

## 1. System Architecture & Topology
```
 [GitHub Actions Singleton Cron (every 6h)]
                  │
                  ▼
         [Python 3.11 Runtime]
                  │
       ┌──────────┴──────────┐
       ▼                     ▼
 [Candle Streamer]     [FinBERT / News]
  (Alpaca Crypto)       (Market Sentiment)
       │                     │
       └──────────┬──────────┘
                  ▼
      [Signal Engine v3 Profit-Max]
      - Breakout & Retest Confirmation
      - Volatility & RSI Filters
      - Composite Scoring [0, 1]
                  │
                  ▼
      [Institutional Risk Manager]
      - Aggregate Portfolio Heat (<= 10%)
      - Cash Headroom Buffer (>= 10%)
      - Volatility Parity / Fractional Qty
                  │
                  ▼
      [Order Routing & Execution Engine]
      - Market Entry Orders via Alpaca API
      - Software SL / Dynamic Trailing / Breakeven
                  │
                  ▼
       [Local SQLite3 / Artifact DB]
```

---

## 2. Standard Operating Procedures (SOP)

### Emergency Kill Switch (War Room Protocol)
To halt all trading and cancel pending orders:
1. Navigate to GitHub Actions > **24/7 Autonomous Crypto Trading Engine**.
2. Click **Cancel run** on the currently running job.
3. Or update the SQLite database via CLI:
   ```bash
   python -c "import db_manager; db_manager.update_control_settings(status='STOPPED')"
   ```

### Manual Position Liquidation (Alpaca Dashboard)
If an asset exhibits extreme decoupling or an external exchange emergency occurs:
1. Log in to [Alpaca Paper Dashboard](https://app.alpaca.markets/paper/dashboard/overview).
2. Go to **Positions** > Select asset > Click **Close Position**.
3. The engine's next cycle will automatically detect the closure via `_check_software_exits()` and remove the tracking record from memory and DB without throwing errors.

---

## 3. Incident Severity Matrix & Diagnostics

| Severity | Symptom | Immediate Remediation |
|---|---|---|
| **SEV-1** | Broker Authentication Failure (401 / 403) | Re-verify `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in GitHub Repository Secrets. |
| **SEV-2** | Rate Limit Hit (429 HTTP) | Engine automatically applies exponential backoff with jitter (10s base). |
| **SEV-3** | Inconsistent Position Tracking | Run reconciliation cycle via `engine._reconcile_orders(broker)`. |
| **SEV-4** | FinBERT Model Warmup Failure | System degrades gracefully to technical-only signals without halting scanner. |

---

## 4. Health Check Verification
To run health and risk regression tests locally:
```bash
.venv\Scripts\python.exe -m unittest discover tests
```
Expected output:
```
Ran 12 tests in 0.237s
OK
```
