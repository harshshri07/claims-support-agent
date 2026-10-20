"""Render the post-call summary email (HTML + plain text)."""
from __future__ import annotations

from html import escape
from typing import Any


def render_email(*, to_name: str, cases: list[dict[str, Any]], discussed: list[str], next_steps: list[str],
                 handoff: dict[str, Any] | None, call_date: str) -> tuple[str, str]:
    first = to_name.split()[0]
    text = [f"Hi {first},", "", f"Here's a summary of your call with Harborview Mutual claims support on {call_date}.", ""]
    if discussed:
        text += ["WHAT WE DISCUSSED"] + [f"- {d}" for d in discussed] + [""]
    if cases:
        text.append("CLAIM STATUS")
        for c in cases:
            text.append(f"- {c['case_id']} ({c['claim_type']}, filed {c['filed_on_pretty']}): {c['status'].upper()}")
            if c.get("denial_reason"):
                text.append(f"  Reason: {c['denial_reason']}")
            if c.get("documents_needed"):
                text.append(f"  Documents needed: {', '.join(c['documents_needed'])}")
            if c.get("appeal_deadline_pretty"):
                text.append(f"  Appeal deadline: {c['appeal_deadline_pretty']}")
        text.append("")
    if next_steps:
        text += ["NEXT STEPS"] + [f"{i}. {s}" for i, s in enumerate(next_steps, 1)] + [""]
    if handoff:
        text += [f"A claims representative is following up (reference {handoff['ticket_id']}).", ""]
    text += ["If anything here looks wrong, reply to this email or call us and mention your claim number.",
             "", "— Alex, Harborview Mutual Claims Support"]

    def li(items):
        return "".join(f"<li>{escape(i)}</li>" for i in items)

    case_rows = ""
    for c in cases:
        extra = ""
        if c.get("denial_reason"):
            extra += f"<div><b>Reason:</b> {escape(c['denial_reason'])}</div>"
        if c.get("documents_needed"):
            extra += f"<div><b>Documents needed:</b> {escape(', '.join(c['documents_needed']))}</div>"
        if c.get("appeal_deadline_pretty"):
            extra += f"<div><b>Appeal deadline:</b> {escape(c['appeal_deadline_pretty'])}</div>"
        color = {"denied": "#b42318", "open": "#b54708", "closed": "#067647"}.get(c["status"], "#344054")
        case_rows += (f"<div style='border:1px solid #e4e7ec;border-radius:8px;padding:12px;margin:8px 0'>"
                      f"<div><b>{escape(c['case_id'])}</b> · {escape(c['claim_type'])} · filed {escape(c['filed_on_pretty'])} "
                      f"<span style='color:{color};font-weight:600;text-transform:uppercase;margin-left:6px'>{escape(c['status'])}</span></div>"
                      f"<div style='margin-top:6px;font-size:14px;color:#475467'>{extra}</div></div>")
    html = f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:600px;color:#101828;line-height:1.5">
<div style="background:#0b3b5c;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0"><b>Harborview Mutual</b> · Call summary</div>
<div style="border:1px solid #e4e7ec;border-top:0;padding:20px;border-radius:0 0 8px 8px">
<p>Hi {escape(first)},</p><p>Here's a summary of your call with claims support on {escape(call_date)}.</p>
{"<h3 style='font-size:15px'>What we discussed</h3><ul>" + li(discussed) + "</ul>" if discussed else ""}
{"<h3 style='font-size:15px'>Claim status</h3>" + case_rows if cases else ""}
{"<h3 style='font-size:15px'>Next steps</h3><ol>" + li(next_steps) + "</ol>" if next_steps else ""}
{f"<p>A claims representative is following up (reference <b>{escape(handoff['ticket_id'])}</b>).</p>" if handoff else ""}
<p style="font-size:13px;color:#667085">If anything here looks wrong, reply to this email or call us and mention your claim number.</p>
<p>— Alex, Harborview Mutual Claims Support</p></div></div>"""
    return html, "\n".join(text)
