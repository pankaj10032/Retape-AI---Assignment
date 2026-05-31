from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

def round_half_up(x: float) -> int:
    """
    Round a float to the nearest integer using round-half-up (0.5 always goes up).

    Python's built-in round() uses banker's rounding:
        round(2.5) == 2   ← rounds to even
    We need:
        round_half_up(2.5) == 3

    Implementation: floor(x + 0.5) achieves this for positive numbers and
    behaves correctly for negative numbers too (e.g. -2.5 → -2, away from zero).
    """
    return math.floor(x + 0.5)


# ---------------------------------------------------------------------------
# Date / cadence helpers
# ---------------------------------------------------------------------------

def last_day_of_month(year: int, month: int) -> int:
    """Return the last calendar day of the given month."""
    # Jump to the 1st of the next month, then go back one day.
    if month == 12:
        return 31
    next_month_first = date(year, month + 1, 1)
    return (next_month_first.replace(day=1) - __import__("datetime").timedelta(days=1)).day


def is_end_of_month(d: date) -> bool:
    """Return True if d is the last day of its month."""
    return d.day == last_day_of_month(d.year, d.month)


def advance_one_month(d: date, anchor_day: int, end_of_month_anchor: bool) -> date:
    """
    Advance a cadence date by exactly one month.

    Rules (from the spec):
    - If end_of_month_anchor is True  → always return the last day of the next month.
    - Otherwise                       → return the same day-of-month, clamped to the
                                        month length (e.g. day 31 in a 30-day month → 30).

    anchor_day is the original first_payment_date day, preserved across all months.
    """
    month = d.month + 1
    year = d.year
    if month > 12:
        month = 1
        year += 1

    if end_of_month_anchor:
        target_day = last_day_of_month(year, month)
    else:
        target_day = min(anchor_day, last_day_of_month(year, month))

    return date(year, month, target_day)


def build_cadence(first_payment_date: date, n: int) -> List[date]:
    """
    Build a list of n cadence dates starting at first_payment_date.

    The cadence follows end-of-month rules if first_payment_date is the last
    day of its month; otherwise it anchors to the same day-of-month.

    Example:
        first_payment_date = 2026-01-31  (last day of Jan)
        → [2026-01-31, 2026-02-28, 2026-03-31, 2026-04-30, ...]

        first_payment_date = 2026-01-15
        → [2026-01-15, 2026-02-15, 2026-03-15, ...]
    """
    eom = is_end_of_month(first_payment_date)
    anchor_day = first_payment_date.day
    dates: List[date] = [first_payment_date]
    for _ in range(n - 1):
        dates.append(advance_one_month(dates[-1], anchor_day, eom))
    return dates


def end_of_month(d: date) -> date:
    return date(d.year, d.month, last_day_of_month(d.year, d.month))


def default_first_payment_date(client: "Client") -> date:
    """Default first payment date: the end of the first draft month."""
    return end_of_month(client.first_draft_date)


def monthly_payment_dates(start: date, count: int) -> List[date]:
    """Generate count monthly cadence dates from the starting date."""
    if count <= 0:
        return []
    return build_cadence(start, count)


def offer_total_cents(offer: "Offer") -> int:
    return offer.offer_total_cents


def program_fee_cents(offer: "Offer", rules: "CreditorRules") -> int:
    return rules.program_fee_total(offer.original_balance_cents)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    """A single credit or debit entry on the SDA."""
    date: date
    amount_cents: int   # always positive; direction is given by 'type'
    type: str           # 'credit' or 'debit'

    @property
    def signed_cents(self) -> int:
        """Return +amount for credits, -amount for debits."""
        return self.amount_cents if self.type == "credit" else -self.amount_cents


@dataclass
class Client:
    """
    Represents the client's escrow account (SDA).

    Fields
    ------
    draft_amount_cents   : fixed monthly deposit
    draft_day            : day-of-month the draft lands
    first_draft_date     : first deposit date
    last_draft_date      : horizon — nothing may be scheduled past this date
    as_of_date           : balance snapshot date; ledger entries after this are future
    current_balance_cents: SDA balance as of as_of_date
    ledger               : all known entries (past = fixed; future = modifiable credits,
                           plus any fixed future debits from other settled debts)
    """
    draft_amount_cents: int
    draft_day: int
    first_draft_date: date
    last_draft_date: date
    as_of_date: date
    current_balance_cents: int
    ledger: List[LedgerEntry]

    @property
    def horizon(self) -> date:
        return self.last_draft_date

    def future_ledger(self) -> List[LedgerEntry]:
        """Entries dated strictly after as_of_date — the modifiable future."""
        return [e for e in self.ledger if e.date > self.as_of_date]


@dataclass
class Offer:
    """
    A settlement offer from a creditor.

    Fields
    ------
    creditor               : creditor name (informational)
    creditor_balance_cents : the current balance owed to this creditor
    original_balance_cents : the original/enrolled balance (used to compute program fee)
    settlement_pct         : fraction of creditor_balance we agree to pay
    first_payment_date     : first cadence date (optional; defaults to EOM of first_draft)
    """
    creditor: str
    creditor_balance_cents: int
    original_balance_cents: int
    settlement_pct: float
    first_payment_date: Optional[date]

    @property
    def offer_total_cents(self) -> int:
        """Total we must pay the creditor = round_half_up(settlement_pct × creditor_balance)."""
        return round_half_up(self.settlement_pct * self.creditor_balance_cents)


