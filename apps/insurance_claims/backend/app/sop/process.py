"""PROCESS_CASE — the phase with the most LLM freedom.

The model reasons freely over read-only tools that are bound to the verified party,
so it physically cannot fetch someone else's claim. The active claim's fact sheet
and any guideline rules matching the question are pre-loaded, so most turns need no
tool round-trip. Everything the model was shown is collected for the grounding guard.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings
from app.data.followup_rules import document_guidance, followup_guidance
from app.data.repository import Repository, pretty_date
from app.llm import ToolSpec, get_llm

from .directive import Directive
from .responder import PERSONA

log = logging.getLogger(__name__)

TOOLS = [
    ToolSpec("get_document_guidance",
             "Requirements for a specific document (what it must contain) and what to do if the caller can't get it.",
             {"type": "object", "properties": {"document": {"type": "string"}}, "required": ["document"]}),
    ToolSpec("get_followup_guidance",
             "Official guidance for follow-up questions on the active claim: submission timing, how/where to submit, "
             "file format, processing time after submission, receipt confirmation, missing-document alternatives.",
             {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}),
    ToolSpec("list_my_claims", "List all claims belonging to the verified caller (brief).",
             {"type": "object", "properties": {}}),
    ToolSpec("get_claim", "Full fact sheet for one of the caller's claims by case ID.",
             {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]}),
    ToolSpec("offer_human_review",
             "Use when the caller needs something the claim file and guidelines can't answer, a manual exception, "
             "or when document alternatives are exhausted. Records that a human transfer is being offered.",
             {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}),
]

AGENT_RULES = """
You are now helping a VERIFIED caller with a specific claim. You may reason freely about what they mean and how
best to explain it, but every factual statement (status, reasons, documents, amounts, dates, deadlines, timelines,
procedures) must come from CLAIM FACTS, PRE-MATCHED GUIDANCE, or a tool result. If none of them answers the
question, say plainly that you don't have that information and offer a human claims representative.

- Explain money fields in plain language using their 'meaning'; don't do speculative math.
- Mention the appeal deadline when relevant to what they should do next (days_until_appeal_deadline is computed for you;
  if appeal_deadline_passed is true, say so and offer human review).
- Never predict whether an appeal or resubmission will be approved.
- If the caller asks about another of THEIR claims, you may use list_my_claims / get_claim.
- Layout: first paragraph = the direct answer in one or two sentences. Then, only if needed, a second short paragraph or
  a "- " bullet list of the key facts or steps. Last line = one short question. Blank line between paragraphs. Never
  repeat a fact. The caller also sees a claim summary card (status, reason, documents, deadline), so give the facts that
  matter for THEIR question, not every field.
- Lead with the direct answer in the first sentence; no preamble.
- Mention the claim ID (e.g. CL-2048) the first time you discuss a claim, so the caller can quote it later.
- When there are 2+ documents or steps, put them on separate lines starting with "- ". Otherwise write short prose.
- Quote dates and amounts exactly as in the facts (e.g. "March 18, 2026", "$1,450.00").
- Answer what was asked. Don't front-load every requirement or procedure; offer the detail instead
  (e.g. "I can walk you through what each document needs to show").
- Keep it tight: usually 2-4 sentences plus any short list. End with one helpful next-step question when natural.
- Out-of-scope questions are handled before you are called; stay on insurance topics.

Accuracy of wording:
- Describe the claim exactly as the record does. The CUSTOMER filed the claim ("you filed it on…", never "we filed").
  Don't add activity the record doesn't state (no "we're reviewing it" unless the facts say so).
- Only bring up documents if CLAIM FACTS has documents_needed. Otherwise don't suggest anything is missing.
- You cannot take actions. Never promise callbacks, that someone "will reach out", that you'll "set up", "arrange" or
  "make sure" anything, and never ask for contact details. You can explain options from the guidance (portal, fax,
  mail), and you can OFFER a transfer to a claims representative as a question (call offer_human_review).
- A one- or two-word reply ("mail", "the first one", "yes") answers your previous question. Continue from it.
- Never offer information you don't have in the facts (e.g. a mailing address, fax number, phone number, or a
  person's name). If the caller needs it, say a claims representative can provide it and offer the transfer.
