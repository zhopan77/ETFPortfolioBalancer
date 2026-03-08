"""
Interactive Brokers ETF portfolio helper

Features:
1. Read all positions from your IBKR account and show total portfolio value.
2. Given a target allocation (from Allocations.csv), compute target share counts.
3. Compare current portfolio to target and output how many shares to buy/sell.

Requirements:
- Download and run the IBKR Client Portal Gateway (https://www.interactivebrokers.com/en/trading/ib-api.php)
- Log in via the gateway browser prompt before running this script.
- The gateway runs locally at https://localhost:5000 by default.
- Set environment variable IB_ACCOUNT_ID with your IBKR account ID (e.g. U1234567),
  or leave unset to auto-detect from the /iserver/accounts endpoint.
"""

import csv
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

import requests
import urllib3

# The gateway uses a self-signed cert; suppress the warning.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ========= CONFIGURATION =========

IB_GATEWAY_BASE_URL = "https://localhost:5000/v1/api"


# ========= DATA STRUCTURES =========

@dataclass
class Position:
    symbol: str
    quantity: float
    market_value: float
    price: float


# ========= LOW-LEVEL API HELPERS =========

def _get(path: str, **kwargs) -> requests.Response:
    """GET against the local gateway (SSL verification disabled for self-signed cert)."""
    url = f"{IB_GATEWAY_BASE_URL}{path}"
    resp = requests.get(url, verify=False, timeout=10, **kwargs)
    resp.raise_for_status()
    return resp

def _post(path: str, **kwargs) -> requests.Response:
    url = f"{IB_GATEWAY_BASE_URL}{path}"
    resp = requests.post(url, verify=False, timeout=10, **kwargs)
    resp.raise_for_status()
    return resp

def check_auth() -> None:
    """Verify the gateway session is authenticated before proceeding."""
    resp = _post("/iserver/auth/status")
    data = resp.json()
    if not data.get("authenticated"):
        raise RuntimeError(
            "IBKR gateway is not authenticated.\n"
            "  - Make sure the Client Portal Gateway is running.\n"
            "  - Open https://localhost:5000 in your browser and log in."
        )

def get_account_id() -> str:
    """Let the user choose from configured account IDs, or auto-detect from the gateway."""
    options = {}
    data = _get("/iserver/accounts").json()
    accounts = data.get("accounts", [])
    if not accounts:
        raise RuntimeError("No IBKR accounts found via /iserver/accounts.")
    if len(accounts) == 1:
        return accounts[0]
    for i, acct in enumerate(accounts, 1):
        options[str(i)] = acct

    print("Select account:")
    for key, acct_id in options.items():
        print(f"  {key}: {acct_id}")
    choice = input("Enter choice: ").strip()
    if choice not in options:
        raise ValueError(f"Invalid choice: {choice!r}")
    return options[choice]

def resolve_conids(symbols: List[str]) -> Dict[str, int]:
    """
    Resolve ticker symbols to IBKR contract IDs (conids).
    Uses GET /iserver/secdef/search?symbol=SYM
    Returns {symbol: conid}.
    """
    conid_map: Dict[str, int] = {}
    for sym in symbols:
        resp = _get("/iserver/secdef/search", params={"symbol": sym, "name": False})
        results = resp.json()
        # Find the first STK (stock/ETF) result in USD
        conid = None
        for r in results:
            if r.get("secType") == "STK":
                conid = r.get("conid")
                break
        if conid is None and results:
            conid = results[0].get("conid")
        if conid is None:
            raise ValueError(f"Could not resolve conid for symbol: {sym}")
        conid_map[sym] = int(conid)
    return conid_map


# ========= API CALLS =========

def get_cash_balance(account_id: str) -> float:
    """
    Returns total cash balance across all currencies (converted to base) for the account.
    GET /portfolio/{accountId}/ledger — response is {currency: {cashbalance: ..., ...}}
    Uses the 'BASE' key which IBKR provides as the currency-converted aggregate.
    """
    resp = _get(f"/portfolio/{account_id}/ledger")
    ledger = resp.json()
    base = ledger.get("BASE", {})
    return float(base.get("cashbalance", 0.0))

