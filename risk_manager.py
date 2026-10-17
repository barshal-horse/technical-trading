"""Portfolio risk management and Kelly-based position sizing."""
from __future__ import annotations

import json
import math
import os
from typing import Any

from broker_adapter import is_crypto_symbol

SECTOR_MAP = {
    "AAPL": "Tech", "MSFT": "Tech", "NVDA": "Tech", "GOOGL": "Tech",
    "GOOG": "Tech", "META": "Tech", "AMZN": "Consumer", "TSLA": "Auto",
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "DIA": "ETF",
    "AVGO": "Tech", "NFLX": "Consumer", "ADBE": "Tech", "CRM": "Tech",
    "INTC": "Tech", "AMD": "Tech", "XLF": "Financial", "XLK": "Tech",
    "XLE": "Energy", "XLV": "Healthcare", "XLI": "Industrial",
    # Crypto Sector Map
    "BTC/USD": "Crypto_Core", "ETH/USD": "Crypto_Core",
    "SOL/USD": "Crypto_L1", "AVAX/USD": "Crypto_L1",
    "DOGE/USD": "Crypto_Meme", "SHIB/USD": "Crypto_Meme",
    "LINK/USD": "Crypto_DeFi", "UNI/USD": "Crypto_DeFi", "AAVE/USD": "Crypto_DeFi",
    "LTC/USD": "Crypto_Payments", "BCH/USD": "Crypto_Payments",
}


