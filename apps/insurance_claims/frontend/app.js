// Claims support agent: test console
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STEPS = [
  ["VERIFY_ID", "Verify identity", "Claim details stay locked until 3 details match."],
  ["RESOLVE_INTENT", "Find the claim", "Works out which claim and what you need."],
  ["PROCESS_CASE", "Help with the claim", "Answers only from the claim file and guidelines."],
  ["POST_PROCESS", "Wrap up", "Offers an email summary. Sending is your choice."],
];
const IDS = [["name", "Full name"], ["dob", "Date of birth"], ["phone", "Phone"], ["email", "Email"], ["id_last4", "SSN / ID last 4"]];
const INTENTS = { denial_question: "Why a claim was denied", status_inquiry: "Claim status", document_submission: "Sending documents",
  next_steps: "Next steps", payment_question: "Payment amounts", appeal: "Appealing", general_claim_question: "Question about a claim",
  unsupported_request: "Something this line can't handle" };
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

const DEMO = "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.";
const STARTERS = [
  ["Run the demo case", DEMO],
  ["Frustrated caller", "I already told you who I am. This is ridiculous. Just tell me why my claim was denied."],
  ["Off-topic question", "What is RL?"],
  ["Calling for a parent", "Hi, I'm David Chen, calling for my mom Margaret Chen. Her date of birth is March 15, 1985 and her SSN ends in 4472."],
];
const SCENARIOS = [
  { title: "Demo case", desc: "Margaret gives her details and asks about her denied claim.", text: STARTERS[0][1] },
  { title: "Frustrated caller", desc: "“I already told you who I am. This is ridiculous…”", text: STARTERS[1][1] },
  { title: "Off-topic question", desc: "“What is RL?” It should politely decline.", text: STARTERS[2][1] },
  { title: "Calling for a parent", desc: "A son calls about his mother's claim. Her approval is needed.", text: STARTERS[3][1] },
];

let sid = null, state = null, traces = [], busy = false, userTurns = 0, consent = "default";

// Backend address: same origin by default (the backend serves this UI); see config.js.
const API_BASE = (window.API_BASE || "").replace(/\/$/, "");
// A random id for this browser tab: a model/key chosen here applies to this tab only, never to other visitors.
const CLIENT_ID = (() => {
  try { return sessionStorage.getItem("cid") || (sessionStorage.setItem("cid", crypto.randomUUID()), sessionStorage.getItem("cid")); }
  catch { return crypto.randomUUID(); }
})();

