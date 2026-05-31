from datetime import date, timedelta
import pytest

from feasibility.engine import evaluate_offer
from feasibility.models import Client, Offer, CreditorRules, LedgerEntry


def make_client(
    first_draft_date: date,
    as_of: date,
    current_balance: int,
    draft_amount: int = 10000,
    num_drafts: int = 6,
    extra_entries: list[LedgerEntry] = None
) -> Client:
    """Helper to build a client with a standard monthly draft schedule."""
    ledger = []
    # Build draft schedule
    current = first_draft_date
    for _ in range(num_drafts):
        ledger.append(LedgerEntry(date=current, amount_cents=draft_amount, type="credit"))
        # Advance by one month
        month = current.month + 1
        year = current.year
        if month > 12:
            month = 1
            year += 1
        current = date(year, month, min(first_draft_date.day, 28)) # safe day clamping
    
    if extra_entries:
        ledger.extend(extra_entries)
        
    ledger.sort(key=lambda e: e.date)
    last_draft = max(e.date for e in ledger if e.type == "credit")
    
    return Client(
        draft_amount_cents=draft_amount,
        draft_day=first_draft_date.day,
        first_draft_date=first_draft_date,
        last_draft_date=last_draft,
        as_of_date=as_of,
        current_balance_cents=current_balance,
        ledger=ledger,
    )


# =====================================================================
# 1. EVEN PAYS REMAINDER DISTRIBUTION TESTS
# =====================================================================

def test_even_pays_remainder_distribution_1():
    # Remainder = 1: distributed to the last payment to keep it non-decreasing
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000)
    o = Offer(creditor="A", creditor_balance_cents=10001, original_balance_cents=10001, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=3, max_payments=3, min_payment_cents=1000, max_token_pays=3,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    assert payments == [3333, 3334, 3334]


def test_even_pays_remainder_distribution_2():
    # Remainder = 2: distributed to the last two payments to keep it non-decreasing
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000)
    o = Offer(creditor="A", creditor_balance_cents=10002, original_balance_cents=10002, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=3, max_payments=3, min_payment_cents=1000, max_token_pays=3,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    assert payments == [3334, 3334, 3334]


# =====================================================================
# 2. TOKEN PAY LIMITATION TESTS
# =====================================================================

def test_token_pay_limit_exceeded_forces_higher_payments():
    # max_token_pays = 2. With k=4, remaining payments must strictly exceed min_payment_cents
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000)
    o = Offer(creditor="A", creditor_balance_cents=10002, original_balance_cents=10000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=4, max_payments=4, min_payment_cents=2500, max_token_pays=2,
        min_payment_tiers=[], even_pays=False, is_ballooning_allowed=False,
        max_segments=2, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    # First 2 are token pays at 2500. Next 2 must strictly exceed 2500.
    assert payments[0] == 2500
    assert payments[1] == 2500
    assert payments[2] > 2500
    assert payments[3] > 2500


def test_zero_token_pays_limit():
    # max_token_pays = 0 means NO payment can be exactly min_payment_cents
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000)
    o = Offer(creditor="A", creditor_balance_cents=6000, original_balance_cents=6000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=2, max_payments=2, min_payment_cents=2500, max_token_pays=0,
        min_payment_tiers=[], even_pays=False, is_ballooning_allowed=False,
        max_segments=2, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    # Both payments must strictly exceed 2500
    assert all(p > 2500 for p in payments)


# =====================================================================
# 3. TIERED MINIMUMS & FLOOR INTERACTIONS
# =====================================================================

def test_multiple_min_payment_tiers():
    # Two tiers: [2, 3000] and [4, 5000]
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=100000)
    o = Offer(creditor="A", creditor_balance_cents=24000, original_balance_cents=24000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=5, max_payments=5, min_payment_cents=1000, max_token_pays=5,
        min_payment_tiers=[(2, 3000), (4, 5000)], even_pays=False, is_ballooning_allowed=False,
        max_segments=3, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    # Check floors: p1 >= 1000, p2 >= 3000, p3 >= 3000, p4 >= 5000, p5 >= 5000
    assert payments[0] >= 1000
    assert payments[1] >= 3000
    assert payments[2] >= 3000
    assert payments[3] >= 5000
    assert payments[4] >= 5000


# =====================================================================
# 4. HORIZON BOUNDARY TESTS
# =====================================================================

def test_horizon_strictly_before_first_payment():
    # If horizon is before first payment date, schedule is impossible
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000, num_drafts=1)
    # Horizon is 2026-01-01. First payment is 2026-01-31.
    o = Offer(creditor="A", creditor_balance_cents=5000, original_balance_cents=5000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=1000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is False


def test_first_payment_date_equals_horizon():
    # If first payment is exactly on horizon, a single-payment schedule is still possible
    first = date(2026, 1, 1)
    c = make_client(first, date(2025, 12, 30), current_balance=5000, num_drafts=1)
    o = Offer(creditor="A", creditor_balance_cents=1000, original_balance_cents=1000, settlement_pct=1.0, first_payment_date=first)
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=1000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    assert len(r.schedule) == 1


# =====================================================================
# 5. BANK FEE & FEE-ONLY CADENCE MONTH TESTS
# =====================================================================

def test_bank_fee_not_charged_on_fee_only_months():
    # k = 1. Cadence dates exist up to horizon. Fee is collected on Month 2.
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=0, draft_amount=12000, num_drafts=3)
    o = Offer(creditor="A", creditor_balance_cents=10000, original_balance_cents=10000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    # Program fee = 5000. Month 1 pays 10000 creditor + 500 bank fee. Month 2 has program fee.
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=10000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=500, program_fee_pct=0.5
    ))
    assert r.feasible is True
    assert len(r.schedule) == 2
    # Month 1 has bank fee
    assert r.schedule[0].bank_fee_cents == 500
    assert r.schedule[0].creditor_payment_cents == 10000
    # Month 2 is fee-only: no bank fee
    assert r.schedule[1].bank_fee_cents == 0
    assert r.schedule[1].creditor_payment_cents == 0
    assert r.schedule[1].program_fee_cents > 0


