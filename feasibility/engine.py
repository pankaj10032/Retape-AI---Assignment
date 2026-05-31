
"""
engine.py — Core feasibility engine.

HIGH-LEVEL APPROACH
===================

evaluate_offer(client, offer, rules) works in three stages:

  1. SHAPE BUILDER  — produce a list of k creditor-payment amounts that satisfies
                      all hard constraints (exact sum, floors, non-decreasing,
                      segment cap, even/balloon flags).

  2. FEE SCHEDULER  — given those payment amounts and their cadence dates, decide
                      how much program fee to collect on each date, maximising
                      early collection (front-load).

  3. SIMULATOR      — walk the ledger day by day (credits first, then debits),
                      check the balance never goes negative, and emit the schedule.

When no valid schedule fits the client's current funds, we compute the minimum
extra money needed in two forms: a one-time lump sum and a per-draft increment.

PAYMENT SHAPES
==============

The spec leaves the payment shape deliberately open-ended.  Our interpretation:

  even_pays = true
    → all payments equal (remainder cents to latest payments so sum is exact
      and sequence is non-decreasing).  Choose k = max_k to maximise the number
      of dates available for front-loading the program fee.

  is_ballooning_allowed = true  (and even_pays = false)
    → token-pay (minimum) payments for as long as the floors allow, then one
      final balloon that absorbs the remaining creditor balance.  This keeps
      early creditor payments as low as possible, freeing maximum cash for the
      program fee on early dates — which directly serves the objective.
      We try k from max_k down to 1 and pick the first k that is feasible.

  neither (staircase)
    → at most max_segments distinct payment levels.  Strategy: fill the first
      (k - 1) payments at their floor (minimum valid amount), which front-loads
      the program fee, then compute the final segment as a catch-up to hit the
      exact sum.  If that produces more distinct levels than max_segments allows,
      we merge levels greedily from the back.
      We try k from max_k down to 1.

In all cases we try the largest k first because more cadence dates = more slots
to collect the program fee early.

PROGRAM FEE FRONT-LOADING
==========================

After fixing the creditor-payment amounts and their dates, we walk the cadence
dates in order.  On each date:
  - The mandatory debit is: creditor_payment + bank_fee (if a creditor payment
    lands on this date).
  - The available_for_fee is: running_balance_after_credits - mandatory_debit.
  - We collect min(available_for_fee, fee_remaining) as the program fee on that
    date, then debit it too.

This greedy left-to-right collection is optimal: collecting a dollar of fee
today is strictly better than collecting it tomorrow (the objective is to
front-load), and it never makes a previously-feasible balance infeasible because
we only collect what's actually free.

LUMP-SUM MINIMUM (Part 2)
==========================

We binary-search over lump amounts L ∈ [0, offer_total].  For each L we try
placing it on the earliest date (first_payment_date or the first draft date,
whichever is earlier) and test feasibility.  The earliest date is weakly
dominant: the same L placed later is never more useful.

MONTHLY INCREMENT MINIMUM (Part 2)
===================================

We binary-search over increment X added to every future draft (drafts dated
strictly after as_of_date).  We rebuild the client's ledger with the augmented
drafts and test feasibility.
"""

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    advance_one_month,
    build_cadence,
    default_first_payment_date,
    is_end_of_month,
    last_day_of_month,
    round_half_up,
)

@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "creditor_payment_cents": self.creditor_payment_cents,
            "program_fee_cents": self.program_fee_cents,
            "bank_fee_cents": self.bank_fee_cents,
            "balance_cents": self.balance_cents,
        }


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    date: Optional[date] = None
    num_drafts: Optional[int] = None


@dataclass
class LumpSum:
    amount_cents: int
    date: date
    within_guardrail: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "amount_cents": self.amount_cents,
            "date": self.date.isoformat(),
            "within_guardrail": self.within_guardrail,
            "reason": self.reason,
        }


@dataclass
class MonthlyIncrement:
    amount_cents: int
    num_drafts: int
    within_guardrail: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "amount_cents": self.amount_cents,
            "num_drafts": self.num_drafts,
            "within_guardrail": self.within_guardrail,
            "reason": self.reason,
        }


