"""Run the offline PPO trainer once per week in its own process.

Start with ``python ml_scheduler.py``. Qualified models may be promoted to the
paper model path, but this process never enables the PPO gate or places orders.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "data" / "ml-training.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S", handlers=[logging.StreamHandler(),
    logging.FileHandler(LOG_PATH, encoding="utf-8")])
logging.Formatter.converter = __import__("time").gmtime
logger = logging.getLogger("ml_scheduler")


def next_run_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    day = int(os.getenv("MODEL_TRAIN_DAY_UTC", "6"))
    hour = int(os.getenv("MODEL_TRAIN_HOUR_UTC", "2"))
    if not 0 <= day <= 6 or not 0 <= hour <= 23:
        raise ValueError("MODEL_TRAIN_DAY_UTC must be 0-6 and MODEL_TRAIN_HOUR_UTC 0-23")
    days_ahead = (day - now.weekday()) % 7
    scheduled = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
    if scheduled <= now:
        scheduled += timedelta(days=7)
    return scheduled


async def run() -> None:
    database = os.getenv("TRADE_DB_PATH", "data/trading.sqlite3")
    timesteps = os.getenv("MODEL_TRAIN_TIMESTEPS", "50000")
    while True:
        scheduled = next_run_utc()
        wait_seconds = max(1.0, (scheduled - datetime.now(timezone.utc)).total_seconds())
        logger.info("Next PPO paper-model training run: %s", scheduled.isoformat())
        await asyncio.sleep(wait_seconds)
        command = [sys.executable, str(ROOT / "train_model.py"), "--db", database,
                   "--timesteps", timesteps, "--promote"]
        logger.info("Starting weekly offline PPO evaluation")
        try:
            result = await asyncio.to_thread(subprocess.run, command, cwd=ROOT, check=False,
                capture_output=True, text=True, timeout=int(os.getenv("MODEL_TRAIN_TIMEOUT_SECONDS", "21600")))
            if result.stdout.strip():
                logger.info("Trainer output:\n%s", result.stdout[-12000:])
            if result.stderr.strip():
                logger.info("Trainer diagnostics:\n%s", result.stderr[-12000:])
            if result.returncode == 0:
                logger.info("Weekly PPO training completed")
            else:
                logger.warning("Weekly PPO training did not promote a model (exit=%d)", result.returncode)
        except subprocess.TimeoutExpired:
            logger.exception("Weekly PPO training exceeded its timeout")
        except Exception:
            logger.exception("Weekly PPO training could not start")


def main() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("ML scheduler stopped")


if __name__ == "__main__":
    main()
