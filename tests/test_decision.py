from app.decision import evaluate


def biz(*tasks, status="in_review"):
    return {
        "status": status,
        "review": {"tasks": [{"key": k, "label": k, "status": s, "message": f"{k} {s}"} for k, s in tasks]},
    }


def test_pending_while_middesk_works():
    assert evaluate({"status": "pending"}, True)["recommendation"] == "pending"


def test_all_success_recommends_approve():
    r = evaluate(biz(("name", "success"), ("tin", "success"), ("watchlist", "success")), True)
    assert r["recommendation"] == "recommend_approve"


def test_watchlist_hit_is_high_priority_review_not_auto_decline():
    r = evaluate(biz(("watchlist", "failure"), ("tin", "failure")), True)
    assert r["recommendation"] == "needs_review" and r["priority"] == "high"


def test_tin_failure_recommends_decline():
    assert evaluate(biz(("tin", "failure")), True)["recommendation"] == "recommend_decline"


def test_address_warning_needs_review():
    assert (
        evaluate(biz(("address_verification", "warning"), ("tin", "success")), True)["recommendation"] == "needs_review"
    )


def test_missing_tin_needs_review():
    r = evaluate(biz(("name", "success")), tin_submitted=False)
    assert r["recommendation"] == "needs_review"
    assert any("TIN" in x for x in r["reasons"])


def test_inactive_sos_recommends_decline():
    r = evaluate(biz(("sos_inactive", "failure"), ("tin", "success")), True)
    assert r["recommendation"] == "recommend_decline"


def test_unknown_sos_status_only_needs_review():
    r = evaluate(biz(("sos_unknown", "warning"), ("tin", "success")), True)
    assert r["recommendation"] == "needs_review"
