# Design notes

## 1. The core problem

A plain LLM agent with an SOP in its system prompt fails in predictable ways. It gets talked out of verification
("I already told you who I am"). It leaks details while trying to be helpful ("I see your claim was denied, but
first…"). It forgets what the caller said three turns ago, and it invents policy when the file doesn't answer the
question. Stricter prompts don't fix this, because the model still holds all the power.

This harness **splits responsibilities by risk**:

| Concern | Owner | Why |
|---|---|---|
| Which phase we're in, when it changes | Code (transition table + gate checks) | Must be impossible to talk past |
| Who the caller is | Code (deterministic matcher) | Identity is a yes/no fact, not a judgement |
| What data the model can see | Code (per-phase visibility) | A model can't leak what it never received |
| What the caller *meant* | LLM extractor (typed schema) | Messy language is the LLM's strength |
| Which claim / intent among bounded options | LLM proposes, code validates | Interpretation is flexible, outcome is bounded |
| Explaining a claim, answering follow-ups | LLM agent over read-only tools | The most freedom, where it's useful |
| How it's phrased | LLM responder | Natural tone |
| Whether the reply is safe | Code (output guard) → regenerate → template | Defence in depth |

## 2. Turn pipeline

```
caller text
  │
  ├─ regex pre-extractor ........ emails, phones, DOBs, SSN-4, policy & case IDs (exact, cheap, no hallucination)
  ├─ LLM extractor (forced tool) . identity, caller role, intent, case hints, dialogue acts, scope, emotion
  ├─ memory merge ............... slots with provenance; future-phase info stored as DEFERRED
  ├─ global guards .............. handoff answers, human request, injection, out-of-scope ladder
  ├─ phase handler(s) ........... may chain transitions in one turn (verify → resolve → process)
  ├─ render ..................... responder (STRICT/GUIDED) or grounded agent (PROCESS_CASE)
  ├─ output guard ............... disclosure firewall / grounding check / PII echo → retry once → template
  └─ trace ...................... everything above, shown in the UI's Trace tab
```

The extractor sees the agent's previous message, the current phase and the pending question type. That lets it
read "yes", "the first one" and "it's 4472" correctly. It never sees claims data.

## 3. Autonomy per phase

Declared as data in [phases.py](../apps/insurance_claims/backend/app/sop/phases.py) (`PhaseSpec.autonomy`, `claim_data_visible`, `allowed_actions`):

- **VERIFY_ID: STRICT.** The handler computes the next act (`ask_identity`, `verification_failed`,
  `note_for_later`, `consent_requested`…). The responder phrases it with **no claim data in context**.
- **RESOLVE_INTENT: GUIDED.** Remembered hints (`case_type`, `status`, `month`, `year`, `case_id`) filter the
  caller's claims. If nothing matches, hints are relaxed least-reliable first and the agent says so. If several
  match, it asks one targeted question using the distinguishing attributes. Intents like "why was it denied" imply
  a soft `status=denied` filter. A case ID that belongs to another party simply doesn't resolve (lookups are
  party-scoped).
- **PROCESS_CASE: GROUNDED-FREE.** The agent gets the claim fact sheet (with a computed deadline countdown),
  the guideline rules pre-matched to the question, per-document guidance, and tools (`get_document_guidance`,
  `get_followup_guidance`, `list_my_claims`, `get_claim`, `offer_human_review`). Every tool is bound to the
  verified `party_id`. The grounding guard rejects any case ID, dollar amount or calendar date in the reply that
  isn't in what the model was shown or what the caller said.
- **POST_PROCESS: STRICT consent.** Offer → explicit yes/no → send or skip → "anything else?" (which can re-enter
  RESOLVE_INTENT) → close.

## 4. Memory across phase boundaries

`Memory` is a slot store where each slot records `value, turn, phase, source, status, history`. Each slot family
has a consumer phase (`identity → VERIFY_ID`, `case/intent/request → RESOLVE_INTENT`, `contact → POST_PROCESS`).
A slot heard *before* its consumer phase is stored as **deferred**. When the consumer runs, it **consumes** the slot
and the trace notes "resolved using hints remembered from turn N".

