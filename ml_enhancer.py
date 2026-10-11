"""Optional FinBERT sentiment and versioned PPO signal approval."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

OBSERVATION_SCHEMA = (
    "trade-state-v2:ma_distance_clip5_div5,atr_ratio_clip0.1_div0.1,"
    "rsi14_div100,sentiment_minus1_to1,sentiment_available"
)
OBSERVATION_FEATURES = ("ma_distance_ratio", "atr_ratio", "rsi14", "sentiment_score", "sentiment_available")


@lru_cache(maxsize=1)
def _sentiment_pipeline():
    model_name = os.getenv("FINBERT_MODEL", "ProsusAI/finbert")
    local_only = os.getenv("FINBERT_LOCAL_ONLY", "false").lower() == "true"
    if local_only:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        from transformers import pipeline
    except ImportError as exc:
        raise RuntimeError("Install requirements-ml.txt to enable FinBERT sentiment.") from exc
    return pipeline("text-classification", model=model_name, tokenizer=model_name,
                    device=-1, top_k=None)


@lru_cache(maxsize=512)
def _score_headlines_cached(headlines: tuple[str, ...]) -> float:
    predictions = _sentiment_pipeline()(list(headlines), truncation=True, batch_size=16)
    scores = []
    for prediction in predictions:
        if isinstance(prediction, list):
            probabilities = {str(item["label"]).lower(): float(item["score"]) for item in prediction}
        else:
            probabilities = {str(prediction["label"]).lower(): float(prediction["score"])}
        scores.append(probabilities.get("positive", 0.0) - probabilities.get("negative", 0.0))
    return float(np.clip(np.mean(scores), -1.0, 1.0)) if scores else 0.0


def get_market_sentiment(news_headlines_list: list[str]) -> float:
    """Mean FinBERT positive-minus-negative probability in [-1, 1]."""
    headlines = tuple(str(item).strip() for item in news_headlines_list if str(item).strip())
    return _score_headlines_cached(headlines) if headlines else 0.0


def warmup_market_sentiment() -> None:
    """Load the cached FinBERT pipeline once during daemon startup."""
    _sentiment_pipeline()


def build_observation(market_features: dict[str, Any]) -> np.ndarray:
    """Apply the exact v2 feature order, normalization, and bounds used in training."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        ma_distance = float(market_features["ma_distance_ratio"])
        atr_ratio = float(market_features["atr_ratio"])
        rsi = float(market_features["rsi14"])
        sentiment_available = bool(market_features["sentiment_available"])
        sentiment = float(market_features.get("sentiment_score", 0.0)) if sentiment_available else 0.0
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Missing or invalid PPO feature: {exc}") from exc
    
    raw = np.asarray([ma_distance, atr_ratio, rsi, sentiment], dtype=np.float64)
    if not np.isfinite(raw).all():
        raise ValueError("PPO observation contains non-finite features")
    
    # Clip to documented bounds with warnings (matching training normalization)
    ma_distance = np.clip(ma_distance, -5.0, 5.0)
    if abs(ma_distance) >= 5.0:
        logger.warning("ma_distance_ratio clipped to [-5, 5]: raw=%.4f", ma_distance)
    
    atr_ratio = np.clip(atr_ratio, 0.0, 0.1)
    if atr_ratio >= 0.1:
        logger.warning("atr_ratio clipped to [0, 0.1]: raw=%.4f", atr_ratio)
    
    rsi = np.clip(rsi, 0.0, 100.0)
    if rsi >= 100.0 or rsi <= 0.0:
        logger.warning("rsi14 clipped to [0, 100]: raw=%.4f", rsi)
    
    sentiment = np.clip(sentiment, -1.0, 1.0) if sentiment_available else 0.0
    if sentiment_available and (sentiment >= 1.0 or sentiment <= -1.0):
        logger.warning("sentiment_score clipped to [-1, 1]: raw=%.4f", sentiment)
    
    normalized = np.asarray([
        ma_distance / 5.0,
        atr_ratio / 0.1,
        rsi / 100.0,
        sentiment,
        1.0 if sentiment_available else 0.0,
    ], dtype=np.float32)
    
    if not np.isfinite(normalized).all():
        raise ValueError("Normalized PPO observation contains non-finite values")
    return normalized.reshape(1, -1)


