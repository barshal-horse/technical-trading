---
name: ml-refiner
description: Add FinBERT sentiment and a PPO-based approval filter to rule-based trading signals with explicit model and feature contracts.
---

# ML signal refiner

Use this skill when adding news sentiment or a trained reinforcement-learning approval gate to the trading pipeline.

## Sentiment

- Use the `ProsusAI/finbert` model through Hugging Face Transformers for news headlines. Load and cache the pipeline once at startup, not on each signal.
- Convert positive and negative class probabilities to a documented polarity score, such as `positive - negative`, then aggregate headlines with a defined empty-input behavior. Do not describe sentiment as a guaranteed market prediction.
- Make the bearish BUY rejection threshold configurable. The project notes specify both `-0.6` and `-0.5`; use a named configuration value (default `-0.5` when no project value exists) and never use inconsistent thresholds in separate code paths.
- Do not claim order-book-flow analysis from a text sentiment model. Only score text inputs appropriate for FinBERT.

## PPO gate

- Use Stable-Baselines3 PPO only with a trained, versioned model and the exact observation schema it was trained on. The project has two proposed state definitions (five values including RSI and current drawdown, and four values omitting them); define and persist one versioned schema with feature order, normalization, units, and bounds. Reject incompatible model/schema combinations.
- Build the observation from available market features (MA spread/distance, ATR ratio, RSI if included, sentiment, and drawdown/PnL if included). Validate shape and finite values before inference. Cache model loading at startup and keep inference isolated from blocking I/O.
- The action contract is `0 = skip`, `1 = execute`. Apply the deterministic bearish-sentiment BUY veto before PPO approval. Return an explicit decision and confidence in `[0, 1]`, with the confidence definition documented; PPO's discrete action alone is not a calibrated probability.
- If a required model or feature is unavailable, follow a documented fail-closed policy for the ML gate rather than silently approving the trade. Provide an explicit configuration for disabling the gate in paper experimentation; do not bypass a live guardrail implicitly.

## Training and evaluation

Use historical or paper-trading records for offline training. Persist model version, observation schema, training window, and evaluation results. Avoid training on future information, leakage from forward returns, or rewards computed using unavailable outcomes. Log signal features, fills, slippage, and forward outcomes with timestamps and a stable trade/signal identifier. A PPO model or positive backtest result is not evidence of future profitability; keep model training separate from live order placement.
