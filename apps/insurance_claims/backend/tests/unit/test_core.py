"""Deterministic tests: no LLM calls. Run: pytest tests/unit -q"""
from datetime import date

import pytest

from app.data.docs_matcher import canonical_document
from app.data.followup_rules import followup_guidance
from app.data.repository import get_repo
from app.nlu import patterns
from app.nlu.normalize import name_matches, norm_date, norm_email, norm_phone
from app.sop.guards import check_reply
from app.sop.intent import resolve_case
from app.sop.phases import Phase
from app.sop.state import SessionState, TransitionError
from app.sop.verification import evaluate, poll_consent, rep_on_file

repo = get_repo()
TODAY = date(2026, 3, 5)
DEMO = ("I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare "
        "claim from January. DOB is 1985-03-15, SSN last four is 4472.")


# ---- normalisation -------------------------------------------------------------
@pytest.mark.parametrize("raw", ["1985-03-15", "3/15/1985", "03/15/85", "March 15, 1985", "15 March 1985", "Mar 15th 1985"])
def test_dob_formats(raw):
    assert norm_date(raw) == "1985-03-15"


def test_phone_email():
    assert norm_phone("(650) 521-2836") == "+16505212836"
    assert norm_phone("+1 650.521.2836") == "+16505212836"
    assert norm_email("Margaret at email dot com") == "margaret@email.com"


def test_names_aliases_and_order():
    assert name_matches("tian ma", "Ma Tian")
    assert name_matches("Yaven Li", "Ya Wen Li", ["Yaven Li"])
    assert name_matches("Margaret L Chen", "Margaret Chen")
    assert not name_matches("Ava Chen", "Margaret Chen")


def test_regex_preextraction_demo():
    p = patterns.extract(DEMO, TODAY)
    assert p["dob"] == "1985-03-15" and p["id_last4"] == "4472" and p["policy_number"] == "POL-9921"
    assert p["case_type"] == "healthcare" and p["case_status"] == "denied" and p["case_month"] == 1