@dataclass
class AdditionalFunds:
    lump_sum: LumpSum
    monthly_increment: MonthlyIncrement

    def to_dict(self) -> dict:
        return {
            "lump_sum": self.lump_sum.to_dict(),
            "monthly_increment": self.monthly_increment.to_dict(),
        }


@dataclass
class Result:
    feasible: bool
    pay_shape_used: Optional[str]           # "even" | "staircase" | "balloon"
    schedule: Optional[List[ScheduleRow]]
    additional_funds: Optional[AdditionalFunds]

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "pay_shape_used": self.pay_shape_used,
            "schedule": [r.to_dict() for r in self.schedule] if self.schedule else None,
            "additional_funds": self.additional_funds.to_dict() if self.additional_funds else None,
        }

def _build_even_payments(offer_total: int, k: int, rules: CreditorRules) -> Optional[List[int]]:
    """
    Build k equal (or as-equal-as-possible) creditor payments summing to offer_total.

    Remainder cents go to the LATEST payments to keep the sequence non-decreasing.
    E.g. offer_total=100, k=3 → [33, 33, 34]  (not [34, 33, 33])

    Returns None if any payment would fall below the floor.
    """
    base = offer_total // k
    remainder = offer_total - base * k

    # remainder payments are at the end and are base+1; earlier ones are base
    payments = [base] * (k - remainder) + [base + 1] * remainder

    # Validate all payments are above their respective floors
    token_pays = 0
    for i, p in enumerate(payments):
        floor = rules.floor_at(i + 1, token_pays)
        if p < floor:
            return None
        # Count a token-pay only if the payment equals the base minimum AND
        # the applicable floor at this position (given current token_pays)
        # is also the base minimum. This avoids counting payments that happen
        # to equal `min_payment_cents` but whose floor was higher.
        if p == rules.min_payment_cents and floor == rules.min_payment_cents:
            token_pays += 1

    return payments


def _build_balloon_payments(offer_total: int, k: int, rules: CreditorRules) -> Optional[List[int]]:
    """
    Build a balloon schedule: first (k-1) payments at their minimum floor,
    final payment = offer_total - sum_of_earlier.

    This maximises cash freed early for program-fee collection.
    Returns None if the balloon is < the floor at position k, or if
    the sum of floors already exceeds offer_total.
    """
    payments: List[int] = []
    token_pays = 0
    running_sum = 0

    for i in range(k - 1):
        floor = rules.floor_at(i + 1, token_pays)
        payments.append(floor)
        # Only treat this as a token-pay if the floor itself is the base min.
        if floor == rules.min_payment_cents:
            token_pays += 1
        running_sum += floor

    # Final balloon payment
    balloon = offer_total - running_sum
    if balloon < 0:
        return None  # floors already exceed offer_total

    final_floor = rules.floor_at(k, token_pays)
    if balloon < final_floor:
        return None  # balloon is below the floor at the final position

    # Must be non-decreasing: balloon >= last regular payment
    if payments and balloon < payments[-1]:
        return None

    payments.append(balloon)
    return payments