@dataclass
class CreditorRules:
    """
    Creditor-specific payment rules. All fields are generic inputs — nothing
    is hard-coded for a specific creditor.

    Fields
    ------
    max_terms            : cap on number of creditor payments (same as max_payments here)
    max_payments         : cap on number of creditor payments
    min_payment_cents    : base minimum per creditor payment
    max_token_pays       : how many payments may sit exactly at min_payment_cents
    min_payment_tiers    : list of (from_payment_number_1based, min_cents) step-ups
    even_pays            : all payments must be equal (or as equal as possible)
    is_ballooning_allowed: final payment may absorb remaining balance
    max_segments         : max distinct payment levels (only when not even, not balloon)
    bank_fee_cents       : flat fee on each date carrying a creditor payment
    program_fee_pct      : our fee = round(pct × original_balance_cents)
    """
    max_terms: int
    max_payments: int
    min_payment_cents: int
    max_token_pays: int
    min_payment_tiers: List[Tuple[int, int]]   # [(from_1based, min_cents), ...]
    even_pays: bool
    is_ballooning_allowed: bool
    max_segments: int
    bank_fee_cents: int
    program_fee_pct: float

    @property
    def max_k(self) -> int:
        """Maximum allowed number of creditor payments."""
        return min(self.max_payments, self.max_terms)

    def floor_at(self, payment_index_1based: int, token_pays_used_so_far: int) -> int:
        """
        Return the minimum allowed payment amount at a given 1-based position.

        Three sources of floor, take the max:
        1. Base minimum (min_payment_cents)
        2. Token-pay rule: if token_pays_used_so_far >= max_token_pays,
           this payment must STRICTLY exceed min_payment_cents → floor = min+1
        3. Tier step-ups: any tier whose from_payment_number <= this index
        """
        floor = self.min_payment_cents

        # Token-pay: once we've used up all allowed token pays, must exceed base min
        if token_pays_used_so_far >= self.max_token_pays:
            floor = max(floor, self.min_payment_cents + 1)

        # Tier step-ups
        for (from_pay, tier_min) in self.min_payment_tiers:
            if payment_index_1based >= from_pay:
                floor = max(floor, tier_min)

        return floor

    def program_fee_total(self, original_balance_cents: int) -> int:
        return round_half_up(self.program_fee_pct * original_balance_cents)


# ---------------------------------------------------------------------------
# JSON loaders
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _ledger_entry_amount_cents(entry: dict[str, object]) -> int:
    if "amount_cents" in entry:
        return entry["amount_cents"]
    if "amount" in entry:
        return entry["amount"]
    raise KeyError("ledger entry missing required 'amount_cents' field")


def load_client(path: Path) -> Client:
    data = json.loads(path.read_text())
    ledger = [
        LedgerEntry(
            date=_parse_date(e["date"]),
            amount_cents=_ledger_entry_amount_cents(e),
            type=e["type"],
        )
        for e in data.get("ledger", [])
    ]
    return Client(
        draft_amount_cents=data["draft_amount_cents"],
        draft_day=data["draft_day"],
        first_draft_date=_parse_date(data["first_draft_date"]),
        last_draft_date=_parse_date(data["last_draft_date"]),
        as_of_date=_parse_date(data["as_of_date"]),
        current_balance_cents=data["current_balance_cents"],
        ledger=ledger,
    )


def load_offer(path: Path) -> Offer:
    data = json.loads(path.read_text())
    # Support both 'creditor_balance_cents' and legacy 'current_balance_cents'
    creditor_balance = data.get("creditor_balance_cents", data.get("current_balance_cents", 0))
    fpd_raw = data.get("first_payment_date")
    return Offer(
        creditor=data["creditor"],
        creditor_balance_cents=creditor_balance,
        original_balance_cents=data["original_balance_cents"],
        settlement_pct=data["settlement_pct"],
        first_payment_date=_parse_date(fpd_raw) if fpd_raw else None,
    )


def load_rules(path: Path) -> CreditorRules:
    data = json.loads(path.read_text())
    tiers = [tuple(t) for t in data.get("min_payment_tiers", [])]
    return CreditorRules(
        max_terms=data.get("max_terms", 12),
        max_payments=data.get("max_payments", 12),
        min_payment_cents=data.get("min_payment_cents", 0),
        max_token_pays=data.get("max_token_pays", 0),
        min_payment_tiers=tiers,
        even_pays=data.get("even_pays", False),
        is_ballooning_allowed=data.get("is_ballooning_allowed", False),
        max_segments=data.get("max_segments", 1),
        bank_fee_cents=data.get("bank_fee_cents", 0),
        program_fee_pct=data.get("program_fee_pct", 0.0),
    )


def load_case(case_dir: Path | str) -> Tuple[Client, Offer, CreditorRules]:
    """Load all three input files from a case directory."""
    case_dir = Path(case_dir)
    return (
        load_client(case_dir / "client.json"),
        load_offer(case_dir / "offer.json"),
        load_rules(case_dir / "creditor_rules.json"),
    )