# =====================================================================
# 6. PROGRAM FEE COMPLIANCE TESTS
# =====================================================================

def test_no_program_fee_before_first_creditor_payment():
    # Program fee cannot land before the first payment date
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000, num_drafts=3)
    o = Offer(creditor="A", creditor_balance_cents=10000, original_balance_cents=10000, settlement_pct=1.0, first_payment_date=date(2026, 2, 28))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=2, max_payments=2, min_payment_cents=5000, max_token_pays=2,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.2
    ))
    assert r.feasible is True
    # First cadence date is 2026-02-28. Ensure no row exists in schedule before this.
    assert r.schedule[0].date >= date(2026, 2, 28)


# =====================================================================
# 7. SAME-DAY ORDERING (CREDITS BEFORE DEBITS) TESTS
# =====================================================================

def test_same_day_ordering_prevents_overdraft():
    # Draft and payment fall on the same day. Start with 0.
    # Jan 1: +5000 draft. Jan 1: -5000 payment. 
    # If credit first: balance stays non-negative (0). If debit first: balance goes -5000.
    ledger = [LedgerEntry(date=date(2026, 1, 1), amount_cents=5000, type="credit")]
    c = Client(
        draft_amount_cents=5000, draft_day=1, first_draft_date=date(2026, 1, 1),
        last_draft_date=date(2026, 1, 1), as_of_date=date(2025, 12, 31),
        current_balance_cents=0, ledger=ledger
    )
    o = Offer(creditor="A", creditor_balance_cents=5000, original_balance_cents=5000, settlement_pct=1.0, first_payment_date=date(2026, 1, 1))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=5000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    assert r.schedule[0].balance_cents == 0


# =====================================================================
# 8. PART 2 GUARDRAIL BOUNDARY TESTS
# =====================================================================

