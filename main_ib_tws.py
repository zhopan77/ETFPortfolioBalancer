"""
Interactive Brokers ETF portfolio helper (ib_insync version)

Features:
1. Read all positions from your IBKR account and show total portfolio value.
2. Given a target allocation (from Allocations.csv), compute target share counts.
3. Compare current portfolio to target and output how many shares to buy/sell.

Requirements:
- TWS must be running and logged in.
- In TWS: Edit -> Global Configuration -> API -> Settings
    - Enable "Enable ActiveX and Socket Clients"
    - Socket port: 7496 (live) or 7497 (paper)
- pip install ib_insync
"""

import csv
import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

from ib_insync import IB, Stock, util

util.logToConsole('CRITICAL')  # suppress ib_insync debug noise


# ========= CONFIGURATION =========

IB_HOST      = "127.0.0.1"
IB_PORT      = 7496   # 7496 = live TWS | 7497 = paper TWS | 4001 = live IB Gateway | 4002 = paper IB Gateway
IB_CLIENT_ID = 10     # any integer unique among concurrent API clients


# ========= DATA STRUCTURES =========

@dataclass
class Position:
    symbol: str
    quantity: float
    market_value: float
    price: float


# ========= ACCOUNT HELPERS =========

def get_account_id(ib: IB) -> str:
    """Let the user choose from available managed accounts."""
    accounts = [a for a in ib.managedAccounts() if a != "All"]
    if not accounts:
        raise RuntimeError("No managed accounts found.")
    if len(accounts) == 1:
        return accounts[0]

    print("Select account:")
    for i, acct in enumerate(accounts, 1):
        print(f"  {i}: {acct}")
    choice = input("Enter choice: ").strip()
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(accounts):
            raise IndexError
        return accounts[idx]
    except (ValueError, IndexError):
        raise ValueError(f"Invalid choice: {choice!r}")


def get_cash_balance(ib: IB, account_id: str) -> float:
    """Return TotalCashValue for the account in base currency."""
    for v in ib.accountValues(account_id):
        if v.tag == "TotalCashValue" and v.currency == "BASE":
            return float(v.value)
    for v in ib.accountValues(account_id):
        if v.tag == "TotalCashValue" and v.currency == "USD":
            return float(v.value)
    return 0.0


# ========= PRICE FETCHING =========

def get_quotes(ib: IB, symbols: List[str]) -> Dict[str, float]:
    """
    Fetch prices for a list of symbols (STK, SMART, USD).
    Uses Delayed Frozen data type so prices are available when market is closed.
    """
    # 4 = Delayed Frozen: returns last known price when market is closed
    # Falls back automatically to live data when market is open
    ib.reqMarketDataType(4)

    contracts = [Stock(sym, "SMART", "USD") for sym in symbols]
    tickers = ib.reqTickers(*contracts)

    prices: Dict[str, float] = {}
    for sym, ticker in zip(symbols, tickers):
        # Try each price field in order of preference
        price = ticker.marketPrice()
        if math.isnan(price) or price <= 0:
            price = ticker.last
        if math.isnan(price) or price <= 0:
            price = ticker.close
        if math.isnan(price) or price <= 0:
            price = ticker.bid  # last resort
        prices[sym] = float(price) if not math.isnan(price) and price > 0 else 0.0

    still_missing = [s for s, px in prices.items() if px == 0]
    if still_missing:
        print(f"  WARNING: Could not fetch prices for: {', '.join(still_missing)}")

    # Reset to live data type for any subsequent requests
    ib.reqMarketDataType(1)

    return prices


# ========= CORE LOGIC =========

def read_current_positions(ib: IB, account_id: str) -> Tuple[List[Position], float]:
    """Read all positions from IBKR and compute total portfolio value."""
    raw = ib.positions(account_id)

    # Collect all unique contracts and fetch prices in one batch
    contracts = {p.contract.symbol: p.contract for p in raw if p.position != 0}
    prices = get_quotes(ib, list(contracts.keys())) if contracts else {}

    positions: List[Position] = []
    total_value = 0.0

    for p in raw:
        symbol = p.contract.symbol
        qty = float(p.position)
        if qty == 0:
            continue
        price = prices.get(symbol, float(p.avgCost))  # fallback to avg cost
        mv = qty * price
        positions.append(Position(symbol=symbol, quantity=qty, market_value=mv, price=price))
        total_value += mv

    return positions, total_value


def compute_target_shares(
    total_investable: float,
    target_allocations: Dict[str, float],
    prices: Dict[str, float],
) -> Dict[str, int]:
    """Given total dollar amount and per-symbol target weights, compute target integer share counts."""
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
    """Compare current portfolio to target shares. Positive = buy, negative = sell."""
    current_map: Dict[str, int] = {p.symbol: int(p.quantity) for p in current_positions}
    all_symbols = set(current_map.keys()).union(target_shares.keys())
    return {sym: target_shares.get(sym, 0) - current_map.get(sym, 0) for sym in all_symbols}


def read_allocations(path: str = "Allocations.csv") -> Dict[str, float]:
    """Read symbol and weight from CSV. Weight format: '5.0%'"""
    allocations: Dict[str, float] = {}
    with open(path, newline="") as f:
        next(f)  # skip blank first line
        reader = csv.DictReader(f)
        for row in reader:
            sym = row.get("Symbol", "").strip().strip('"')
            weight_str = row.get("Weight", "").strip().strip('"')
            if not sym or not weight_str or "%" not in weight_str:
                continue
            allocations[sym] = float(weight_str.replace("%", "")) / 100
    return allocations


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
    print("\n######## CALCULATING IB ACCOUNT REBLANCE TRADES BASED ON LI EXPORT ########\n")
    print("* Make sure ib_insync is installed in the Python environment")
    print("* Make sure IB TWS is running on this computer with API enabled at port 7496")
    print("* Export LI portfolio as Allocations.csv and store in the script folder")
    print()

    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
    print(f"Connected to IBKR TWS at {IB_HOST}:{IB_PORT}")

    try:
        account_id = get_account_id(ib)
        print(f"\nUsing account: {account_id}")

        # 1) Read current positions
        positions, total_value = read_current_positions(ib, account_id)
        print_positions(positions, total_value)

        # 2) Load target allocations from CSV
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
            cash = get_cash_balance(ib, account_id)
            total_account = total_value + cash
            total_investable = total_account * 0.99
            print(f"  Positions: ${total_value:,.2f}  Cash: ${cash:,.2f}  Total: ${total_account:,.2f}")
            print(f"  Using 99%: ${total_investable:,.2f}")

        # 5) Fetch live prices for target symbols
        print("\nFetching live quotes...")
        prices = get_quotes(ib, target_symbols)

        # 6) Compute target shares
        target_shares = compute_target_shares(total_investable, target_allocations, prices)

        # 7) Compute and print trade diff
        trades = compute_trade_diff(positions, target_shares)
        current_map = {p.symbol: int(p.quantity) for p in positions}
        print_trades(trades, prices, current_map, target_shares, positions)

    finally:
        ib.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
