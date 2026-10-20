"""End-to-end SOP flow with the LLM switched off (heuristic extractor + templates).
Proves the workflow, gates and memory are enforced by code, not by prompts."""
from app.data.repository import get_repo
from app.sop.controller import Controller
from app.sop.guards import _sensitive_lexicon
from app.sop.phases import Phase
from app.sop.state import SessionState

from .test_core import DEMO

ctl = Controller(use_llm=False)
LEX = [t for t in _sensitive_lexicon(get_repo()) if not t.isdigit()]


def new():
    s = SessionState()
    ctl.start(s)
    return s


def test_demo_one_utterance_verifies_and_uses_remembered_hint():
    s = new()
    r = ctl.handle(s, DEMO)
    assert s.verification.verified and s.verification.party_id == "P9"
    assert s.phase == Phase.PROCESS_CASE and s.active_case_id == "CL-2048"
    assert [t["to"] for t in r["trace"]["transitions"]] == ["RESOLVE_INTENT", "PROCESS_CASE"]
    assert s.memory.slots["case.status"].status == "consumed"


def test_hint_remembered_across_turns_without_disclosure():
    s = new()
    r1 = ctl.handle(s, "Hi, I'm calling about my denied healthcare claim from January. My name is Margaret Chen.")
    assert s.phase == Phase.VERIFY_ID
    assert not any(t in r1["reply"].lower() for t in LEX)
    assert s.memory.slots["case.status"].status == "deferred"
    ctl.handle(s, "DOB is 1985-03-15 and SSN last four 4472")
    assert s.phase == Phase.PROCESS_CASE and s.active_case_id == "CL-2048"


def test_three_failed_attempts_lock_and_handoff():
    s = new()
    for dob in ("1985-01-01", "1985-01-02", "1985-01-03"):
        ctl.handle(s, f"My name is Margaret Chen, DOB {dob}, SSN last four 4472")
    assert s.verification.locked and s.phase == Phase.HANDOFF and s.handoff["identity_verified"] is False


def test_off_topic_ladder_offers_human():
    s = new()
    for _ in range(3):
        ctl.handle(s, "what is RL?")
    assert s.pending_offer == "handoff" and s.phase == Phase.VERIFY_ID
    ctl.handle(s, "yes")
    assert s.phase == Phase.HANDOFF


def test_email_send_and_skip():
    s = new()
    ctl.handle(s, DEMO)
    ctl.handle(s, "that's all")
    assert s.phase == Phase.POST_PROCESS and s.pending_offer == "email"
    ctl.handle(s, "yes")
    assert s.email_status == "sent" and len(s.outbox) == 1 and s.outbox[0]["to"] == "margaret@email.com"
    assert "CL-2048" in s.outbox[0]["text"] and "March 18, 2026" in s.outbox[0]["text"]
    ctl.handle(s, "no")
    assert s.phase == Phase.CLOSED

    s2 = new()
    ctl.handle(s2, DEMO)
    ctl.handle(s2, "that's all")
    ctl.handle(s2, "no thanks")
    assert s2.email_status == "skipped" and not s2.outbox