During VERIFY_ID the agent may acknowledge a deferred hint, but only by paraphrasing the caller's own words ("I've
noted you're calling about your denied claim; I'll pull it up as soon as you're verified"). The guard allows this
because the terms came from the caller.

Two grounding rules keep memory honest:
- A `year` or `case_id` the caller never typed is dropped. The model guessing "2025" for "January" is exactly the
  kind of silent error this catches.
- Identity values that fail an attempt are marked **rejected** rather than deleted, so the inspector shows what
  happened. The caller is never told which field failed.

## 5. Verification details

- Factors: name, DOB, phone, email, government-ID last 4. **Policy number is a lookup key, not a factor.** A
  contradicting policy number still fails the attempt.
- Normalization: many DOB formats; E.164 phones; spoken emails ("margaret at email dot com"); name order
  ("Tian Ma"); middle initials; alias lists ("Yaven Li"); a conservative fuzzy match for speech-to-text-style typos.
- `ssn_last4` and `national_id_last4` are one factor, so a caller who says "SSN" for a national ID isn't rejected.
- Success needs ≥3 matching factors on exactly one record, with no contradictions.
- An *attempt* is a turn with new identity info and ≥3 factors (or ≥2 with a contradiction). Three failed attempts
  lock verification and transfer to a human. Re-verifying requires new information, so dropping the wrong field and
  waiting for the remaining three to match doesn't pass.
- Representatives: the policyholder's 3 factors, the rep's name on file for that party, and a live consent request
  (`consent_scenarios.json`: `pending → approved`, or a timeout). An unknown third party learns nothing, not even
  whether claims exist.

## 6. Emotion and recovery

The extractor reports `{label, intensity, cue}` every turn. [emotion.py](../apps/insurance_claims/backend/app/sop/emotion.py) turns that into:

1. **Style guidance** per emotion (anger: concise and non-defensive; anxiety: steps that restore control;
   confusion: one thing at a time).
2. **A rationale** whenever the caller pushes against a gate: why identity checks protect *them*, why consent
   protects the policyholder, why emails only go to the address on file. The responder must paraphrase it, not
   recite it.
3. **Alternatives**: other ID fields (refusals are remembered), the policyholder contacting us directly, or a human.
4. **A stop rule**: 3 consecutive high-frustration turns, very high anger, or 3 push-backs at a gate → a sincere
   offer of a human, while staying willing to continue. An explicit request for a human is honoured immediately.
   The handoff packet (phase, verification status, case, intent, emotion, recent transcript) means the caller
   doesn't repeat themselves.

Persuasion never relaxes a gate. The gates are in code, so there is nothing for the model to concede.

## 7. Failure modes and fallbacks

| Failure | Behaviour |
|---|---|
| LLM extractor errors | Heuristic extractor (regex + keyword acts) |
| Responder output fails the guard | One regeneration with the violation named, then the deterministic template |
| Reply promises an action the system can't take ("someone will reach out", "I'll set that up") | Commitment guard rejects it → regenerate → template. Allowed only on steps that really act (transfer, email sent, consent request) |
| Agent output ungrounded or empty | Retry, then a template built from the fact sheet |
| Summary bullets ungrounded | Template bullets; claim facts are code-rendered anyway |
| Wrong key / unreachable model | *Test connection* reports the provider's own error; nothing is switched until it passes |
| Model rejects a parameter (newer SDKs/models) | Adapters retry without it (`temperature`, `max_tokens` → `max_completion_tokens`, forced `tool_choice`) |
| Model answers with JSON text instead of a tool call | Structured output is parsed from the text |

The whole SOP runs with no model at all (*rules-only*), which is how `tests/unit/test_flow_offline.py` shows the
workflow and gates hold without one.

