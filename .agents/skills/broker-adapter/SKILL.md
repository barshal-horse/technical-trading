---
name: broker-adapter
description: Build or update a Python broker abstraction for Alpaca, Angel One, and Groww, including execution, positions, market data, and environment routing.
---

# Broker adapter

Use this skill when implementing the project's broker connectivity or order routing.

## Required interface

Keep broker-specific details behind a small `AbstractBroker` contract. Support connection/authentication, latest price or normalized market data, order placement with symbol, side, quantity, order type, optional limit price, stop loss and take profit, position retrieval, and order cancellation. Keep argument names and return types consistent across implementations; represent order/position results as documented normalized objects or dictionaries.

Provide Alpaca, Angel One, and Groww implementations using `alpaca-py`, `smartapi-python`, and `growwapi` respectively. Alpaca should default to paper trading and the paper API URL; use distinct credentials and the live endpoint only for explicit `LIVE_ALPACA`. Angel One authentication should obtain a time-based one-time password with `pyotp` and establish a SmartAPI session. Groww should use its supported access-token flow and instrument exchange tokens. Route only from an explicit environment setting such as `PAPER_ALPACA`, `LIVE_ALPACA`, `LIVE_ANGEL_ONE`, or `LIVE_GROWW`; reject unknown values instead of guessing.

## Runtime behavior

- Keep credentials in environment variables or a secret store. Never commit real keys, PINs, passwords, TOTP seeds, access tokens, or feed tokens. Do not use interactive prompts as a service credential path.
- Keep paper trading the safe default. Live routing must require an explicit live environment selection and should be easy to identify in logs and configuration. Do not place a live order as part of setup, validation, or examples.
- Keep live Alpaca orders separately gated from paper orders, with separate live credentials, an explicit live account selection, and a distinct live-order enable flag. Live market-data observation must not itself enable order submission.
- Use async-native clients where available. When a vendor SDK is synchronous, isolate blocking calls with `asyncio.to_thread` or a bounded executor; do not claim the SDK itself is asynchronous.
- Add bounded timeouts, clear typed errors, timestamped structured logs with millisecond precision, and reconnect/backoff behavior for streaming connections. Avoid logging secrets or full auth payloads.
- Normalize provider ticks into a documented common event/candle schema, including symbol, exchange, event time, OHLC, volume when available, and source. Handle provider units and timestamp conventions explicitly.
- Define how order types and attached stop/take-profit instructions map to each broker. If a broker cannot provide an atomic bracket order, report that limitation and avoid implying the protective orders are live when they are not.
- For brokers with linked/OCO exits, reconcile entry fills and partial fills before attaching protective orders. Keep live entry routing fail-closed until the protective-exit lifecycle is implemented and verified for the selected product and segment.
- Make connection and order methods safe to retry only when the operation is idempotent or a client/order identifier prevents duplicate submission. Surface ambiguous submission outcomes for reconciliation.

## Implementation approach

Inspect the repository's existing interfaces and dependencies before editing. Preserve compatibility where practical; document any provider-specific behavior that cannot be unified. Keep optional broker SDK imports isolated so users of one broker do not need the other SDK installed. Do not add secrets, execute orders, or change an environment to live while implementing this skill.
