"""Run the live-LLM conversation scenarios.

    python tests/run_scenarios.py              # all
    python tests/run_scenarios.py demo angry   # names containing any of the words
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.repository import get_repo  # noqa: E402
from app.sop.controller import Controller  # noqa: E402
from app.sop.guards import check_reply, unauthorized_commitments  # noqa: E402
from app.sop.state import SessionState  # noqa: E402

repo = get_repo()
ctl = Controller()


def check(exp: dict, s: SessionState, reply: str, trace: dict) -> list[str]:
    v, fails = s.verification, []
    r = reply.lower()

    def eq(label, got, want):
        if got != want:
            fails.append(f"{label}: expected {want!r}, got {got!r}")

    for k, want in exp.items():
        if k == "phase": eq(k, s.phase.value, want)
        elif k == "verified": eq(k, v.verified, want)
        elif k == "party": eq(k, v.party_id, want)
        elif k == "active_case": eq(k, s.active_case_id, want)
        elif k == "pending_offer": eq(k, s.pending_offer, want)
        elif k == "email_status": eq(k, s.email_status, want)
        elif k == "outbox": eq(k, len(s.outbox), want)
        elif k == "caller_role": eq(k, v.caller_role, want)
        elif k == "consent_status": eq(k, v.consent_status, want)
        elif k == "failures": eq(k, v.failures, want)
        elif k == "locked": eq(k, v.locked, want)
        elif k == "off_topic_streak": eq(k, s.counters.off_topic_streak, want)
        elif k == "injection_attempts": eq(k, s.counters.injection_attempts, want)
        elif k == "used_remembered_hint":
            eq(k, any(n.startswith("resolved using hints") for n in trace["notes"]), want)
        elif k == "memory_deferred":
            for key in want:
                st = s.memory.slots.get(key)
                if not st or st.status != "deferred":
                    fails.append(f"memory {key} not deferred ({st.status if st else 'missing'})")
        elif k == "emotion_in":
            if (trace.get("emotion") or {}).get("label") not in want:
                fails.append(f"emotion {(trace.get('emotion') or {}).get('label')} not in {want}")
        elif k == "empathy":
            eq(k, (trace.get("emotion") or {}).get("mode") == "empathize", want)
        elif k in ("reply_contains_any", "reply_contains_any_2"):
            if not any(w.lower() in r for w in want):
                fails.append(f"reply lacks any of {want}")
        elif k == "reply_contains_all":
            if missing := [w for w in want if w.lower() not in r]:
                fails.append(f"reply lacks {missing}")
        elif k == "reply_not_contains":
            if hits := [w for w in want if w.lower() in r]:
                fails.append(f"reply contains forbidden {hits}")
        elif k == "explain_contains":
            text = " | ".join(e["text"] for e in trace.get("explain", []))
            if missing := [w for w in want if w.lower() not in text.lower()]:
                fails.append(f"explanation lacks {missing}")
        elif k == "reply_not_regex":
            if hits := [w for w in want if re.search(w, reply)]:
                fails.append(f"reply matches forbidden pattern {hits}")
        elif k == "not_refused":
            if hits := [f for f in want if f"refused.{f}" in s.memory.slots]:
                fails.append(f"wrongly marked as refused: {hits}")
        else:
            fails.append(f"unknown expectation {k}")
    return fails


def run(sc: dict) -> dict:
    s = SessionState(consent_scenario=sc.get("consent_scenario", "default"))
    ctl.start(s)
    log, ok = [], True
    for i, turn in enumerate(sc["turns"], 1):
        out = ctl.handle(s, turn["say"])
        fails = check(turn.get("expect", {}), s, out["reply"], out["trace"])
        v = s.verification
        cleared = v.verified and v.consent_status in ("not_required", "approved")
        said = " ".join(m["content"] for m in s.transcript if m["role"] == "user")
        if not cleared:  # universal: no claims-system data before the caller is cleared
            leaks = check_reply(out["reply"], repo=repo, verified=False, allowed_facts={}, caller_text=said, party=None)
            fails += [f"DISCLOSURE: {x}" for x in leaks]
        elif v.party_id:  # universal: never read back on-file personal data the caller didn't say
            party = repo.party(v.party_id)
            said_digits = re.sub(r"\D", "", said)
            for label, val in (("phone", party["phone"][-10:]), ("email", party["email"]), ("dob", party["dob"])):
                if val.lower() in out["reply"].lower() and val.lower() not in said.lower() and val not in said_digits:
                    fails.append(f"PII READ-BACK: {label}")
        acts = {a["kind"] for a in (out["trace"].get("directive") or {}).get("acts", [])}
        fails += [f"COMMITMENT: {x}" for x in unauthorized_commitments(out["reply"], acts)]  # universal
        ok &= not fails
        log.append({"turn": i, "say": turn["say"], "reply": out["reply"], "phase": s.phase.value,
                    "latency_ms": out["trace"]["latency_ms"], "fails": fails})
    return {"name": sc["name"], "passed": ok, "turns": log}


def main():
    folder = Path(__file__).parent / "scenarios"
    scenarios = [sc for f in sorted(folder.glob("*.yaml")) for sc in yaml.safe_load(f.read_text(encoding="utf-8"))]
    if filters := sys.argv[1:]:
        scenarios = [s for s in scenarios if any(f in s["name"] for f in filters)]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, scenarios))
    for res in results:
        print(f"{'PASS' if res['passed'] else 'FAIL'}  {res['name']}")
        for t in res["turns"]:
            if t["fails"]:
                print(f"   turn {t['turn']}: {t['say'][:70]}")
                print(f"      reply: {t['reply'][:220]}")
                for f in t["fails"]:
                    print(f"      - {f}")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed in {time.time() - t0:.0f}s")
    out = Path(__file__).parent / "scenarios" / "last_report.json"
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"full transcripts: {out}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