Two more "don't trust the model" rules sit in the memory merge. A `year` or claim ID the caller never typed is
dropped. A *refused* field only counts if the caller actually named it, because "this is ridiculous, just tell me"
is frustration, not a refusal to give a date of birth.

## 8. Any model

`apps/insurance_claims/backend/app/llm/` has three adapters: Bedrock Converse, Anthropic Messages, and OpenAI Chat Completions. The
OpenAI one also covers Gemini's OpenAI endpoint. Supported providers are OpenAI, Anthropic, Gemini and AWS Bedrock;
each lists only the models that suit the app. A small registry holds the active model. It's configured from `.env` or at runtime from the UI, and
the key is held in memory, never persisted or echoed. The harness needs only two capabilities from a model: plain
completion and tool calling.

The live suite (20 functional + 16 adversarial conversations) passes 36/36 on Claude Haiku 4.5 and on Sonnet 4.5.
The functional part also passed through the OpenAI adapter. The
Anthropic adapter was also exercised end to end.

## 9. Reply layout and the UI

Replies have to be easy to read, whichever model writes them, so layout is enforced in code
(`app/sop/formatting.py`) and not left to the prompt alone:
- a long single paragraph is split into short paragraphs of at most two sentences
- a closing question goes on its own line
- claim numbers, dates and amounts are bold
- text the author already structured (blank lines, bullet lists) keeps its layout

Lists of options (identity details, the caller's claims) are real bullet lists. The opening message is structured
the same way. The UI also shows a **claim card** (status, reason, documents, deadline) built by the backend from
the claims system and not from the model's words, so the facts at a glance can't drift from the record, and the
agent's text can stay short.

The interface follows goaly.ai's visual language: warm off-white, ink with tinted greys, a single copper accent,
hairlines, pill buttons, Instrument Sans for the interface and Geist Mono for small uppercase labels. The tone is
deliberately plain: no slogans or marketing copy. The product name is generic ("Claims Support Agent"); the starter
data names no insurer, so none is invented.

The page has two states. It opens on a **landing screen** (a plain headline and one sentence on what the agent can
help with, the opening message, labelled *test scenarios*, one input box) and switches to the **chat** with a single
side rail (progress, identity tags, what was remembered) after the first message.

Follow-up suggestions (`app/sop/suggestions.py`) are deterministic and grounded, like the claim card. They are
derived from the workflow state: the phase, the pending question, the verified caller's real claim (document names,
status, amounts, deadline) and, when the agent asked "which claim?", the actual candidates, named consistently.
Before verification they are generic. Picked suggestions are remembered in the session so they aren't repeated.

## 10. Verifiability

`explain.py` turns each turn's trace into a short, deterministic explanation. It's shown in the side panel's
collapsed *How each reply was handled* section, not in the chat, so the conversation stays clean. It covers:
- the step and its autonomy level, and any step changes
- the identity and consent gates
- whether claim data was locked for that reply
- what was remembered, and what was used to pick the claim (and from which earlier message)
- which sources the facts came from (claim file, named guideline topics, document requirements)
- tone handling
- privacy and fact-check results, including when a draft was blocked and rewritten

No model writes it, so it's an honest record rather than a self-report. The raw trace is available in the API
(`trace` in each `/api/chat` response).

## 11. Trade-offs and what I'd do next

- **Latency.** Two model calls per turn (extract + respond), about 2–5 s on Haiku 4.5. The extractor and responder
  could be fused for STRICT turns, or the extractor run on a smaller model; the split was kept because it makes
  the firewall structural.
- **Sessions are in memory.** That's fine for a demo. Production would use Redis/Postgres, plus an audit log of
  traces (they're already structured for it).
- **Consent is simulated per turn** (one poll per caller message), matching the fixture's sequence model. A real
  system would use an async callback/webhook.
- **The guard is lexical.** It catches IDs, amounts, dates, documents and PII. Paraphrased leaks ("the lab
  report wasn't there") are prevented upstream, because pre-verification the model has no claim data to
  paraphrase.