def _build_staircase_payments(offer_total: int, k: int, rules: CreditorRules) -> Optional[List[int]]:
    """
    Build a staircase (stepped) schedule with at most max_segments distinct levels.

    Strategy:
      - Explore segmentations of the k payments into at most max_segments blocks.
      - For each segmentation, choose the smallest feasible early segment values
        and compute the final segment as the exact remainder.
      - Validate floors, non-decreasing order, and token-pay rules.

    Returns None if no valid schedule can be constructed within the constraints.
    """
    if k == 1:
        floor = rules.floor_at(1, 0)
        if offer_total < floor:
            return None
        return [offer_total]

    def compositions(n: int, m: int) -> List[List[int]]:
        if m == 1:
            return [[n]]
        result: List[List[int]] = []
        for first in range(1, n - m + 2):
            for rest in compositions(n - first, m - 1):
                result.append([first] + rest)
        return result

    def segment_min_constant_level(start_pos: int, count: int, token_pays: int) -> int:
        floor_value = 0
        for offset in range(count):
            pos = start_pos + offset + 1
            floor_value = max(floor_value, rules.floor_at(pos, token_pays))
        return floor_value

    def can_use_base_level(start_pos: int, count: int, token_pays: int) -> bool:
        if token_pays + count > rules.max_token_pays:
            return False
        current_token_pays = token_pays
        for offset in range(count):
            pos = start_pos + offset + 1
            if rules.floor_at(pos, current_token_pays) != rules.min_payment_cents:
                return False
            current_token_pays += 1
        return True

    def build_for_counts(counts: List[int]) -> Optional[List[int]]:
        levels: List[int] = []
        current_pos = 0
        token_pays = 0
        prev_level = 0
        subtotal = 0

        for segment_index, count in enumerate(counts[:-1]):
            min_above = segment_min_constant_level(current_pos, count, token_pays)
            base_allowed = can_use_base_level(current_pos, count, token_pays)
            if base_allowed and rules.min_payment_cents >= prev_level:
                level = rules.min_payment_cents
            else:
                level = max(prev_level, min_above)

            if level < rules.min_payment_cents:
                level = rules.min_payment_cents
            if level == rules.min_payment_cents and not base_allowed:
                level = max(level + 1, min_above, prev_level)

            levels.append(level)
            if level == rules.min_payment_cents:
                token_pays += count
            prev_level = level
            subtotal += level * count
            current_pos += count

        last_count = counts[-1]
        min_above = segment_min_constant_level(current_pos, last_count, token_pays)
        base_allowed = can_use_base_level(current_pos, last_count, token_pays)
        required = offer_total - subtotal
        if required < 0:
            return None

        min_last = max(prev_level, min_above)
        if required < min_last:
            return None
        if required == rules.min_payment_cents and not base_allowed:
            return None
        if required < rules.min_payment_cents:
            return None

        levels.append(required)

        payments: List[int] = []
        token_pays_check = 0
        for count, level in zip(counts, levels):
            for _ in range(count):
                payment_index = len(payments) + 1
                floor = rules.floor_at(payment_index, token_pays_check)
                if level < floor:
                    return None
                # Only increment the token_pays_check when this payment is a
                # true token pay: the payment equals the base min and the
                # applicable floor equals the base min.
                if level == rules.min_payment_cents and floor == rules.min_payment_cents:
                    token_pays_check += 1
                payments.append(level)

        if len(payments) != k or sum(payments) != offer_total:
            return None
        return payments

    best: Optional[List[int]] = None
    for segments in range(1, rules.max_segments + 1):
        for counts in sorted(compositions(k, segments), key=lambda c: tuple([-x for x in c])):
            payments = build_for_counts(counts)
            if payments is not None:
                return payments

    return None


def _schedule_program_fee(
    cadence_dates: List[date],
    payment_amounts: List[int],          # len = k (creditor payments)
    program_fee_total: int,
    bank_fee_cents: int,
    future_ledger: List[LedgerEntry],    # credits (drafts) + fixed debits
    initial_balance: int,
    horizon: date,
) -> Optional[List[ScheduleRow]]:
    """
    Given fixed creditor-payment amounts and their cadence dates, greedily
    collect the program fee as early as possible.

    The cadence_dates list may include fee-only dates after the last creditor
    payment, and all fixed ledger credits/debits on the same date are applied
    before any fees or payments.
    """
    # Map: date → (credits, debits) from the fixed ledger
    ledger_by_date: Dict[date, Tuple[int, int]] = defaultdict(lambda: (0, 0))
    for entry in future_ledger:
        credits, debits = ledger_by_date[entry.date]
        if entry.type == "credit":
            credits += entry.amount_cents
        else:
            debits += entry.amount_cents
        ledger_by_date[entry.date] = (credits, debits)

    all_dates = sorted(set(list(ledger_by_date.keys()) + cadence_dates))
    cadence_index = {date_: idx for idx, date_ in enumerate(cadence_dates)}

    balance = initial_balance
    fee_remaining = program_fee_total
    rows: List[ScheduleRow] = []

    for current_date in all_dates:
        if current_date > horizon:
            break

        credits_today, debits_today = ledger_by_date.get(current_date, (0, 0))
        balance += credits_today

        if current_date in cadence_index:
            idx = cadence_index[current_date]
            creditor_pay = payment_amounts[idx] if idx < len(payment_amounts) else 0
            bank_fee = bank_fee_cents if idx < len(payment_amounts) else 0
            mandatory_debit = creditor_pay + bank_fee

            available_for_fee = balance - debits_today - mandatory_debit
            if available_for_fee < 0:
                return None

            fee_here = min(available_for_fee, fee_remaining)
            fee_remaining -= fee_here

            total_debit = debits_today + mandatory_debit + fee_here
            balance -= total_debit

            if creditor_pay > 0 or fee_here > 0 or bank_fee > 0:
                rows.append(ScheduleRow(
                    date=current_date,
                    creditor_payment_cents=creditor_pay,
                    program_fee_cents=fee_here,
                    bank_fee_cents=bank_fee,
                    balance_cents=balance,
                ))
        else:
            balance -= debits_today
            if balance < 0:
                return None

    if fee_remaining > 0:
        return None

    return rows


