"""
Public.com ETF portfolio helper

Features:
1. Read all positions from your Public.com account and show total portfolio value.
2. Given a target allocation (from Allocations.csv), compute target share counts.
3. Compare current portfolio to target and output how many shares to buy/sell.

Requirements:
- Set environment variable PUBLIC_API_TOKEN with your API access token.
- Set environment variable PUBLIC_ACCOUNT_ID with your account ID,
  or leave unset to be prompted at startup.
"""

import csv
import os
from dataclasses import dataclass
from typing import Dict, List, Tuple

import requests


# ========= CONFIGURATION =========

PUBLIC_API_BASE_URL = "https://api.public.com"
PUBLIC_ACCOUNT_ID   = os.getenv("PUBLIC_ACCOUNT_ID")
PUBLIC_API_SECRET   = os.getenv("PUBLIC_API_TOKEN")


# ========= DATA STRUCTURES =========

@dataclass
class Position:
    symbol: str
    quantity: float
    market_value: float
    price: float


# ========= LOW-LEVEL API HELPERS =========

def get_access_token() -> str:
    if not PUBLIC_API_SECRET:
        raise RuntimeError("PUBLIC_API_TOKEN env var not set")
    url = f"{PUBLIC_API_BASE_URL}/userapiauthservice/personal/access-tokens"
    resp = requests.post(url, json={"secret": PUBLIC_API_SECRET, "validityInMinutes": 60}, timeout=10)
    resp.raise_for_status()
    return resp.json()["accessToken"]

_ACCESS_TOKEN: str | None = None

def _headers() -> Dict[str, str]:
    global _ACCESS_TOKEN
    if not _ACCESS_TOKEN:
        _ACCESS_TOKEN = get_access_token()
    return {"Authorization": f"Bearer {_ACCESS_TOKEN}", "Accept": "application/json"}


def get_account_portfolio(account_id: str) -> Dict:
    url = f"{PUBLIC_API_BASE_URL}/userapigateway/trading/{account_id}/portfolio/v2"
    resp = requests.get(url, headers=_headers(), timeout=10)
    if resp.status_code == 403:
        raise RuntimeError(
            f"403 Forbidden: Access denied.\n"
            f"  - Verify PUBLIC_API_TOKEN is set correctly.\n"
            f"  - Confirm the token has portfolio/read permissions.\n"
            f"  - Ensure account ID is correct (got: {account_id}).\n"
            f"  - Token may have expired — regenerate at Public.com > Settings > API."
        )
    resp.raise_for_status()
    return resp.json()

def get_account_balance(account_id: str) -> float:
    """
    Returns equity + cash balance for the account.
    Public returns 'equity' as a list of {type, value} dicts.
    """
    portfolio = get_account_portfolio(account_id)

    equity_list = portfolio.get("equity", [])
    total = sum(float(item["value"]) for item in equity_list if item.get("value"))
    return total

def get_quotes(symbols: List[str], account_id: str) -> Dict[str, float]:
    """Retrieve last prices for symbols via Public marketdata quotes endpoint."""
    if not symbols:
        return {}
    url = f"{PUBLIC_API_BASE_URL}/userapigateway/marketdata/{account_id}/quotes"
    body = {"instruments": [{"symbol": s, "type": "EQUITY"} for s in symbols]}
    resp = requests.post(url, headers=_headers(), json=body, timeout=10)
    resp.raise_for_status()
    prices: Dict[str, float] = {}
    for quote in resp.json().get("quotes", []):
        sym = quote["instrument"]["symbol"]
        # Try last, then closePrice as fallback for market-closed sessions
        price = quote.get("last") or quote.get("closePrice") or quote.get("previousClose") or 0.0
        prices[sym] = float(price)
    return prices


# ========= CORE LOGIC =========

def read_current_positions(account_id: str) -> Tuple[List[Position], float]:
    """Read all positions from Public and compute total portfolio value."""
    portfolio = get_account_portfolio(account_id)
    positions_raw = portfolio.get("positions", [])

    positions: List[Position] = []
    total_value = 0.0

    for p in positions_raw:
        symbol = p["instrument"]["symbol"]
        qty    = float(p["quantity"])
        mv     = float(p["currentValue"])
        price  = float(p["lastPrice"]["lastPrice"]) if p.get("lastPrice") else (mv / qty if qty != 0 else 0.0)
        positions.append(Position(symbol=symbol, quantity=qty, market_value=mv, price=price))
        total_value += mv

    return positions, total_value


def read_allocations(path: str = "Allocations.csv") -> Dict[str, float]:
    """
    Read symbol and weight from CSV. Weight format: "5.0%"
    Only uses Symbol, Holding, and Weight columns — ignores Amount, Price, Shares.
    """
    allocations: Dict[str, float] = {}
    with open(path, newline="") as f:
        next(f)  # skip blank first line
        reader = csv.DictReader(f)
        for row in reader:
            sym        = row.get("Symbol", "").strip().strip('"')
            weight_str = row.get("Weight", "").strip().strip('"')
            if not sym or not weight_str or "%" not in weight_str:
                continue
            allocations[sym] = float(weight_str.replace("%", "")) / 100
    return allocations


def compute_target_shares(
    total_investable: float,
    target_allocations: Dict[str, float],
    prices: Dict[str, float],
) -> Dict[str, int]:
    total_weight = sum(target_allocations.values())
    if not (0.98 <= total_weight <= 1.02):
        raise ValueError(f"Target weights sum to {total_weight:.4f}, expected ~1.0")

    target_shares: Dict[str, int] = {}
    for symbol, weight in target_allocations.items():
        if symbol not in prices:
            raise ValueError(f"Missing price for {symbol}")
        dollars = total_investable * weight
        target_shares[symbol] = int(dollars / prices[symbol]) if prices[symbol] != 0 else 0

    return target_shares


