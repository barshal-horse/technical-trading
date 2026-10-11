"""Train and chronologically evaluate an offline PPO paper-signal filter.

This is research tooling over realized bracket results where reconciled, and
15-minute forward returns for other signals; it is not a fill simulator.
Candidate models are kept separate from the deployed paper-gate path. Promotion
requires explicit --promote and passing the holdout criteria in this file.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ml_enhancer import OBSERVATION_FEATURES, OBSERVATION_SCHEMA

RAW_FEATURES = ("ma_distance_ratio", "atr_ratio", "rsi14", "sentiment_score", "sentiment_available")


def load_training_rows(db_path: str):
    """Load feature-bearing signals with realized or matured counterfactual outcomes."""
    import pandas as pd

    with sqlite3.connect(db_path) as conn:
        frame = pd.read_sql_query("""SELECT id,timestamp,action,status,features_json,forward_return_15m,
            realized_pnl,slippage,entry_fill_price,filled_qty FROM trade_logs
            WHERE action IN ('BUY','SELL') AND features_json != '{}'
            AND (forward_return_15m IS NOT NULL OR realized_pnl IS NOT NULL)
            ORDER BY timestamp,id""", conn)
    if frame.empty:
        raise ValueError("No matured paper-signal outcome labels are available yet.")
    parsed = frame["features_json"].fillna("{}").map(lambda value: json.loads(value) if value else {})
    for key in RAW_FEATURES:
        if key == "sentiment_available":
            frame[key] = parsed.map(lambda row: float(bool(row.get(key, False))))
        elif key == "sentiment_score":
            frame[key] = parsed.map(lambda row: row.get(key) if row.get("sentiment_available", False) else 0.0)
        else:
            frame[key] = parsed.map(lambda row: row.get(key))
    frame["forward_return_15m"] = pd.to_numeric(frame["forward_return_15m"], errors="coerce")
    frame["realized_pnl"] = pd.to_numeric(frame["realized_pnl"], errors="coerce")
    frame["entry_fill_price"] = pd.to_numeric(frame["entry_fill_price"], errors="coerce")
    frame["filled_qty"] = pd.to_numeric(frame["filled_qty"], errors="coerce")
    notional = frame["entry_fill_price"] * frame["filled_qty"]
    has_realized = frame["realized_pnl"].notna() & notional.gt(0)
    frame["outcome"] = np.where(has_realized, frame["realized_pnl"] / notional,
                                 frame["forward_return_15m"])
    frame["label_source"] = np.where(has_realized, "realized_bracket_fill", "forward_return_15m")
    frame["measured_slippage"] = pd.to_numeric(frame["slippage"], errors="coerce")
    frame = frame[~frame["status"].fillna("").str.startswith(("BLOCKED:", "ERROR:"))]
    frame = frame.dropna(subset=["timestamp", *RAW_FEATURES, "outcome"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values(["timestamp", "id"]).reset_index(drop=True)
    # Keep inference and training normalization identical; all source fields are logged raw.
    frame["ma_distance_ratio"] = np.clip(frame["ma_distance_ratio"].astype(float), -5.0, 5.0) / 5.0
    frame["atr_ratio"] = np.clip(frame["atr_ratio"].astype(float), 0.0, 0.1) / 0.1
    frame["rsi14"] = np.clip(frame["rsi14"].astype(float), 0.0, 100.0) / 100.0
    frame["sentiment_score"] = np.clip(frame["sentiment_score"].astype(float), -1.0, 1.0)
    frame["sentiment_available"] = frame["sentiment_available"].astype(float).clip(0.0, 1.0)
    frame["outcome"] = frame["outcome"].astype(float)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=[*OBSERVATION_FEATURES, "outcome"])
    return frame.reset_index(drop=True)


def make_env(frame, slippage_bps: float):
    import pandas as pd

    try:
        import gymnasium as gym
        from gymnasium import spaces
    except ImportError as exc:
        raise RuntimeError("Install requirements-ml.txt to train PPO.") from exc

    class HistoricalApprovalEnv(gym.Env):
        metadata = {"render_modes": []}

        def __init__(self):
            super().__init__()
            self.x = frame[list(OBSERVATION_FEATURES)].to_numpy(dtype=np.float32)
            self.returns = frame["outcome"].to_numpy(dtype=np.float32)
            fallback_cost = slippage_bps / 10_000.0
            measured = frame.get("measured_slippage")
            self.costs = (pd.to_numeric(measured, errors="coerce").fillna(fallback_cost).to_numpy(dtype=np.float32)
                          if measured is not None else np.full(len(frame), fallback_cost, dtype=np.float32))
            self.index = 0
            self.equity = 0.0
            self.peak = 0.0
            self.action_space = spaces.Discrete(2)  # 0 skip, 1 approve
            self.observation_space = spaces.Box(low=0.0, high=1.0, shape=(5,), dtype=np.float32)
            self.observation_space.low[0] = -1.0
            self.observation_space.high[0] = 1.0
            self.observation_space.low[3] = -1.0
            self.observation_space.high[3] = 1.0

        def reset(self, seed=None, options=None):
            super().reset(seed=seed)
            self.index, self.equity, self.peak = 0, 0.0, 0.0
            return self.x[0].copy(), {}

        def step(self, action):
            reward = 0.0
            if int(action) == 1:
                net_return = float(self.returns[self.index]) - float(self.costs[self.index])
                self.equity += net_return
                self.peak = max(self.peak, self.equity)
                drawdown = max(0.0, self.peak - self.equity)
                # Scale fractional returns to percentage points for PPO optimization.
                reward = float(np.clip(net_return * 100.0 - 5.0 * drawdown * 100.0, -10.0, 10.0))
            self.index += 1
            terminated = self.index >= len(self.x)
            observation = self.x[self.index].copy() if not terminated else np.zeros(5, dtype=np.float32)
            return observation, reward, terminated, False, {}

    return HistoricalApprovalEnv()


def _max_drawdown(returns: np.ndarray) -> float:
    equity = np.cumsum(returns)
    peaks = np.maximum.accumulate(np.maximum(equity, 0.0))
    return float(np.max(peaks - equity)) if len(equity) else 0.0


def evaluate_model(model, frame, slippage_bps: float) -> dict[str, Any]:
    import pandas as pd

    observations = frame[list(OBSERVATION_FEATURES)].to_numpy(dtype=np.float32)
    actions, _ = model.predict(observations, deterministic=True)
    actions = np.asarray(actions).reshape(-1).astype(int)
    outcomes = frame["outcome"].to_numpy(dtype=float)
    fallback_cost = slippage_bps / 10_000.0
    measured = frame.get("measured_slippage")
    costs = (np.where(pd.to_numeric(measured, errors="coerce").notna(),
                      pd.to_numeric(measured, errors="coerce"), fallback_cost)
             if measured is not None else np.full(len(frame), fallback_cost))
    net = np.where(actions == 1, outcomes - costs, 0.0)
    baseline = outcomes - costs
    selected = net[actions == 1]
    std = float(np.std(selected, ddof=1)) if len(selected) > 1 else 0.0
    return {
        "holdout_rows": int(len(frame)),
        "approved_signals": int(actions.sum()),
        "approval_rate": float(actions.mean()) if len(actions) else 0.0,
        "mean_net_return_per_approved_signal": float(selected.mean()) if len(selected) else None,
        "mean_net_return_per_input_signal": float(net.mean()) if len(net) else 0.0,
        "per_signal_sharpe_unannualized": float(selected.mean() / std) if std > 0 else None,
        "max_drawdown_cumulative_return": _max_drawdown(net),
        "all_signals_mean_net_return": float(baseline.mean()) if len(baseline) else 0.0,
        "all_signals_max_drawdown": _max_drawdown(baseline),
    }


def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trading.sqlite3")
    parser.add_argument("--output", default="models/ppo_trade_filter_candidate")
    parser.add_argument("--production-output", default="models/ppo_trade_filter")
    parser.add_argument("--timesteps", type=int, default=50_000)
    parser.add_argument("--slippage-bps", type=float, default=1.5,
                        help="Fallback research cost for samples without reconciled fills.")
    parser.add_argument("--min-rows", type=int, default=100)
    parser.add_argument("--min-holdout-approvals", type=int, default=5)
    parser.add_argument("--promote", action="store_true", help="Copy a qualified candidate to the paper gate path.")
    args = parser.parse_args()
    if args.timesteps < 1 or args.slippage_bps < 0 or args.min_holdout_approvals < 1:
        parser.error("timesteps must be positive, slippage non-negative, and min holdout approvals at least one")
    rows = load_training_rows(args.db)
    if len(rows) < args.min_rows:
        raise SystemExit(f"Need at least {args.min_rows} matured labeled rows; found {len(rows)}. No model was trained.")
    split = int(len(rows) * 0.8)
    train, holdout = rows.iloc[:split].copy(), rows.iloc[split:].copy()
    if len(train) < 50 or len(holdout) < 20:
        raise SystemExit("Need at least 50 chronological training rows and 20 holdout rows. No model was trained.")
    try:
        from stable_baselines3 import PPO
    except ImportError as exc:
        raise SystemExit("Install requirements-ml.txt to train PPO.") from exc

    environment = make_env(train, args.slippage_bps)
    model = PPO("MlpPolicy", environment, verbose=1, n_steps=128, batch_size=64,
                seed=7, device="cpu")
    model.learn(total_timesteps=args.timesteps)
    evaluation = evaluate_model(model, holdout, args.slippage_bps)
    enough_approvals = evaluation["approved_signals"] >= args.min_holdout_approvals
    positive_expectancy = (evaluation["mean_net_return_per_approved_signal"] is not None
                           and evaluation["mean_net_return_per_approved_signal"] > 0)
    better_than_baseline = (evaluation["mean_net_return_per_input_signal"]
                            > evaluation["all_signals_mean_net_return"])
    lower_drawdown = (evaluation["max_drawdown_cumulative_return"]
                      <= evaluation["all_signals_max_drawdown"] + 1e-12)
    qualified = bool(enough_approvals and positive_expectancy and better_than_baseline and lower_drawdown)

    candidate = Path(args.output)
    production = Path(args.production_output)
    if candidate.resolve() == production.resolve():
        raise SystemExit("Candidate output and production output must be different paths.")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    model.save(str(candidate))
    last_stamp = rows["timestamp"].iloc[-1].isoformat()
    metadata = {
        "observation_schema": OBSERVATION_SCHEMA,
        "observation_features": list(OBSERVATION_FEATURES),
        "observation_bounds_and_normalization": {
            "ma_distance_ratio": "clip(-5,5)/5",
            "atr_ratio": "clip(0,0.1)/0.1",
            "rsi14": "clip(0,100)/100",
            "sentiment_score": "clip(-1,1), or 0 if unavailable",
            "sentiment_available": "1 when FinBERT scored headlines, otherwise 0",
        },
        "rows": int(len(rows)), "train_rows": int(len(train)), "holdout_rows": int(len(holdout)),
        "label_source_counts": {str(key): int(value) for key, value in rows["label_source"].value_counts().items()},
        "training_window_start_utc": rows["timestamp"].iloc[0].isoformat(),
        "training_window_end_utc": last_stamp,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "timesteps": args.timesteps, "slippage_bps_assumption": args.slippage_bps,
        "label": "realized bracket P&L divided by entry notional when available; otherwise direction-adjusted 15-minute forward return",
        "evaluation": evaluation,
        "qualification": {"approved_for_paper": qualified,
            "checks": {"minimum_holdout_approvals": enough_approvals,
                       "positive_holdout_expectancy": bool(positive_expectancy),
                       "better_than_all_signal_baseline": bool(better_than_baseline),
                       "drawdown_no_worse_than_baseline": bool(lower_drawdown)}},
        "approved_for_paper": qualified,
        "promotion_requires_explicit_flag": True,
        "limitations": ["Forward-return fallback labels are not bracket-order fill simulation.",
                        "Samples without reconciled fills use the fixed slippage assumption.",
                        "Holdout metrics are historical research, not evidence of future returns."],
    }
    candidate_metadata = candidate.with_suffix(".metadata.json")
    _write_metadata(candidate_metadata, metadata)
    print(json.dumps({"candidate": str(candidate), "qualified_for_paper": qualified,
                      "evaluation": evaluation}, indent=2))

    if args.promote:
        if not qualified:
            raise SystemExit("Candidate did not pass the holdout checks; it was not promoted.")
        source_zip = Path(str(candidate) + ".zip")
        production_zip = Path(str(production) + ".zip")
        production_zip.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_zip, production_zip)
        promoted = {**metadata, "promoted_at_utc": datetime.now(timezone.utc).isoformat(),
                    "model_file": production_zip.name}
        _write_metadata(production.with_suffix(".metadata.json"), promoted)
        print(f"Promoted qualified paper-gate model to {production}.")


if __name__ == "__main__":
    main()
