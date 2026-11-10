"""Turns Middesk review tasks into a RECOMMENDATION. A human always makes the final call.

Why recommendations and not auto-decisions? Banks need a person (often BSA/AML staff)
to own declines and sanctions hits. The app saves reviewers time; it does not replace them.

Edit RULES to match your institution's KYB policy. Check task keys against
Middesk's review-task docs for your account.
"""

from __future__ import annotations

WAITING_STATUSES = {"open", "pending", "in_audit"}

RULES = {
    # Matched on the task "key", which Middesk documents per task (see docs.middesk.com).
    # A failure on any of these means "likely decline" (a person confirms).
    "hard_stop_keys": {"tin", "sos_not_found", "sos_inactive", "sos_match"},
    # Any non-success on these means "needs review" with HIGH priority (e.g. sanctions).
    "escalate_keys": {"watchlist"},
    # Treat a missing TIN as needing review.
    "require_tin": True,
    # An owner at or above this percentage must be listed (beneficial ownership rule).
    "owner_threshold_pct": 25,
    # Listed owners adding up to less than this may mean someone with 25%+ is missing.
    "full_ownership_pct": 100,
    # A person must be listed who controls the business.
    "require_control_person": True,
}


RECOMMEND_APPROVE = "recommend_approve"
NEEDS_REVIEW = "needs_review"
RECOMMEND_DECLINE = "recommend_decline"
PENDING = "pending"


def evaluate(business: dict, tin_submitted: bool, people: list | None = None, kyc: list | None = None) -> dict:
    """`people` is the applicant's owners and `kyc` the SIMULATED identity checks.
    With people=None the owner and identity rules are skipped."""
    status = business.get("status")
    if status in WAITING_STATUSES:
        return {
            "recommendation": PENDING,
            "priority": "normal",
            "reasons": ["Middesk is still verifying this business."],
        }

    tasks = ((business.get("review") or {}).get("tasks")) or []
    reasons: list[str] = []
    hard_stop = escalate = soft_issue = False

    for t in tasks:
        key, st = t.get("key") or t.get("category"), t.get("status")
        if st == "success":
            continue
        reasons.append(f"{t.get('label', key)}: {t.get('message', st)} ({st})")
        if key in RULES["escalate_keys"]:
            escalate = True
        elif st == "failure" and key in RULES["hard_stop_keys"]:
            hard_stop = True
        else:
            soft_issue = True

    if RULES["require_tin"] and not tin_submitted:
        reasons.append("No TIN/EIN was submitted, so the IRS match could not run.")
        soft_issue = True

    if not tasks:
        reasons.append("Middesk returned no review tasks.")
        soft_issue = True

    if people is not None:
        if RULES["require_control_person"] and not any(p.get("control_person") for p in people):
            reasons.append("No control person was listed (someone who manages the business).")
            soft_issue = True
        pcts = [p.get("ownership_pct") or 0 for p in people]
        if sum(pcts) < RULES["full_ownership_pct"]:
            reasons.append("Owners with 25% or more may be missing.")
            soft_issue = True
        for c in kyc or []:
            if c.get("status") == "failed":
                reasons.append(f"Identity check (simulated) for {c.get('name')}: {c.get('message')}")
                escalate = True
            elif c.get("status") == "needs_review":
                reasons.append(f"Identity check (simulated) for {c.get('name')}: {c.get('message')}")
                soft_issue = True

    if escalate:
        return {"recommendation": NEEDS_REVIEW, "priority": "high", "reasons": reasons}
    if hard_stop:
        return {"recommendation": RECOMMEND_DECLINE, "priority": "normal", "reasons": reasons}
    if soft_issue:
        return {"recommendation": NEEDS_REVIEW, "priority": "normal", "reasons": reasons}
    return {"recommendation": RECOMMEND_APPROVE, "priority": "normal", "reasons": ["All Middesk checks passed."]}


def evaluate_personal(kyc: list | None) -> dict:
    """Rules for a personal account. `kyc` holds the SIMULATED identity and watchlist checks, one per person.
    Middesk is not involved. A failure or watchlist hit is HIGH priority needs_review, never a decline."""
    reasons: list[str] = []
    escalate = soft_issue = False
    if not kyc:
        return {"recommendation": NEEDS_REVIEW, "priority": "normal", "reasons": ["No identity checks were run."]}
    for c in kyc:
        who = c.get("name")
        if c.get("watchlist"):
            reasons.append(f"Watchlist screen (simulated) for {who}: {c.get('message')}")
            escalate = True
        elif c.get("status") == "failed":
            reasons.append(f"Identity check (simulated) for {who}: {c.get('message')}")
            escalate = True
        elif c.get("status") != "verified":
            reasons.append(f"Identity check (simulated) for {who}: {c.get('message')}")
            soft_issue = True
    if escalate:
        return {"recommendation": NEEDS_REVIEW, "priority": "high", "reasons": reasons}
    if soft_issue:
        return {"recommendation": NEEDS_REVIEW, "priority": "normal", "reasons": reasons}
    return {
        "recommendation": RECOMMEND_APPROVE,
        "priority": "normal",
        "reasons": ["All simulated identity and watchlist checks passed."],
    }


_SEVERITY = {RECOMMEND_APPROVE: 0, NEEDS_REVIEW: 1, RECOMMEND_DECLINE: 2}


def evaluate_combined(business_ev: dict, personal_ev: dict) -> dict:
    """One recommendation for an application that opens business AND personal accounts.
    `business_ev` is evaluate() on the business with its owners; `personal_ev` is evaluate_personal() on the
    individuals. Pending while the business part is pending, otherwise the most serious of the two
    (recommend_decline, then needs_review, then recommend_approve). High priority if either part is high.
    Reasons are prefixed "Business: " and "Personal: ". Still only a recommendation: a person decides."""
    reasons = [f"Business: {r}" for r in business_ev["reasons"]] + [f"Personal: {r}" for r in personal_ev["reasons"]]
    priority = "high" if "high" in (business_ev["priority"], personal_ev["priority"]) else "normal"
    if business_ev["recommendation"] == PENDING:
        recommendation = PENDING
    else:
        recommendation = max(
            (business_ev["recommendation"], personal_ev["recommendation"]), key=lambda r: _SEVERITY.get(r, 1)
        )
    return {"recommendation": recommendation, "priority": priority, "reasons": reasons}