async function api(path, body, method) {
  const headers = { "X-Client-Id": CLIENT_ID };
  const opts = { method: method || (body === undefined ? "GET" : "POST"), headers };
  if (body !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(API_BASE + path, opts);
  if (!r.ok) {
    const err = new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    err.status = r.status; throw err;
  }
  return r.json();
}

// ---------- thread ----------
const thread = () => $("#thread");
function scroll() { $("#messages").scrollTop = 1e9; }
// light formatting for agent replies: paragraphs, "- " bullet lists, **bold**
function format(text) {
  const out = [];
  let list = null;
  const flush = () => {
    if (!list) return;
    const compact = list.length >= 4 && list.every((i) => i.plain.length <= 34);   // short options flow into two columns
    out.push(`<ul${compact ? ` class="cols" style="--rows:${Math.ceil(list.length / 2)}"` : ""}>${list.map((i) => `<li>${i.html}</li>`).join("")}</ul>`);
    list = null;
  };
  for (const raw of esc(text).split("\n")) {
    const line = raw.trim().replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    const item = line.match(/^[-•*]\s+(.*)/);
    if (item) { (list ||= []).push({ html: item[1], plain: item[1].replace(/<[^>]+>/g, "") }); continue; }
    flush();
    if (line) out.push(`<p>${line}</p>`);
  }
  flush();
  return out.join("");
}

function addMsg(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  if (role === "user") { el.textContent = text; thread().appendChild(el); scroll(); return; }
  el.innerHTML = format(text);
  thread().appendChild(agentRow(el)); scroll();
}
const AVATAR = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4.2 5.8 6.6v4.8c0 3.7 2.5 6.6 6.2 8 3.7-1.4 6.2-4.3 6.2-8V6.6L12 4.2Z"/><circle cx="12" cy="11.6" r="2.1"/></svg>`;
function agentRow(el) {
  const row = document.createElement("div");
  row.className = "agent-row";
  row.innerHTML = `<span class="avatar">${AVATAR}</span><div class="agent-wrap"></div>`;
  row.lastElementChild.appendChild(el);
  return row;
}
function addNote(html, kind = "") {
  const el = document.createElement("div");
  el.className = `note ${kind}`;
  el.innerHTML = html;
  thread().appendChild(el); scroll();
  return el;
}
function typing(on) {
  if (on) {
    const el = document.createElement("div");
    el.className = "msg agent typing";
    el.innerHTML = "<span></span><span></span><span></span>";
    const row = agentRow(el); row.id = "typing";
    thread().appendChild(row); scroll();
  } else $("#typing")?.remove();
}

// milestones shown inline, in plain words
function milestones(prev, next) {
  if (next.claim && next.claim.case_id !== prev.claim?.case_id) addClaimCard(next.claim);
  if (next.outbox_count > prev.outbox_count) {
    const n = addNote(`Email summary sent · <button>View</button>`, "ok");
    n.querySelector("button").onclick = showMail;
  }
  if (next.phase === "HANDOFF" && prev.phase !== "HANDOFF") addNote(`Transferred to a person · ref ${esc(next.handoff?.ticket_id)}`, "warn");
}

async function send(text) {
  text = text.trim();
  if (!text || busy || !sid) return;
  busy = true; lockInput();
  $("#input").value = ""; autosize();
  document.body.classList.remove("landing"); $("#hero")?.remove(); $("#scenarios")?.remove();
  addMsg("user", text); userTurns++; renderChips(true);
  typing(true);
  try {
    const r = await api("/api/chat", { session_id: sid, message: text });
    typing(false);
    addMsg("agent", r.reply);
    if (r.trace.model_error) addNote("The model didn't respond, so this reply is a safe pre-written one. Try again in a moment, or check <button>Model</button>.", "warn")
      .querySelector("button").onclick = () => openModel();
    const prev = state; state = r.state; traces.push(r.trace);
    milestones(prev, state);
    render();
  } catch (e) {
    typing(false);
    if (e.status === 409) {
      thread().lastElementChild?.remove(); userTurns--; if (userTurns === 0) { document.body.classList.add("landing"); showHero(); showScenarios(); } renderChips();
      $("#input").value = text; openModel("Connect a model first, or choose “No model (rules-only check)”.");
    } else addNote(`Something went wrong: ${esc(e.message)}`, "warn");
  } finally {
    busy = false; lockInput();
    if (!ended()) $("#input").focus();
  }
}

async function newChat() {
  $("#messages").innerHTML = `<div class="thread" id="thread"></div>`;
  traces = []; userTurns = 0;
  document.body.classList.add("landing"); document.body.classList.remove("rail-open");
  let r;
  try {
    r = await api("/api/session", { consent_scenario: consent });
  } catch (e) {   // never leave a blank page: say what happened and offer a retry
    $("#thread").innerHTML = `<div class="hero"><h1>Claims support</h1><p class="sub">${esc(e.message === "Failed to fetch" ? "Can't reach the server. Check your connection and try again." : e.message)}</p>
      <div class="try"><button class="btn" id="retryBtn">Try again</button></div></div>`;
    $("#retryBtn").onclick = newChat;
    return;
  }
  sid = r.session_id; state = r.state;
  addMsg("agent", r.reply);
  showHero(); showScenarios();
  $("#messages").scrollTop = 0;
  render(); lockInput(); $("#input").focus({ preventScroll: true });
}

function showHero() {
  const el = document.createElement("div");
  el.className = "hero"; el.id = "hero";
  el.innerHTML = `<div class="eyebrow"><span class="sdot"></span><span class="label">Demo environment · fictional customers</span></div>
    <h1>Claims support</h1>
    <p class="sub">Verify your identity to get started. You can then ask about the status of a claim, the documents it needs, deadlines and next steps.</p>`;
  thread().prepend(el);
}

// Test affordance, clearly labelled as such: each button sends a ready-made caller message.
function showScenarios() {
  const el = document.createElement("div");
  el.className = "scenarios"; el.id = "scenarios";
  el.innerHTML = `<span class="label">Test scenarios · click one to send it</span>
    <div class="cards">${SCENARIOS.map((c, i) => `<button class="scn" data-i="${i}" title="${esc(c.text)}">
      <b>${esc(c.title)}</b><span class="d">${esc(c.desc)}</span></button>`).join("")}</div>`;
  el.querySelectorAll(".scn").forEach((b) => b.onclick = () => send(SCENARIOS[+b.dataset.i].text));
  thread().appendChild(el);
}

// A compact claim summary, built by the backend from the claims system (not written by the model).
const TYPE_NAMES = { healthcare: "Healthcare", dental: "Dental", auto: "Auto" };
function addClaimCard(c) {
  const rows = [];
  if (c.reason) rows.push(["Why", esc(c.reason.charAt(0).toUpperCase() + c.reason.slice(1))]);
  if (c.documents_needed?.length) rows.push(["Needed", `<div class="docs">${c.documents_needed.map((d) => `<span>${esc(d)}</span>`).join("")}</div>`]);
  if (c.appeal_deadline) {
    const left = c.deadline_passed ? `<span class="days late">deadline passed</span>` : `<span class="days">${c.days_left} day${c.days_left === 1 ? "" : "s"} left</span>`;
    rows.push(["Appeal by", `${esc(c.appeal_deadline)} · ${left}`]);
  }
  if (c.expected_payment) rows.push(["Expected", esc(c.expected_payment)]);
  if (c.paid_so_far) rows.push(["Paid so far", esc(c.paid_so_far)]);
  const el = document.createElement("div");
  el.className = "claim";
  el.innerHTML = `<div class="claim-head"><span class="label">Claim ${esc(c.case_id)}</span><span class="status ${esc(c.status)}">${esc(c.status.charAt(0).toUpperCase() + c.status.slice(1))}</span></div>
    <h4>${esc(TYPE_NAMES[c.claim_type] || c.claim_type)} claim <span>· filed ${esc(c.filed_on)}</span></h4>
    ${rows.length ? `<dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>` : ""}`;
  thread().appendChild(el); scroll();
}

const ended = () => state && ["HANDOFF", "CLOSED"].includes(state.phase);
function lockInput() {
  const off = busy || ended();
  $("#input").disabled = off; $("#sendBtn").disabled = off;
  $("#input").placeholder = ended() ? "This conversation has ended. Start a new chat to try again." : "Reply as the caller…";
}

// ---------- side panel ----------
function render() { renderSteps(); renderStrip(); renderIds(); renderMemory(); renderTrace(); renderChips(); }

function renderSteps() {
  const order = STEPS.map((s) => s[0]);
  let idx = order.indexOf(state.phase);
  if (idx === -1) idx = order.length;                       // closed / handed off
  const reached = new Set(traces.flatMap((t) => [t.phase_before, t.phase_after, ...(t.transitions || []).map((x) => x.to)]));
  $("#steps").innerHTML = STEPS.map(([key, title, desc], i) => {
    let cls = i < idx ? "done" : i === idx ? "now" : "";
    if (idx === order.length) cls = reached.has(key) ? "done" : "";
    return `<li class="${cls}"><span class="dot"></span><div class="t">${title}</div><div class="d">${desc}</div></li>`;
  }).join("") + (state.phase === "HANDOFF" ? `<li class="now"><span class="dot"></span><div class="t">With a person</div><div class="d">Ref ${esc(state.handoff?.ticket_id)}. Notes were passed along.</div></li>` : "");
}

function renderStrip() {
  const order = STEPS.map((s) => s[0]), i = order.indexOf(state.phase);
  const bars = STEPS.map((_, k) => `<span class="${i === -1 || k < i ? "done" : k === i ? "now" : ""}"></span>`).join("");
  const label = state.phase === "HANDOFF" ? "<b>With a person</b>" : state.phase === "CLOSED" ? "<b>Conversation ended</b>"
    : `${i + 1}/4 · <b>${STEPS[i][1]}</b>`;
  const v = state.verification;
  const id = v.verified ? "Verified" : `ID ${Math.min(v.collected.length, 3)}/3`;
  $("#strip").innerHTML = `<div class="bars">${bars}</div><span>${label}</span><span class="idc">${id}</span><span class="chev" aria-hidden="true">▾</span>`;
}

function renderIds() {
  const v = state.verification, mem = state.memory;
  const given = Math.min(v.collected.length, 3);
  $("#idHint").textContent = v.verified ? "Verified" : `${given} of 3`;
  $("#idBar").className = `idbar ${v.verified ? "done" : ""}`;
  $("#idBar").innerHTML = [0, 1, 2].map((i) => `<i class="${v.verified || i < given ? "on" : ""}"></i>`).join("");
  const SHORT = { name: "Name", dob: "Date of birth", phone: "Phone", email: "Email", id_last4: "SSN / ID" };
  let rows = IDS.map(([k]) => {
    const s = mem[`identity.${k}`];
    let cls = "";
    if (v.matched_factors.includes(k)) cls = "ok";
    else if (s?.status === "rejected") cls = "bad";
    else if (s) cls = "given";
    return `<li class="idchip ${cls}">${SHORT[k]}</li>`;
  }).join("");
  if (v.caller_role === "representative") {
    const c = { pending: ["wait", "Approval pending"], approved: ["ok", "Approved"], timeout: ["bad", "Approval expired"], not_authorized: ["bad", "Not on file"], rep_name_needed: ["wait", "Need caller's name"] }[v.consent_status];
    if (c) rows += `<li class="idchip ${c[0]}">${c[1]}</li>`;
  }
  $("#ids").innerHTML = rows;
}

function memoryItems() {
  const m = state.memory, items = [];
  const val = (k) => m[k]?.value;
  const status = (keys) => {
    const s = keys.map((k) => m[k]).filter(Boolean);
    if (!s.length) return "";
    if (s.every((x) => x.status === "consumed")) return `<span class="badge ok">Used</span>`;
    if (s.some((x) => x.status === "deferred")) return `<span class="badge wait">Saved for later</span>`;
    return "";
  };
  const caseKeys = ["case.status", "case.case_type", "case.month", "case.year", "case.case_id"].filter((k) => m[k]);
  if (caseKeys.length) {
    const parts = [val("case.case_id"), val("case.status"), val("case.case_type"),
      val("case.month") ? `from ${MONTHS[val("case.month") - 1]}${val("case.year") ? " " + val("case.year") : ""}` : null].filter(Boolean);
    items.push(["Claim you mentioned", parts.join(" · "), status(caseKeys)]);
  }
  if (val("intent.primary")) items.push(["What you need", INTENTS[val("intent.primary")] || val("intent.primary"), status(["intent.primary"])]);
  if (val("caller.rep_name")) items.push(["Calling on behalf of someone", `${val("caller.rep_name")}${val("caller.relationship") ? ` (${val("caller.relationship")})` : ""}`, ""]);
  const refused = Object.keys(m).filter((k) => k.startsWith("refused.")).map((k) => IDS.find(([f]) => f === k.split(".")[1])?.[1]).filter(Boolean);
  if (refused.length) items.push(["Prefers not to share", refused.join(", "), ""]);
  if (state.active_case_id) items.push(["Open claim", state.active_case_id, ""]);
  return items;
}

function renderMemory() {
  const items = memoryItems();
  $("#mem").innerHTML = items.length
    ? items.map(([k, v, b]) => `<li><div class="k"><span>${k}</span>${b}</div>${esc(v)}</li>`).join("")
    : `<li class="empty">Anything useful you mention is kept here, even before you're verified.</li>`;
}

function renderTrace() {
  const mark = { true: "✓", false: "!", null: "·" };
  $("#trace").innerHTML = traces.slice().reverse().map((t) => `<div class="turn">Reply ${t.turn}</div><ul class="checks">${
    (t.explain || []).map((e) => `<li class="${e.ok === true ? "ok" : e.ok === false ? "bad" : ""}"><i>${mark[e.ok]}</i>${esc(e.text)}</li>`).join("")}</ul>`
  ).join("") || `<p class="empty">Send a message to see how each reply was handled.</p>`;
}

function renderChips(clear) {
  const box = $("#chips");
  if (clear || document.body.classList.contains("landing")) { box.innerHTML = ""; return; }
  if (ended()) {
    box.innerHTML = `<button class="chip" id="restart">Start a new chat</button>`;
    $("#restart").onclick = newChat; return;
  }
  const list = state?.suggestions || [];
  box.innerHTML = list.length ? `<span class="label">You could say</span>` + list.map((c, i) => `<button class="chip" data-i="${i}">${esc(c.label)}</button>`).join("") : "";
  box.querySelectorAll(".chip").forEach((b) => b.onclick = () => send(list[+b.dataset.i].text));
}

// ---------- dialogs ----------
async function showMail() {
  const { emails } = await api(`/api/session/${sid}/outbox`);
  const m = emails[emails.length - 1];
  $("#mailTitle").textContent = m.subject;
  $("#mailFrame").srcdoc = m.html;
  $("#mailDlg").showModal();
}

async function showData() {
  const d = await api("/api/test-data");
  const fmtPhone = (n) => n.replace(/^\+1(\d{3})(\d{3})(\d{4})$/, "($1) $2-$3");
  const idLabel = () => "SSN / ID, last 4";
  const fact = (k, v) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`;
  const people = d.policyholders.map((p) => {
    const sample = `Hi, my name is ${p.name}. My date of birth is ${p.dob} and the last four of my ${p.id_type === "ssn_last4" ? "SSN" : "national ID"} are ${p.id_last4}.`;
    return `<div class="person"><div class="n"><span>${esc(p.name)}</span><button class="use" data-say="${esc(sample)}">Use in chat</button></div>
      <dl class="facts">${fact("Date of birth", p.dob)}${fact(idLabel(p), p.id_last4)}${fact("Phone", fmtPhone(p.phone))}${fact("Email", p.email)}</dl>
      ${p.name_aliases ? `<p class="note-line">Also answers to “${esc(p.name_aliases.join(", "))}”.</p>` : ""}</div>`;
  }).join("");
  const reps = d.representatives.map((r) => `<div class="person"><div class="n"><span>${esc(r.rep_name)}</span>
      <button class="use" data-say="${esc(STARTERS[3][1])}">Use in chat</button></div>
      <p class="note-line">${esc(r.buyer_name)}'s ${esc(r.relationship)}, listed as an authorized representative. ${esc(r.buyer_name)} has to approve before anything is shared.</p></div>`).join("");
  $("#dataBody").innerHTML = `<p class="muted small" style="margin:0">You're playing the caller. Pick a fictional customer and give the agent any three of their details to get verified. <b>Use in chat</b> puts a ready-made message in the box.</p>
    <h4 class="label sec">Policyholders</h4>${people}
    <h4 class="label sec">Authorized representative</h4>${reps}
    <label class="field"><span>When a representative calls</span>
      <select id="consentSel"><option value="default">The policyholder approves the request</option><option value="timeout">The policyholder doesn't respond</option></select></label>`;
  $("#consentSel").value = consent;
  $("#consentSel").onchange = (e) => { consent = e.target.value; $("#dataDlg").close(); newChat(); };
  $("#dataBody").querySelectorAll(".use").forEach((b) => b.onclick = () => {
    $("#dataDlg").close(); $("#input").value = b.dataset.say; autosize(); $("#input").focus();
  });
  $("#dataDlg").showModal();
}

// ---------- model ----------
const FIELD = {
  api_key: ["API key", "password"], model: ["Model", "text"], base_url: ["Base URL", "text"],
  api_version: ["API version", "text"], region: ["Region", "text"],
  aws_access_key_id: ["AWS access key ID", "password"], aws_secret_access_key: ["AWS secret access key", "password"],
};
let models = null;

async function refreshModel() {
  models = await api("/api/model");
  const short = (m) => (m || "").replace(/^(us|eu|apac|global)\.[a-z]+\./, "").replace(/-\d{8}(-v\d+:\d+)?$/, "");
  $("#modelName").textContent = models.configured ? (models.provider === "rules" ? "Rules only" : short(models.model) || models.provider_label) : "No model";
  $("#modelBtn").title = models.configured ? `Using ${models.provider_label}${models.model ? " · " + models.model : ""}. Click to switch model or use your own API key.` : "Add your API key to start";
  $("#modelCta").textContent = models.configured ? "Use your own key" : "Add your API key";
  $("#mdot").className = `mdot ${models.configured ? "on" : "off"}`;
  return models;
}

const OTHER = "__other";
// readable model name: drop the region prefix, vendor and date/version suffix (the full id stays the option's value)
const shortModel = (m) => (m || "").replace(/^(us|eu|apac|global)\.[a-z0-9]+\./, "").replace(/-\d{8}(-v\d+:\d+)?$/, "");

function modelOptions(list, selected) {
  const opts = [...new Set(list.filter(Boolean))];
  if (selected && selected !== OTHER && !opts.includes(selected)) opts.unshift(selected);
  return opts.map((m) => `<option value="${esc(m)}" title="${esc(m)}" ${m === selected ? "selected" : ""}>${esc(shortModel(m))}</option>`).join("")
    + `<option value="${OTHER}" ${selected === OTHER ? "selected" : ""}>Other (type a name)…</option>`;
}

function renderProviderFields() {
  const p = $("#provider").value, meta = models.providers[p];
  const hints = meta.hints || {};
  const sameAsConnected = models.server_provider === p;  // the shared server key can be reused for this provider
  if (p === "rules") {
    $("#providerFields").innerHTML = `<p class="muted small" style="margin:0">Runs the workflow with no model: understanding by patterns, replies from fixed templates. Conversation quality is basic, but it shows the gates and memory are enforced in code.</p>`;
  } else {
    $("#providerFields").innerHTML = meta.fields.map((f) => {
      const [label, type] = FIELD[f];
      if (f === "model" && p !== "azure") {
        const current = models.provider === p ? models.model : sameAsConnected ? models.server_model : meta.model;
        const list = sameAsConnected && models.server_key_models?.length ? [...models.server_key_models] : [...(meta.suggested || [])];
        const selected = current || list[0] || OTHER;
        return `<label class="field"><span>Model <small id="modelStatus"></small></span>
          <select id="modelChoice">${modelOptions(list, selected)}</select>
          <input id="modelOther" type="text" placeholder="${p === "openai_compatible" ? "e.g. qwen2.5:14b" : "exact model name"}" ${selected === OTHER ? "" : "hidden"}></label>`;
      }
      const def = f === "region" ? meta.region : f === "api_version" ? meta.api_version : f === "base_url" ? meta.base_url
        : f === "model" ? (models.provider === p ? models.model : "") : "";
      const label2 = p === "azure" && f === "base_url" ? "Endpoint" : p === "azure" && f === "model" ? "Deployment name" : label;
      const ph = type === "password" && sameAsConnected ? "Leave blank to use the shared demo key" : (hints[f] || "");
      return `<label class="field"><span>${label2}</span><input name="${f}" type="${type}" value="${esc(def || "")}" placeholder="${esc(ph)}" ${type === "password" ? 'autocomplete="new-password"' : ""}>${hints[f] && def ? `<small>${esc(hints[f])}</small>` : ""}</label>`;
    }).join("");
    const choice = $("#modelChoice");
    if (choice) choice.onchange = () => { $("#modelOther").hidden = choice.value !== OTHER; if (choice.value === OTHER) $("#modelOther").focus(); };
    // reload the real model list whenever the credentials change
    $("#providerFields").querySelectorAll('input[name="api_key"], input[name="aws_access_key_id"], input[name="aws_secret_access_key"], input[name="base_url"], input[name="region"]')
      .forEach((i) => i.addEventListener("input", () => { clearTimeout(listTimer); listTimer = setTimeout(loadModels, 700); }));
    if (sameAsConnected || p === "openai_compatible") loadModels();
  }
  $("#modelResult").className = "result"; $("#modelResult").textContent = "";
}

let listTimer = null, listSeq = 0;
async function loadModels() {
  const choice = $("#modelChoice"), status = $("#modelStatus");
  if (!choice) return;
  const body = modelPayload(); delete body.model;
  const p = body.provider, sameAsConnected = models.server_provider === p;
  const hasCreds = body.api_key || body.aws_access_key_id || (p === "openai_compatible" && body.base_url);
  if (!hasCreds && !sameAsConnected) { status.textContent = "· enter your key to load the full list"; return; }
  const seq = ++listSeq;
  status.textContent = "· loading available models…";
  try {
    const r = await api("/api/model/list", body);
    if (seq !== listSeq || $("#provider").value !== p) return;
    if (r.models?.length) {
      const keep = choice.value;
      const preferred = keep !== OTHER && r.models.includes(keep) ? keep : r.models.includes(models.providers[p].model) ? models.providers[p].model : r.models[0];
      choice.innerHTML = modelOptions(r.models, keep === OTHER ? OTHER : preferred);
      status.textContent = `· ${r.models.length} listed for this key (Test connection confirms access)`;
    } else {
      status.textContent = r.error ? "· couldn't load the list, showing suggestions" : "· no models found";
    }
  } catch { status.textContent = "· couldn't load the list, showing suggestions"; }
}

function modelPayload() {
  const body = { provider: $("#provider").value };
  $("#providerFields").querySelectorAll("input[name]").forEach((i) => { body[i.name] = i.value.trim(); });
  const choice = $("#modelChoice");
  if (choice) body.model = choice.value === OTHER ? $("#modelOther").value.trim() : choice.value;
  return body;
}

async function openModel(intro) {
  await refreshModel();
  const sel = $("#provider");
  sel.innerHTML = Object.entries(models.providers).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  sel.value = models.provider || "openai";
  renderProviderFields();
  const allowed = models.ui_config_allowed;
  const shared = (models.server_key_models || []).map(shortModel).join(" and ");
  $("#modelIntro").textContent = !allowed ? "This server's model is set by its administrator." :
    intro || ((models.configured ? `Using ${models.provider_label}${models.model ? " · " + shortModel(models.model) : ""}. ` : "")
      + "To use your own model, choose a provider, paste your API key and press Connect. "
      + "Your key is used only for your own chat in this browser tab and is never saved."
      + (shared ? ` The shared demo key works with ${shared}.` : ""));
  $("#modelForm").querySelectorAll("select, input, .dlg-actions button").forEach((x) => { x.disabled = !allowed; });
  $("#modelDlg").showModal();
}

async function modelCall(path) {
  const res = $("#modelResult");
  res.className = "result busy"; res.textContent = "Checking… (two small test calls)";
  $("#testBtn").disabled = $("#connectBtn").disabled = true;
  try {
    const r = await api(path, modelPayload());
    res.className = `result ${r.ok ? "ok" : "bad"}`;
    res.textContent = r.message + (r.latency_s ? ` (${r.latency_s}s)` : "");
    return r;
  } catch (e) {
    res.className = "result bad"; res.textContent = e.message;
  } finally {
    $("#testBtn").disabled = $("#connectBtn").disabled = false;
  }
}

$("#provider").onchange = renderProviderFields;
$("#testBtn").onclick = () => modelCall("/api/model/test");
$("#modelForm").onsubmit = async (e) => {
  e.preventDefault();
  const r = await modelCall("/api/model");
  if (r?.connected) {
    await refreshModel();
    setTimeout(() => $("#modelDlg").close(), 600);
    addNote(`Now using ${esc($("#modelName").textContent)}`);
  }
};
$("#modelBtn").onclick = () => openModel();

// A dialog's Close button shouldn't show a focus ring just because the dialog opened.
const nativeShowModal = HTMLDialogElement.prototype.showModal;
HTMLDialogElement.prototype.showModal = function () {
  nativeShowModal.call(this);
  if (document.activeElement?.matches?.(".nav-link")) document.activeElement.blur();
};

// ---------- wiring ----------
function autosize() {
  const t = $("#input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 160) + "px";
  t.style.overflowY = t.scrollHeight > 160 ? "auto" : "hidden";  // no scroll arrows until it's actually needed
}
$("#form").onsubmit = (e) => { e.preventDefault(); send($("#input").value); };
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("#input").value); } });
$("#input").addEventListener("input", autosize);
$("#newBtn").onclick = newChat;
$("#strip").onclick = () => document.body.classList.toggle("rail-open");   // phones: show/hide progress, identity, memory
$("#dataBtn").onclick = showData;
$("#dataBtn2").onclick = showData;
document.querySelectorAll("[data-close]").forEach((b) => b.onclick = () => b.closest("dialog").close());
document.querySelectorAll("dialog").forEach((d) => d.addEventListener("click", (e) => { if (e.target === d) d.close(); }));

// ?say=...&say=... replays messages (handy for demos and screenshots)
// opens the model dialog automatically when no model is connected (or with ?model)
refreshModel().then((m) => {
  if (!m.configured || m.error || new URLSearchParams(location.search).has("model")) openModel(m.error ? `The server's model setup failed: ${m.error}` : undefined);
}).catch(() => {});
if (new URLSearchParams(location.search).has("identities")) setTimeout(showData, 300);   // direct link to the test identities
newChat().then(async () => {
  for (const q of new URLSearchParams(location.search).getAll("say")) await send(q);
  if (new URLSearchParams(location.search).has("details")) document.body.classList.add("rail-open");   // for screenshots
});