def compute_trade_diff(
    current_positions: List[Position],
    target_shares: Dict[str, int],
) -> Dict[str, int]:
    current_map = {p.symbol: int(p.quantity) for p in current_positions}
    all_symbols = set(current_map.keys()).union(target_shares.keys())
    return {sym: target_shares.get(sym, 0) - current_map.get(sym, 0) for sym in all_symbols}


# ========= DISPLAY =========

def bold_red(text: str) -> str:
    return f"\033[1;31m{text}\033[0m"

def bold_green(text: str) -> str:
    return f"\033[1;32m{text}\033[0m"

def print_positions(positions: List[Position], total_value: float) -> None:
    print("Current positions:")
    print(f"{'Symbol':<8}{'Qty':>12}{'Price':>12}{'Value':>14}")
    print("-" * 50)  # separator after header
    for p in positions:
        print(f"{p.symbol:<8}{int(p.quantity):>12}{p.price:>12.4f}{p.market_value:>14.2f}")
    print("-" * 50)
    print(f"Total portfolio value: {total_value:,.2f}")


def print_trades(
    trades: Dict[str, int],
    prices: Dict[str, float],
    current_map: Dict[str, int],
    target_shares: Dict[str, int],
    positions: List[Position],
) -> None:
    print(bold_green("\nTrade instructions (positive = buy, negative = sell):"))

    position_prices = {p.symbol: p.price for p in positions}

    rows = []
    for symbol, diff_shares in sorted(trades.items()):
        if diff_shares == 0:
            continue
        px     = prices.get(symbol) or position_prices.get(symbol, 0.0)
        after  = target_shares.get(symbol, 0)
        dollars = after * px
        if abs(dollars) < 0.005:
            dollars = 0.0
        rows.append((symbol, current_map.get(symbol, 0), after, diff_shares, px, dollars))

    total = sum(r[5] for r in rows)

    print(f"{'Symbol':<8}{'Before':>10}{'After':>10}{'Change':>10}{'Price':>14}{'Target Position':>22}{'Target Weight':>16}")
    print("-" * 88)  # separator after header
    for symbol, before, after, diff_shares, px, dollars in rows:
        pct        = (dollars / total * 100) if total else 0.0
        price_str  = f"$ {px:,.2f}"
        dollar_str = f"$ {dollars:,.2f}"
        print(f"{symbol:<8}{before:>10}{after:>10}{diff_shares:>10}{price_str:>14}    {dollar_str:>18}  {pct:>12.1f}%")

    print("-" * 88)
    total_str = f"$ {total:,.2f}"
    print(f"{'Total':<8}{'':>10}{'':>10}{'':>10}{'':>14}    {total_str:>18}  {'100.0%':>9}")


# ========= MAIN =========

def main():
    print("\n######## CALCULATING PUBLIC.COM ACCOUNT REBLANCE TRADES BASED ON LI EXPORT ########\n")
    print("* Set environment variable PUBLIC_API_TOKEN with your API access token.")
    print("* Set environment variable PUBLIC_ACCOUNT_ID with your account ID, or leave unset to be prompted at startup.")
    print("* Export LI portfolio as Allocations.csv and store in the script folder")
    print()

    account_id = PUBLIC_ACCOUNT_ID or input("Enter your Public account ID: ").strip()

    # 1) Read current positions
    positions, total_value = read_current_positions(account_id)
    print_positions(positions, total_value)

    # 2) Load target allocations from CSV (Symbol + Weight only)
    target_allocations = read_allocations("Allocations.csv")
    print(f"\nLoaded {len(target_allocations)} allocations from Allocations.csv")
    target_symbols = list(target_allocations.keys())

    # 3) Show CSV allocations table
    print("\nTarget allocations (from Allocations.csv):")
    with open("Allocations.csv", newline="") as f:
        next(f)
        reader = csv.DictReader(f)
        print(f"{'Symbol':<8}{'Holding':<36}{'Weight':>8}")
        print("-" * 54)
        for row in reader:
            sym     = (row.get("Symbol")  or "").strip().strip('"')
            weight  = (row.get("Weight")  or "").strip().strip('"')
            holding = (row.get("Holding") or "").strip().strip('"')
            if not sym or "%" not in weight:
                continue
            print(f"{sym:<8}{holding:<36}{weight:>8}")

    # 4) Ask for total amount to allocate (Enter = auto from account balance)
    raw = input("\nTotal $ amount to allocate (Enter to use 99% of account balance): ").strip()
    if raw:
        total_investable = float(raw)
    else:
        balance = get_account_balance(account_id)
        total_investable = balance * 0.99
        print(f"  Account balance: ${balance:,.2f}  Using 99%: ${total_investable:,.2f}")

    # 5) Fetch live prices for target symbols
    print("\nFetching live quotes...")
    prices = get_quotes(target_symbols, account_id)

    # 6) Compute target shares
    target_shares = compute_target_shares(total_investable, target_allocations, prices)

    # 7) Compute and print trade diff
    trades = compute_trade_diff(positions, target_shares)
    current_map = {p.symbol: int(p.quantity) for p in positions}
    print_trades(trades, prices, current_map, target_shares, positions)


if __name__ == "__main__":
    main()