def _build_cadence_to_horizon(first_payment_date: date, horizon: date) -> List[date]:
    dates: List[date] = []
    current = first_payment_date
    anchor_day = first_payment_date.day
    end_of_month_anchor = is_end_of_month(first_payment_date)
    while current <= horizon:
        dates.append(current)
        current = advance_one_month(current, anchor_day, end_of_month_anchor)
    return dates


def _resolve_first_payment_date(client: Client, offer: Offer) -> date:
    """
    If first_payment_date is omitted in the offer, default to the last day of
    the month of first_draft_date.
    """
    if offer.first_payment_date is not None:
        return offer.first_payment_date
    return default_first_payment_date(client)


def _try_schedule(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_ledger_entries: Optional[List[LedgerEntry]] = None,
) -> Optional[Tuple[List[ScheduleRow], str]]:
    """
    Try to find a feasible schedule.  Returns (rows, pay_shape) or None.

    We try k from max_k down to 1 to maximise early fee-collection slots.
    Within each k, we try the appropriate shape builder.

    extra_ledger_entries: optional additional credits (for Part 2 testing).
    """
    offer_total = offer.offer_total_cents
    program_fee_total = rules.program_fee_total(offer.original_balance_cents)
    first_pay_date = _resolve_first_payment_date(client, offer)
    horizon = client.horizon

    future_entries = client.future_ledger()
    if extra_ledger_entries:
        future_entries = future_entries + extra_ledger_entries
    future_entries.sort(key=lambda e: e.date)

    cadence_dates = _build_cadence_to_horizon(first_pay_date, horizon)
    if not cadence_dates:
        return None

    for k in range(rules.max_k, 0, -1):
        if k > len(cadence_dates):
            continue

        # --- Choose and build the payment shape ---
        payments: Optional[List[int]] = None
        shape: str = ""

        if rules.even_pays:
            payments = _build_even_payments(offer_total, k, rules)
            shape = "even"
        elif rules.is_ballooning_allowed:
            payments = _build_balloon_payments(offer_total, k, rules)
            shape = "balloon"
        else:
            payments = _build_staircase_payments(offer_total, k, rules)
            shape = "staircase"

        if payments is None:
            continue

        if sum(payments) != offer_total:
            continue

        rows = _schedule_program_fee(
            cadence_dates=cadence_dates,
            payment_amounts=payments,
            program_fee_total=program_fee_total,
            bank_fee_cents=rules.bank_fee_cents,
            future_ledger=future_entries,
            initial_balance=client.current_balance_cents,
            horizon=horizon,
        )

        if rows is not None:
            return rows, shape

    return None