def test_lump_sum_within_guardrail():
    # Lump sum L = 5000. Offer total = 10000. Guardrail is 65% of 10000 = 6500.
    # L <= 6500 -> within_guardrail = True
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=5000, draft_amount=0, num_drafts=6)
    o = Offer(creditor="A", creditor_balance_cents=10000, original_balance_cents=10000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=10000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is False
    assert r.additional_funds.lump_sum.amount_cents == 5000
    assert r.additional_funds.lump_sum.within_guardrail is True


def test_lump_sum_outside_guardrail():
    # Lump sum L = 9000. Offer total = 10000. Guardrail is 65% of 10000 = 6500.
    # L > 6500 -> within_guardrail = False
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=1000, draft_amount=0, num_drafts=6)
    o = Offer(creditor="A", creditor_balance_cents=10000, original_balance_cents=10000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=10000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is False
    assert r.additional_funds.lump_sum.amount_cents == 9000
    assert r.additional_funds.lump_sum.within_guardrail is False
    assert "exceeds 65%" in r.additional_funds.lump_sum.reason


def test_monthly_increment_within_guardrail():
    # Increment X = 1000. Draft amount = 10000.
    # Guardrail limit is max(10000, 40% of 10000) = 10000.
    # X <= 10000 -> within_guardrail = True
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=0, draft_amount=10000, num_drafts=6)
    o = Offer(creditor="A", creditor_balance_cents=22000, original_balance_cents=22000, settlement_pct=1.0, first_payment_date=date(2026, 2, 28))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=22000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is False
    assert r.additional_funds.monthly_increment.amount_cents == 1000  # draft lands twice (Jan 1, Feb 1), need 2000 total -> X = 1000
    assert r.additional_funds.monthly_increment.within_guardrail is True


