"""The SOP harness turn loop.

    regex pre-extract -> LLM extract -> memory merge -> global guards -> phase handler(s)
    -> render (responder or grounded agent) -> output guard -> trace

The controller owns phase, gates and allowed actions. The LLM interprets and phrases.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from app.config import settings
from app.llm import llm_enabled
from app.data.repository import Repository, get_repo
from app.nlu import extractor, patterns
from app.nlu.normalize import mask_email, mask_phone, norm_date, norm_email, norm_last4, norm_phone

from . import emotion, responder
from .explain import explain
from .formatting import tidy
from .directive import Directive
from .guards import check_reply, unauthorized_commitments
from .handoff import create_ticket
from .intent import IMPLIED_STATUS, resolve_case
from .phases import SPECS, Phase
from .post_process import build_and_send
from .process import ProcessAgent
from .state import FACTOR_LABELS, IDENTITY_FACTORS, SessionState, TransitionError
from .verification import REQUIRED_FACTORS, evaluate, poll_consent, rep_on_file

log = logging.getLogger(__name__)

GREETING = ("Hi, I'm Alex from claims support.\n\n"
            "Before I can open a claim, I need to verify who I'm speaking with. Please share any three of these:\n\n"
            "- Full name\n- Date of birth\n- Phone number on file\n- Email address on file\n- Last four digits of your SSN")

# What the agent may say about verification itself (never anything about the account or claims).
VERIFICATION_FAQ = {
    "accepted_details": ["full name", "date of birth", "phone number on file", "email on file",
                         "last four digits of SSN (or national ID)"],
    "how_many": "any three of the five, matching the same customer record",
    "policy_number": "helps find the account, but doesn't count as one of the three details",
    "on_file": "'on file' means the phone number or email you gave us when you set up the policy",
    "why": "claim files hold medical and financial details, so identity is confirmed before anything is shared",
    "after": "once verified, I'll pull up the claim and help with it right away",
    "data_use": "the details are only used to confirm identity for this conversation",
}

CAPABILITIES = ["checking a claim's status", "explaining a denial", "what documents are needed and how to submit them",
                "deadlines and next steps", "what the payment amounts on a claim mean"]


SELF_INTRO = re.compile(r"(?i:\bmy name is|\bthis is|\bi'm|\bi am)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")


def _bullets(briefs: list[dict[str, str]]) -> str:
    return "\n".join(f"- {b['case_id']} · {b['type']} · filed {b['filed']} · {b['status']}" for b in briefs)


TRANSFER_WORDS = re.compile(r"\b(representative|transfer|connect you|a person|human|specialist|someone from our)\b", re.I)
FIELD_WORDS = {"name": ("name",), "dob": ("birth", "dob", "birthday", "born"), "phone": ("phone", "number", "mobile"),
               "email": ("email", "e-mail", "mail"), "id_last4": ("ssn", "social", "id", "last four", "last 4")}
# canned openers models love; dropped so replies start with substance
FILLER = re.compile(r"^\s*(I hear (you|the \w+|your \w+)( about that)?|I get it|Great question|Absolutely|Perfect|"
                    r"Certainly|Of course|Got it)[,.!]\s+", re.I)


def text_of_turn(state: SessionState) -> str:
    return state.transcript[-1]["content"] if state.transcript else ""


def _policy(v: str | None) -> str | None:
    if not v:
        return None
    digits = "".join(ch for ch in v if ch.isdigit())
    return f"POL-{digits}" if digits else None


class Controller:
    def __init__(self, repo: Repository | None = None, use_llm: bool | None = None):
        """use_llm=None follows whichever model is connected (none = rules-only mode)."""
        self.repo = repo or get_repo()
        self._use_llm = use_llm
        self.handlers: dict[Phase, Callable[..., None]] = {
            Phase.VERIFY_ID: self._verify,
            Phase.RESOLVE_INTENT: self._resolve,
            Phase.PROCESS_CASE: self._process,
            Phase.POST_PROCESS: self._post,
        }

    @property
    def use_llm(self) -> bool:
        return llm_enabled() if self._use_llm is None else self._use_llm

    # ------------------------------------------------------------------ entry
    def start(self, state: SessionState) -> str:
        state.transcript.append({"role": "assistant", "content": GREETING, "turn": 0})
        return GREETING

    def handle(self, state: SessionState, text: str) -> dict[str, Any]:
        t0 = time.time()
        state.turn += 1
        trace: dict[str, Any] = {"turn": state.turn, "phase_before": state.phase.value, "transitions": [],
                                 "guards": [], "tools": [], "notes": [], "memory_updates": []}
        state.transcript.append({"role": "user", "content": text, "turn": state.turn})

        if state.phase in (Phase.HANDOFF, Phase.CLOSED):
            reply = self._terminal_reply(state)
            return self._finish(state, reply, trace, t0, Directive())

        pre = patterns.extract(text, settings.demo_today)
        last_agent = next((m["content"] for m in reversed(state.transcript[:-1]) if m["role"] == "assistant"), "")
        collected = list(state.memory.family("identity"))
        ext = (extractor.extract(text, last_agent, state.phase.value, state.pending_offer, collected)
               if self.use_llm else extractor.heuristic(text, state.pending_offer))
        trace["pre_extraction"], trace["extraction"] = pre, ext
        self._merge(state, ext, pre, trace)

        emo = emotion.assess(state, ext)
        trace["emotion"] = emo
        d = Directive(empathy=emo)
        acts = set(ext.get("acts") or [])

        # ---------------- global guards (apply in every phase) ----------------
        if state.pending_offer == "handoff":
            if acts & {"affirm", "request_human"}:
                self._handoff(state, d, trace, state.handoff_reason or "caller accepted transfer offer")
                return self._finish(state, self._render(state, d, trace), trace, t0, d)
            if "deny" in acts:
                state.offer(None)
                ext["_handoff_declined"] = True
                d.add("transfer_declined", "Acknowledge they'd rather continue with you, in a few words.",
                      "No problem, let's keep going together.")
        if "request_human" in acts:
            self._handoff(state, d, trace, "caller asked for a human")
            return self._finish(state, self._render(state, d, trace), trace, t0, d)

        if "manipulation_attempt" in acts:
            state.counters.injection_attempts += 1
            trace["guards"].append({"guard": "manipulation", "action": "ignored; gates are enforced in code"})
            d.add("hold_the_line", "Politely say you can't skip or change the security/privacy steps, "
                                   "without lecturing, then carry on.",
                  "I'm not able to skip or change our security steps, but I'm glad to help within them.")
            d.must_not.append("acting as if any rule was changed or verification was skipped")

        scope = ext.get("scope", "in_scope")
        if scope == "out_of_scope" and not (acts & {"affirm", "deny"} and state.pending_offer):
            if int(emo.get("intensity") or 0) < 2:
                d.empathy = {**emo, "mode": None}
            self._off_topic(state, d, trace, ext)
            return self._finish(state, self._render(state, d, trace), trace, t0, d)
        if scope == "mixed":
            d.add("decline_off_topic_part", "In one short clause, say the unrelated part is outside what you can help "
                                             "with here (don't answer it), then handle the rest.",
                  "That other question is outside what I can help with here, but on your claim:",
                  off_topic=ext.get("off_topic_text"))
        else:
            state.counters.off_topic_streak = 0

        # ---------------- phase handlers (may chain within one turn) ----------
        entered = False
        for _ in range(5):
            before = state.phase
            self.handlers[state.phase](state, ext, text, d, trace, entered)
            if state.phase == before or state.phase in (Phase.HANDOFF, Phase.CLOSED):
                break
            entered = True

        # ---------------- know when to stop persuading -----------------------
        if emo.get("offer_human") and state.pending_offer != "handoff" and state.phase not in (Phase.HANDOFF, Phase.CLOSED):
            state.handoff_reason = "; ".join(emo["escalation_reasons"])
            state.offer("handoff")
            d.add("offer_human", "Sincerely offer to connect them with a human claims representative now "
                                 "(they'll get the notes so they won't repeat themselves), while making clear you're "
                                 "also happy to keep going. Ask which they'd prefer.",
                  "If you'd prefer, I can connect you with a claims representative now and pass along my notes, "
                  "or we can keep going together. Which would you like?")
        return self._finish(state, self._render(state, d, trace), trace, t0, d)

    # ------------------------------------------------------------------ memory
    def _merge(self, state: SessionState, ext: dict[str, Any], pre: dict[str, Any], trace: dict[str, Any]) -> None:
        mem, t, ph = state.memory, state.turn, state.phase
        src = ext.get("_source", "llm")
        updates = []

        def put(key, value, source=src):
            if mem.put(key, value, t, ph, source):
                updates.append(key)

        if not state.verification.verified:
            ident = ext.get("identity") or {}
            put("identity.name", (ident.get("full_name") or "").strip() or None)
            put("identity.dob", norm_date(ident.get("dob")) or pre.get("dob"))
            put("identity.phone", pre.get("phone") or norm_phone(ident.get("phone")))
            put("identity.email", pre.get("email") or norm_email(ident.get("email")))
            put("identity.id_last4", pre.get("id_last4") or norm_last4(ident.get("id_last4")))
            put("identity.policy_number", pre.get("policy_number") or _policy(ident.get("policy_number")))
        if ph == Phase.VERIFY_ID:
            caller = ext.get("caller") or {}
            if state.pending_offer == "rep_name" and not caller.get("rep_name"):
                caller = {**caller, "role": "representative",
                          "rep_name": (ext.get("identity") or {}).get("full_name")}
            if caller.get("role") == "representative" and not caller.get("rep_name"):
                # deterministic backstop: "I'm Kevin, her neighbour" -> rep_name "Kevin"
                m = SELF_INTRO.search(text_of_turn(state))
                policyholder = (ext.get("identity") or {}).get("full_name") or ""
                if m and m.group(1).split()[0].lower() not in policyholder.lower().split():
                    caller = {**caller, "rep_name": m.group(1)}
            if caller.get("role") == "representative":
                put("caller.role", "representative")
                put("caller.rep_name", caller.get("rep_name"))
                put("caller.relationship", caller.get("relationship"))

        hint = dict(ext.get("case_hint") or {})
        if src != "llm":  # heuristic mode: lean on regex hints
            hint.setdefault("case_type", pre.get("case_type"))
            hint.setdefault("status", pre.get("case_status"))
            hint.setdefault("month", pre.get("case_month"))
            hint.setdefault("year", pre.get("case_year"))
        hint.setdefault("case_id", pre.get("case_id"))
        # grounding the extractor: a year or claim ID the caller never said is a model guess, drop it
        if hint.get("year") and str(hint["year"]) not in text_of_turn(state):
            trace["notes"].append(f"dropped unstated year hint {hint['year']}")
            hint["year"] = None
        if hint.get("case_id") and "".join(ch for ch in str(hint["case_id"]) if ch.isdigit()) not in text_of_turn(state):
            hint["case_id"] = None
        # same for status / month: keep them only if the caller's own words carry them
        # ("the denied one" -> denied; "my auto claim" must not become "open, from February")
        if hint.get("status") and not patterns.canonical_status(text_of_turn(state)):
            hint["status"] = None
        if hint.get("month") and not pre.get("case_month"):
            hint["month"] = None
        for k in ("case_id", "case_type", "status", "month", "year"):
            put(f"case.{k}", hint.get(k))
        put("intent.primary", ext.get("intent"))
        if ext.get("question") and (ext.get("intent") or any(hint.get(k) for k in ("case_id", "case_type", "status", "month"))):
            put("request.text", ext["question"])
        if ext.get("alt_email"):
            put("contact.alt_email", norm_email(ext["alt_email"]))
        trace["memory_updates"] = updates

    # ------------------------------------------------------------------ VERIFY_ID (STRICT)
    def _verify(self, state, ext, text, d: Directive, trace, entered):
        v, mem, acts = state.verification, state.memory, set(ext.get("acts") or [])
        if mem.get("caller.role") == "representative":
            v.caller_role = "representative"

        if v.verified:  # representative caller: identity checked, consent gate still closed
            if v.consent_status == "pending":
                return self._consent_step(state, d, trace)
            if v.consent_status == "rep_name_needed":
                if mem.get("caller.rep_name"):
                    state.offer(None)
                    return self._check_rep(state, d, trace, self.repo.party(v.party_id))
                d.add("ask_rep_name", "Ask for the caller's own full name (they're calling for someone else).",
                      "Could I have your own full name, please?")
                return
            state.offer("handoff")
            d.add("gate_closed", "Explain kindly that without the policyholder's approval/authorization you can't "
                                 "share any claim information. Offer the transfer to a representative, or the "
                                 "policyholder can contact us directly.",
                  "I'm sorry, without the policyholder's approval I can't share any claim information. I can connect "
                  "you with a representative, or the policyholder can contact us directly. Would you like the transfer?",
                  rationale=emotion.RATIONALES["consent"])
            return

        ident = mem.family("identity")
        new_info = any(k.startswith("identity.") for k in trace["memory_updates"])
        if new_info:
            res = evaluate(self.repo, {k: ident.get(k) for k in IDENTITY_FACTORS}, ident.get("policy_number"))
            # matched/mismatched details are logged for the inspector but never spoken
            trace["verification"] = {"status": res.status, "collected": res.collected,
                                     "matched_count": len(res.matched), "mismatched": res.mismatched}
            if res.status == "verified":
                party = self.repo.party(res.party_id)
                v.verified, v.party_id, v.verified_turn, v.matched_factors = True, res.party_id, state.turn, res.matched
                mem.consume("identity", state.turn)
                if v.caller_role == "representative":
                    if not mem.get("caller.rep_name"):
                        v.consent_status = "rep_name_needed"
                        state.offer("rep_name")
                        d.add("ask_rep_name", "Thank them; the policyholder's details check out. Since they're calling "
                                              "on someone else's behalf, ask for THEIR own full name.",
                              "Thanks, those details check out. Since you're calling on the policyholder's behalf, "
                              "could I have your own full name?")
                        return
                    return self._check_rep(state, d, trace, party)
                self._verified(state, d, trace, party)
                return
            if res.status == "failed":
                v.failures += 1
                for k in res.mismatched:
                    mem.reject(f"identity.{k}", state.turn)
                if v.failures >= settings.max_verify_failures:
                    v.locked = True
                    d.add("verification_locked", "Explain kindly that for security you can't continue verification "
                                                 "in this chat, and that you're transferring them to a representative "
                                                 "who can verify them another way. Don't say which detail failed.",
                          "For security, I can't continue verifying in this chat, so I'm connecting you with a "
                          "representative who can verify your identity another way.")
                    self._handoff(state, d, trace, "identity verification failed repeatedly")
                    return
                d.add("verification_failed",
                      "Say you couldn't verify the account with those details (don't say which one, don't imply "
                      "which matched). Ask them to double-check, or offer a different detail from the options.",
                      "I wasn't able to verify the account with those details. Could you double-check them, or give me "
                      "a different detail such as " + " or ".join(self._alternatives(state, ext)[:2]) + "?",
                      options=self._alternatives(state, ext))
                return

        # still collecting
        ident = mem.family("identity")
        have = [f for f in IDENTITY_FACTORS if ident.get(f)]
        need = max(REQUIRED_FACTORS - len(have), 1)
        if not state.hint_acknowledged and any(k.split(".")[0] in ("case", "intent", "request")
                                               for k in trace["memory_updates"]):
            state.hint_acknowledged = True
            d.add("note_for_later", "Briefly confirm you've noted what they're calling about (paraphrase ONLY the "
                                    "caller's own words) and will pull it up right after verification.",
                  "I've noted what you're calling about and I'll pull it up as soon as you're verified.",
                  caller_said=mem.get("request.text"))
        if "question" in acts and ext.get("question") and not acts & {"demand_bypass", "manipulation_attempt"} \
                and not any(k.split(".")[0] in ("case", "intent") for k in trace["memory_updates"]):
            # clarification about the verification step itself ("why do you need my DOB?", "is my policy number enough?")
            d.add("answer_clarification",
                  "First answer the caller's question about verification in one or two sentences, using only "
                  "VERIFICATION_FAQ. If the question is really about their claim or account, say you'll cover it "
                  "right after verification. Never reveal anything about the account.",
                  "Happy to explain: I need any three of your full name, date of birth, phone number, email, or SSN "
                  "last four, matching what's on file. A policy number helps me find the account but doesn't count "
                  "as one of the three.",
                  question=ext["question"], VERIFICATION_FAQ=VERIFICATION_FAQ)
        if acts & {"refusal"} and ext.get("refused_fields"):
            d.add("respect_refusal", "Accept their choice not to share that detail without pressure; there are "
                                     "other options.", "That's completely fine, we can use something else.")
        d.add("ask_identity",
              f"Say in one short sentence that you need {need} {'more ' if have else ''}identity detail{'s' if need > 1 else ''}, "
              "then list the remaining options as a '- ' bullet list, one per line. Nothing after the list. "
              "Don't repeat back values they gave, and don't say anything is 'on file' or matched.",
              f"To verify you, I still need {need} {'more ' if have else ''}detail{'s' if need > 1 else ''}. Any of these work:\n"
              + "\n".join(f"- {o[:1].upper()}{o[1:]}" for o in self._alternatives(state, ext)),
              details_needed=need, options=self._alternatives(state, ext))

    def _alternatives(self, state: SessionState, ext: dict[str, Any]) -> list[str]:
        ident = state.memory.family("identity")
        # a refusal only counts for a field the caller actually mentioned ("not giving you my SSN")
        said = text_of_turn(state).lower()
        refused = {f for f in ext.get("refused_fields") or []
                   if any(re.search(rf"\b{w}\b", said) for w in FIELD_WORDS.get(f, ()))}
        for k, s in state.memory.slots.items():  # refusals are remembered across turns
            if k.startswith("refused."):
                refused.add(k.split(".", 1)[1])
        for f in refused:
            state.memory.put(f"refused.{f}", True, state.turn, state.phase, "llm")
        return [FACTOR_LABELS[f] for f in IDENTITY_FACTORS if not ident.get(f) and f not in refused]

    def _check_rep(self, state, d: Directive, trace, party):
        v, mem = state.verification, state.memory
        v.rep_on_file = rep_on_file(self.repo, v.party_id, mem.get("caller.rep_name"))
        trace["representative"] = {"rep_name": mem.get("caller.rep_name"), "on_file": v.rep_on_file}
        if not v.rep_on_file:
            v.consent_status = "not_authorized"
            state.handoff_reason = "third-party caller not on file as authorized representative"
            state.offer("handoff")
            d.add("rep_not_authorized",
                  "Explain you can't discuss this policyholder's claims because the caller isn't listed as an "
                  "authorized representative. Do NOT reveal anything about the account (not even that claims "
                  "exist). Offer options: the policyholder can call directly, or you can transfer to a "
                  "representative who can explain how to get authorized.",
                  "I'm sorry, but I can't discuss this account because you're not listed as an authorized "
                  "representative. The policyholder can contact us directly or add you as an authorized "
                  "representative. Would you like me to connect you with someone who can explain how?",
                  rationale=emotion.RATIONALES["consent"])
            return
        v.consent_status = "pending"
        d.add("consent_requested",
              f"Thank {mem.get('caller.rep_name')}: the policyholder's details check out and they're listed as an "
              "authorized representative. Explain that for the policyholder's privacy you've sent an approval request "
              "to the policyholder's phone on file, and you'll continue as soon as it's approved. Ask them to let you "
              "know when it's done.",
              "Thanks, those details check out and you're listed as an authorized representative. For the "
              f"policyholder's privacy I've sent an approval request to their phone ending in {party['phone'][-4:]}. "
              "Let me know once it's approved.",
              phone_hint=mask_phone(party["phone"]))
        return self._consent_step(state, d, trace, first=True)

    def _consent_step(self, state, d: Directive, trace, first: bool = False):
        v = state.verification
        status = poll_consent(self.repo, state.consent_scenario, v.consent_polls)
        v.consent_polls += 1
        trace["consent"] = {"scenario": state.consent_scenario, "poll": v.consent_polls, "status": status}
        if status == "approved":
            v.consent_status = "approved"
            if not first:
                d.add("consent_approved", "Tell them the policyholder approved the request.",
                      "Good news, the policyholder has approved it.")
            self._verified(state, d, trace, self.repo.party(v.party_id))
        elif status == "timeout":
            v.consent_status = "timeout"
            state.handoff_reason = "policyholder consent request timed out"
            state.offer("handoff")
            d.add("consent_timeout", "Explain the approval request expired without a response, so you can't share "
                                     "claim details. Offer: try again later after the policyholder is available, or "
                                     "transfer to a representative.",
                  "The approval request expired without a response, so I can't share the claim details yet. The "
                  "policyholder can approve a new request later, or I can connect you with a representative. "
                  "Would you like that?")
        elif not first:
            d.add("consent_pending", "Say the approval is still pending; you can't share claim details until it's "
                                     "approved. Reassure them and invite them to let you know when it's done.",
                  "The approval is still pending on the policyholder's phone. As soon as it comes through I can help.")

    def _verified(self, state, d: Directive, trace, party):
        # address callers by the name THEY used (handles aliases like "Yaven Li" for "Ya Wen Li")
        said = (state.memory.get("caller.rep_name") if state.verification.caller_role == "representative"
                else state.memory.get("identity.name")) or party["name"]
        first = said.split()[0].title()
        if state.verification.caller_role == "representative":
            holder = party["name"].split()[0]
            d.add("verified", f"In one short sentence, tell {first} that {holder}'s identity is confirmed and they're "
                              f"authorized to help with {holder}'s claims (don't say {first} is 'verified').",
                  f"Thanks, {first}. {holder} approved access, so I can help with {holder}'s claims now.",
                  first_name=first)
        else:
            d.add("verified", f"In one short sentence, tell {first} you've confirmed their identity "
                              "(you did the verifying; don't say 'thanks for verifying').",
                  f"Thanks, {first}. I've confirmed your identity.", first_name=first)
        state.transition(Phase.RESOLVE_INTENT, "identity verified with 3+ matching factors", trace["transitions"])

    # ------------------------------------------------------------------ RESOLVE_INTENT (GUIDED)
    def _resolve(self, state, ext, text, d: Directive, trace, entered):
        mem, acts = state.memory, set(ext.get("acts") or [])
        party_id = state.verification.party_id
        cases = self.repo.cases_for(party_id)
        if not entered and "done" in acts and not ext.get("question"):
            state.transition(Phase.POST_PROCESS, "caller has nothing further", trace["transitions"])
            return
        intent = mem.get("intent.primary", include_consumed=False)
        if intent == "unsupported_request":
            state.handoff_reason = "request outside this line's workflow"
            state.offer("handoff")
            mem.consume("intent", state.turn)
            d.add("unsupported", "Say that request isn't something you can handle on this line, and offer to "
                                 "transfer them to a representative who can. Ask if they'd like that.",
                  "That's not something I can handle here, but I can connect you with a representative who can. "
                  "Would you like that?", request=ext.get("question"))
            return

        hints = mem.family("case")
        from_memory = {k: mem.slots[f"case.{k}"].turn for k in hints if mem.slots[f"case.{k}"].status == "deferred"}
        if (idx := (ext.get("case_hint") or {}).get("choice_index")) and state.last_candidates:
            if 1 <= idx <= len(state.last_candidates):
                hints = {"case_id": state.last_candidates[idx - 1]}
        if not hints and state.active_case_id and not ext.get("refers_to_different_case"):
            hints = {"case_id": state.active_case_id}
        soft = IMPLIED_STATUS.get(intent or state.process_intent or "")
        res = resolve_case(cases, hints, soft_status=soft)
        trace["case_resolution"] = {"kind": res.kind, "hints": res.used_hints, "relaxed": res.relaxed,
                                    "candidates": [c["case_id"] for c in res.candidates],
                                    "hints_from_earlier_turns": from_memory}
        briefs = [self.repo.brief(c) for c in res.candidates]

        if res.kind == "unique":
            case = res.case
            state.active_case_id = case["case_id"]
            state.active_case_needs_documents = bool(case.get("documents_needed"))
            state.case_ledger.setdefault(case["case_id"], {"opened_turn": state.turn, "topics": []})
            state.process_intent = intent or state.process_intent
            request = mem.get("request.text", include_consumed=False) or ext.get("question") or text
            for fam in ("case", "intent", "request"):
                mem.consume(fam, state.turn)
            state.last_candidates = []
            if state.pending_offer == "case_choice":
                state.offer(None)
            if from_memory:
                trace["notes"].append(f"resolved using hints remembered from turn(s) {sorted(set(from_memory.values()))}")
            d.process_request = request
            state.transition(Phase.PROCESS_CASE, f"case {case['case_id']} resolved ({res.kind})", trace["transitions"])
            return
        if res.kind == "ambiguous":
            state.last_candidates = [c["case_id"] for c in res.candidates]
            state.offer("case_choice")
            d.add("disambiguate", "More than one claim fits what they described. One short sentence saying so, then a "
                                  "'- ' bullet list with one candidate per line (claim number, type, filed month/year, "
                                  "status), then a short question asking which one.",
                  "A few claims could match what you described:\n" + _bullets(briefs)
                  + "\n\nWhich one are you calling about?", candidates=briefs, they_described=res.used_hints)
            return
        if res.kind == "none":
            state.last_candidates = [c["case_id"] for c in res.candidates]
            state.offer("case_choice")
            mem.consume("case", state.turn)
            d.add("no_exact_match", "One short sentence: no claim matches exactly what they described. Then a '- ' bullet "
                                    "list of the closest claims (one per line), then a short question asking which one.",
                  "I don't see an exact match, but these are on your account:\n" + _bullets(briefs)
                  + "\n\nWhich one do you mean?", they_described=res.used_hints, candidates=briefs)
            return
        if res.kind == "no_cases":
            state.handoff_reason = "caller has no claims on file"
            d.add("no_claims", "Say there are no claims on their account. Ask if there's something else, or offer "
                               "a representative if they want to file a new claim.",
                  "I don't see any claims on your account. If you'd like to file a new claim, I can connect you "
                  "with a representative. Is there anything else I can help with?")
            return
        state.last_candidates = [c["case_id"] for c in res.candidates]
        state.offer("case_choice")
        d.add("ask_which_claim", "One short sentence welcoming them, then a '- ' bullet list of the claims on file "
                                 "(one per line: claim number, type, filed month/year, status), then a short question: "
                                 "which one would they like help with?",
              "Here are the claims on your account:\n" + _bullets(briefs)
              + "\n\nWhich one would you like help with?", claims_on_file=briefs)

    # ------------------------------------------------------------------ PROCESS_CASE (GROUNDED-FREE)
    def _process(self, state, ext, text, d: Directive, trace, entered):
        acts = set(ext.get("acts") or [])
        if entered:
            d.delegate_process = True
            return
        if acts & {"done", "deny"} and not ext.get("question") and not ext.get("_handoff_declined"):
            state.transition(Phase.POST_PROCESS, "caller finished with this claim", trace["transitions"])
            return
        hint_case = (ext.get("case_hint") or {}).get("case_id")
        if ext.get("refers_to_different_case") or (hint_case and hint_case.upper() != state.active_case_id):
            if ext.get("question"):
                state.memory.put("request.text", ext["question"], state.turn, state.phase, "llm")
            state.transition(Phase.RESOLVE_INTENT, "caller asked about a different claim", trace["transitions"])
            return
        if ext.get("intent") and ext["intent"] != "unsupported_request":
            state.process_intent = ext["intent"]
        elif ext.get("intent") == "unsupported_request":
            state.handoff_reason = "request outside this line's workflow"
            state.offer("handoff")
            d.add("unsupported", "Say that's not something you can handle here and offer a transfer to a "
                                 "representative; ask if they'd like that.",
                  "That's not something I can handle here, but I can connect you with a representative who can. "
                  "Would you like that?")
            return
        d.delegate_process = True
        d.process_request = ext.get("question") or text

    # ------------------------------------------------------------------ POST_PROCESS (STRICT consent)
    def _post(self, state, ext, text, d: Directive, trace, entered):
        acts = set(ext.get("acts") or [])
        party = self.repo.party(state.verification.party_id)
        masked = mask_email(party["email"])
        has_new_request = bool(ext.get("question")) and ("question" in acts or ext.get("intent")) \
            and not acts & {"affirm", "deny", "done"}

        if entered or state.post_stage is None:
            state.post_stage = "offered"
            state.offer("email")
            whose = "the policyholder's email on file" if state.verification.caller_role == "representative" \
                else "your email on file"
            d.add("offer_email", f"Offer to email a summary of this conversation to {whose} ({masked}): what was "
                                 "discussed, the claim status, and next steps/deadlines. End with ONE direct question "
                                 "that names both choices, e.g. 'Would you like me to send it, or skip it?'",
                  f"Before you go, would you like me to email a summary of today's conversation to {masked}? It "
                  "would include what we discussed, your claim status, and next steps with deadlines. Send it or skip?",
                  masked_email=masked, recipient=whose)
            return

        if state.post_stage == "offered":
            alt = state.memory.get("contact.alt_email", include_consumed=False)
            if alt and alt != party["email"].lower():
                state.memory.consume("contact", state.turn)
                d.add("alt_email_declined", "Explain kindly that the summary can only go to the email on file, "
                                            f"{masked}, and why. They can update their email through the member "
                                            "portal or a representative. Re-ask: send to the email on file or skip?",
                      f"For your security, I can only send the summary to the email already on file, {masked}. "
                      "Would you like me to send it there, or skip it?", masked_email=masked,
                      rationale=emotion.RATIONALES["email_on_file"])
                return
            if "affirm" in acts:
                email = build_and_send(self.repo, state, self.use_llm)
                state.email_status, state.post_stage = "sent", "sent"
                state.offer("anything_else")
                trace["email"] = {"id": email["id"], "delivery": email["delivery"], "content_source": email["content_source"]}
                d.add("email_sent", f"Confirm the summary was sent to {masked}. Then ask if there's anything else.",
                      f"Done, I've sent the summary to {masked}. Is there anything else I can help you with?",
                      masked_email=masked)
                return
            if "deny" in acts:
                state.email_status, state.post_stage = "skipped", "skipped"
                state.offer("anything_else")
                d.add("email_skipped", "Acknowledge they'd rather skip the email (no pressure). Ask if there's "
                                       "anything else.",
                      "No problem, I won't send it. Is there anything else I can help you with?")
                return
            if has_new_request:
                return self._reopen(state, ext, d, trace)
            d.add("reask_email", f"Gently ask again whether to send the summary to {masked} or skip it.",
                  f"Just to confirm, should I email the summary to {masked}, or skip it?", masked_email=masked)
            return

        # after email decision: anything else?
        if has_new_request or "question" in acts and ext.get("question"):
            return self._reopen(state, ext, d, trace)
        if acts & {"deny", "done"}:
            state.transition(Phase.CLOSED, "caller has nothing else", trace["transitions"])
            d.add("goodbye", "Warm, brief goodbye; mention the most important next step/deadline if there is one in facts.",
                  "Thanks for reaching out. Take care!", key_deadline=self._key_deadline(state))
            return
        d.add("ask_what_else", "Ask what else they need help with.", "Sure, what else can I help you with?")

    def _reopen(self, state, ext, d, trace):
        state.post_stage = None
        if ext.get("question"):
            state.memory.put("request.text", ext["question"], state.turn, state.phase, "llm")
        state.transition(Phase.RESOLVE_INTENT, "caller raised a new request", trace["transitions"])

    def _key_deadline(self, state) -> str | None:
        for cid in state.case_ledger:
            c = self.repo.case(state.verification.party_id, cid)
            if c and c.get("appeal_deadline"):
                return self.repo.fact_sheet(c, settings.demo_today).get("appeal_deadline_pretty")
        return None

    # ------------------------------------------------------------------ global behaviours
    def _off_topic(self, state, d: Directive, trace, ext):
        c = state.counters
        c.off_topic_streak += 1
        trace["guards"].append({"guard": "scope", "off_topic_streak": c.off_topic_streak})
        step = self._current_step(state)
        if c.off_topic_streak >= settings.off_topic_limit:
            state.handoff_reason = "repeated out-of-scope requests"
            state.offer("handoff")
            d.add("off_topic_offer_human",
                  "Politely say again this line only handles insurance claim support, so you can't help with that. "
                  "Offer to connect them with a human representative, or continue with their claim. Do not answer "
                  "the unrelated question.",
                  "I'm only able to help with your insurance claims here, so I can't help with that. Would you like "
                  "me to connect you with a representative, or shall we continue with your claim?",
                  capabilities=CAPABILITIES)
        elif c.off_topic_streak == 2:
            d.add("off_topic_firm", "Decline again, kindly but more clearly; list briefly what you CAN help with. "
                                    "Do not answer the unrelated question.",
                  "I'm sorry, that's outside what I can help with. I can help with things like "
                  + ", ".join(CAPABILITIES[:3]) + ".", capabilities=CAPABILITIES)
            d.add("redirect", step[0], step[1])
        else:
            d.add("off_topic_decline", "Politely say that's outside what you can help with here (don't answer it at "
                                       "all), then redirect.",
                  "Sorry, that's outside what I can help with here.")
            d.add("redirect", step[0], step[1])
        d.must_not.append("answering or explaining the unrelated question in any way")

    def _current_step(self, state) -> tuple[str, str]:
        if state.phase == Phase.VERIFY_ID:
            if state.verification.consent_status == "pending":
                return ("Remind them you're waiting on the policyholder's approval.",
                        "We're still waiting on the policyholder's approval.")
            return ("Bring them back to verification: ask for the remaining identity details.",
                    "To get started on your claim, could you share any three of: full name, date of birth, phone "
                    "number, email, or the last four digits of your SSN?")
        if state.phase == Phase.RESOLVE_INTENT:
            return ("Ask which claim or what they need help with.", "What can I help you with on your claims today?")
        if state.phase == Phase.PROCESS_CASE:
            return (f"Ask if they have questions about claim {state.active_case_id}.",
                    f"Do you have any other questions about claim {state.active_case_id}?")
        return ("Return to the open question (email summary or anything else).", "Shall we pick up where we left off?")

    def _handoff(self, state, d: Directive, trace, reason: str):
        ticket = create_ticket(state, reason, d.empathy)
        state.offer(None)
        state.transition(Phase.HANDOFF, reason, trace["transitions"])
        extra = "" if state.verification.verified else " They'll need to verify your identity as well."
        d.add("handoff", "Tell them you're transferring them to a claims representative, give the reference number, "
                         "and say you've passed along notes so they won't have to repeat themselves."
                         + ("" if state.verification.verified else " Mention the representative will also need to verify them."),
              f"I'm connecting you with a claims representative now. Your reference number is {ticket['ticket_id']}, "
              f"and I've passed along my notes so you won't have to start over.{extra}",
              reference=ticket["ticket_id"])

    def _terminal_reply(self, state) -> str:
        if state.phase == Phase.HANDOFF:
            return (f"You're in the queue for a claims representative (reference {state.handoff['ticket_id']}). "
                    "They'll have the notes from our conversation. You can start a new chat any time.")
        return "This conversation has ended. Start a new chat if you need anything else."

    # ------------------------------------------------------------------ rendering + output guard
    def _render(self, state: SessionState, d: Directive, trace) -> str:
        v = state.verification
        cleared = v.verified and v.consent_status in ("not_required", "approved")
        party = self.repo.party(v.party_id) if v.party_id else None
        caller_text = " ".join(m["content"] for m in state.transcript if m["role"] == "user")
        trace["directive"] = d.to_dict()
        trace["claim_data_visible"] = SPECS[state.phase].claim_data_visible and cleared

        if d.delegate_process and cleared and state.active_case_id:
            case = self.repo.case(v.party_id, state.active_case_id)
            agent = ProcessAgent(self.repo, v.party_id)
            correction, reply = None, None
            if self.use_llm:
                for attempt in range(2):
                    try:
                        draft = agent.answer(case, d.process_request or "", state.process_intent, state.transcript,
                                             d, correction)
                    except Exception as e:  # noqa: BLE001
                        log.warning("process agent failed: %s", e)
                        trace["model_error"] = str(e)[:300]
                        break
                    violations = check_reply(draft, repo=self.repo, verified=True, caller_text=caller_text,
                                             allowed_facts=agent.seen + [d.facts()], party=party)
                    if not draft.strip():
                        violations.append("empty reply")
                    violations += unauthorized_commitments(draft, {a.kind for a in d.acts})
                    trace["guards"].append({"guard": "grounding", "attempt": attempt + 1, "violations": violations})
                    if not violations:
                        reply = draft
                        break
                    correction = "; ".join(violations)
            trace["tools"] = agent.tool_log
            low = (reply or "").lower()
            trace["sources"] = {
                "claim": case["case_id"], "topics": agent.topics, "fallback": agent.fallback_used,
                "documents": [d for d in case.get("documents_needed", []) if d.split()[0].lower() in low],
            }
            # a transfer offer is only pending if the reply actually offered one; otherwise the caller's next
            # "yes"/"mail" would be read as accepting a transfer they were never asked about
            if agent.offer_human and reply and TRANSFER_WORDS.search(reply):
                state.handoff_reason = agent.offer_human
                state.offer("handoff")
            elif state.pending_offer == "handoff":
                state.offer(None)
            entry = state.case_ledger.setdefault(case["case_id"], {"opened_turn": state.turn, "topics": []})
            if d.process_request:
                entry["topics"].append(d.process_request)
            if reply is None:
                trace["notes"].append("fell back to deterministic claim template")
                reply = (d.template() + " " + ProcessAgent.template(case, self.repo.fact_sheet(case, settings.demo_today))).strip()
            return reply

        if not self.use_llm:
            return d.template()
        allowed: list[Any] = [d.facts()]
        if cleared:  # a verified caller's already-opened claims may be referred to again
            allowed += [self.repo.fact_sheet(c, settings.demo_today) for cid in state.case_ledger
                        if (c := self.repo.case(v.party_id, cid))]
        correction = None
        for attempt in range(2):
            try:
                draft = responder.render(d, state.transcript, correction)
            except Exception as e:  # noqa: BLE001
                log.warning("responder failed: %s", e)
                trace["model_error"] = str(e)[:300]
                break
            violations = check_reply(draft, repo=self.repo, verified=cleared, caller_text=caller_text,
                                     allowed_facts=allowed, party=party)
            violations += unauthorized_commitments(draft, {a.kind for a in d.acts})
            # the customer must be able to choose: an email offer has to actually ask send-or-skip
            if {a.kind for a in d.acts} & {"offer_email", "reask_email", "alt_email_declined"} and "?" not in draft:
                violations.append("email offer doesn't ask the caller to choose send or skip")
            trace["guards"].append({"guard": "disclosure" if not cleared else "grounding",
                                    "attempt": attempt + 1, "violations": violations})
            if not violations and draft:
                return draft
            correction = "; ".join(violations)
        trace["notes"].append("fell back to deterministic template")
        return d.template()

    def _finish(self, state, reply: str, trace, t0: float, d: Directive) -> dict[str, Any]:
        reply = re.sub("\\s*[\u2014\u2013]\\s*", ", ", reply).replace(",,", ",")  # no em/en dashes
        reply = FILLER.sub("", reply, count=1)
        reply = tidy(reply[:1].upper() + reply[1:])
        state.transcript.append({"role": "assistant", "content": reply, "turn": state.turn})
        trace["phase_after"] = state.phase.value
        trace["latency_ms"] = int((time.time() - t0) * 1000)
        trace["explain"] = explain(trace, state, self.use_llm)
        state.traces.append(trace)
        return {"reply": reply, "trace": trace}