# ---- verification ----------------------------------------------------------------
def test_three_factors_verify():
    r = evaluate(repo, {"name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}, "POL-9921")
    assert r.status == "verified" and r.party_id == "P9"


def test_two_factors_incomplete():
    assert evaluate(repo, {"name": "Margaret Chen", "dob": "1985-03-15"}).status == "incomplete"


def test_wrong_detail_fails_without_identifying_party():
    r = evaluate(repo, {"name": "Margaret Chen", "dob": "1985-03-16", "id_last4": "4472"})
    assert r.status == "failed" and r.party_id is None and "dob" in r.mismatched


def test_policy_number_contradiction_blocks():
    r = evaluate(repo, {"name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}, "POL-1044")
    assert r.status == "failed"


def test_national_id_alias_and_reordered_name():
    assert evaluate(repo, {"name": "Tian Ma", "dob": "1964-09-10", "id_last4": "6688"}).party_id == "P12"
    assert evaluate(repo, {"name": "Yaven Li", "dob": "1989-12-03", "email": "yawen.li@example.com"}).party_id == "P13"


def test_representative_and_consent():
    assert rep_on_file(repo, "P9", "David Chen") and rep_on_file(repo, "P9", "David")
    assert not rep_on_file(repo, "P9", "Kevin Chen")
    assert [poll_consent(repo, "default", i) for i in range(3)] == ["pending", "approved", "approved"]
    assert poll_consent(repo, "timeout", 5) == "timeout"


# ---- case resolution ---------------------------------------------------------------
def test_january_ambiguity_resolved_by_status():
    cases = repo.cases_for("P9")
    amb = resolve_case(cases, {"case_type": "healthcare", "month": 1})
    assert amb.kind == "ambiguous" and {c["case_id"] for c in amb.candidates} == {"CL-2048", "CL-2011"}
    assert resolve_case(cases, {"case_type": "healthcare", "month": 1, "status": "denied"}).case["case_id"] == "CL-2048"


def test_implied_status_from_intent():
    assert resolve_case(repo.cases_for("P9"), {}, soft_status="denied").case["case_id"] == "CL-2048"


def test_foreign_case_id_never_resolves():
    r = resolve_case(repo.cases_for("P9"), {"case_id": "CL-3001"})  # belongs to P12
    assert r.kind != "unique" or r.case["party_id"] == "P9"
    assert repo.case("P9", "CL-3001") is None


def test_no_match_relaxes():
    r = resolve_case(repo.cases_for("P9"), {"case_type": "auto", "status": "denied"})
    assert r.kind == "none" and r.relaxed == ["status"] and r.candidates[0]["case_id"] == "CL-2102"


# ---- guideline engine ---------------------------------------------------------------
def test_document_canonicalisation():
    keys = list(repo.guidelines["document_guidance"])
    assert canonical_document("pathology report", keys) == "original pathology report"
    assert canonical_document("office note", keys) == "treating provider office note"
    assert canonical_document("photos", keys) == "supplemental accident scene photos"
    assert canonical_document("diagnosis report", keys) is None


def test_followup_rules_fill_templates():
    case = repo.case("P9", "CL-2048")
    g = followup_guidance(repo, case, "How soon do I need to submit them?", "document_submission")
    timing = next(m for m in g["matched"] if m["topic"] == "submission_timing")
    assert "within a week" in timing["guidance"] and "the pathology report and the office note" in timing["guidance"]
    g2 = followup_guidance(repo, case, "how long does it take after I send them", None)
    assert any("less than a week" in m["guidance"] for m in g2["matched"])
    g3 = followup_guidance(repo, repo.case("P9", "CL-2102"), "how long does it take", None)  # no docs needed
    assert not g3["matched"] and "fallback" in g3


def test_deadline_math():
    sheet = repo.fact_sheet(repo.case("P9", "CL-2048"), TODAY)
    assert sheet["days_until_appeal_deadline"] == 13 and not sheet["appeal_deadline_passed"]


# ---- output guard -------------------------------------------------------------------
def test_pre_verification_disclosure_blocked():
    v = check_reply("Your claim CL-2048 needs a pathology report.", repo=repo, verified=False,
                    allowed_facts={}, caller_text="hi, I'm calling about my denied claim", party=None)
    assert v and any("cl-2048" in x for x in v)


def test_caller_words_are_not_disclosure():
    assert check_reply("I've noted you're calling about your denied healthcare claim from January.", repo=repo,
                       verified=False, allowed_facts={}, caller_text=DEMO, party=None) == []


def test_grounding_catches_invented_amount_and_date():
    sheet = repo.fact_sheet(repo.case("P9", "CL-2048"), TODAY)
    ok = check_reply("The allowed maximum was $1,450.00 and the deadline is March 18, 2026.", repo=repo,
                     verified=True, allowed_facts=sheet, caller_text="", party=repo.party("P9"))
    bad = check_reply("You'll get $900.00 by April 2.", repo=repo, verified=True, allowed_facts=sheet,
                      caller_text="", party=repo.party("P9"))
    assert ok == [] and len(bad) == 2


def test_pii_echo_blocked():
    v = check_reply("I'll text you at 6505212836.", repo=repo, verified=True, allowed_facts={}, caller_text="",
                    party=repo.party("P9"))
    assert any("PII" in x for x in v)


# ---- state machine gates -------------------------------------------------------------
def test_gates_cannot_be_skipped():
    s = SessionState()
    with pytest.raises(TransitionError):
        s.transition(Phase.RESOLVE_INTENT, "try", [])          # not verified
    with pytest.raises(TransitionError):
        s.transition(Phase.PROCESS_CASE, "try", [])            # illegal edge
    s.verification.verified = True
    s.verification.consent_status = "pending"
    with pytest.raises(TransitionError):
        s.transition(Phase.RESOLVE_INTENT, "try", [])          # consent gate


def test_memory_defers_future_phase_slots():
    s = SessionState()
    s.memory.put("case.status", "denied", 1, Phase.VERIFY_ID, "llm")
    s.memory.put("identity.dob", "1985-03-15", 1, Phase.VERIFY_ID, "llm")
    assert s.memory.slots["case.status"].status == "deferred"
    assert s.memory.slots["identity.dob"].status == "active"


def test_no_invented_commitments():
    from app.sop.guards import unauthorized_commitments
    assert unauthorized_commitments("A claims representative will reach out shortly.", set())
    assert unauthorized_commitments("I'll set that up for you.", set())
    assert not unauthorized_commitments("You can upload it in the member portal, or send it by fax or mail.", set())
    assert not unauthorized_commitments("Would you like me to connect you with a representative?", set())
    # legitimate when the step really performs the action
    assert not unauthorized_commitments("A representative will follow up, reference HX-1.", {"handoff"})


# ---- reply layout --------------------------------------------------------------------
def test_tidy_splits_long_paragraph_and_isolates_question():
    from app.sop.formatting import tidy
    raw = ("Your healthcare claim CL-2048 was denied on January 12, 2026 because the review file was missing two documents. "
           "You have until March 18, 2026 to appeal or resubmit them, so there is still time. The pathology report is the "
           "first one needed and the office note is the second. Would you like help getting them?")
    out = tidy(raw)
    paras = out.split("\n\n")
    assert len(paras) >= 3 and paras[-1] == "Would you like help getting them?"
    assert all(len(p.split(". ")) <= 2 for p in paras[:-1])
    assert "**CL-2048**" in out and "**January 12, 2026**" in out and "**March 18, 2026**" in out


def test_tidy_is_idempotent_and_keeps_authored_structure():
    from app.sop.formatting import tidy
    authored = "Here is what I need:\n\n- Date of birth\n- Phone number\n\nWhich would you like to share?"
    assert tidy(authored) == authored
    once = tidy("Claim CL-2048 is open and the expected payment is $3,200.00 as of March 5, 2026.")
    assert tidy(once) == once and "**$3,200.00**" in once


def test_tidy_does_not_split_on_abbreviations_or_amounts():
    from app.sop.formatting import tidy
    raw = ("Please ask Dr. Smith at the clinic for a replacement copy of the report, and send the allowed amount of $1,450.00 "
           "details along with it. Once we receive the files the review restarts. It usually takes less than a week.")
    out = tidy(raw)
    assert "Dr. Smith" in out and "\n\nSmith" not in out
    assert "$1,450.00" in out


# ---- follow-up suggestions -----------------------------------------------------------
def _cleared_state(party="P9", case="CL-2048", phase=Phase.PROCESS_CASE):
    s = SessionState()
    s.verification.verified, s.verification.party_id, s.verification.consent_status = True, party, "not_required"
    s.active_case_id, s.phase = case, phase
    return s


def test_suggestions_before_verification_are_generic():
    from app.sop.suggestions import suggest
    out = suggest(SessionState(), repo, TODAY)
    text = " ".join(x["text"] for x in out)
    assert out and "CL-" not in text and "pathology" not in text and "$" not in text


def test_suggestions_use_the_real_claim():
    from app.sop.suggestions import suggest
    s = _cleared_state()
    texts = [x["text"] for x in suggest(s, repo, TODAY)]
    assert any("pathology report" in t for t in texts)                 # the actual missing document
    assert not any("wrap" in x["id"] for x in suggest(s, repo, TODAY))  # nothing answered yet, so no "that's all"
    s.case_ledger["CL-2048"] = {"opened_turn": 1, "topics": ["why denied"]}
    assert suggest(s, repo, TODAY)[-1]["id"] == "CL-2048.wrap.up"


def test_suggestions_differ_by_claim_status():
    from app.sop.suggestions import suggest
    open_claim = [x["text"] for x in suggest(_cleared_state(case="CL-2102"), repo, TODAY)]
    closed = [x["text"] for x in suggest(_cleared_state(case="CL-2011"), repo, TODAY)]
    assert "Why hasn't anything been paid yet?" in open_claim and not any("submit" in t for t in open_claim)
    assert any("$800.00" in t for t in closed)                          # paid $780.00 of an allowed $800.00


def test_suggestions_for_ambiguous_claims_name_the_candidates():
    from app.sop.suggestions import suggest
    s = _cleared_state(case=None, phase=Phase.RESOLVE_INTENT)
    s.pending_offer, s.last_candidates = "case_choice", ["CL-2048", "CL-2011"]
    assert [x["label"] for x in suggest(s, repo, TODAY)] == ["The denied one", "The closed one"]


def test_used_suggestions_are_not_offered_again_and_offers_come_first():
    from app.sop.suggestions import suggest
    s = _cleared_state()
    first = suggest(s, repo, TODAY)[0]
    s.used_suggestions.add(first["id"])
    assert first["id"] not in [x["id"] for x in suggest(s, repo, TODAY)]
    s.pending_offer = "handoff"
    assert [x["id"] for x in suggest(s, repo, TODAY)] == ["handoff.yes", "handoff.no"]


def test_suggestions_name_all_four_claims_consistently():
    from app.sop.suggestions import suggest
    s = _cleared_state(case=None, phase=Phase.RESOLVE_INTENT)
    s.pending_offer, s.last_candidates = "case_choice", [c["case_id"] for c in repo.cases_for("P9")]
    assert [x["label"] for x in suggest(s, repo, TODAY)] == [
        "The open auto claim", "The denied healthcare claim", "The closed dental claim", "The closed healthcare claim"]