def _compute_lump_sum(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
) -> LumpSum:
    """
    Binary search for the smallest lump sum L that makes a feasible schedule.

    We place the lump on the earliest useful date:
    min(first_payment_date, first future draft date).
    Earlier is weakly better — an earlier credit can cover earlier deficits.
    """
    first_pay_date = _resolve_first_payment_date(client, offer)
    future_entries = client.future_ledger()
    future_dates = [entry.date for entry in future_entries]
    lump_date = min(future_dates + [first_pay_date]) if future_dates else first_pay_date

    lo, hi = 0, max(
        offer.offer_total_cents + rules.program_fee_total(offer.original_balance_cents) + rules.max_k * rules.bank_fee_cents,
        sum(entry.amount_cents for entry in future_entries if entry.type == "debit"),
        1,
    )

    extra = [LedgerEntry(date=lump_date, amount_cents=hi, type="credit")]
    while _try_schedule(client, offer, rules, extra) is None:
        hi *= 2
        extra = [LedgerEntry(date=lump_date, amount_cents=hi, type="credit")]
        if hi > 10_000_000:
            return LumpSum(amount_cents=hi, date=lump_date, within_guardrail=False,
                           reason="Could not find a feasible lump within search bounds")

    while lo < hi:
        mid = (lo + hi) // 2
        extra = [LedgerEntry(date=lump_date, amount_cents=mid, type="credit")]
        if _try_schedule(client, offer, rules, extra) is not None:
            hi = mid
        else:
            lo = mid + 1

    L = lo
    guardrail_limit = round_half_up(0.65 * offer.offer_total_cents)
    within = L <= guardrail_limit
    reason = "" if within else f"Lump sum {L} exceeds 65% of offer_total ({guardrail_limit})"
    return LumpSum(amount_cents=L, date=lump_date, within_guardrail=within, reason=reason)


def _compute_monthly_increment(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
) -> MonthlyIncrement:
    """
    Binary search for the smallest uniform increment X added to every future draft.

    Future drafts = ledger credits dated strictly after as_of_date.
    """
    draft_dates: List[date] = []
    current = client.first_draft_date
    anchor_day = client.first_draft_date.day
    end_of_month_anchor = is_end_of_month(client.first_draft_date)
    while current <= client.last_draft_date:
        if current > client.as_of_date:
            draft_dates.append(current)
        current = advance_one_month(current, anchor_day, end_of_month_anchor)

    n = len(draft_dates)
    if n == 0:
        return MonthlyIncrement(amount_cents=0, num_drafts=0,
                                within_guardrail=False,
                                reason="No future drafts to increment")

    def make_augmented_client(x: int) -> Client:
        new_ledger = []
        for entry in client.ledger:
            if entry.type == "credit" and entry.date in draft_dates:
                new_ledger.append(LedgerEntry(
                    date=entry.date,
                    amount_cents=entry.amount_cents + x,
                    type="credit",
                ))
            else:
                new_ledger.append(entry)
        return Client(
            draft_amount_cents=client.draft_amount_cents + x,
            draft_day=client.draft_day,
            first_draft_date=client.first_draft_date,
            last_draft_date=client.last_draft_date,
            as_of_date=client.as_of_date,
            current_balance_cents=client.current_balance_cents,
            ledger=new_ledger,
        )

    upper = 1
    while _try_schedule(make_augmented_client(upper), offer, rules) is None:
        upper *= 2
        if upper > 10_000_000:
            return MonthlyIncrement(amount_cents=upper, num_drafts=n,
                                    within_guardrail=False,
                                    reason="Could not find a feasible increment within search bounds")

    lo, hi = 0, upper
    while lo < hi:
        mid = (lo + hi) // 2
        aug = make_augmented_client(mid)
        if _try_schedule(aug, offer, rules) is not None:
            hi = mid
        else:
            lo = mid + 1

    X = lo
    draft_amt = client.draft_amount_cents
    guardrail_limit = max(10000, round_half_up(0.40 * draft_amt))
    within = X <= guardrail_limit
    reason = "" if within else (
        f"Increment {X} exceeds max({10000}, 40% of draft_amount={guardrail_limit})"
    )
    return MonthlyIncrement(amount_cents=X, num_drafts=n, within_guardrail=within, reason=reason)



def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """
    Main entry point.

    Part 1: attempt to find a feasible schedule.
    Part 2: if infeasible, compute lump-sum and monthly-increment minima.

    Returns a Result that serialises to the spec's JSON shape via .to_dict().
    """
    result = _try_schedule(client, offer, rules)

    if result is not None:
        rows, shape = result
        return Result(
            feasible=True,
            pay_shape_used=shape,
            schedule=rows,
            additional_funds=None,
        )

    # --- Part 2: compute minimum additional funds ---
    lump = _compute_lump_sum(client, offer, rules)
    increment = _compute_monthly_increment(client, offer, rules)

    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=AdditionalFunds(lump_sum=lump, monthly_increment=increment),
    )