- No stock phrases ("I get it", "Here's the good news", "The key is", "totally up to you").
"""


class ProcessAgent:
    def __init__(self, repo: Repository, party_id: str):
        self.repo, self.party_id = repo, party_id
        self.seen: list[Any] = []          # everything shown to the model -> grounding guard
        self.tool_log: list[dict[str, Any]] = []
        self.offer_human: str | None = None
        self.case_ids_touched: set[str] = set()
        self.topics: list[str] = []          # guideline topics the model was given or fetched
        self.fallback_used = False

    def _run_tool(self, name: str, args: dict[str, Any], active_case: dict[str, Any]) -> dict[str, Any]:
        today = settings.demo_today
        if name == "get_document_guidance":
            out = document_guidance(self.repo, active_case, args.get("document", ""))
        elif name == "get_followup_guidance":
            out = followup_guidance(self.repo, active_case, args.get("question", ""), None)
            self.topics += [m["topic"] for m in out["matched"] if m["topic"] not in self.topics]
        elif name == "list_my_claims":
            out = {"claims": [self.repo.brief(c) for c in self.repo.cases_for(self.party_id)]}
        elif name == "get_claim":
            c = self.repo.case(self.party_id, args.get("case_id", ""))
            out = self.repo.fact_sheet(c, today) if c else {"error": "No claim with that ID on this caller's account."}
            if c:
                self.case_ids_touched.add(c["case_id"])
        elif name == "offer_human_review":
            self.offer_human = args.get("reason", "caller needs manual review")
            out = {"ok": True, "note": "Offer the transfer to the caller and ask if they'd like it."}
        else:
            out = {"error": f"unknown tool {name}"}
        self.seen.append(out)
        self.tool_log.append({"tool": name, "args": args, "result": out})
        return out

    def answer(self, case: dict[str, Any], question: str, intent: str | None, transcript: list[dict[str, Any]],
               directive: Directive, correction: str | None = None) -> str:
        today = settings.demo_today
        sheet = self.repo.fact_sheet(case, today)
        pre = followup_guidance(self.repo, case, question, intent)
        self.topics += [m["topic"] for m in pre["matched"] if m["topic"] not in self.topics]
        self.fallback_used = "fallback" in pre and not pre["matched"]
        docs = {d: document_guidance(self.repo, case, d) for d in case.get("documents_needed", [])}
        self.seen += [sheet, pre, docs, {"today": today.isoformat(), "today_pretty": pretty_date(today.isoformat())}]
        self.case_ids_touched.add(case["case_id"])

        preamble = [{"act": a.kind, "instruction": a.instruction, "facts": a.facts} for a in directive.acts]
        context = {
            "TODAY": pretty_date(today.isoformat()),
            "CLAIM FACTS": sheet,
            "PRE-MATCHED GUIDANCE for this question": pre,
            "DOCUMENT GUIDANCE for the documents this claim needs": docs,
        }
        if preamble:
            context["ALSO ACCOMPLISH FIRST (briefly, in the same message)"] = preamble
        if directive.empathy and directive.empathy.get("mode"):
            context["EMPATHY"] = {k: directive.empathy.get(k) for k in ("label", "intensity", "cue", "style")
                                  if directive.empathy.get(k)}
        system = PERSONA + "\n" + AGENT_RULES + "\n\nCONTEXT:\n" + json.dumps(context, indent=1, default=str)

        convo = "\n".join(f"{'CALLER' if m['role'] == 'user' else 'ALEX'}: {m['content']}" for m in transcript[-10:])
        ask = (f"Conversation so far:\n{convo}\n\nThe caller's current need (intent: {intent or 'unspecified'}): "
               f"{question}\n\nWrite ALEX's next message.")
        if correction:
            ask += f"\n\nYour previous draft was rejected by the grounding check: {correction}. Remove anything not in the facts."
        messages: list[dict[str, Any]] = [{"role": "user", "content": ask}]
        llm = get_llm()
        spoken: list[str] = []  # text the model wrote alongside tool calls (it may say nothing after the result)
        for _ in range(4):
            resp = llm.complete(system, messages, tools=TOOLS, max_tokens=600, temperature=0.3)
            if not resp.tool_calls:
                # the model often writes its answer, calls a tool, then adds only a closing line;
                # keep everything it said to the caller, in order
                return "\n\n".join(spoken + [resp.text.strip()]).strip()
            if resp.text.strip():
                spoken.append(resp.text.strip())
            messages.append({"role": "assistant", "content": resp.text,
                             "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in resp.tool_calls]})
            messages.append({"role": "tool", "results": [
                {"id": c.id, "content": self._run_tool(c.name, c.input, case)} for c in resp.tool_calls]})
        resp = llm.complete(system, messages + [{"role": "user", "content": "Answer the caller now without tools."}],
                            max_tokens=600, temperature=0.3)
        return resp.text.strip()

    @staticmethod
    def template(case: dict[str, Any], sheet: dict[str, Any]) -> str:
        """Deterministic fallback when the model output fails the grounding check."""
        parts = [f"Claim {case['case_id']} ({case['case_type']}, filed {sheet['filed_on_pretty']}) is currently {case['status']}."]
        if sheet.get("denial_reason"):
            parts.append(f"It was denied because {sheet['denial_reason']}.")
        if sheet.get("documents_needed"):
            parts.append("To have it reconsidered, please submit: " + ", ".join(sheet["documents_needed"]) + ".")
        if sheet.get("appeal_deadline_pretty"):
            parts.append(f"The appeal deadline is {sheet['appeal_deadline_pretty']}.")
        parts.append("What else can I help you with on this claim?")
        return " ".join(parts)