def get_account_positions(account_id: str) -> List[Dict]:
    # Step 1: iserver/accounts MUST be called first per IBKR docs
    try:
        r = _get("/iserver/accounts")
        # print(f"  [debug] /iserver/accounts: {r.json()}")
    except Exception as e:
        print(f"  [debug] /iserver/accounts failed: {e}")

    # Step 2: portfolio/accounts warm-up
    try:
        r = _get("/portfolio/accounts")
        # print(f"  [debug] /portfolio/accounts: {r.json()}")
    except Exception as e:
        print(f"  [debug] /portfolio/accounts failed: {e}")

    # Step 3: invalidate cache
    try:
        r = _post(f"/portfolio/{account_id}/positions/invalidate")
        # print(f"  [debug] invalidate: {r.status_code} {r.text}")
    except Exception as e:
        print(f"  [debug] invalidate failed: {e}")

    time.sleep(3)

    # Step 4: fetch positions and print raw
    resp = _get(f"/portfolio/{account_id}/positions/0")
    # print(f"  [debug] positions raw ({resp.status_code}): {resp.text[:500]}")
    page_data = resp.json()

    if not page_data:
        # Try the /portfolio/subaccounts endpoint — needed for some advisor setups
        try:
            r = _get("/portfolio/subaccounts")
            # print(f"  [debug] /portfolio/subaccounts: {r.text[:300]}")
        except Exception as e:
            print(f"  [debug] /portfolio/subaccounts failed: {e}")

    return page_data if isinstance(page_data, list) else []


def switch_account(account_id: str) -> None:
    """
    For advisor/multi-account setups, set the active account context.
    This is required before calling /portfolio/{accountId}/positions.
    """
    try:
        _post("/iserver/account", json={"acctId": account_id})
        time.sleep(1)
    except Exception:
        pass  # non-fatal if already set


def get_quotes(conid_map: Dict[str, int]) -> Dict[str, float]:
    """
    Fetch last prices for a set of symbols via their conids.
    GET /iserver/marketdata/snapshot?conids=...&fields=31
    Field "31" = Last Price.
    Calls twice with a short delay as IBKR recommends for first-use subscription.
    """
    if not conid_map:
        return {}
    conids_str = ",".join(str(c) for c in conid_map.values())
    params = {"conids": conids_str, "fields": "31"}

    # First call subscribes to the feed; second call returns populated data.
    _get("/iserver/marketdata/snapshot", params=params)
    time.sleep(1)
    resp = _get("/iserver/marketdata/snapshot", params=params)
    snapshots = resp.json()

    # Build reverse map: conid -> symbol
    conid_to_sym = {v: k for k, v in conid_map.items()}

    prices: Dict[str, float] = {}
    for snap in snapshots:
        conid = snap.get("conid")
        sym = conid_to_sym.get(conid)
        if sym is None:
            continue
        raw = snap.get("31")  # last price field
        if raw is not None:
            try:
                prices[sym] = float(str(raw).replace("C", "").replace("H", ""))
            except ValueError:
                prices[sym] = 0.0
    return prices


# ========= CORE LOGIC =========

def read_current_positions(account_id: str) -> Tuple[List[Position], float]:
    """Read all positions from IBKR and compute total portfolio value."""
    positions_raw = get_account_positions(account_id)

    positions: List[Position] = []
    total_value = 0.0

    for p in positions_raw:
        symbol = p.get("contractDesc", "") or p.get("ticker", "")  # contractDesc is the actual field
        qty = float(p.get("position", 0))
        mv = float(p.get("mktValue", 0))
        price = float(p.get("mktPrice", 0))
        if not symbol or qty == 0:
            continue
        positions.append(Position(symbol=symbol, quantity=qty, market_value=mv, price=price))
        total_value += mv

    return positions, total_value


def compute_target_shares(
    total_investable: float,
    target_allocations: Dict[str, float],
    prices: Dict[str, float],
) -> Dict[str, int]:
    """
    Given total dollar amount and per-symbol target weights (sum to ~1.0),
    compute target integer share counts.
    """
    total_weight = sum(target_allocations.values())
    if not (0.98 <= total_weight <= 1.02):
        raise ValueError(f"Target weights sum to {total_weight}, expected ~1.0")

    target_shares: Dict[str, int] = {}
    for symbol, weight in target_allocations.items():
        if symbol not in prices:
            raise ValueError(f"Missing price for {symbol}")
        dollars_for_symbol = total_investable * weight
        target_shares[symbol] = int(dollars_for_symbol / prices[symbol]) if prices[symbol] != 0 else 0

    return target_shares