def test_monthly_increment_outside_guardrail():
    # Draft amount = 2000. Limit is max(10000, 800) = 10000.
    # Increment required is 18000.
    # X > 10000 -> within_guardrail = False
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=0, draft_amount=2000, num_drafts=6)
    o = Offer(creditor="A", creditor_balance_cents=20000, original_balance_cents=20000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=20000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is False
    assert r.additional_funds.monthly_increment.amount_cents == 18000
    assert r.additional_funds.monthly_increment.within_guardrail is False
    assert "exceeds max" in r.additional_funds.monthly_increment.reason


# =====================================================================
# 9. STAIRCASE SEGMENT COUNT AND PLACEMENT TESTS
# =====================================================================

def test_staircase_strictly_respects_max_segments():
    # max_segments = 2, k = 6. Builders should never produce 3 distinct levels.
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=100000)
    o = Offer(creditor="A", creditor_balance_cents=20000, original_balance_cents=20000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=6, max_payments=6, min_payment_cents=1000, max_token_pays=6,
        min_payment_tiers=[], even_pays=False, is_ballooning_allowed=False,
        max_segments=2, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    distinct_levels = len(set(payments))
    assert distinct_levels <= 2


def test_staircase_max_segments_1_acts_like_even_pays():
    # max_segments = 1 means all payments must be equal (or as equal as possible)
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=100000)
    o = Offer(creditor="A", creditor_balance_cents=10001, original_balance_cents=10001, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=3, max_payments=3, min_payment_cents=1000, max_token_pays=3,
        min_payment_tiers=[], even_pays=False, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    assert payments == [3333, 3334, 3334]


# =====================================================================
# 10. EXTRA EDGE CASES (TOTAL 20 DIVERSE SCENARIOS)
# =====================================================================

def test_balloon_with_no_balloon_space():
    # Offer total = 5000. k = 2. min_payment = 2500.
    # Regular payment 1 floor = 2500. Balloon remainder = 5000 - 2500 = 2500.
    # Balloon = last payment floor = 2500. This is allowed since balloon >= last floor.
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000)
    o = Offer(creditor="A", creditor_balance_cents=5000, original_balance_cents=5000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=2, max_payments=2, min_payment_cents=2500, max_token_pays=2,
        min_payment_tiers=[], even_pays=False, is_ballooning_allowed=True,
        max_segments=2, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    assert payments == [2500, 2500]


def test_balloon_strictly_greater_than_regular():
    # Regular payment floor = 2500. Balloon = 10000. 10000 >= 2500 is allowed.
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000)
    o = Offer(creditor="A", creditor_balance_cents=12500, original_balance_cents=12500, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=2, max_payments=2, min_payment_cents=2500, max_token_pays=2,
        min_payment_tiers=[], even_pays=False, is_ballooning_allowed=True,
        max_segments=2, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    assert payments == [2500, 10000]


def test_balloon_not_allowed_but_balloon_requested():
    # If is_ballooning_allowed is False, even if we try a balloon-like staircase,
    # it must satisfy segment limits and staircase floors.
    # Here k=3, max_segments=2. [2500, 2500, 10000] has 2 segments ([2500, 2500] and [10000]).
    # So staircase builder will naturally generate it if it's the best!
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000)
    o = Offer(creditor="A", creditor_balance_cents=15000, original_balance_cents=15000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=3, max_payments=3, min_payment_cents=2500, max_token_pays=3,
        min_payment_tiers=[], even_pays=False, is_ballooning_allowed=False,
        max_segments=2, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    assert payments == [2500, 2500, 10000]


def test_no_future_drafts_to_increment():
    # If all drafts are already past (as_of_date is after the last draft),
    # then no future drafts exist. Increment num_drafts should be 0.
    # last draft is 2026-01-01. as_of is 2026-01-02.
    c = make_client(date(2026, 1, 1), date(2026, 1, 2), current_balance=0, num_drafts=1)
    o = Offer(creditor="A", creditor_balance_cents=10000, original_balance_cents=10000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=10000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is False
    assert r.additional_funds.monthly_increment.num_drafts == 0
    assert r.additional_funds.monthly_increment.within_guardrail is False


def test_balance_hits_exactly_zero():
    # Feasible schedule where running balance hits exactly 0. Allowed.
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=0, draft_amount=10000, num_drafts=6)
    o = Offer(creditor="A", creditor_balance_cents=10000, original_balance_cents=10000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=10000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    assert r.schedule[0].balance_cents == 0


def test_offer_balance_exceeds_draft_capacity():
    # Strictly infeasible offer because even with all future drafts, we don't have enough.
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=0, draft_amount=1000, num_drafts=3)
    o = Offer(creditor="A", creditor_balance_cents=20000, original_balance_cents=20000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=3, max_payments=3, min_payment_cents=1000, max_token_pays=3,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is False
    assert r.additional_funds.lump_sum.amount_cents > 0
    assert r.additional_funds.monthly_increment.amount_cents > 0


def test_committed_ledger_debits_respected():
    # If the ledger contains pre-existing committed future debits,
    # they must be paid first and are fixed.
    extra = [LedgerEntry(date=date(2026, 1, 15), amount_cents=5000, type="debit")]
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=0, draft_amount=10000, num_drafts=6, extra_entries=extra)
    o = Offer(creditor="A", creditor_balance_cents=5000, original_balance_cents=5000, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=1, max_payments=1, min_payment_cents=5000, max_token_pays=1,
        min_payment_tiers=[], even_pays=True, is_ballooning_allowed=False,
        max_segments=1, bank_fee_cents=0, program_fee_pct=0.0
    ))
    # Jan 1: +10000 draft. Jan 15: -5000 committed debit. Jan 31: -5000 creditor pay.
    # Total debits = 10000. Balance ends at exactly 0.
    assert r.feasible is True
    assert r.schedule[0].balance_cents == 0


def test_balloon_with_min_tier_respected():
    # Tier on position 2: min 4000.
    # Position 1 floor: 2500. Position 2 floor (balloon): 4000.
    # Offer total = 6500. Balloon = 6500 - 2500 = 4000.
    # Balloon matches the tier floor exactly, so it is valid!
    c = make_client(date(2026, 1, 1), date(2025, 12, 31), current_balance=50000)
    o = Offer(creditor="A", creditor_balance_cents=6500, original_balance_cents=6500, settlement_pct=1.0, first_payment_date=date(2026, 1, 31))
    r = evaluate_offer(c, o, CreditorRules(
        max_terms=2, max_payments=2, min_payment_cents=2500, max_token_pays=2,
        min_payment_tiers=[(2, 4000)], even_pays=False, is_ballooning_allowed=True,
        max_segments=2, bank_fee_cents=0, program_fee_pct=0.0
    ))
    assert r.feasible is True
    payments = [row.creditor_payment_cents for row in r.schedule]
    assert payments == [2500, 4000]
