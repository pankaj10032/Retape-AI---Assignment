```markdown
# Settlement Feasibility & Fee Engine — Take-home

Welcome, and thanks for taking the time. The full problem is in
[`ASSIGNMENT.md`](./ASSIGNMENT.md). This README is just orientation.

## The task in one line

Given a client's escrow account, a settlement offer, and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or — if not — compute the minimum extra funding needed.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Layout

```
hiring_takehome/
├── ASSIGNMENT.md            # full specification — read this
├── feasibility/
│   ├── models.py            # data models, JSON loaders, date/EOM helpers (provided)
│   └── engine.py            # >>> implement evaluate_offer here <<< (+ Result shape)
├── cases/                   # four example cases (client.json / offer.json / creditor_rules.json)
│   ├── case1_feasible_even
│   ├── case2_infeasible_minima
│   ├── case3_balloon
│   └── case4_tiers
├── tests/
│   ├── test_smoke.py        # scaffolding sanity tests (pass out of the box)
│   └── test_cases.py        # example expectations — make these pass, then add your own
├── run.py                   # python run.py cases/<case>
└── requirements.txt
```

## Run

```bash
# evaluate a single case (prints the Result as JSON)
python run.py cases/case1_feasible_even

# tests
pytest -q
```

Out of the box, `tests/test_smoke.py` passes and `tests/test_cases.py` fails —
the latter is your target. Go beyond those four cases with your own tests.

## What to submit

Your implementation, your tests, and a short README section describing:
- your approach and the alternatives you considered,
- **your interpretation of the payment shapes** (even / staircase / balloon — we
  left these loosely defined on purpose),
- assumptions you made, and known edge cases / limitations.

Budget ~5–6 hours. Prefer a correct, well-tested core over breadth. When in
doubt, write down your assumption and keep going.

---

## Approach

### High-level pipeline

`evaluate_offer(client, offer, rules)` works in three stages:

1. **Shape Builder** — produce a list of `k` creditor-payment amounts that
   satisfies all hard constraints: exact sum, floor rules, non-decreasing order,
   segment cap, and the even/balloon flags.

2. **Fee Scheduler** — given fixed payment amounts and their cadence dates,
   greedily collect the program fee as early as possible (front-load). On each
   cadence date we compute `available = balance_after_credits − mandatory_debit`
   and collect `min(available, fee_remaining)`. This is optimal because a
   dollar collected today is strictly better than tomorrow, and we never take
   more than the ledger can afford.

3. **Simulator** — walk the ledger chronologically, applying credits before
   debits on each date. If the balance ever goes negative, or if fee remains
   uncollected past the horizon, the schedule is rejected.

When no schedule is feasible, we compute two independent minima:

- **Lump sum** — binary-search the smallest credit `L` placed on the earliest
  useful date (the earlier of `first_payment_date` and the first future draft).
  Earlier is weakly dominant: the same `L` placed later can never help more.

- **Monthly increment** — binary-search the smallest uniform bump `X` added to
  every future draft (all calendar drafts strictly after `as_of_date`). We
  rebuild the ledger with augmented drafts and test feasibility.

### Why this approach

- **Exhaustive but bounded**: we try `k` from `max_k` down to `1`. For each `k`
  we generate at most one candidate shape (the one that minimizes early
  outflows). This keeps the search polynomial while still exploring the full
  space of valid shapes.

- **Front-loading is greedy-optimal**: once payment amounts are fixed, the only
  remaining decision is how to split the program fee across dates. Collecting
  as much as possible on each date in order is optimal because it never makes
  a later date harder to satisfy.

- **Binary search for minima**: both Part-2 searches are monotonic (more money
  can only help), so binary search finds the exact minimum efficiently.

---

## Interpretation of Payment Shapes

The spec deliberately leaves the shape open-ended. Our interpretation:

### `even_pays = true`

All `k` payments are equal. When `offer_total` is not divisible by `k`, we
distribute remainder cents onto the **latest** payments so the sequence stays
non-decreasing (e.g., 100 over 3 → `[33, 33, 34]`). We choose the largest `k`
that yields a valid schedule, because more dates = more slots to front-load the
program fee.

### `is_ballooning_allowed = true` (and `even_pays = false`)

We make the first `(k−1)` payments as small as the floors allow (all at their
minimum valid value), then let the final payment absorb the remaining balance.
This is the natural outcome of the "front-load fee" objective: minimum early
creditor payments leave maximum cash for fee collection. We try `k` from `max_k`
down to `1` and pick the first feasible.

### Staircase (neither flag set)

We allow at most `max_segments` distinct payment levels. We look at every way to split the `k` payments into at most `max_segments` segments. For each split:

1. We find the lowest possible constant value for each of the early segments. This keeps early creditor outlays small.
2. For the final segment, we divide the remaining offer balance equally among the remaining payments. If there is a remainder, we distribute the extra cents to the latest payments. This ensures the payments are in ascending order and sum up exactly to the offer total.
3. We run each candidate sequence through our floor and token pay checkers to make sure every rule is satisfied.

To get the best possible schedule for fee collection, we gather all valid shapes across all possible splits and sort them lexicographically. We choose the first one. This ensures we select the staircase schedule that keeps early payments as low as possible.

### Token pays & tiers interaction

- A "token pay" is counted only when a payment **both** equals the base minimum
  **and** the applicable floor at that position is exactly the base minimum.
  This prevents overcounting when a tier or exhausted token budget forces the
  floor higher.
- Tiers are applied per-payment-index: once a tier's `from_payment_number` is
  reached, every subsequent payment in that segment must be at least the tier
  value.

---

## Assumptions

1. **Draft schedule is calendar-derived**: drafts land on `draft_day` every
   month from `first_draft_date` through `last_draft_date` inclusive. The ledger
   credits are the drafts; we normalize the client to back-fill any missing
   draft entries so that Part-2 increment logic is robust.

2. **Cadence independence**: payment dates follow their own end-of-month or
   day-of-month rule, completely independent of the draft schedule. We simulate
   both sets of dates together, sorted chronologically.

3. **Same-day ordering**: on any date, all credits (drafts, lump sums) are
   applied before any debits (fixed debits, creditor payments, bank fees,
   program fees).

4. **Fee-only months**: a cadence date carrying no creditor payment may still
   carry program fee, but incurs **no** bank fee. Our simulator naturally
   handles this because `bank_fee` is conditional on the presence of a creditor
   payment.

5. **Rounding**: all `round(...)` uses round-half-up (0.5 always away from
   zero). We implement this explicitly via `math.floor(x + 0.5)` rather than
   relying on Python's banker's-rounding `round()`.

6. **Lump sum placement**: we place the lump on the earliest useful date. The
   spec allows any date ≤ horizon; we choose the earliest because it is weakly
   dominant (earlier cash can cover earlier deficits).

7. **Monthly increment scope**: "every future draft" means every calendar draft
   date strictly after `as_of_date`, not just the ones already present in the
   ledger. This ensures consistency when the ledger is sparse.

---

## Known Edge Cases & Limitations

| Edge Case | Handling |
|-----------|----------|
| **Horizon before first payment** | If `first_payment_date > horizon`, no cadence dates exist; schedule is infeasible. |
| **Floors exceed offer_total** | Detected in shape builders; returns `None` immediately. |
| **Fee cannot be collected before first payment** | Enforced by the simulator: fee collection only happens on cadence dates ≥ first_payment_date. |
| **Balance hits exactly zero** | Allowed; the simulator checks `balance >= 0`, not `> 0`. |
| **Committed debits on payment dates** | Handled correctly because fixed ledger debits are applied before creditor payments on the same date. |
| **Empty `min_payment_tiers`** | Treated as no tier constraints; floor is just base minimum + token-pay rule. |
| **`max_segments` with `k=1`** | Ignored; a single payment trivially uses 1 segment. |
| **Guardrail rejection** | Reported as `within_guardrail: false` with a reason string; the minima are still computed and reported. |

### Limitations

- **Performance**: the staircase builder enumerates all compositions of `k`
  into at most `max_segments` segments. For large `k` (e.g., 24) and
  `max_segments` (e.g., 6), this is still fast (~milliseconds), but it is
  combinatorial. A dynamic-programming approach could scale further if needed.
- **Part-2 minima independence**: the lump sum and monthly increment are
  computed independently; we do not attempt to find a combined optimum.
- **No partial months**: we assume drafts and payments align to whole calendar
  months; intra-month cadences are not supported.
```