class RLSignalFilter:
    def __init__(self, model_path: str | None = None, enabled: bool | None = None,
                 bearish_buy_threshold: float | None = None):
        self.model_path = model_path or os.getenv("PPO_MODEL_PATH", "models/ppo_trade_filter")
        self.enabled = (os.getenv("ML_GATE_ENABLED", "false").lower() == "true") if enabled is None else enabled
        self.threshold = float(os.getenv("BEARISH_BUY_THRESHOLD", "-0.5")) if bearish_buy_threshold is None else float(bearish_buy_threshold)
        if not -1.0 <= self.threshold <= 1.0:
            raise ValueError("bearish_buy_threshold must be in [-1, 1]")
        self._model = None

    def _load_model(self):
        if self._model is None:
            metadata_path = Path(self.model_path).with_suffix(".metadata.json")
            if not metadata_path.is_file():
                raise RuntimeError(f"PPO metadata file is missing: {metadata_path}")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("observation_schema") != OBSERVATION_SCHEMA:
                raise RuntimeError("PPO model observation schema does not match the live feature contract.")
            if metadata.get("approved_for_paper") is not True:
                raise RuntimeError("PPO model has not passed the configured chronological holdout checks.")
            try:
                from stable_baselines3 import PPO
            except ImportError as exc:
                raise RuntimeError("Install requirements-ml.txt to enable PPO inference.") from exc
            self._model = PPO.load(self.model_path, device="cpu")
        return self._model

    def should_execute_trade(self, technical_signal: dict[str, Any],
                             market_features: dict[str, Any]) -> dict[str, Any]:
        action = str(technical_signal.get("action", "FLAT")).upper()
        if action not in {"BUY", "SELL"}:
            return {"approved": False, "confidence": None, "reason": "no technical signal"}
        try:
            sentiment_available = bool(market_features.get("sentiment_available", False))
            sentiment = float(market_features.get("sentiment_score", 0.0)) if sentiment_available else 0.0
        except (TypeError, ValueError):
            return {"approved": False, "confidence": None, "reason": "invalid sentiment feature"}
        if sentiment_available and not np.isfinite(sentiment):
            return {"approved": False, "confidence": None, "reason": "non-finite sentiment feature"}
        if action == "BUY" and sentiment_available and sentiment < self.threshold:
            return {"approved": False, "confidence": None, "reason": "bearish sentiment veto"}
        if not self.enabled:
            return {"approved": True, "confidence": None, "reason": "PPO gate disabled; technical signal passed"}
        try:
            observation = build_observation(market_features)
            policy_action, _ = self._load_model().predict(observation, deterministic=True)
            approved = int(np.asarray(policy_action).item()) == 1
            # A discrete PPO action is a decision, not a calibrated probability.
            return {"approved": approved, "confidence": None,
                    "reason": "PPO action approved" if approved else "PPO rejected",
                    "observation_schema": OBSERVATION_SCHEMA}
        except Exception as exc:
            return {"approved": False, "confidence": None, "reason": f"ML gate failed closed: {exc}"}

    def get_approval_probability(self, technical_signal: dict[str, Any],
                                  market_features: dict[str, Any]) -> float:
        """Return P(approve) from the PPO policy (0-1). Falls back to 0.5 when gate disabled."""
        if not self.enabled:
            return 0.5
        action = str(technical_signal.get("action", "FLAT")).upper()
        if action not in {"BUY", "SELL"}:
            return 0.0
        try:
            observation = build_observation(market_features)
            obs_tensor = observation.reshape(1, -1)
            import torch
            obs_t = torch.as_tensor(obs_tensor, dtype=torch.float32)
            dist = self._load_model().policy.get_distribution(obs_t).distribution
            probs = dist.probs.detach().cpu().numpy()[0]
            return float(probs[1])  # P(approve)
        except Exception:
            return 0.0


class EnsembleRLFilter:
    """Multi-seed PPO ensemble for robust approval decisions."""

    def __init__(self, model_path_template: str | None = None,
                 seeds: list[int] | None = None,
                 enabled: bool | None = None):
        seeds = seeds or [int(s) for s in os.getenv("PPO_ENSEMBLE_SEEDS", "7,42,123").split(",")]
        template = model_path_template or os.getenv("PPO_MODEL_PATH", "models/ppo_trade_filter")
        self.filters: list[RLSignalFilter] = []
        self.enabled = (os.getenv("ML_GATE_ENABLED", "false").lower() == "true") if enabled is None else enabled
        for s in seeds:
            path = template.replace(".zip", "") + f"_seed_{s}"
            try:
                f = RLSignalFilter(model_path=path, enabled=self.enabled)
                self.filters.append(f)
            except Exception:
                pass  # skip unavailable seed models
        if not self.filters:
            # Fallback: single model at the base path
            self.filters = [RLSignalFilter(model_path=template, enabled=self.enabled)]

    def get_approval_probability(self, technical_signal: dict[str, Any],
                                  market_features: dict[str, Any]) -> float:
        probs = [f.get_approval_probability(technical_signal, market_features) for f in self.filters]
        return float(np.mean(probs)) if probs else 0.0

    def should_execute_trade(self, technical_signal: dict[str, Any],
                             market_features: dict[str, Any]) -> dict[str, Any]:
        probs = [f.get_approval_probability(technical_signal, market_features) for f in self.filters]
        if not probs:
            return {"approved": False, "confidence": None, "reason": "no models loaded"}
        mean_prob = float(np.mean(probs))
        std_prob = float(np.std(probs)) if len(probs) > 1 else 0.0
        threshold = float(os.getenv("PPO_APPROVAL_THRESHOLD", "0.6"))
        max_std = float(os.getenv("PPO_CONSENSUS_STD", "0.2"))
        approved = mean_prob > threshold and std_prob < max_std
        return {
            "approved": approved,
            "confidence": mean_prob,
            "reason": f"ensemble_prob={mean_prob:.2f},std={std_prob:.2f}",
            "prob_std": std_prob,
        }
