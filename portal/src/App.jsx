import { useState, useEffect, useRef, useCallback } from "react";
import Landing from "./Landing.jsx";

// ─── Privacy notes ────────────────────────────────────────────────────────
// No PHI stored in localStorage — all state is in React memory only.
// Session clears on page refresh. Patient ID held in state, never in URL.
// ─────────────────────────────────────────────────────────────────────────

const API_BASE = (typeof import.meta !== "undefined" && import.meta.env && import.meta.env.VITE_API_BASE) || "https://robin-production-542a.up.railway.app";

// Optional bearer token for a backend running with API_KEY set. Provided at
// build time via VITE_API_KEY. NOTE: anything in a client bundle is public, so
// this matches the backend's shared-key model (gate the API, keep CORS tight) —
// it is not a per-user secret. Real per-user auth would need a login + a
// server-side session/proxy, which this anonymous, case-id-as-capability app
// doesn't have.
const API_KEY = (typeof import.meta !== "undefined" && import.meta.env && import.meta.env.VITE_API_KEY) || "";

const apiFetch = (url, options = {}) => {
  const headers = { ...(options.headers || {}) };
  if (API_KEY) headers["Authorization"] = `Bearer ${API_KEY}`;
  return fetch(url, { ...options, headers });
};

// Open a letter PDF. A plain link can't carry the bearer header, so when a key
// is configured we fetch the bytes (authenticated) and open a blob URL instead.
const openLetter = async (url) => {
  if (!API_KEY) { window.open(url, "_blank", "noopener"); return; }
  try {
    const resp = await apiFetch(url);
    if (!resp.ok) throw new Error(String(resp.status));
    const obj = URL.createObjectURL(await resp.blob());
    const a = document.createElement("a");
    a.href = obj; a.target = "_blank"; a.rel = "noopener";
    a.click();
    setTimeout(() => URL.revokeObjectURL(obj), 60000);
  } catch (e) {
    console.warn("open letter failed:", e.message);
    window.open(url, "_blank", "noopener");
  }
};
const SESSION_TIMEOUT_MS = 15 * 60 * 1000;

const C = {
  red: "#E03E27", redLight: "#FDECEA",
  dark: "#1C1A18", charcoal: "#2E2B27",
  slate: "#4A4A4A", muted: "#A8A6A2",
  border: "#E5E5E5", bg: "#F7F7F7", white: "#FFF",
  green: "#1A7F4E", greenLight: "#EBF7F1",
  blue: "#1A4A7F", blueLight: "#EBF0FB",
  amber: "#966000", amberLight: "#FFF4DC",
};

const fmt$ = n => n == null ? "—" : "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 });

// ─── Mock data for demo when API unreachable ──────────────────────────────
const MOCK_RESULT = {
  case_id: "demo-case-001",
  patient_id: "demo-patient-001",
  result: {
    bill: {
      provider: { name: "Springfield General Hospital", state: "CA" },
      total_billed_amount: 4800,
      date_of_service: "2026-03-14",
      line_items: [
        { description: "Office visit", procedure_code: "99213", billed_amount: 300 },
        { description: "Lab panel", procedure_code: "80053", billed_amount: 4500 },
      ],
    },
    synthesis: {
      headline_low: 1440,
      headline_high: 4800,
      reasons: [
        { outcome_type: "partial_reduction", summary: "Based on your household income and the facility's published Financial Assistance Policy, your balance may qualify for reduction to approximately $1,440." },
        { outcome_type: "procedural_leverage", summary: "The hospital's price transparency file lists negotiated rates for these procedures at $1,200–$1,800." },
      ],
    },
  },
};

// ─── Session timeout hook ─────────────────────────────────────────────────
function useSessionTimeout(active, onTimeout) {
  const timer = useRef(null);
  const reset = useCallback(() => {
    clearTimeout(timer.current);
    if (active) timer.current = setTimeout(onTimeout, SESSION_TIMEOUT_MS);
  }, [active, onTimeout]);
  useEffect(() => {
    if (!active) return;
    const events = ["mousedown", "keydown", "touchstart"];
    events.forEach(e => window.addEventListener(e, reset, { passive: true }));
    reset();
    return () => { events.forEach(e => window.removeEventListener(e, reset)); clearTimeout(timer.current); };
  }, [active, reset]);
}

// ─── Chat stages ──────────────────────────────────────────────────────────
// welcome → uploading → ask_insurance → ask_income → ask_size → analyzing →
// results (leads with the recommended path, e.g. charity care) → ask_plan →
// ask_letter → ask_send → done → (ask_response / ask_outcome)

const INSURANCE_OPTIONS = [
  { id: "insured", label: "Insurance processed it" },
  { id: "uninsured", label: "I'm paying out of pocket" },
  { id: "unsure", label: "I'm not sure" },
];