def compute_trade_diff(
    current_positions: List[Position],
    target_shares: Dict[str, int],
) -> Dict[str, int]:
    """
    Compare current portfolio to target shares.
    Positive = buy, negative = sell.
    """
    current_map: Dict[str, int] = {p.symbol: int(p.quantity) for p in current_positions}
    all_symbols = set(current_map.keys()).union(target_shares.keys())

    trades: Dict[str, int] = {}
    for symbol in all_symbols:
        diff = target_shares.get(symbol, 0) - current_map.get(symbol, 0)
        trades[symbol] = diff

    return trades

def read_allocations(path: str = "Allocations.csv") -> Dict[str, float]:
    """
    Read symbol and weight from CSV. Weight format: "5.0%"
    Returns allocations dict only — total amount is asked separately.
    """
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

def print_positions(positions: List[Position], total_value: float) -> None:
    print("Current positions:")
    print(f"{'Symbol':<8}{'Qty':>12}{'Price':>12}{'Value':>14}")
    for p in positions:
        print(f"{p.symbol:<8}{int(p.quantity):>12}{p.price:>12.4f}{p.market_value:>14.2f}")
    print("-" * 50)
    print(f"Total portfolio value: {total_value:,.2f}")

def print_trades(trades: Dict[str, int], prices: Dict[str, float], current_map: Dict[str, int], target_shares: Dict[str, int], positions: List[Position]) -> None:
    print("\nTrade instructions (positive = buy, negative = sell):")

    # Build a price fallback from current position data
    position_prices = {p.symbol: p.price for p in positions}

    rows = []
    for symbol, diff_shares in sorted(trades.items()):
        if diff_shares == 0:
            continue
        px = prices.get(symbol) or position_prices.get(symbol, 0.0)  # fallback to position price
        after = target_shares.get(symbol, 0)
        dollars = after * px
        if abs(dollars) < 0.005:
            dollars = 0.0
        rows.append((symbol, current_map.get(symbol, 0), after, diff_shares, px, dollars))

    total = sum(r[5] for r in rows)

    print(f"{'Symbol':<8}{'Before':>10}{'After':>10}{'Change':>10}{'Price':>10}{'Target Position':>22}{'Target Weight':>16}")
    for symbol, before, after, diff_shares, px, dollars in rows:
        pct = (dollars / total * 100) if total else 0.0
        dollar_str = f"$ {dollars:,.2f}"
        price_str = f"{px:,.4f}"
        print(f"{symbol:<8}{before:>10}{after:>10}{diff_shares:>10}{price_str:>10}    {dollar_str:>18}  {pct:>12.1f}%")

    print("-" * 84)
    total_str = f"$ {total:,.2f}"
    print(f"{'Total':<8}{'':>10}{'':>10}{'':>10}{'':>10}    {total_str:>18}  {'100.0%':>9}")


# ========= MAIN =========

def main():
    # Verify gateway is up and authenticated
    check_auth()

    account_id = get_account_id()
    print(f"\nUsing account: {account_id}")
    switch_account(account_id)
    time.sleep(2)  # give the account switch time to settle before querying positions

    # 1) Read current positions
    positions, total_value = read_current_positions(account_id)
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
            sym    = (row.get("Symbol")  or "").strip().strip('"')
            weight = (row.get("Weight")  or "").strip().strip('"')
            if not sym or "%" not in weight:
                continue
            holding = (row.get("Holding") or "").strip().strip('"')
            print(f"{sym:<8}{holding:<36}{weight:>8}")

    # 4) Ask for total amount to allocate (Enter = auto from account balance)
    raw = input("\nTotal amount to allocate (Enter to use 99% of account balance): ").strip()
    if raw:
        total_investable = float(raw)
    else:
        cash = get_cash_balance(account_id)
        total_account = total_value + cash
        total_investable = total_account * 0.99
        print(f"  Positions: ${total_value:,.2f}  Cash: ${cash:,.2f}  Total: ${total_account:,.2f}")
        print(f"  Using 99%: ${total_investable:,.2f}")

    # 5) Resolve symbols to conids, then fetch live prices
    print("\nResolving symbols to contract IDs...")
    conid_map = resolve_conids(target_symbols)
    print("Fetching live quotes...")
    prices = get_quotes(conid_map)

    # 6) Compute target shares
    target_shares = compute_target_shares(total_investable, target_allocations, prices)

    # 7) Compute and print trade diff
    trades = compute_trade_diff(positions, target_shares)
    current_map = {p.symbol: int(p.quantity) for p in positions}
    print_trades(trades, prices, current_map, target_shares, positions)


if __name__ == "__main__":
    main()