class PortfolioRiskManager:
    def __init__(self, max_positions: int = 15, max_correlated: int = 15,
                 daily_loss_pct: float = 3.0, kelly_fraction: float = 0.25,
                 max_position_pct: float = 15.0, max_portfolio_heat_pct: float = 10.0,
                 min_cash_buffer_pct: float = 10.0):
        # [FDE-WHY]: Uncap position count; portfolio capacity is dynamically governed by Aggregate Portfolio Heat (VaR budget)
        self.max_positions = max_positions
        self.max_correlated = max_correlated
        self.daily_loss_pct = daily_loss_pct
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.max_portfolio_heat_pct = max_portfolio_heat_pct
        self.min_cash_buffer_pct = min_cash_buffer_pct
        self.sector_map = SECTOR_MAP

    def calculate_portfolio_heat(self, positions: list[dict], equity: float) -> float:
        """Calculate total aggregate dollar risk across all open positions relative to equity."""
        if equity <= 0:
            return 0.0
        total_risk_dollars = 0.0
        for p in positions:
            qty = float(p.get("qty", 0.0))
            entry = float(p.get("avg_entry_price") or p.get("entry_price") or p.get("entry") or p.get("price") or p.get("current_price") or 0.0)
            sl = float(p.get("sl", 0.0)) if p.get("sl") is not None else entry * 0.985
            risk_per_unit = max(0.0, entry - sl)
            total_risk_dollars += qty * risk_per_unit
        return (total_risk_dollars / equity) * 100.0

    def can_open_position(self, signal: dict, positions: list[dict],
                          equity: float, risk_params: dict) -> tuple[bool, float, str]:
        """Check if a new position can be opened with strict Portfolio Heat & cash buffer limits."""
        symbol = signal["symbol"]

        # 0. Spot crypto long-only check
        if is_crypto_symbol(symbol) and signal.get("action") == "SELL":
            return False, 0.0, "short selling is not supported on spot crypto"

        # 1. Duplicate position check (Never double down on same token)
        for p in positions:
            p_sym = p.get("symbol", "")
            if p_sym == symbol or p_sym.replace("/", "") == symbol.replace("/", ""):
                return False, 0.0, f"already have position in {symbol}"

        # 2. Maximum open position limit (soft ceiling)
        if len(positions) >= self.max_positions:
            return False, 0.0, f"max positions ceiling ({self.max_positions}) reached"

        # 3. Sector correlation ceiling
        sector = self.get_sector(symbol)
        sector_count = sum(1 for p in positions if self.get_sector(p.get("symbol", "")) == sector)
        if sector_count >= self.max_correlated:
            return False, 0.0, f"max correlated positions in {sector} ({self.max_correlated}) reached"

        # 4. Daily loss limit circuit breaker
        daily_pnl = risk_params.get("daily_realized_pnl", 0.0)
        if daily_pnl < -equity * self.daily_loss_pct / 100:
            return False, 0.0, f"daily loss limit ({self.daily_loss_pct}%) breached"

        # 5. Cash Buffer & Exposure Guard (Preserve minimum unallocated cash)
        current_market_value = sum(float(p.get("market_value", 0.0) or (float(p.get("qty", 0.0)) * float(p.get("current_price", 0.0)))) for p in positions)
        max_allowed_exposure = equity * (1.0 - self.min_cash_buffer_pct / 100.0)
        remaining_cash_budget = max(0.0, max_allowed_exposure - current_market_value)
        if remaining_cash_budget < 10.0:
            return False, 0.0, f"insufficient cash headroom (${remaining_cash_budget:.2f} remaining, minimum cash buffer {self.min_cash_buffer_pct}%)"

        # 6. Sizing via Volatility & Fractional Kelly
        win_rate = risk_params.get("win_rate_estimate", 0.55)
        win_loss_ratio = risk_params.get("win_loss_ratio_estimate", 1.5)
        kelly_pct = win_rate - (1 - win_rate) / win_loss_ratio
        kelly_pct = max(0.05, min(kelly_pct, 0.30))

        ml_confidence = signal.get("ml_approval_prob", 0.5)
        position_value = equity * self.kelly_fraction * kelly_pct * (0.8 + 0.4 * ml_confidence)
        position_value = min(position_value, equity * self.max_position_pct / 100)
        position_value = min(position_value, remaining_cash_budget)

        entry_price = signal.get("entry", 0.0)
        if entry_price <= 0 or not math.isfinite(entry_price):
            return False, 0.0, "invalid entry price"

        min_val = 10.0 if is_crypto_symbol(symbol) else 100.0
        if position_value < min_val:
            return False, 0.0, f"position value ${position_value:.2f} below minimum ${min_val:.0f}"

        # 7. Aggregate Portfolio Heat (VaR) Guard
        sl = signal.get("stop_loss", entry_price * 0.985)
        trade_risk_dollars = (entry_price - sl) * (position_value / entry_price) if entry_price > sl else position_value * 0.015
        current_heat_pct = self.calculate_portfolio_heat(positions, equity)
        incremental_heat_pct = (trade_risk_dollars / equity) * 100.0 if equity > 0 else 0.0

        if current_heat_pct + incremental_heat_pct > self.max_portfolio_heat_pct:
            return False, 0.0, f"aggregate portfolio heat ({current_heat_pct + incremental_heat_pct:.2f}%) exceeds budget ({self.max_portfolio_heat_pct}%)"

        if is_crypto_symbol(symbol):
            decimals = 4 if entry_price > 100 else 2
            qty = round(position_value / entry_price, decimals)
            if qty * entry_price < 1.0:
                return False, 0.0, f"crypto notional ${qty * entry_price:.2f} below $1.00 minimum"
            max_qty = round((equity * 0.5) / entry_price, decimals)
            qty = min(qty, max_qty)
            if qty <= 0:
                return False, 0.0, "calculated crypto quantity rounded to 0"
            return True, float(qty), "approved"
        else:
            qty = max(1, int(position_value / entry_price))
            max_shares = int(equity * 0.5 / entry_price)
            qty = min(qty, max_shares)
            return True, float(qty), "approved"

    def get_sector(self, symbol: str) -> str:
        s = symbol.upper()
        if s in self.sector_map:
            return self.sector_map[s]
        # Match normalized pair
        s_norm = s if "/" in s else f"{s[:-3]}/{s[-3:]}" if s.endswith("USD") else s
        if s_norm in self.sector_map:
            return self.sector_map[s_norm]
        if is_crypto_symbol(s):
            return "Crypto"
        return "Unknown"

    def get_sector_exposure(self, positions: list[dict]) -> dict[str, int]:
        exposure = {}
        for p in positions:
            sector = self.get_sector(p.get("symbol", ""))
            exposure[sector] = exposure.get(sector, 0) + 1
        return exposure


def load_risk_params_from_db() -> dict[str, Any]:
    """Load risk parameters from database for the risk manager."""
    import db_manager
    params = db_manager.get_risk_params()
    params["daily_realized_pnl"] = db_manager.get_daily_realized_pnl()
    return params