// ─── Message bubble ───────────────────────────────────────────────────────
function Bubble({ msg }) {
  const isRobin = msg.from === "robin";
  return (
    <div style={{ display: "flex", flexDirection: isRobin ? "row" : "row-reverse", gap: 8, alignItems: "flex-end", marginBottom: 12 }}>
      {isRobin && (
        <div style={{ width: 28, height: 28, borderRadius: "50%", background: C.red, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 12, color: C.white, flexShrink: 0, marginBottom: 2 }}>R</div>
      )}
      <div style={{ maxWidth: "80%", display: "flex", flexDirection: "column", gap: 4, alignItems: isRobin ? "flex-start" : "flex-end" }}>
        {/* Main text bubble */}
        {msg.text && (
          <div style={{
            background: isRobin ? C.charcoal : C.red,
            color: C.white,
            borderRadius: isRobin ? "4px 16px 16px 16px" : "16px 4px 16px 16px",
            padding: "11px 15px",
            fontSize: 15,
            lineHeight: 1.65,
            whiteSpace: "pre-wrap",
          }}>{msg.text}</div>
        )}
        {/* Analysis card */}
        {msg.card && (
          <div style={{ background: C.dark, borderRadius: 12, padding: "16px 20px", minWidth: 260 }}>
            <p style={{ color: "#A8A6A2", fontSize: 11, margin: "0 0 4px", letterSpacing: "0.5px" }}>POTENTIAL SAVINGS</p>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 12 }}>
              <span style={{ color: C.red, fontSize: 32, fontWeight: 800, letterSpacing: "-1px" }}>{fmt$(msg.card.saving)}</span>
              <span style={{ color: "#9C9A97", fontSize: 14 }}>or more</span>
            </div>
            <div style={{ display: "flex", gap: 16, marginBottom: 12 }}>
              <div>
                <p style={{ color: "#9C9A97", fontSize: 10, margin: "0 0 2px" }}>BILLED</p>
                <p style={{ color: C.white, fontWeight: 700, fontSize: 16, margin: 0 }}>{fmt$(msg.card.billed)}</p>
              </div>
              <div style={{ color: "#8C8A87", alignSelf: "center" }}>→</div>
              <div>
                <p style={{ color: "#9C9A97", fontSize: 10, margin: "0 0 2px" }}>ESTIMATED AFTER</p>
                <p style={{ color: C.red, fontWeight: 700, fontSize: 16, margin: 0 }}>{fmt$(msg.card.low)}</p>
              </div>
            </div>
            {msg.card.reasons?.map((r, i) => (
              <div key={i} style={{ display: "flex", gap: 8, marginBottom: 6 }}>
                <span style={{ fontSize: 14, flexShrink: 0 }}>{r.outcome_type === "partial_reduction" ? "💰" : "⚖️"}</span>
                <p style={{ color: "#AAA", fontSize: 12, lineHeight: 1.5, margin: 0 }}>{r.summary}</p>
              </div>
            ))}
          </div>
        )}
        {/* Outcome receipt card */}
        {msg.receipt && (
          <div style={{ background: C.dark, borderRadius: 12, padding: "16px 20px", minWidth: 260 }}>
            <p style={{ color: "#A8A6A2", fontSize: 11, margin: "0 0 4px", letterSpacing: "0.5px" }}>YOUR OUTCOME</p>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 12 }}>
              <span style={{ color: C.green, fontSize: 32, fontWeight: 800, letterSpacing: "-1px" }}>{fmt$(msg.receipt.amount_saved)}</span>
              <span style={{ color: "#9C9A97", fontSize: 14 }}>saved</span>
            </div>
            <div style={{ display: "flex", gap: 16, marginBottom: 12 }}>
              <div>
                <p style={{ color: "#9C9A97", fontSize: 10, margin: "0 0 2px" }}>BILLED</p>
                <p style={{ color: C.white, fontWeight: 700, fontSize: 16, margin: 0 }}>{fmt$(msg.receipt.original_billed_amount)}</p>
              </div>
              <div style={{ color: "#8C8A87", alignSelf: "center" }}>→</div>
              <div>
                <p style={{ color: "#9C9A97", fontSize: 10, margin: "0 0 2px" }}>AGREED</p>
                <p style={{ color: C.green, fontWeight: 700, fontSize: 16, margin: 0 }}>{fmt$(msg.receipt.agreed_amount)}</p>
              </div>
            </div>
            <div style={{ borderTop: "1px solid #2A2A2A", paddingTop: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "#AAA", marginBottom: 4 }}>
                <span>RobinHealth fee{msg.receipt.plan === "membership" ? " (membership)" : ""}</span>
                <span style={{ color: C.white }}>{fmt$(msg.receipt.robinhealth_fee)}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13 }}>
                <span style={{ fontWeight: 700, color: C.white }}>Your net savings</span>
                <span style={{ fontWeight: 700, color: C.green }}>{fmt$(msg.receipt.patient_net_savings)}</span>
              </div>
            </div>
          </div>
        )}
        {/* Letter card — a server-rendered PDF (pdfUrl) or a local text draft (text) */}
        {msg.letter && (
          <div style={{ background: C.white, border: `1px solid ${C.border}`, borderRadius: 12, padding: "16px 20px", minWidth: 280 }}>
            <p style={{ fontWeight: 700, color: C.dark, fontSize: 13, margin: "0 0 8px" }}>
              📄 {msg.letter.reference ? `Draft letter — ${msg.letter.reference}` : "Draft letter"}
            </p>
            {msg.letter.text && (
              <div style={{ background: C.bg, borderRadius: 8, padding: 12, marginBottom: 12, maxHeight: 200, overflowY: "auto" }}>
                <pre style={{ fontFamily: "inherit", fontSize: 12, color: C.slate, whiteSpace: "pre-wrap", margin: 0, lineHeight: 1.6 }}>{msg.letter.text}</pre>
              </div>
            )}
            {msg.letter.pdfUrl ? (
              <button
                onClick={() => openLetter(msg.letter.pdfUrl)}
                style={{ background: C.red, color: C.white, border: "none", borderRadius: 8, padding: "9px 16px", fontSize: 12, fontWeight: 600, cursor: "pointer", width: "100%" }}
              >
                Open your letter (PDF)
              </button>
            ) : (
              <button
                onClick={() => {
                  const blob = new Blob([msg.letter.text], { type: "text/plain" });
                  const a = document.createElement("a");
                  a.href = URL.createObjectURL(blob);
                  a.download = "robin-negotiation-letter.txt";
                  a.click();
                }}
                style={{ background: C.red, color: C.white, border: "none", borderRadius: 8, padding: "8px 16px", fontSize: 12, fontWeight: 600, cursor: "pointer", width: "100%" }}
              >
                ⬇ Download as file
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Typing indicator ─────────────────────────────────────────────────────
function TypingIndicator() {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginBottom: 12 }}>
      <div style={{ width: 28, height: 28, borderRadius: "50%", background: C.red, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 12, color: C.white, flexShrink: 0 }}>R</div>
      <div style={{ background: C.charcoal, borderRadius: "4px 16px 16px 16px", padding: "12px 16px", display: "flex", gap: 5 }}>
        {[0, 1, 2].map(i => (
          <div key={i} style={{ width: 7, height: 7, borderRadius: "50%", background: "#9C9A97", animation: `bounce 1.2s ${i * 0.2}s infinite` }} />
        ))}
      </div>
    </div>
  );
}

// ─── Main App ─────────────────────────────────────────────────────────────
const PLAN_OPTIONS = [
  {
    id: "membership",
    title: "Membership — $50/month",
    sub: "We keep 0% of your savings. Free until your first win, cancel anytime. Best if your bill is large or you have more than one.",
    confirm: "Great — you're on Membership: a flat $50/month and we keep 0% of whatever we save you. You won't be charged until we get your first win, and you can cancel anytime.",
  },
  {
    id: "contingency",
    title: "Pay-per-win — 20% of savings",
    sub: "Never more than $1,000. You pay nothing if we don't save you anything. Best for a single bill.",
    confirm: "Got it — Pay-per-win: we only charge if we save you money (20% of the savings, never more than $1,000), and nothing at all if we can't reduce your bill.",
  },
];

// Plain-language fallback shown if the live fee terms can't be fetched.
const FEE_TERMS_SUMMARY = `RobinHealth Fee Agreement (summary)

You'll never pay more than $50/month, or 20% of what we save you. Choose either:

• Pay-per-win — 20% of the amount we save you, capped at $1,000 per bill. Nothing if we don't save you anything.
• Membership — a flat $50/month, and we take 0% of your savings. Free until your first win, cancel anytime.

By choosing a plan you authorize RobinHealth to contact your provider (and, where relevant, your insurer) on your behalf about this bill. You can withdraw any time before an agreement is reached, with no fee owed. RobinHealth is in beta — please review everything carefully.`;

const LETTER_TIPS = "A few tips:\n• Send by certified mail or email with a read receipt so you have proof\n• Keep a copy for your records\n• Providers typically respond within 2–4 weeks\n\nFeel free to ask me anything about the letter or what to do next.";

// Client-side fallback appeal text (used only when the API is unreachable / demo).
const generateAppealLetter = (name, insurer, bill) => {
  const today = new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  const dos = bill?.date_of_service || "[Date of service]";
  return `${today}

${insurer}
Appeals Department

RE: Appeal of Claim Determination
Patient: ${name}
Member ID: [Your member ID]
Claim #: [Your claim number]
Date of Service: ${dos}

Dear Appeals Department,

I am writing to formally appeal the determination on the claim referenced above. I respectfully request that you reconsider and reprocess this claim in accordance with my plan benefits, and issue a corrected Explanation of Benefits.

Under 45 CFR §147.136, I am entitled to a full and fair internal appeal and, if the denial is upheld, an independent external review. Please provide a written determination within 30 days.

Sincerely,
${name}
[Your phone / email]

---
Prepared with assistance from Robin (robinhealth.com), an AI-enabled patient advocacy service. Robin is in beta — please review carefully before sending.`;
};

// ── Save / resume ─────────────────────────────────────────────────────────
// Persists ONLY opaque identifiers (case/patient ids), the plan, and a flag —
// never PHI. The bill, name, and amounts stay server-side and are re-fetched
// on an explicit resume, so the "no PHI in localStorage" posture holds.
const RESUME_KEY = "robin_resume_v1";
const RESUME_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

const loadResume = () => {
  try {
    const r = JSON.parse(localStorage.getItem(RESUME_KEY) || "null");
    if (!r?.caseId || r.caseId === "demo-case-001") return null;
    if (!r.savedAt || Date.now() - r.savedAt > RESUME_TTL_MS) { localStorage.removeItem(RESUME_KEY); return null; }
    return r;
  } catch { return null; }
};
const saveResume = (r) => {
  try { localStorage.setItem(RESUME_KEY, JSON.stringify({ ...r, savedAt: Date.now() })); } catch { /* storage unavailable */ }
};
const clearResume = () => { try { localStorage.removeItem(RESUME_KEY); } catch { /* storage unavailable */ } };

function Chat({ onHome }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [typing, setTyping] = useState(false);
  const [stage, setStage] = useState("welcome");
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [sessionActive, setSessionActive] = useState(true);
  const [timedOut, setTimedOut] = useState(false);

  // State collected during conversation
  const [billResult, setBillResult] = useState(null);
  const [income, setIncome] = useState(null);
  const [householdSize, setHouseholdSize] = useState(null);
  const [patientId, setPatientId] = useState(null);
  const [caseId, setCaseId] = useState(null);
  const [plan, setPlan] = useState(null);
  const [patientName, setPatientName] = useState(null);
  const [insuranceStatus, setInsuranceStatus] = useState(null); // 'insured' | 'uninsured' | 'unsure'
  const [billFile, setBillFile] = useState(null);   // held until we have income/EOB, then sent to /intake
  const [eobFile, setEobFile] = useState(null);     // optional EOB (insured patients)
  const [pendingLetterKind, setPendingLetterKind] = useState(null); // 'provider' | 'insurer' (which letter we're collecting for)
  const [letterFacts, setLetterFacts] = useState({}); // answers that unlock statutory leverage
  const [factStep, setFactStep] = useState(0);
  const [lastLetter, setLastLetter] = useState(null);        // { storageKey, reference }
  const [negotiationStarted, setNegotiationStarted] = useState(false);
  const [pendingChannel, setPendingChannel] = useState(null); // 'letter_email' | 'letter_fax'
  const [lastResponse, setLastResponse] = useState(null);     // last /response result
  // Lazy init: read the saved pointer once on mount. Any saved case can be
  // resumed now that /full restores the bill + analysis, not just negotiations.
  const [resumeAvailable, setResumeAvailable] = useState(() => loadResume());

  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useSessionTimeout(sessionActive, () => {
    setTimedOut(true);
    setSessionActive(false);
  });

  useEffect(() => {
    const reduce = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    bottomRef.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth" });
  }, [messages, typing]);

  // Persist the opaque case pointer as it changes (never for the demo case).
  useEffect(() => {
    if (caseId && caseId !== "demo-case-001") {
      saveResume({ caseId, patientId, plan, negotiationStarted });
    }
  }, [caseId, patientId, plan, negotiationStarted]);

  // ── Message helpers ──────────────────────────────────────────────────────
  const addMsg = (msg) => setMessages(prev => [...prev, { id: Date.now() + Math.random(), ...msg }]);

  const robinSay = async (text, delay = 700, extra = {}) => {
    setTyping(true);
    await new Promise(r => setTimeout(r, delay));
    setTyping(false);
    addMsg({ from: "robin", text, ...extra });
  };

  const userSay = (text) => addMsg({ from: "user", text });

  // ── Compact summary of the user's case, so Robin answers about THEIR bill ──
  const buildChatContext = () => {
    const bill = billResult?.bill;
    const synthesis = billResult?.synthesis;
    if (!bill && !synthesis && income == null && householdSize == null) return null;
    return {
      provider: bill?.provider?.name,
      billed_amount: bill?.total_billed_amount,
      estimated_low: synthesis?.headline_low,
      household_income: income,
      household_size: householdSize,
      reasons: (synthesis?.reasons || []).map(r => r.summary).filter(Boolean),
    };
  };

  // ── Free-form Q&A → backend LLM (Claude). Replaces canned keyword replies. ──
  const askRobin = async (text) => {
    setTyping(true);
    try {
      const fd = new FormData();
      fd.append("message", text);
      const ctx = buildChatContext();
      if (ctx) fd.append("context_json", JSON.stringify(ctx));
      const resp = await apiFetch(`${API_BASE}/chat`, { method: "POST", body: fd });
      const data = await resp.json();
      setTyping(false);
      addMsg({ from: "robin", text: data.reply || "I'm not sure how to answer that — could you rephrase?" });
    } catch (e) {
      console.warn("chat unreachable:", e.message);
      setTyping(false);
      addMsg({
        from: "robin",
        text: "Sorry — I had trouble answering just now. You can try again, or drop your bill into the chat and I'll analyze it.",
      });
    }
  };

  // ── Show the full fee terms without leaving the plan step ─────────────────
  const showFeeTerms = async () => {
    userSay("Read the full terms");
    setTyping(true);
    let termsText = null;
    try {
      if (patientId) {
        const resp = await apiFetch(`${API_BASE}/patients/${patientId}/fee-terms`);
        if (resp.ok) termsText = (await resp.json())?.terms?.text;
      }
    } catch (e) { console.warn("fee-terms fetch failed:", e.message); }
    setTyping(false);
    addMsg({ from: "robin", text: termsText || FEE_TERMS_SUMMARY });
  };

  // ── Plan selection (the "$50/mo OR 20%" ceiling) ──────────────────────────
  // Tapping a plan is the patient's explicit, active confirmation — so it
  // records consent to the fee terms AND sets the plan. Consent must be on
  // record before any negotiation can start on their behalf.
  const choosePlan = async (planId) => {
    const opt = PLAN_OPTIONS.find(p => p.id === planId);
    if (!opt) return;
    setPlan(planId);
    userSay(opt.title);
    // Persist server-side when we have a real patient (best-effort; the demo
    // patient id won't exist on the server, so ignore failures).
    if (patientId) {
      try {
        const agreeFd = new FormData();
        agreeFd.append("affirmed", "true");
        await apiFetch(`${API_BASE}/patients/${patientId}/agree-to-terms`, { method: "POST", body: agreeFd });
        const planFd = new FormData();
        planFd.append("plan", planId);
        await apiFetch(`${API_BASE}/patients/${patientId}/plan`, { method: "POST", body: planFd });
      } catch (e) { console.warn("plan/consent save failed:", e.message); }
    }
    await robinSay(opt.confirm, 500);
    await robinSay("Would you like me to draft a letter to send to your provider?", 700);
    setStage("ask_letter");
  };

  // ── Draft the negotiation letter ──────────────────────────────────────────
  // Uses the real backend renderer (LLM draft → branded PDF) when we have a
  // live case + billed amount; falls back to a client-side text draft for the
  // demo or if the API is unreachable. nameOverride avoids reading stale state
  // right after setPatientName.
  const draftLetter = async (nameOverride, facts) => {
    const bill = billResult?.bill;
    const billed = bill?.total_billed_amount;
    const isDemo = !caseId || caseId === "demo-case-001";
    const f = facts || letterFacts || {};

    await robinSay("Drafting your letter now…", 400);

    if (!isDemo && billed) {
      try {
        const fd = new FormData();
        fd.append("patient_name", nameOverride || patientName || "[Patient name]");
        fd.append("facility_name", bill?.provider?.name || "Billing Department");
        fd.append("billed_amount", String(billed));
        if (bill?.account_number) fd.append("account_number", bill.account_number);
        if (bill?.date_of_service) fd.append("date_of_service", bill.date_of_service);
        if (bill?.provider?.address) fd.append("facility_address", bill.provider.address);
        fd.append("letter_type", "initial");
        // Statutory-leverage facts (No Surprises Act, itemized bill, GFE).
        if (f.emergency) fd.append("emergency", f.emergency);
        if (f.out_of_network) fd.append("out_of_network", f.out_of_network);
        if (f.received_itemized) fd.append("received_itemized", f.received_itemized);
        if (f.good_faith_estimate) fd.append("good_faith_estimate", f.good_faith_estimate);
        if (insuranceStatus === "uninsured") fd.append("self_pay", "yes");

        const resp = await apiFetch(`${API_BASE}/cases/${caseId}/draft-letter`, { method: "POST", body: fd });
        if (resp.status === 409) {
          await robinSay(
            "I couldn't read your bill clearly enough to draft a letter I'd trust to send — a wrong figure could hurt your case. Could you upload a clearer photo or an itemized copy, and I'll try again?",
            600
          );
          setStage("done");
          return;
        }
        if (!resp.ok) throw new Error(`draft-letter ${resp.status}`);
        const data = await resp.json();
        setLastLetter({ storageKey: data.storage_key, reference: data.reference_number });
        await robinSay(
          "Here's your letter — drafted and rendered to a PDF on RobinHealth letterhead. Please review it carefully.",
          500,
          { letter: { pdfUrl: `${API_BASE}/letters/${data.storage_key}`, reference: data.reference_number } }
        );
        await robinSay("Want me to send it to your provider for you, or will you send it yourself?", 800);
        setStage("ask_send");
        return;
      } catch (e) {
        console.warn("draft-letter failed, using local draft:", e.message);
        // fall through to the client-side draft below
      }
    }

    const letterText = generateLetter(bill, billResult?.synthesis, income, householdSize);
    await robinSay(
      "Here's your draft letter. Review it carefully, fill in your contact details, then send it to your provider's billing department.",
      500,
      { letter: { text: letterText } }
    );
    await robinSay(LETTER_TIPS, 1000);
    setStage("done");
  };

  // ── A few quick questions that unlock legal leverage in the letter ────────
  const letterFactQuestions = () => {
    const qs = [
      { key: "emergency", q: "A couple of quick questions to make your letter stronger. Was this care for an emergency (ER or an urgent situation)?" },
      { key: "out_of_network", q: "Was any provider out-of-network, or did you get a surprise bill you weren't expecting?" },
      { key: "received_itemized", q: "Did you receive a fully itemized bill — every charge listed with its codes?" },
    ];
    if (insuranceStatus === "uninsured") {
      qs.push({ key: "good_faith_estimate", q: "Before your care, did the provider give you a written Good Faith Estimate of the cost?" });
    }
    return qs;
  };
  const beginLetterFacts = async () => {
    setLetterFacts({});
    setFactStep(0);
    await robinSay(letterFactQuestions()[0].q, 500);
    setStage("ask_letter_facts");
  };
  const answerFact = async (value) => {
    const qs = letterFactQuestions();
    const cur = qs[factStep];
    userSay(value === "yes" ? "Yes" : value === "no" ? "No" : "Not sure");
    const updated = { ...letterFacts, [cur.key]: value };
    setLetterFacts(updated);
    const next = factStep + 1;
    if (next < qs.length) {
      setFactStep(next);
      await robinSay(qs[next].q, 400);
    } else {
      await draftLetter(patientName, updated);
    }
  };

  // ── Letter routing: provider dispute vs. insurer appeal ───────────────────
  const startProviderLetter = async () => {
    setPendingLetterKind("provider");
    if (!patientName) {
      await robinSay("Sure. What name should I put on the letter? (The patient name the provider will see.)", 500);
      setStage("ask_name");
    } else {
      await beginLetterFacts();
    }
  };
  const startInsurerAppeal = async () => {
    setPendingLetterKind("insurer");
    if (!patientName) {
      await robinSay("Sure. First, what's the member's name on the insurance plan?", 500);
      setStage("ask_name");
    } else {
      await askInsurerName();
    }
  };
  const askInsurerName = async () => {
    await robinSay("What's the name of your insurance company? (e.g. 'Aetna', 'Blue Cross') I'll address the appeal to their appeals department.", 500);
    setStage("ask_insurer");
  };

  // Draft the insurer appeal PDF (or a client-side text fallback in demo).
  const draftInsurerAppeal = async (name, insurerName) => {
    setPendingLetterKind(null);
    await robinSay("Drafting your insurer appeal now…", 400);
    const bill = billResult?.bill;
    const isDemo = !caseId || caseId === "demo-case-001";
    if (!isDemo) {
      try {
        const fd = new FormData();
        fd.append("patient_name", name || "[Patient name]");
        fd.append("insurer_name", insurerName);
        if (bill?.date_of_service) fd.append("date_of_service", bill.date_of_service);
        const resp = await apiFetch(`${API_BASE}/cases/${caseId}/appeal-letter`, { method: "POST", body: fd });
        if (!resp.ok) throw new Error(`appeal-letter ${resp.status}`);
        const data = await resp.json();
        setLastLetter({ storageKey: data.storage_key, reference: data.reference_number });
        await robinSay(
          "Here's your appeal to the insurer — review it and fill in any [bracketed] details like your member ID and claim number (they're on your EOB or insurance card), then I can send it or you can.",
          500,
          { letter: { pdfUrl: `${API_BASE}/letters/${data.storage_key}`, reference: data.reference_number } }
        );
        await robinSay("Want me to send it to your insurer?", 700);
        setStage("ask_send");
        return;
      } catch (e) {
        console.warn("appeal draft failed:", e.message);
      }
    }
    const txt = generateAppealLetter(name, insurerName, bill);
    await robinSay(
      "Here's your appeal letter. Review it, fill in your member ID and claim number, and send it to your insurer's appeals department.",
      500,
      { letter: { text: txt } }
    );
    await robinSay(LETTER_TIPS, 1000);
    setStage("done");
  };

  // ── Start the negotiation (creates the negotiations row to track against) ──
  // Requires the fee-agreement consent recorded at the plan step. Best-effort:
  // a failure shouldn't block delivery. Returns true if a negotiation exists.
  const ensureNegotiation = async () => {
    const billed = billResult?.bill?.total_billed_amount;
    if (!caseId || caseId === "demo-case-001" || !billed) return false;
    if (negotiationStarted) return true;
    try {
      const fd = new FormData();
      fd.append("billed_amount", String(billed));
      const target = billResult?.synthesis?.headline_low;
      if (target != null) fd.append("target_amount", String(target));
      const resp = await apiFetch(`${API_BASE}/cases/${caseId}/negotiate`, { method: "POST", body: fd });
      if (resp.ok) { setNegotiationStarted(true); return true; }
      console.warn("negotiate failed:", resp.status);
      return false;
    } catch (e) { console.warn("negotiate error:", e.message); return false; }
  };

  // ── Ask for the recipient contact, then deliver ───────────────────────────
  const startSend = async (channel) => {
    setPendingChannel(channel);
    userSay(channel === "letter_email" ? "Email it for me" : "Fax it for me");
    await robinSay(
      channel === "letter_email"
        ? "What's the billing department's email address? It's often printed on the bill, or on the hospital's billing/contact page."
        : "What's the billing department's fax number?",
      500
    );
    setStage("ask_send_contact");
  };

  // ── Deliver the letter via the backend (and log it against the negotiation) ─
  const deliverLetter = async (channel, recipient) => {
    if (!lastLetter?.storageKey) {
      await robinSay("I don't have a finished letter to send yet — let's draft one first.", 500);
      setStage("done");
      return;
    }
    await robinSay("Okay — sending that now…", 400);
    await ensureNegotiation();  // create the negotiation first so the letter is logged against it
    const dest = recipient.email || recipient.fax || "your provider";
    try {
      const fd = new FormData();
      fd.append("storage_key", lastLetter.storageKey);
      fd.append("reference_number", lastLetter.reference || "");
      fd.append("channel", channel);
      if (recipient.email) fd.append("recipient_email", recipient.email);
      if (recipient.fax) fd.append("recipient_fax", recipient.fax);
      if (recipient.name) fd.append("recipient_name", recipient.name);
      if (recipient.address) fd.append("recipient_address", recipient.address);
      const resp = await apiFetch(`${API_BASE}/cases/${caseId}/send-letter`, { method: "POST", body: fd });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(`send-letter ${resp.status}`);
      const ref = data.reference_number || lastLetter.reference;
      if (data.delivery_status === "sent") {
        await robinSay(`Done — I've sent your letter to ${dest} (reference ${ref}) and I'm now tracking your case. Providers usually respond within 2–4 weeks; come back and tell me what they say and I'll draft your next move.`, 700);
      } else {
        await robinSay(`I've logged your letter (reference ${ref}) and started tracking your case. Automatic delivery isn't switched on here yet, so please send the PDF above to ${dest} yourself for now — then check back and I'll help with their response.`, 800);
      }
    } catch (e) {
      console.warn("send-letter failed:", e.message);
      await robinSay("I couldn't send it automatically just now. Please download the PDF above and send it to your provider — I've still got your case and can help with their response.", 800);
    }
    setStage("done");
  };

  // ── Patient sends it themselves; still start tracking the case ────────────
  const sendSelf = async () => {
    userSay("I'll send it myself");
    await ensureNegotiation();
    await robinSay("Sounds good. Download the PDF above and send it to your provider's billing department — certified mail, or email with a read receipt, so you have proof. I've started tracking your case; come back and tell me what they say and I'll draft your next move.", 800);
    setStage("done");
  };

  // ── Inbound: provider response → recommendation → follow-up / outcome ──────
  const askForResponse = () => {
    setStage("ask_response");
    robinSay("Go ahead and paste what the provider said (or the key part), and I'll tell you what it means and what to do next.", 400);
  };
  const askForOutcome = () => {
    setStage("ask_outcome");
    robinSay("What amount did you and the provider finally agree on? Enter the dollar figure.", 400);
  };

  const feeLine = (r) => r.plan === "membership"
    ? "RobinHealth's fee is $0 — it's covered by your $50/month membership"
    : `RobinHealth's fee is ${fmt$(r.robinhealth_fee)} (20% of savings, capped at $1,000)`;

  // Classify the provider's reply and recommend the next step.
  const handleResponse = async (responseText) => {
    await robinSay("Let me look at what they said…", 400);
    if (!caseId || caseId === "demo-case-001") {
      await askRobin(`My provider responded to my bill negotiation with: "${responseText}". What does it mean and what should I do next?`);
      setStage("done");
      return;
    }
    try {
      const fd = new FormData();
      fd.append("response_text", responseText);
      const resp = await apiFetch(`${API_BASE}/cases/${caseId}/response`, { method: "POST", body: fd });
      if (!resp.ok) throw new Error(`response ${resp.status}`);
      const data = await resp.json();
      setLastResponse(data);
      const f = data.followup || {};
      await robinSay(
        (f.urgency === "immediate" ? "⚠️ This one needs attention soon. " : "") +
        (f.explanation || "Here's what I make of their response.") +
        (f.action ? `\n\nRecommended next step: ${f.action}.` : ""),
        700
      );
      setStage("after_response");
    } catch (e) {
      console.warn("response classify failed:", e.message);
      await askRobin(`My provider responded with: "${responseText}". What should I do next?`);
      setStage("done");
    }
  };

  // Draft the recommended follow-up letter (appeal, doc request, etc.).
  const draftFollowupLetter = async () => {
    const ctx = lastResponse?.followup?.followup_letter_context;
    if (!ctx) {
      await robinSay("There's no follow-up letter needed here — you can record the final amount, or ask me anything.", 600);
      return;
    }
    await robinSay("Drafting your follow-up letter…", 400);
    try {
      const fd = new FormData();
      fd.append("patient_name", patientName || "[Patient name]");
      fd.append("facility_name", billResult?.bill?.provider?.name || "Billing Department");
      fd.append("billed_amount", String(billResult?.bill?.total_billed_amount || 0));
      fd.append("letter_type", "followup");
      fd.append("followup_context_json", JSON.stringify(ctx));
      fd.append("round_number", "2");
      const resp = await apiFetch(`${API_BASE}/cases/${caseId}/draft-letter`, { method: "POST", body: fd });
      if (!resp.ok) throw new Error(`draft-letter ${resp.status}`);
      const data = await resp.json();
      setLastLetter({ storageKey: data.storage_key, reference: data.reference_number });
      await robinSay(
        "Here's your follow-up letter as a PDF — review it, then I can send it or you can.",
        500,
        { letter: { pdfUrl: `${API_BASE}/letters/${data.storage_key}`, reference: data.reference_number } }
      );
      await robinSay("Want me to send it to your provider?", 700);
      setStage("ask_send");
    } catch (e) {
      console.warn("followup draft failed:", e.message);
      await robinSay("I couldn't draft the follow-up just now — please try again in a moment.", 600);
    }
  };

  // Record the final agreed amount and show the savings/fee receipt.
  const recordOutcome = async (agreedAmount) => {
    await robinSay("Recording that now…", 400);
    if (!caseId || caseId === "demo-case-001") {
      await robinSay("In this demo I can't save the final amount, but in the live version I'd record it and show your exact savings and fee.", 600);
      setStage("done");
      return;
    }
    try {
      const fd = new FormData();
      fd.append("agreed_amount", String(agreedAmount));
      const resp = await apiFetch(`${API_BASE}/cases/${caseId}/outcome`, { method: "POST", body: fd });
      if (resp.status === 404) {
        await robinSay("I need an active negotiation on file before I can record the outcome — let's send a letter first, then come back.", 600);
        setStage("done");
        return;
      }
      if (!resp.ok) throw new Error(`outcome ${resp.status}`);
      const r = await resp.json();
      await robinSay(
        `That's a great result — you saved ${fmt$(r.amount_saved)}. ${feeLine(r)}, so your net savings is ${fmt$(r.patient_net_savings)}.`,
        700,
        { receipt: r }
      );
      setStage("done");
    } catch (e) {
      console.warn("outcome failed:", e.message);
      await robinSay("I couldn't record that just now — please try again in a moment.", 600);
    }
  };

  // ── Resume a saved case (re-fetches server-side state; no PHI was stored) ──
  const resumeCase = async () => {
    const r = resumeAvailable;
    if (!r) return;
    userSay("Resume my case");
    setResumeAvailable(null);
    setPatientId(r.patientId || null);
    setCaseId(r.caseId);
    if (r.plan) setPlan(r.plan);
    setTyping(true);
    try {
      const resp = await apiFetch(`${API_BASE}/cases/${r.caseId}/full`);
      if (!resp.ok) throw new Error(`case ${resp.status}`);
      const data = await resp.json();
      const bill = data.bill;
      const synthesis = data.synthesis;
      const neg = data.negotiation;
      setTyping(false);

      if (bill || synthesis) {
        setBillResult({ bill: bill || {}, synthesis: synthesis || {} });
      }

      // Already negotiating/resolved → tracking view.
      if (neg) {
        setNegotiationStarted(true);
        let recap = `Welcome back. Your bill was ${fmt$(neg.original_billed_amount)}`;
        if (neg.agreed_amount != null) {
          recap += `, and you've recorded an outcome — you saved ${fmt$(neg.amount_saved)} (net ${fmt$(neg.patient_net_savings)}).`;
        } else if ((neg.contacts || []).length) {
          recap += `, and your letter is with the provider (status: ${data.case_status}). Providers usually take 2–4 weeks.`;
        } else {
          recap += ` (status: ${data.case_status}).`;
        }
        recap += " You can tell me what the provider said, or record the final amount, anytime below.";
        await robinSay(recap, 500);
        setStage("done");
        return;
      }

      // Analysis done, no negotiation yet → re-show the estimate and continue.
      if (synthesis && bill?.total_billed_amount != null) {
        const billed = bill.total_billed_amount;
        const low = synthesis.headline_low;
        const saving = billed != null && low != null ? billed - low : null;
        await robinSay(
          "Welcome back — here's the estimate I'd put together for your bill:",
          500,
          saving ? { card: { billed, low, saving, reasons: synthesis.reasons || [] } } : {}
        );
        if (r.plan) {
          await robinSay("You'd already chosen your plan. Want me to draft your letter to the provider?", 800);
          setStage("ask_letter");
        } else {
          await robinSay("When you're ready, choose how you'd like to proceed:", 800);
          setStage("ask_plan");
        }
        return;
      }

      // Bill on file but no usable estimate.
      await robinSay("I found your case but couldn't restore the full estimate — the quickest way to pick up is to upload your bill again. Drop it in whenever you're ready.", 500);
      clearResume();
      setStage("welcome");
    } catch (e) {
      console.warn("resume failed:", e.message);
      setTyping(false);
      await robinSay("I couldn't load that case just now — let's start fresh. Drop your bill in whenever you're ready.", 500);
      clearResume();
      setStage("welcome");
    }
  };

  // ── Boot: Robin's opening messages ───────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    const boot = async () => {
      await new Promise(r => setTimeout(r, 300));
      if (cancelled) return;
      addMsg({ from: "robin", text: "Hi, I'm Robin — your automated health advocate. I help people fight back against confusing medical bills. It's important to know this product is still in beta testing and everything should be reviewed carefully by you, the user." });
      await new Promise(r => setTimeout(r, 900));
      if (cancelled) return;
      setTyping(true);
      await new Promise(r => setTimeout(r, 600));
      if (cancelled) return;
      setTyping(false);
      const resume = loadResume();
      addMsg({
        from: "robin",
        text: resume
          ? "Welcome back — I kept a private reference to your case on this device (no personal details are stored here). Want to pick up where you left off, or start fresh?"
          : "Drop a bill here and I'll analyze it — no commitment.",
      });
    };
    boot();
    return () => { cancelled = true; };
  }, []);

  // ── Bill upload ───────────────────────────────────────────────────────────
  // The bill is held in state; we run /intake once at the end, after we've also
  // collected insurance status, an optional EOB, and income/household size — so
  // the analysis reflects all of it (charity-care eligibility needs income; the
  // insurance angle needs the EOB).
  const handleFile = async (file) => {
    if (!file || uploading || stage !== "welcome") return;
    setBillFile(file);
    userSay(`📎 ${file.name} (${Math.round(file.size / 1024)} KB)`);
    await robinSay("Got it. A couple of quick questions and I'll analyze everything together.", 400);
    await robinSay("First, so I point you the right way: has insurance already processed this bill, or are you paying out of pocket?", 700);
    setStage("ask_insurance");
  };

  // ── Triage: insurance status routes the flow + how Robin frames things ─────
  const chooseInsurance = async (status) => {
    setInsuranceStatus(status);
    const opt = INSURANCE_OPTIONS.find(o => o.id === status);
    userSay(opt ? opt.label : status);
    if (status === "insured") {
      await robinSay("Got it. Do you have your insurer's Explanation of Benefits (EOB) for this claim? Uploading it lets me check whether you're being billed more than your insurer says you owe. You can add it, or skip.", 700);
      setStage("ask_eob");
    } else {
      await robinSay("Thanks. To check what financial assistance you may qualify for, what's your approximate annual household income? (e.g. $50,000 or 'about 50k')", 600);
      setStage("ask_income");
    }
  };

  // ── Optional EOB upload (insured patients) ────────────────────────────────
  const handleEobFile = async (file) => {
    if (!file) return;
    setEobFile(file);
    userSay(`📎 ${file.name} (${Math.round(file.size / 1024)} KB)`);
    await robinSay("Perfect — I'll cross-check your bill against that. Now, what's your approximate annual household income? (e.g. $50,000 or 'about 50k')", 600);
    setStage("ask_income");
  };
  const skipEob = async () => {
    userSay("Skip");
    await robinSay("No problem. What's your approximate annual household income? (e.g. $50,000 or 'about 50k')", 500);
    setStage("ask_income");
  };

  // ── Run intake with everything collected, then show the analysis ───────────
  const runIntake = async (incomeAmt, sizeAmt) => {
    setUploading(true);
    await robinSay("Analyzing everything now — give me a moment.", 300);
    let result;
    try {
      const fd = new FormData();
      if (billFile) fd.append("bill_document", billFile, billFile.name);
      if (eobFile) fd.append("eob_document", eobFile, eobFile.name);
      if (incomeAmt != null) fd.append("household_income", String(incomeAmt));
      if (sizeAmt != null) fd.append("household_size", String(sizeAmt));
      const resp = await apiFetch(`${API_BASE}/intake`, { method: "POST", body: fd });
      if (!resp.ok) throw new Error(`intake ${resp.status}`);
      const data = await resp.json();
      result = data.result || {};
      setBillResult(result);
      setPatientId(data.patient_id);
      setCaseId(data.case_id);
    } catch (e) {
      console.warn("API unreachable, using demo data:", e.message);
      result = MOCK_RESULT.result;
      setBillResult(result);
      setPatientId(MOCK_RESULT.patient_id);
      setCaseId(MOCK_RESULT.case_id);
    }
    setUploading(false);
    await showAnalysis(result);
  };

  // ── Parse income from free text ──────────────────────────────────────────
  const parseIncome = (text) => {
    const t = text.toLowerCase().replace(/,/g, "");
    const match = t.match(/[\d]+(?:\.\d+)?/);
    if (!match) return null;
    let n = parseFloat(match[0]);
    if (t.includes("k")) n *= 1000;
    if (t.includes("m") && !t.includes("mo")) n *= 1000000;
    return n > 500 ? n : n * 1000; // treat small numbers as thousands
  };

  // ── Parse household size from free text ──────────────────────────────────
  const parseSize = (text) => {
    const match = text.match(/\d+/);
    return match ? parseInt(match[0]) : null;
  };

  // ── Generate letter text ─────────────────────────────────────────────────
  const generateLetter = (bill, synthesis, incomeAmt, sizeAmt) => {
    const provider = bill?.provider?.name || "Billing Department";
    const billed = bill?.total_billed_amount || 0;
    const target = synthesis?.headline_low;
    const today = new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });

    return `${today}

${provider}
Billing Department

RE: Request for Financial Assistance / Bill Reduction
Patient Account — [YOUR NAME]

Dear Billing Department,

I am writing to request a reduction in the balance on my account, which currently shows a total of ${fmt$(billed)}.

I am experiencing financial hardship and am requesting consideration under your Financial Assistance Policy (FAP). My household of ${sizeAmt || "[SIZE]"} has an annual income of approximately ${fmt$(incomeAmt) || "[INCOME]"}, which I believe qualifies me for assistance under your published eligibility criteria.

I have reviewed comparable pricing for these services and believe a fair and reasonable settlement would be in the range of ${fmt$(target) || "[TARGET AMOUNT]"}.

I am committed to resolving this account and am prepared to make prompt payment upon agreement of a reduced balance.

Under 26 CFR 1.501(r)-4, nonprofit hospitals are required to have a Financial Assistance Policy and apply it consistently to all patients who apply and meet eligibility criteria. I respectfully request a written response within 21 days.

Please contact me to discuss a resolution.

Sincerely,

[YOUR NAME]
[YOUR PHONE / EMAIL]
[YOUR ADDRESS]

---
This letter was prepared with assistance from Robin (robinhealth.com), an AI-enabled patient advocacy service. Robin is in beta — please review all content carefully before sending.`;
  };

  // ── Show analysis and ask about letter ───────────────────────────────────
  const showAnalysis = async (resultOverride) => {
    const r = resultOverride || billResult;
    const bill = r?.bill;
    const synthesis = r?.synthesis;
    const billed = bill?.total_billed_amount;
    const low = synthesis?.headline_low;
    const saving = billed && low ? billed - low : null;

    await robinSay(
      saving
        ? `Based on your income and household size, here's what I think you could save:`
        : "Here's what I found on your bill:",
      600,
      saving ? {
        card: {
          billed, low, saving,
          reasons: synthesis?.reasons || [],
        }
      } : {}
    );

    await robinSay(
      "This is a beta estimate — actual results depend on your provider and situation, so please review carefully.",
      900
    );

    // Triage: lead with the highest-value path. Charity care can wipe the
    // whole bill, so it comes first whenever the synthesis says it's possible.
    const reasons = synthesis?.reasons || [];
    const canEliminate = synthesis?.headline_could_eliminate || reasons.some(x => x.outcome_type === "full_elimination");
    const hasEobReason = reasons.some(x => /insurer|insurance/i.test(x.summary || ""));
    if (canEliminate) {
      await robinSay(
        "💡 Here's the most important part: your biggest opportunity is charity care. Based on your household income and the hospital's financial assistance policy, this bill could be reduced all the way to $0 — and applying is your right. That's what I'll lead with: the letter I prepare will request a full waiver under the hospital's policy.",
        800
      );
    } else if (hasEobReason) {
      await robinSay(
        "💡 Here's the key issue I see: your insurer has already set what these services should cost. If you're being billed more than your share of that amount, that's balance billing — and I'll dispute the bill on that basis, using your EOB as the evidence.",
        800
      );
    } else if (saving) {
      await robinSay(
        `My recommendation: push for a reduction. Your charges look well above fair and typical negotiated rates, so I'll aim to bring the bill down toward ${fmt$(low)} — well below what you were billed.`,
        800
      );
    } else {
      await robinSay(
        "My recommendation: let's request an itemized review first — there isn't enough detail yet to pin down a number, and itemized bills often surface errors and overcharges worth disputing.",
        800
      );
    }
    if (insuranceStatus === "insured" && !eobFile) {
      await robinSay(
        "Since insurance processed this, if you can find your insurer's Explanation of Benefits later, share it and I'll check the claim was paid correctly.",
        700
      );
    }

    if (plan) {
      // Already chose a plan on an earlier bill — don't re-ask pricing.
      await robinSay("Would you like me to draft a letter to send to your provider?", 700);
      setStage("ask_letter");
    } else {
      await robinSay(
        "Before we go further, here's how our pricing works: you'll never pay more than $50/month, or 20% of what we save you (and never more than $1,000). Choosing a plan also confirms you've read and agree to our fee terms — tap “Read the full terms” first if you'd like. Pick whichever fits you best:",
        700
      );
      setStage("ask_plan");
    }
  };

  // ── Handle user messages ─────────────────────────────────────────────────
  const handleSend = async () => {
    const text = input.trim();
    if (!text || uploading) return;
    setInput("");
    userSay(text);
    const lower = text.toLowerCase();

    if (stage === "welcome") {
      // General questions before upload — answered by Robin (LLM), not canned replies
      await askRobin(text);
      return;
    }

    if (stage === "ask_insurance") {
      if (lower.includes("insur") || lower.includes("processed") || lower.includes("covered") || lower === "yes") {
        await chooseInsurance("insured");
      } else if (lower.includes("pocket") || lower.includes("uninsured") || lower.includes("self") || lower === "no" || lower.startsWith("no ")) {
        await chooseInsurance("uninsured");
      } else if (lower.includes("not sure") || lower.includes("unsure") || lower.includes("know")) {
        await chooseInsurance("unsure");
      } else {
        await robinSay("No problem — just tap one of the options above.", 500);
      }
      return;
    }

    if (stage === "ask_eob") {
      if (lower.includes("skip") || lower.includes("no") || lower.includes("don't") || lower.includes("dont") || lower.includes("later")) {
        await skipEob();
      } else {
        await robinSay("You can attach your EOB with the button above, or tap Skip to continue.", 500);
      }
      return;
    }

    if (stage === "ask_income") {
      const parsed = parseIncome(text);
      if (!parsed) {
        await robinSay("I didn't quite catch that — could you share your approximate annual household income? For example: '$48,000' or 'about 60k'.", 600);
        return;
      }
      setIncome(parsed);
      await robinSay(`Got it — ${fmt$(parsed)}/year. And how many people are in your household, including yourself?`, 600);
      setStage("ask_size");
      return;
    }

    if (stage === "ask_size") {
      const parsed = parseSize(text);
      if (!parsed || parsed < 1 || parsed > 20) {
        await robinSay("Could you give me the number of people in your household? For example: '3' or 'family of 4'.", 600);
        return;
      }
      setHouseholdSize(parsed);
      setStage("analyzing");
      await robinSay(`Perfect — household of ${parsed}.`, 300);
      await runIntake(income, parsed);
      return;
    }

    if (stage === "ask_plan") {
      // Buttons are the primary path; also accept a typed choice.
      if (lower.includes("member") || lower.includes("month") || lower.includes("50") || lower.includes("subscri")) {
        await choosePlan("membership");
      } else if (lower.includes("win") || lower.includes("20") || lower.includes("percent") || lower.includes("per bill") || lower.includes("per-bill") || lower.includes("contingen") || lower.includes("only if")) {
        await choosePlan("contingency");
      } else {
        await robinSay("No rush — tap one of the two options above, whichever feels right. You can always change it later.", 600);
      }
      return;
    }

    if (stage === "ask_name") {
      const name = text.trim();
      if (name.length < 2 || name.length > 80) {
        await robinSay("Could you share the patient's full name as it should appear on the letter?", 500);
        return;
      }
      setPatientName(name);
      if (pendingLetterKind === "insurer") {
        await askInsurerName();
      } else {
        await beginLetterFacts();
      }
      return;
    }

    if (stage === "ask_letter_facts") {
      if (lower.includes("yes") || lower.includes("yeah") || lower.includes("yep")) await answerFact("yes");
      else if (lower.includes("no") || lower.includes("nope") || lower.includes("didn't") || lower.includes("dont") || lower.includes("don't")) await answerFact("no");
      else if (lower.includes("not sure") || lower.includes("unsure") || lower.includes("idk") || lower.includes("maybe")) await answerFact("unsure");
      else await robinSay("Just tap Yes, No, or Not sure above. 🙂", 400);
      return;
    }

    if (stage === "ask_insurer") {
      const insurer = text.trim();
      if (insurer.length < 2) {
        await robinSay("Could you share your insurance company's name?", 500);
        return;
      }
      await draftInsurerAppeal(patientName, insurer);
      return;
    }

    if (stage === "ask_send") {
      if (lower.includes("email")) { await startSend("letter_email"); }
      else if (lower.includes("fax")) { await startSend("letter_fax"); }
      else if (lower.includes("myself") || lower.includes("self") || lower.includes("mail") || lower.includes("no")) { await sendSelf(); }
      else { await robinSay("Tap one of the options above — I can email or fax it for you, or you can send it yourself.", 500); }
      return;
    }

    if (stage === "ask_send_contact") {
      const val = text.trim();
      if (pendingChannel === "letter_email") {
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(val)) {
          await robinSay("That doesn't look like an email address — could you double-check it?", 500);
          return;
        }
        await deliverLetter("letter_email", { email: val });
      } else {
        const digits = val.replace(/[^\d]/g, "");
        if (digits.length < 7) {
          await robinSay("Could you share the fax number, including area code?", 500);
          return;
        }
        await deliverLetter("letter_fax", { fax: val });
      }
      return;
    }

    if (stage === "ask_response") {
      await handleResponse(text);
      return;
    }

    if (stage === "after_response") {
      if (lower.includes("follow") || lower.includes("letter") || lower.includes("appeal")) {
        await draftFollowupLetter();
      } else if (lower.includes("record") || lower.includes("amount") || lower.includes("agreed") || lower.includes("final") || lower.includes("settle")) {
        askForOutcome();
      } else {
        await askRobin(text);
      }
      return;
    }

    if (stage === "ask_outcome") {
      const n = parseFloat(text.replace(/[^0-9.]/g, ""));
      const billed = billResult?.bill?.total_billed_amount;
      if (!isFinite(n) || n < 0) {
        await robinSay("Could you enter the final agreed amount as a number? For example: '$2,000'.", 500);
        return;
      }
      if (billed && n >= billed) {
        await robinSay(`That's not lower than your billed amount of ${fmt$(billed)} — what did they agree to reduce it to?`, 500);
        return;
      }
      await recordOutcome(n);
      return;
    }

    if (stage === "ask_letter") {
      if (lower.includes("appeal") || lower.includes("insurer") || (lower.includes("insurance") && !lower.includes("no"))) {
        await startInsurerAppeal();
        return;
      }
      const yes = lower.includes("yes") || lower.includes("sure") || lower.includes("please") || lower.includes("yeah") || lower.includes("yep") || lower.includes("ok") || lower.includes("generate") || lower.includes("letter") || lower.includes("provider") || lower.includes("help");
      const no = lower.includes("no") || lower.includes("not") || lower.includes("later") || lower.includes("skip");

      if (yes) {
        await startProviderLetter();
      } else if (no) {
        await robinSay("No problem — if you change your mind, just say 'generate letter' and I'll put one together. Is there anything else I can help you with?", 700);
        setStage("done");
      } else {
        await robinSay("Would you like me to generate a draft letter to send to your provider? Just say yes or no.", 600);
      }
      return;
    }

    if (stage === "done") {
      if (lower.includes("appeal") || lower.includes("insurer")) {
        await startInsurerAppeal();
      } else if (lower.includes("letter") || lower.includes("generate") || lower.includes("draft")) {
        await startProviderLetter();
      } else if (lower.includes("another") || lower.includes("new bill") || lower.includes("different bill")) {
        await robinSay("Sure — drop your next bill into the chat and I'll analyze it.", 600);
        setStage("welcome");
        setBillResult(null);
        setIncome(null);
        setHouseholdSize(null);
        // New bill = new case; keep the patient's name and plan, reset the rest.
        setLastLetter(null);
        setNegotiationStarted(false);
        setPendingChannel(null);
        setLastResponse(null);
        setInsuranceStatus(null);
        setBillFile(null);
        setEobFile(null);
        setPendingLetterKind(null);
        setLetterFacts({});
        setFactStep(0);
      } else if (lower.includes("respond") || lower.includes("replied") || lower.includes("reply") || lower.includes("heard back") || lower.includes("they said") || lower.includes("offer") || lower.includes("denied") || lower.includes("counter") || lower.includes("collections")) {
        askForResponse();
      } else if (lower.includes("record") || lower.includes("final amount") || lower.includes("agreed") || lower.includes("settled") || lower.includes("paid")) {
        askForOutcome();
      } else {
        // Any other follow-up question → answered by Robin (LLM), with case context
        await askRobin(text);
      }
      return;
    }
  };

  // ── Quick prompts ────────────────────────────────────────────────────────
  const QUICK = ["How does the fee work?", "How does Robin work?", "What if I have insurance?", "Is my data private?"];

  // ── Timed out screen ─────────────────────────────────────────────────────
  if (timedOut) return (
    <div style={{ background: C.dark, minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 12, padding: 40, textAlign: "center", fontFamily: "system-ui, sans-serif" }}>
      <p style={{ color: C.white, fontWeight: 700, fontSize: 16 }}>Session expired</p>
      <p style={{ color: "#A8A6A2", fontSize: 13 }}>You were signed out after 15 minutes to protect your privacy.</p>
      <button onClick={() => { setTimedOut(false); setSessionActive(true); }} style={{ background: C.red, color: C.white, border: "none", borderRadius: 8, padding: "9px 20px", fontWeight: 600, cursor: "pointer", fontSize: 13 }}>Start again</button>
    </div>
  );

  // ── Main chat UI ─────────────────────────────────────────────────────────
  return (
    <div style={{ minHeight: "100vh", background: C.dark, display: "flex", flexDirection: "column", fontFamily: "system-ui, -apple-system, sans-serif" }}>
      <style>{`
        @keyframes bounce { 0%, 60%, 100% { transform: translateY(0); } 30% { transform: translateY(-5px); } }
        * { box-sizing: border-box; }
        input, textarea, button { font-family: inherit; }
        ::-webkit-scrollbar { width: 6px; } ::-webkit-scrollbar-thumb { background: #4A4641; border-radius: 3px; }
        ::placeholder { color: #A8A6A2; }
        button:focus-visible, input:focus-visible, a:focus-visible, label:focus-within { outline: 2px solid ${C.red}; outline-offset: 2px; border-radius: 6px; }
        @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; } }
      `}</style>

      {/* Header */}
      <div style={{ padding: "12px 18px", borderBottom: "1px solid #2A2A2A", display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
        <button onClick={onHome} aria-label="Back to Robin Health home"
          style={{ display: "flex", alignItems: "center", gap: 12, background: "none", border: "none", padding: 0, cursor: onHome ? "pointer" : "default" }}>
          <div aria-hidden="true" style={{ width: 32, height: 32, borderRadius: "50%", background: C.red, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 14, color: C.white, flexShrink: 0 }}>R</div>
          <span style={{ color: C.white, fontWeight: 700, fontSize: 15 }}>Robin</span>
        </button>
        <span style={{ color: "#A8A6A2", fontSize: 12, marginLeft: "auto" }}>Beta · Session expires after 15 min inactivity</span>
      </div>

      {/* Messages */}
      <div role="log" aria-live="polite" aria-label="Conversation with Robin"
        style={{ flex: 1, overflowY: "auto", padding: "20px 16px" }}>
        {messages.map(m => <Bubble key={m.id} msg={m} />)}
        {typing && <TypingIndicator />}
        <div ref={bottomRef} />
      </div>

      {/* Resume a saved case */}
      {stage === "welcome" && resumeAvailable && (
        <div style={{ padding: "0 16px 10px", display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button onClick={resumeCase}
            style={{ background: C.red, color: C.white, border: "none", borderRadius: 20, padding: "6px 14px", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>Resume my case</button>
          <button onClick={() => { clearResume(); setResumeAvailable(null); }}
            style={{ background: "none", border: "1px solid #333", borderRadius: 20, padding: "6px 14px", color: "#A8A6A2", fontSize: 12, cursor: "pointer" }}>Start fresh</button>
        </div>
      )}

      {/* Quick prompts */}
      {stage === "welcome" && messages.length >= 2 && input === "" && (
        <div style={{ padding: "0 16px 10px", display: "flex", gap: 8, flexWrap: "wrap" }}>
          {QUICK.map(q => (
            <button key={q} onClick={() => { setInput(q); setTimeout(() => inputRef.current?.focus(), 0); }}
              style={{ background: "none", border: "1px solid #333", borderRadius: 20, padding: "5px 12px", color: "#A8A6A2", fontSize: 11, cursor: "pointer", whiteSpace: "nowrap" }}>
              {q}
            </button>
          ))}
        </div>
      )}

      {/* Plan picker — shown when Robin asks the patient to choose a plan */}
      {stage === "ask_plan" && (
        <div style={{ padding: "0 16px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
          {PLAN_OPTIONS.map(p => (
            <button key={p.id} onClick={() => choosePlan(p.id)}
              style={{ textAlign: "left", background: C.charcoal, border: `1px solid ${C.red}55`, borderRadius: 12, padding: "12px 14px", cursor: "pointer", display: "flex", flexDirection: "column", gap: 3 }}>
              <span style={{ color: C.white, fontWeight: 700, fontSize: 14 }}>{p.title}</span>
              <span style={{ color: "#9A9A9A", fontSize: 12, lineHeight: 1.45 }}>{p.sub}</span>
            </button>
          ))}
          <button onClick={showFeeTerms}
            style={{ background: "none", border: "none", color: "#9A9A9A", fontSize: 12, textDecoration: "underline", cursor: "pointer", alignSelf: "flex-start", padding: "2px" }}>
            Read the full terms
          </button>
        </div>
      )}

      {/* Yes / No / Not sure chips for the letter-strengthening questions */}
      {stage === "ask_letter_facts" && (
        <div style={{ padding: "0 16px 10px", display: "flex", gap: 8, flexWrap: "wrap" }}>
          {[["yes", "Yes"], ["no", "No"], ["unsure", "Not sure"]].map(([v, label]) => (
            <button key={v} onClick={() => answerFact(v)}
              style={{ background: "none", border: "1px solid #3A3A3A", borderRadius: 20, padding: "7px 16px", color: C.white, fontSize: 13, cursor: "pointer" }}>
              {label}
            </button>
          ))}
        </div>
      )}

      {/* Letter choice for insured patients: provider dispute vs. insurer appeal */}
      {stage === "ask_letter" && insuranceStatus === "insured" && (
        <div style={{ padding: "0 16px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
          <button onClick={() => { userSay("Draft provider letter"); startProviderLetter(); }}
            style={{ textAlign: "left", background: C.charcoal, border: `1px solid ${C.red}55`, borderRadius: 12, padding: "12px 14px", cursor: "pointer", color: C.white, fontWeight: 600, fontSize: 14 }}>
            Draft a letter to my provider
          </button>
          <button onClick={() => { userSay("Appeal to my insurer"); startInsurerAppeal(); }}
            style={{ textAlign: "left", background: C.charcoal, border: `1px solid ${C.red}55`, borderRadius: 12, padding: "12px 14px", cursor: "pointer", color: C.white, fontWeight: 600, fontSize: 14 }}>
            Appeal the claim with my insurer
          </button>
        </div>
      )}

      {/* EOB upload (insured patients) */}
      {stage === "ask_eob" && (
        <div style={{ padding: "0 16px 10px", display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <label style={{ cursor: "pointer" }}>
            <input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" style={{ display: "none" }}
              onChange={e => e.target.files[0] && handleEobFile(e.target.files[0])} />
            <span style={{ display: "inline-block", background: C.charcoal, border: `1px solid ${C.red}55`, borderRadius: 20, padding: "8px 16px", color: C.white, fontWeight: 600, fontSize: 13 }}>📎 Upload my EOB</span>
          </label>
          <button onClick={skipEob}
            style={{ background: "none", border: "1px solid #333", borderRadius: 20, padding: "8px 16px", color: "#A8A6A2", fontSize: 13, cursor: "pointer" }}>Skip</button>
        </div>
      )}

      {/* Insurance triage options */}
      {stage === "ask_insurance" && (
        <div style={{ padding: "0 16px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
          {INSURANCE_OPTIONS.map(o => (
            <button key={o.id} onClick={() => chooseInsurance(o.id)}
              style={{ textAlign: "left", background: C.charcoal, border: `1px solid ${C.red}55`, borderRadius: 12, padding: "12px 14px", cursor: "pointer", color: C.white, fontWeight: 600, fontSize: 14 }}>
              {o.label}
            </button>
          ))}
        </div>
      )}

      {/* Send-letter options */}
      {stage === "ask_send" && (
        <div style={{ padding: "0 16px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
          {[
            { id: "letter_email", label: "Email it to the provider for me" },
            { id: "letter_fax", label: "Fax it for me" },
            { id: "self", label: "I'll send it myself" },
          ].map(o => (
            <button key={o.id} onClick={() => (o.id === "self" ? sendSelf() : startSend(o.id))}
              style={{ textAlign: "left", background: C.charcoal, border: `1px solid ${C.red}55`, borderRadius: 12, padding: "12px 14px", cursor: "pointer", color: C.white, fontWeight: 600, fontSize: 14 }}>
              {o.label}
            </button>
          ))}
        </div>
      )}

      {/* Inbound actions once a negotiation is being tracked */}
      {stage === "done" && negotiationStarted && (
        <div style={{ padding: "0 16px 10px", display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button onClick={() => { userSay("The provider responded"); askForResponse(); }}
            style={{ background: "none", border: "1px solid #333", borderRadius: 20, padding: "5px 12px", color: "#A8A6A2", fontSize: 11, cursor: "pointer" }}>The provider responded</button>
          <button onClick={() => { userSay("Record the final amount"); askForOutcome(); }}
            style={{ background: "none", border: "1px solid #333", borderRadius: 20, padding: "5px 12px", color: "#A8A6A2", fontSize: 11, cursor: "pointer" }}>Record the final amount</button>
        </div>
      )}

      {stage === "after_response" && (
        <div style={{ padding: "0 16px 10px", display: "flex", gap: 8, flexWrap: "wrap" }}>
          {lastResponse?.followup?.followup_letter_context && (
            <button onClick={() => { userSay("Draft my follow-up letter"); draftFollowupLetter(); }}
              style={{ background: "none", border: `1px solid ${C.red}55`, borderRadius: 20, padding: "5px 12px", color: C.white, fontSize: 11, cursor: "pointer" }}>Draft my follow-up letter</button>
          )}
          <button onClick={() => { userSay("Record the final amount"); askForOutcome(); }}
            style={{ background: "none", border: "1px solid #333", borderRadius: 20, padding: "5px 12px", color: "#A8A6A2", fontSize: 11, cursor: "pointer" }}>Record the final amount</button>
        </div>
      )}

      {/* File drop overlay */}
      <div
        onDragOver={e => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={e => { e.preventDefault(); setDragOver(false); handleFile(e.dataTransfer.files[0]); }}
        style={{ margin: "0 16px 8px" }}
      >
        {dragOver && (
          <div style={{ background: C.red + "22", border: `2px dashed ${C.red}`, borderRadius: 10, padding: 14, textAlign: "center", color: C.red, fontSize: 13, fontWeight: 600 }}>
            Drop your bill to analyze it
          </div>
        )}
      </div>

      {/* Input bar */}
      <div style={{ padding: "10px 16px 20px", borderTop: "1px solid #2A2A2A", flexShrink: 0 }}>
        <div style={{ display: "flex", gap: 8, background: C.charcoal, borderRadius: 14, padding: "6px 6px 6px 14px", alignItems: "center" }}>
          {/* Upload button — only show when in welcome or done stage */}
          {(stage === "welcome" || stage === "done") && (
            <label style={{ cursor: uploading ? "not-allowed" : "pointer", flexShrink: 0 }}>
              <input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" aria-label="Upload a bill" style={{ display: "none" }}
                onChange={e => e.target.files[0] && handleFile(e.target.files[0])} disabled={uploading} />
              <div style={{ width: 40, height: 40, borderRadius: "50%", background: "#3A3A3A", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18, opacity: uploading ? 0.4 : 1 }} title="Upload a bill" role="img" aria-label="Upload a bill">📎</div>
            </label>
          )}

          <input
            ref={inputRef}
            value={input}
            aria-label="Message Robin"
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && !e.shiftKey && handleSend()}
            placeholder={
              uploading ? "Analyzing your bill…" :
              stage === "ask_insurance" ? "Tap an option above…" :
              stage === "ask_eob" ? "Upload your EOB above, or type 'skip'…" :
              stage === "ask_income" ? "Enter your annual household income…" :
              stage === "ask_size" ? "Enter number of people in your household…" :
              stage === "ask_plan" ? "Tap a plan above, or type your choice…" :
              stage === "ask_name" ? "Type the patient's name…" :
              stage === "ask_letter_facts" ? "Tap Yes, No, or Not sure…" :
              stage === "ask_insurer" ? "Enter your insurance company's name…" :
              stage === "ask_send" ? "Tap an option above…" :
              stage === "ask_send_contact" ? (pendingChannel === "letter_fax" ? "Enter the fax number…" : "Enter the billing email…") :
              stage === "ask_response" ? "Paste what the provider said…" :
              stage === "after_response" ? "Tap an option, or ask me anything…" :
              stage === "ask_outcome" ? "Enter the final agreed amount…" :
              stage === "ask_letter" ? "Type yes or no…" :
              "Ask a question or upload a bill"
            }
            disabled={uploading}
            style={{ flex: 1, background: "none", border: "none", color: C.white, fontSize: 16, outline: "none", opacity: uploading ? 0.5 : 1 }}
          />

          <button onClick={handleSend} disabled={!input.trim() || uploading} aria-label="Send message"
            style={{ width: 40, height: 40, borderRadius: "50%", background: input.trim() && !uploading ? C.red : "#3A3A3A", border: "none", cursor: input.trim() && !uploading ? "pointer" : "default", color: C.white, fontSize: 18, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, transition: "background .15s" }}>
            ↑
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Top-level: marketing site wraps the chat; chat stays the main interface ─
export default function App() {
  const [view, setView] = useState("home");
  return view === "chat"
    ? <Chat onHome={() => setView("home")} />
    : <Landing onStart={() => setView("chat")} />;
}
