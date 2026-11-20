from jobradar import lastmile, promote, refit


def _rec(**kw):
    return {"key": "k", "company": "A", "title": "SE", "issue_number": 5, "status": "new", "score": 6,
            "scored_by": "claude-mcp", "history": [], **kw}


def test_queue_skips_applied_and_frozen():
    d = {"jobs": {"a": _rec(), "b": _rec(status="applied"), "c": _rec(resume_frozen="x")}}
    assert refit.queue(d, "resume changed") == 1 and d["jobs"]["a"]["rescore_pending"]


def test_rescore_to_7_asks_for_a_tailored_resume():
    rec = _rec(rescore_pending={"reason": "r"}, pending_brief="posting")
    d = {"jobs": {"k": rec}}
    assert [r["key"] for r in lastmile.to_score(d)] == ["k"]
    lastmile._apply_scores([{"key": "k", "score": 7, "summary": "s"}], d, 7, [])
    assert rec["retailor"] and rec["kit_pending"] and rec["card_refresh"] and "rescore_pending" not in rec
    assert rec["score_history"][0]["score"] == 6 and [r["key"] for r in lastmile.to_build(d)] == ["k"]


def test_rescore_that_stays_low_just_updates_the_card():
    rec = _rec(rescore_pending={"reason": "r"}, pending_brief="posting")
    lastmile._apply_scores([{"key": "k", "score": 5, "summary": "s"}], {"jobs": {"k": rec}}, 7, [])
    assert rec["score"] == 5 and rec["card_refresh"] and not rec.get("retailor")


def test_extra_experience_gets_a_6_a_tailored_resume_not_a_standard_card():
    rec = {"key": "k", "company": "A", "title": "SE", "queued_at": "x"}
    d = {"jobs": {"k": rec}}
    lastmile._apply_scores([{"key": "k", "score": 6, "summary": "s", "uses_extra_experience": True}], d, 7, [])
    assert rec["force_kit"] and rec["kit_pending"]
    assert promote.auto_cards(d, {"card_min_score": 6, "alert_min_score": 7}, {}, []) == 0
