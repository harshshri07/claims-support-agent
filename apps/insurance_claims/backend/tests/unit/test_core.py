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
