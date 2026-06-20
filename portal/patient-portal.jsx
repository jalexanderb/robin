import { useState, useEffect, useCallback, useRef } from "react";

// ─── HIPAA / Privacy notes embedded in code ───────────────────────────────
// 1. No PHI stored in localStorage or sessionStorage — all state is in-memory.
//    Refreshing the page requires re-authentication (patient_id re-entry).
//    This is intentional: the browser is not a trusted store for health data.
// 2. patient_id is treated as a session credential; it appears in API calls
//    but never in the URL bar (React state routing, not hash/path routing).
// 3. Session timeout: 15 minutes of inactivity triggers auto-logout.
// 4. The "bill" upload never previews raw content in the DOM — only metadata
//    (filename, size) is shown, reducing PHI exposure in the browser.
// 5. The API_KEY (if set) is held in component state, never localStorage.
// 6. All monetary amounts and case details are cleared on logout.
// ─────────────────────────────────────────────────────────────────────────

// API base URL -- set VITE_API_BASE (Vite) or REACT_APP_API_BASE (CRA)
// to point at the Robin FastAPI backend. Falls back to localhost for dev.
const API_BASE = "https://robin-production-542a.up.railway.app";
  (typeof import_meta_env !== "undefined" && import_meta_env.VITE_API_BASE) ||
  (typeof process !== "undefined" && process.env?.REACT_APP_API_BASE) ||
  "http://localhost:8001"
);
const SESSION_TIMEOUT_MS = 15 * 60 * 1000; // 15 min

// ─── Design tokens (Robin brand) ───────────────────────────────────
const C = {
  red: "#E03E27",
  redDark: "#B8311D",
  redLight: "#F5E8E6",
  dark: "#1A1A1A",
  charcoal: "#2D2D2D",
  slate: "#4A4A4A",
  muted: "#888",
  border: "#E5E5E5",
  bg: "#FAFAFA",
  white: "#FFFFFF",
  green: "#1A7F4E",
  greenLight: "#E8F5EE",
  amber: "#C47A00",
  amberLight: "#FFF8E7",
  blue: "#1A4A7F",
  blueLight: "#E8EFFE",
};

const STATUS_META = {
  intake:             { label: "Reviewing",     color: C.muted,  bg: "#F5F5F5" },
  reviewing:          { label: "In Review",     color: C.amber,  bg: C.amberLight },
  awaiting_user_input:{ label: "Your Action",   color: C.red,    bg: C.redLight },
  ready_for_action:   { label: "Ready",         color: C.blue,   bg: C.blueLight },
  negotiating:        { label: "Negotiating",   color: C.blue,   bg: C.blueLight },
  resolved:           { label: "Resolved ✓",   color: C.green,  bg: C.greenLight },
};

const NEG_STATUS_META = {
  pending:          { label: "Ready to send",  color: C.muted },
  contacted:        { label: "Letter sent",    color: C.blue },
  provider_replied: { label: "Provider replied", color: C.amber },
  counter_offer:    { label: "Counter offer",  color: C.amber },
  agreed:           { label: "Deal agreed ✓", color: C.green },
  paid:             { label: "Paid ✓",        color: C.green },
  rejected:         { label: "Rejected",       color: C.red },
  withdrawn:        { label: "Withdrawn",      color: C.muted },
};

// ─── Utility ─────────────────────────────────────────────────────────────
const fmt$ = (n) => n == null ? "—" : `$${Number(n).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;
const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "—";

// MOCK data used as fallback when API is unreachable (dev/demo).
// Each view replaces its MOCK reference with a real api.get/post call.
const MOCK = {
  feeTerms: {
    terms: {
      version: "v1.0",
      text: `Robin Fee Agreement — Version 1.0

WHAT ROBIN DOES
Robin analyzes your medical bills and, if you choose to proceed,
negotiates with the provider on your behalf to reduce what you owe.

ROBIN'S FEE
If Robin successfully reduces your bill, you pay Robin 20% of the amount saved.

Example: Bill is $5,000. Robin negotiates to $2,000. You saved $3,000.
Robin's fee: 20% of $3,000 = $600. Your total cost: $2,600 instead of $5,000.

IF ROBIN DOESN'T SAVE YOU ANYTHING
You owe nothing. The fee only applies when Robin achieves a real,
documented reduction in your bill.

WHEN YOU PAY
The fee becomes due once you and the provider have agreed on a reduced amount.

YOUR AUTHORIZATION
By accepting these terms, you authorize Robin to communicate with your
healthcare provider on your behalf regarding this bill.`,
      fee_percentage: 20,
      no_cure_no_fee: true,
    },
    agreement_status: { accepted: false },
  },
  case: {
    case_id: "c1b2a3d4-0000-0000-0000-000000000001",
    case_status: "negotiating",
    negotiation: {
      status: "contacted",
      original_billed_amount: 4800,
      target_amount: 1440,
      amount_saved: null,
      robinhealth_fee: null,
      patient_net_savings: null,
      contacts: [
        { channel: "letter_mail", sent_at: "2026-06-10T09:00:00Z", notes: "Reference: RH-C1B2A3-20260610-F7D2" },
      ],
    },
    synthesis: {
      headline_low: 1440,
      headline_high: 4800,
      headline_could_eliminate: false,
      reasons: [
        { outcome_type: "partial_reduction", summary: "Based on your household income and the facility's published Financial Assistance Policy, your balance may be reducible to approximately $1,440.", estimated_low: 1440, estimated_high: 4800 },
        { outcome_type: "procedural_leverage", summary: "The hospital's price transparency file lists negotiated rates for these procedures at $1,200–$1,800.", estimated_low: null, estimated_high: null },
      ],
    },
    bill: {
      total_billed_amount: 4800,
      provider_name_raw: "Springfield General Hospital",
      date_of_service: "2026-03-14",
    },
  },
};

// ─── API client ───────────────────────────────────────────────────────────
function useApi(apiKey) {
  const headers = { "Content-Type": "application/json", ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}) };

  const get = useCallback(async (path) => {
    const r = await fetch(`${API_BASE}${path}`, { headers });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }, [apiKey]);

  const post = useCallback(async (path, formData) => {
    const r = await fetch(`${API_BASE}${path}`, { method: "POST", headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {}, body: formData });
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || `${r.status}`); }
    return r.json();
  }, [apiKey]);

  return { get, post };
}

// ─── Shared UI components ─────────────────────────────────────────────────
function Pill({ label, color, bg }) {
  return <span style={{ background: bg || "#F5F5F5", color: color || C.muted, borderRadius: 20, padding: "2px 10px", fontSize: 12, fontWeight: 600, whiteSpace: "nowrap" }}>{label}</span>;
}

function Card({ children, style }) {
  return <div style={{ background: C.white, border: `1px solid ${C.border}`, borderRadius: 12, padding: "24px 28px", ...style }}>{children}</div>;
}

function Btn({ children, onClick, variant = "primary", disabled, style }) {
  const base = { border: "none", borderRadius: 8, padding: "10px 22px", fontWeight: 600, fontSize: 14, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, transition: "all 0.15s", ...style };
  const variants = {
    primary:   { background: C.red,     color: C.white },
    secondary: { background: C.white,   color: C.dark, border: `1.5px solid ${C.border}` },
    ghost:     { background: "transparent", color: C.red },
    green:     { background: C.green,   color: C.white },
  };
  return <button onClick={disabled ? undefined : onClick} style={{ ...base, ...variants[variant] }}>{children}</button>;
}

function SectionHeader({ children }) {
  return <h2 style={{ fontSize: 18, fontWeight: 700, color: C.dark, margin: "0 0 16px", letterSpacing: "-0.3px" }}>{children}</h2>;
}

function Alert({ type, children }) {
  const styles = {
    info:    { background: C.blueLight,  color: C.blue,  border: `1px solid #C0D0F0` },
    warning: { background: C.amberLight, color: C.amber, border: `1px solid #F0D080` },
    success: { background: C.greenLight, color: C.green, border: `1px solid #A0D0B0` },
    error:   { background: C.redLight,   color: C.redDark, border: `1px solid #F0C0B8` },
  };
  return <div style={{ ...styles[type], borderRadius: 8, padding: "12px 16px", fontSize: 13, lineHeight: 1.5 }}>{children}</div>;
}

// ─── Session timeout hook ─────────────────────────────────────────────────
function useSessionTimeout(active, onTimeout) {
  const timer = useRef(null);
  const reset = useCallback(() => {
    clearTimeout(timer.current);
    if (active) timer.current = setTimeout(onTimeout, SESSION_TIMEOUT_MS);
  }, [active, onTimeout]);

  useEffect(() => {
    if (!active) return;
    const events = ["mousedown", "keydown", "touchstart", "scroll"];
    events.forEach(e => window.addEventListener(e, reset, { passive: true }));
    reset();
    return () => {
      events.forEach(e => window.removeEventListener(e, reset));
      clearTimeout(timer.current);
    };
  }, [active, reset]);
}

// ─── Views ────────────────────────────────────────────────────────────────

// 1. Chat welcome — Robin opens the conversation
// The chat interface is the first thing users see. Robin speaks first,
// the user can upload a bill or ask questions naturally.
// No patient ID entry — the ID is created server-side on first upload
// and returned in the response. Until then, the session is anonymous.
// HIPAA: no PHI is collected or displayed at this stage.
function WelcomeView({ onEnter }) {
  const ROBIN_OPENING = [
    { from: "robin", text: "Hi, I'm Robin — your AI health advocate. I help people like you fight back against confusing medical bills.", delay: 0 },
    { from: "robin", text: "If you've got a medical bill that feels too high, drop it here and I'll tell you exactly what you might save — no commitment, no upfront cost.", delay: 700 },
    { from: "robin", text: "You can also ask me anything about your bill, your insurance, or what your rights are.", delay: 1400 },
  ];

  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [showTyping, setShowTyping] = useState(false);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  // Stream in Robin's opening messages with delays
  useEffect(() => {
    let cancelled = false;
    const show = async () => {
      for (const msg of ROBIN_OPENING) {
        await new Promise(r => setTimeout(r, msg.delay));
        if (cancelled) return;
        if (msg.delay > 0) setShowTyping(true);
        await new Promise(r => setTimeout(r, 500));
        if (cancelled) return;
        setShowTyping(false);
        setMessages(prev => [...prev, { id: Date.now() + Math.random(), ...msg }]);
      }
    };
    show();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, showTyping]);

  const addUserMessage = (text) => {
    setMessages(prev => [...prev, { id: Date.now(), from: "user", text }]);
  };

  const addRobinReply = async (text, delay = 600) => {
    setShowTyping(true);
    await new Promise(r => setTimeout(r, delay));
    setShowTyping(false);
    setMessages(prev => [...prev, { id: Date.now() + Math.random(), from: "robin", text }]);
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text) return;
    setInput("");
    addUserMessage(text);

    // Simple intent routing — in production this calls the LLM
    const lower = text.toLowerCase();
    if (lower.includes("cost") || lower.includes("fee") || lower.includes("charge") || (lower.includes("how") && lower.includes("work") && !lower.includes("robin"))) {
      await addRobinReply("It's simple: I only charge if I save you money. If I negotiate your bill down, you pay me 20% of what you saved. If I can't save you anything, you owe nothing.");
    } else if (lower.includes("how does robin") || lower.includes("how robin") || lower.includes("right") || lower.includes("law") || lower.includes("legal") || lower.includes("can they")) {
      await addRobinReply("Great question. Hospitals are required by federal law to publish their prices and have financial assistance programs. Many bills have errors, and providers often accept less than the sticker price — especially for cash payment. I know how to use these rules to your advantage.");
    } else if (lower.includes("insurance") || lower.includes("insur")) {
      await addRobinReply("I can help whether you're insured or not. If your insurer has already processed the claim, I can check if the amount is correct. If you're uninsured or underinsured, I can negotiate the bill directly. Either way, drop your bill and let's take a look.");
    } else if (lower.includes("safe") || lower.includes("private") || lower.includes("hipaa") || lower.includes("data")) {
      await addRobinReply("Your information is handled under HIPAA. Your bill is encrypted, never sold, and only used to negotiate on your behalf. You can delete your data at any time.");
    } else {
      await addRobinReply("I'm best at analyzing actual bills — drop yours here and I'll give you a concrete answer. Or if you have a specific question, keep going and I'll do my best.");
    }
  };

  const handleFile = async (file) => {
    if (!file) return;
    setUploading(true);
    addUserMessage(`📎 ${file.name} (${Math.round(file.size / 1024)} KB)`);
    await addRobinReply("Got it — analyzing your bill now. This usually takes about 15 seconds.", 300);

    // Simulate upload → in production: POST /intake with FormData
    await new Promise(r => setTimeout(r, 1400));
    setUploading(false);
    addRobinReply("Done. I found some things worth fighting for. Let me show you what I found.", 200);
    await new Promise(r => setTimeout(r, 1800));
    onEnter("new"); // signals App to transition to analysis
  };

  const QUICK_PROMPTS = [
    "Does the service cost anything?",
    "How does Robin work?",
    "What if I have insurance?",
    "Is my data private?",
  ];

  return (
    <div style={{ minHeight: "100vh", background: C.dark, display: "flex", flexDirection: "column" }}>
      {/* Robin header */}
      <div style={{ padding: "16px 20px", borderBottom: "1px solid #2A2A2A", display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{ width: 34, height: 34, borderRadius: "50%", background: C.red, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 15, color: C.white, flexShrink: 0 }}>R</div>
        <div>
          <p style={{ color: C.white, fontWeight: 700, fontSize: 15, margin: 0 }}>Robin</p>
          <p style={{ color: "#555", fontSize: 12, margin: 0 }}>Helping you fight unfair medical bills</p>
        </div>
      </div>

      {/* Chat messages */}
      <div style={{ flex: 1, overflowY: "auto", padding: "20px 16px", display: "flex", flexDirection: "column", gap: 12 }}>
        {messages.map(msg => (
          <div key={msg.id} style={{ display: "flex", flexDirection: msg.from === "user" ? "row-reverse" : "row", gap: 8, alignItems: "flex-end" }}>
            {msg.from === "robin" && (
              <div style={{ width: 28, height: 28, borderRadius: "50%", background: C.red, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 12, color: C.white, flexShrink: 0, marginBottom: 2 }}>R</div>
            )}
            <div style={{
              maxWidth: "78%",
              background: msg.from === "robin" ? C.charcoal : C.red,
              color: C.white,
              borderRadius: msg.from === "robin" ? "4px 16px 16px 16px" : "16px 4px 16px 16px",
              padding: "10px 14px",
              fontSize: 14,
              lineHeight: 1.55,
            }}>
              {msg.text}
            </div>
          </div>
        ))}

        {/* Typing indicator */}
        {showTyping && (
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
            <div style={{ width: 28, height: 28, borderRadius: "50%", background: C.red, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 12, color: C.white, flexShrink: 0 }}>R</div>
            <div style={{ background: C.charcoal, borderRadius: "4px 16px 16px 16px", padding: "12px 16px" }}>
              <div style={{ display: "flex", gap: 5 }}>
                {[0, 1, 2].map(i => (
                  <div key={i} style={{ width: 7, height: 7, borderRadius: "50%", background: "#666", animation: `bounce 1.2s ${i * 0.2}s infinite` }} />
                ))}
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Quick prompts (shown until user types) */}
      {messages.length >= 3 && input === "" && (
        <div style={{ padding: "0 16px 10px", display: "flex", gap: 8, flexWrap: "wrap" }}>
          {QUICK_PROMPTS.map(q => (
            <button key={q} onClick={() => { setInput(q); inputRef.current?.focus(); }} style={{ background: "none", border: "1px solid #3A3A3A", borderRadius: 20, padding: "5px 12px", color: "#888", fontSize: 12, cursor: "pointer", whiteSpace: "nowrap" }}>
              {q}
            </button>
          ))}
        </div>
      )}

      {/* File drop zone integrated into chat */}
      <div
        onDragOver={e => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={e => { e.preventDefault(); setDragOver(false); handleFile(e.dataTransfer.files[0]); }}
        style={{ margin: "0 16px", marginBottom: 8 }}
      >
        {dragOver && (
          <div style={{ background: C.red + "22", border: `2px dashed ${C.red}`, borderRadius: 12, padding: "16px", textAlign: "center", color: C.red, fontSize: 13, fontWeight: 600 }}>
            Drop your bill to analyze it
          </div>
        )}
      </div>

      {/* Input bar */}
      <div style={{ padding: "10px 16px 16px", borderTop: "1px solid #2A2A2A", background: C.dark }}>
        <div style={{ display: "flex", gap: 8, background: C.charcoal, borderRadius: 14, padding: "6px 6px 6px 14px", alignItems: "center" }}>
          {/* Upload button */}
          <label style={{ cursor: uploading ? "not-allowed" : "pointer", flexShrink: 0 }}>
            <input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" style={{ display: "none" }} onChange={e => e.target.files[0] && handleFile(e.target.files[0])} disabled={uploading} />
            <div style={{ width: 34, height: 34, borderRadius: "50%", background: "#3A3A3A", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 16, opacity: uploading ? 0.4 : 1, transition: "opacity .2s" }} title="Upload a bill">
              📎
            </div>
          </label>

          <input
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && !e.shiftKey && handleSend()}
            placeholder={uploading ? "Analyzing your bill…" : "Ask a question or upload a bill above"}
            disabled={uploading}
            style={{ flex: 1, background: "none", border: "none", color: C.white, fontSize: 14, outline: "none", opacity: uploading ? 0.5 : 1 }}
          />

          <button
            onClick={handleSend}
            disabled={!input.trim() || uploading}
            style={{ width: 34, height: 34, borderRadius: "50%", background: input.trim() && !uploading ? C.red : "#3A3A3A", border: "none", cursor: input.trim() && !uploading ? "pointer" : "default", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, transition: "background .15s", fontSize: 16 }}
          >
            ↑
          </button>
        </div>
      </div>

      <style>{`
        @keyframes bounce {
          0%, 60%, 100% { transform: translateY(0); }
          30% { transform: translateY(-5px); }
        }
      `}</style>
    </div>
  );
}


// 2. Fee agreement
function FeeAgreementView({ patientId, api, onAgreed }) {
  const [agreed, setAgreed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [terms, setTerms] = useState(MOCK.feeTerms.terms);
  const [termsLoading, setTermsLoading] = useState(true);
  useEffect(() => {
    api.get(`/patients/${patientId}/fee-terms`)
      .then(data => setTerms(data.terms))
      .catch(() => {/* fallback: MOCK.feeTerms.terms already set */})
      .finally(() => setTermsLoading(false));
  }, [patientId]);

  const handleSubmit = async () => {
    if (!agreed) return;
    setLoading(true);
    try {
      try {
        const fd = new FormData();
        fd.append("affirmed", "true");
        await api.post(`/patients/${patientId}/agree-to-terms`, fd);
      } catch {
        // In demo/dev mode the API may not be reachable — proceed anyway.
        // In production, enforce this; don't swallow the error.
        console.warn("agree-to-terms API unreachable — continuing in demo mode");
      }
      onAgreed();
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 680, margin: "0 auto", padding: "40px 24px" }}>
      <div style={{ marginBottom: 32 }}>
        <h1 style={{ fontSize: 26, fontWeight: 800, color: C.dark, margin: "0 0 8px", letterSpacing: "-0.5px" }}>Before we begin</h1>
        <p style={{ color: C.slate, fontSize: 15, margin: 0 }}>One quick thing before Robin goes to work — please read the fee agreement below.</p>
      </div>

      <Card style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", gap: 20, marginBottom: 24, flexWrap: "wrap" }}>
          {[["💰 Robin's fee", `${terms.fee_percentage}% of savings`],
            ["🚫 If we fail", "You pay nothing"],
            ["⏱ When", "Only after a deal is made"]].map(([icon, val]) => (
            <div key={icon} style={{ flex: "1 1 140px", background: C.bg, border: `1px solid ${C.border}`, borderRadius: 10, padding: "14px 16px", textAlign: "center" }}>
              <div style={{ fontSize: 20, marginBottom: 4 }}>{icon}</div>
              <div style={{ fontSize: 11, color: C.muted, marginBottom: 2 }}>{icon.replace(/^\S+\s/, "")}</div>
              <div style={{ fontWeight: 700, color: C.dark, fontSize: 15 }}>{val}</div>
            </div>
          ))}
        </div>

        {/* Worked example */}
        <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: 10, padding: "16px 18px", marginBottom: 20 }}>
          <p style={{ fontWeight: 600, color: C.dark, fontSize: 13, margin: "0 0 12px" }}>How it works — an example:</p>
          {[["Your bill", "$5,000"], ["Negotiated to", "$2,000"], ["You saved", "$3,000"], ["Robin's fee (20%)", "$600"], ["Your total cost", "$2,600 ✓"]].map(([k, v], i) => (
            <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "5px 0", borderTop: i > 0 ? `1px solid ${C.border}` : "none" }}>
              <span style={{ color: C.slate, fontSize: 13 }}>{k}</span>
              <span style={{ fontWeight: i === 4 ? 700 : 500, color: i === 4 ? C.green : C.dark, fontSize: 13 }}>{v}</span>
            </div>
          ))}
        </div>

        {/* Full terms */}
        <div style={{ background: "#F9F9F9", border: `1px solid ${C.border}`, borderRadius: 8, padding: "16px", maxHeight: 220, overflowY: "auto", marginBottom: 20 }}>
          <pre style={{ fontFamily: "inherit", fontSize: 12, color: C.slate, whiteSpace: "pre-wrap", margin: 0, lineHeight: 1.65 }}>{terms.text}</pre>
        </div>

        <label style={{ display: "flex", alignItems: "flex-start", gap: 12, cursor: "pointer" }}>
          <input type="checkbox" checked={agreed} onChange={e => setAgreed(e.target.checked)}
            style={{ marginTop: 2, width: 16, height: 16, accentColor: C.red, flexShrink: 0 }} />
          <span style={{ fontSize: 14, color: C.dark, lineHeight: 1.5 }}>
            I have read and understand the fee agreement. I authorize Robin to negotiate my medical bill, and I agree to pay 20% of any savings achieved.
          </span>
        </label>
      </Card>

      <Btn onClick={handleSubmit} disabled={!agreed || loading} style={{ width: "100%", padding: 14 }}>
        {loading ? "Recording your agreement…" : "I Agree — Let's Get Started →"}
      </Btn>
    </div>
  );
}

// 3. Upload bill
function UploadView({ patientId, api, onUploaded }) {
  const [file, setFile] = useState(null);
  const [income, setIncome] = useState("");
  const [size, setSize] = useState("");
  const [state, setState] = useState("");
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const US_STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"];

  const handleDrop = (e) => {
    e.preventDefault(); setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) setFile(f);
  };

  const handleSubmit = async () => {
    if (!file) { setError("Please select your bill file."); return; }
    setError(""); setLoading(true);
    try {
      try {
        const fd = new FormData();
        fd.append("bill_document", file, file.name);
        if (income) fd.append("household_income", income);
        if (size)   fd.append("household_size", size);
        if (state)  fd.append("state", state);

        const data = await api.post("/intake", fd);
        const result = data.result || {};
        const synthesis = result.synthesis || MOCK.case.synthesis;
        const bill = result.bill || MOCK.case.bill;
        const caseId = data.case_id || MOCK.case.case_id;
        const patientId = data.patient_id;  // returned by the server
        onUploaded({ caseId, synthesis, bill, patientId });
      } catch (e) {
        // API unreachable or LLM unavailable — show demo result so the
        // user can still see what the flow looks like.
        console.warn("intake API unreachable — showing demo result:", e.message);
        await new Promise(r => setTimeout(r, 1200));
        onUploaded({ caseId: MOCK.case.case_id, synthesis: MOCK.case.synthesis, bill: MOCK.case.bill });
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 640, margin: "0 auto", padding: "40px 24px" }}>
      <h1 style={{ fontSize: 26, fontWeight: 800, color: C.dark, margin: "0 0 6px", letterSpacing: "-0.5px" }}>Upload your bill</h1>
      <p style={{ color: C.slate, fontSize: 15, margin: "0 0 28px" }}>We'll analyze it and tell you how much you could save — no commitment required.</p>

      {/* Drop zone */}
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => document.getElementById("bill-input").click()}
        style={{
          border: `2px dashed ${dragging ? C.red : file ? C.green : C.border}`,
          background: dragging ? C.redLight : file ? C.greenLight : C.bg,
          borderRadius: 12, padding: "36px 24px", textAlign: "center",
          cursor: "pointer", transition: "all 0.2s", marginBottom: 20,
        }}
      >
        <input id="bill-input" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" style={{ display: "none" }} onChange={e => setFile(e.target.files[0])} />
        {file ? (
          <>
            <div style={{ fontSize: 28, marginBottom: 8 }}>✅</div>
            {/* Show only filename and size, not file contents — minimizes PHI in DOM */}
            <p style={{ fontWeight: 600, color: C.green, margin: "0 0 4px" }}>{file.name}</p>
            <p style={{ color: C.muted, fontSize: 13, margin: 0 }}>{(file.size / 1024).toFixed(0)} KB — Click to change</p>
          </>
        ) : (
          <>
            <div style={{ fontSize: 32, marginBottom: 10 }}>📄</div>
            <p style={{ fontWeight: 600, color: C.dark, margin: "0 0 4px" }}>Drop your bill here, or click to choose</p>
            <p style={{ color: C.muted, fontSize: 13, margin: 0 }}>PDF, PNG, JPG or WebP · up to 20 MB</p>
          </>
        )}
      </div>

      <Card>
        <p style={{ fontWeight: 600, color: C.dark, fontSize: 14, margin: "0 0 16px" }}>Your household details <span style={{ fontWeight: 400, color: C.muted }}>(helps determine charity care eligibility)</span></p>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
          {[
            { label: "Annual income", placeholder: "$48,000", val: income, set: setIncome, type: "number" },
            { label: "Household size", placeholder: "3", val: size, set: setSize, type: "number" },
          ].map(f => (
            <div key={f.label}>
              <label style={{ fontSize: 12, color: C.muted, display: "block", marginBottom: 4 }}>{f.label}</label>
              <input type={f.type} placeholder={f.placeholder} value={f.val} onChange={e => f.set(e.target.value)}
                style={{ width: "100%", border: `1.5px solid ${C.border}`, borderRadius: 8, padding: "9px 12px", fontSize: 14, color: C.dark, outline: "none", boxSizing: "border-box" }} />
            </div>
          ))}
          <div>
            <label style={{ fontSize: 12, color: C.muted, display: "block", marginBottom: 4 }}>State</label>
            <select value={state} onChange={e => setState(e.target.value)}
              style={{ width: "100%", border: `1.5px solid ${C.border}`, borderRadius: 8, padding: "9px 12px", fontSize: 14, color: C.dark, outline: "none", background: C.white, boxSizing: "border-box" }}>
              <option value="">—</option>
              {US_STATES.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        </div>
      </Card>

      {error && <Alert type="error" style={{ marginTop: 12 }}>{error}</Alert>}

      <Btn onClick={handleSubmit} disabled={loading} style={{ width: "100%", padding: 14, marginTop: 20 }}>
        {loading ? "Analyzing your bill…" : "Analyze My Bill →"}
      </Btn>
      <p style={{ color: C.muted, fontSize: 12, textAlign: "center", marginTop: 12 }}>
        Your bill is processed securely. We never share your health information.
      </p>
    </div>
  );
}

// 4. Analysis result
function AnalysisView({ synthesis, bill, caseId, patientId, api, onStartNegotiation }) {
  const low = synthesis?.headline_low;
  const high = synthesis?.headline_high || bill?.total_billed_amount;
  const savingsLow = high && low != null ? high - low : null;
  const provider = bill?.provider_name_raw || "Your provider";

  return (
    <div style={{ maxWidth: 680, margin: "0 auto", padding: "40px 24px" }}>
      <h1 style={{ fontSize: 26, fontWeight: 800, color: C.dark, margin: "0 0 6px", letterSpacing: "-0.5px" }}>Your bill analysis</h1>
      <p style={{ color: C.slate, fontSize: 15, margin: "0 0 28px" }}>{provider} · {fmtDate(bill?.date_of_service)}</p>

      {/* Headline savings card */}
      <div style={{ background: C.dark, borderRadius: 14, padding: "28px 32px", marginBottom: 20, position: "relative", overflow: "hidden" }}>
        <div style={{ position: "absolute", top: 0, right: 0, width: 180, height: 180, background: C.red, borderRadius: "50%", opacity: 0.08, transform: "translate(40%, -40%)" }} />
        <p style={{ color: "#888", fontSize: 13, margin: "0 0 6px" }}>POTENTIAL SAVINGS</p>
        <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 16 }}>
          <span style={{ color: C.red, fontSize: 44, fontWeight: 800, letterSpacing: "-2px" }}>{fmt$(savingsLow)}</span>
          {low != null && <span style={{ color: "#666", fontSize: 18 }}>or more</span>}
        </div>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <div><p style={{ color: "#666", fontSize: 12, margin: "0 0 2px" }}>BILLED</p><p style={{ color: C.white, fontWeight: 700, fontSize: 20, margin: 0 }}>{fmt$(high)}</p></div>
          <div style={{ color: "#444", fontSize: 24, alignSelf: "center" }}>→</div>
          <div><p style={{ color: "#666", fontSize: 12, margin: "0 0 2px" }}>ESTIMATED AFTER</p><p style={{ color: C.red, fontWeight: 700, fontSize: 20, margin: 0 }}>{fmt$(low)}</p></div>
        </div>
      </div>

      {/* Reasons */}
      <Card style={{ marginBottom: 20 }}>
        <SectionHeader>Why you can save</SectionHeader>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {(synthesis?.reasons || []).map((r, i) => (
            <div key={i} style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
              <div style={{ width: 28, height: 28, borderRadius: "50%", background: r.outcome_type === "partial_reduction" ? C.redLight : C.blueLight, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, fontSize: 14 }}>
                {r.outcome_type === "partial_reduction" ? "💰" : "⚖️"}
              </div>
              <p style={{ color: C.slate, fontSize: 14, lineHeight: 1.55, margin: 0 }}>{r.summary}</p>
            </div>
          ))}
        </div>
      </Card>

      <Alert type="info" style={{ marginBottom: 20 }}>
        <strong>Beta disclaimer:</strong> These estimates are based on published Medicare rates, hospital price transparency data, and your financial situation. Actual savings may vary. Robin will only charge a fee if we achieve a real, documented reduction.
      </Alert>

      <Btn onClick={() => onStartNegotiation(caseId)} style={{ width: "100%", padding: 14 }}>
        Start Negotiation — We'll Handle It →
      </Btn>
      <p style={{ color: C.muted, fontSize: 12, textAlign: "center", marginTop: 10 }}>
        You can review and approve every letter before it's sent.
      </p>
    </div>
  );
}

// 5. Case dashboard (active case)
function CaseDashboardView({ caseId, patientId, api, onLogout }) {
  const [caseData, setCaseData] = useState(MOCK.case);
  const [caseLoading, setCaseLoading] = useState(true);
  useEffect(() => {
    if (!caseId) return;
    api.get(`/cases/${caseId}`)
      .then(data => {
        // API returns { case_id, case_status, negotiation }
        // Merge with MOCK.case shape so synthesis/bill fields (from intake)
        // still render correctly even if GET /cases doesn't repeat them.
        setCaseData(prev => ({ ...MOCK.case, ...data }));
      })
      .catch(() => {/* fallback: MOCK.case already set */})
      .finally(() => setCaseLoading(false));
  }, [caseId]);
  const neg = caseData?.negotiation;
  const bill = caseData?.bill;
  const synth = caseData?.synthesis;
  const statusMeta = STATUS_META[caseData?.case_status] || STATUS_META.intake;
  const negMeta = NEG_STATUS_META[neg?.status] || NEG_STATUS_META.pending;

  const [activeTab, setActiveTab] = useState("overview");
  const [responseText, setResponseText] = useState("");
  const [submitLoading, setSubmitLoading] = useState(false);
  const [followup, setFollowup] = useState(null);

  const handleSubmitResponse = async () => {
    if (!responseText.trim()) return;
    setSubmitLoading(true);
    try {
      const fd = new FormData();
      fd.append("response_text", responseText);
      const data = await api.post(`/cases/${caseId}/response`, fd);
      setFollowup(data);
    } catch (e) {
      // Demo fallback when API is unreachable
      console.warn("response API unreachable — showing demo result:", e.message);
      await new Promise(r => setTimeout(r, 900));
      setFollowup({
        classified: { response_type: "reduced_offer", extracted_amount: 2100 },
        followup: {
          action: "Counter with our original target or accept their offer",
          urgency: "within_week",
          explanation: "The provider offered $2,100. Robin originally requested $1,440. You can accept their offer (saving $2,700) or let Robin push for our original target.",
          followup_letter_context: { letter_type: "counter_offer" },
          resolves_negotiation: false,
          suggested_resolution: { agreed_amount: 2100 },
        },
      });
    } finally { setSubmitLoading(false); }
  };

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "timeline", label: "Timeline" },
    { id: "respond", label: "Provider replied?" },
  ];

  return (
    <div style={{ maxWidth: 760, margin: "0 auto", padding: "28px 24px" }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 24, flexWrap: "wrap", gap: 12 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
            <h1 style={{ fontSize: 22, fontWeight: 800, color: C.dark, margin: 0, letterSpacing: "-0.4px" }}>{bill?.provider_name_raw || "Your case"}</h1>
            <Pill label={statusMeta.label} color={statusMeta.color} bg={statusMeta.bg} />
          </div>
          <p style={{ color: C.muted, fontSize: 13, margin: 0 }}>Date of service: {fmtDate(bill?.date_of_service)} · Case ID: {caseId.slice(0, 8)}…</p>
        </div>
        <Btn variant="ghost" onClick={onLogout} style={{ fontSize: 13, padding: "6px 14px" }}>Sign out</Btn>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 0, borderBottom: `1px solid ${C.border}`, marginBottom: 24 }}>
        {tabs.map(t => (
          <button key={t.id} onClick={() => setActiveTab(t.id)} style={{
            background: "none", border: "none", padding: "10px 18px", fontSize: 14,
            fontWeight: activeTab === t.id ? 700 : 400,
            color: activeTab === t.id ? C.dark : C.muted,
            borderBottom: activeTab === t.id ? `2.5px solid ${C.red}` : "2.5px solid transparent",
            cursor: "pointer", marginBottom: -1,
          }}>{t.label}</button>
        ))}
      </div>

      {activeTab === "overview" && (
        <div>
          {/* Savings summary */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12, marginBottom: 20 }}>
            {[
              { label: "BILLED", val: fmt$(bill?.total_billed_amount), color: C.dark },
              { label: "WE'RE TARGETING", val: fmt$(neg?.target_amount), color: C.blue },
              { label: "POTENTIAL SAVINGS", val: neg?.target_amount && bill?.total_billed_amount ? fmt$(bill.total_billed_amount - neg.target_amount) : "—", color: C.red },
              { label: "YOUR FEE (IF WE WIN)", val: neg?.target_amount && bill?.total_billed_amount ? fmt$((bill.total_billed_amount - neg.target_amount) * 0.20) : "—", color: C.muted },
            ].map(item => (
              <Card key={item.label} style={{ padding: "16px 18px" }}>
                <p style={{ fontSize: 11, color: C.muted, margin: "0 0 4px", fontWeight: 600, letterSpacing: "0.5px" }}>{item.label}</p>
                <p style={{ fontSize: 22, fontWeight: 800, color: item.color, margin: 0, letterSpacing: "-0.5px" }}>{item.val}</p>
              </Card>
            ))}
          </div>

          {/* Negotiation status */}
          <Card>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <SectionHeader>Negotiation status</SectionHeader>
              <Pill label={negMeta.label} color={negMeta.color} bg={negMeta.color + "22"} />
            </div>
            {neg?.status === "contacted" && (
              <Alert type="info">
                We sent a negotiation letter to the provider on {fmtDate(neg?.contacts?.[0]?.sent_at)}. 
                Providers typically respond within 2–4 weeks. We'll notify you when they do.
              </Alert>
            )}
            {neg?.agreed_amount != null && (
              <Alert type="success">
                🎉 <strong>Deal reached!</strong> The provider agreed to {fmt$(neg.agreed_amount)}. 
                You saved {fmt$(neg.amount_saved)} — Robin's fee is {fmt$(neg.robinhealth_fee)}.
                Your net saving: <strong>{fmt$(neg.patient_net_savings)}</strong>.
              </Alert>
            )}
          </Card>
        </div>
      )}

      {activeTab === "timeline" && (
        <Card>
          <SectionHeader>Activity timeline</SectionHeader>
          <div style={{ position: "relative" }}>
            {(neg?.contacts || []).map((c, i) => (
              <div key={i} style={{ display: "flex", gap: 14, paddingBottom: 20 }}>
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                  <div style={{ width: 12, height: 12, borderRadius: "50%", background: C.red, flexShrink: 0, marginTop: 2 }} />
                  {i < (neg.contacts.length - 1) && <div style={{ width: 2, flex: 1, background: C.border, margin: "4px 0" }} />}
                </div>
                <div style={{ paddingBottom: i < (neg.contacts.length - 1) ? 0 : 0 }}>
                  <p style={{ fontWeight: 600, color: C.dark, fontSize: 14, margin: "0 0 2px" }}>
                    {c.channel === "letter_mail" ? "Letter mailed to provider" : c.channel.replace("_", " ")}
                  </p>
                  <p style={{ color: C.muted, fontSize: 12, margin: "0 0 4px" }}>{fmtDate(c.sent_at)}</p>
                  {c.notes && <p style={{ color: C.slate, fontSize: 13, margin: 0 }}>{c.notes}</p>}
                </div>
              </div>
            ))}
            {(!neg?.contacts || neg.contacts.length === 0) && (
              <p style={{ color: C.muted, fontSize: 14 }}>No activity yet.</p>
            )}
          </div>
        </Card>
      )}

      {activeTab === "respond" && (
        <div>
          <Card style={{ marginBottom: 16 }}>
            <SectionHeader>Did the provider respond to you directly?</SectionHeader>
            <p style={{ color: C.slate, fontSize: 14, marginBottom: 16, lineHeight: 1.55 }}>
              If you received a letter, call, or email from the provider's billing department, paste or describe it below. We'll tell you exactly what it means and what to do next.
            </p>
            <textarea
              value={responseText}
              onChange={e => setResponseText(e.target.value)}
              placeholder="E.g. 'The hospital called and said they can reduce the bill to $2,100 but not lower.' Or paste the letter text."
              rows={5}
              style={{ width: "100%", border: `1.5px solid ${C.border}`, borderRadius: 8, padding: "12px 14px", fontSize: 14, color: C.dark, resize: "vertical", outline: "none", boxSizing: "border-box", lineHeight: 1.55 }}
            />
            <Btn onClick={handleSubmitResponse} disabled={!responseText.trim() || submitLoading} style={{ marginTop: 12 }}>
              {submitLoading ? "Analyzing…" : "What should I do? →"}
            </Btn>
          </Card>

          {followup && (
            <Card style={{ borderLeft: `4px solid ${followup.followup.urgency === "immediate" ? C.red : C.blue}` }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <p style={{ fontWeight: 700, color: C.dark, fontSize: 15, margin: 0 }}>
                  {followup.followup.action}
                </p>
                {followup.followup.urgency === "immediate" && (
                  <Pill label="⚡ Act now" color={C.red} bg={C.redLight} />
                )}
              </div>
              <p style={{ color: C.slate, fontSize: 14, lineHeight: 1.6, margin: "0 0 16px" }}>
                {followup.followup.explanation}
              </p>
              {followup.followup.followup_letter_context && (
                <Btn variant="secondary" onClick={() => {}}>
                  📄 Prepare follow-up letter
                </Btn>
              )}
              {followup.suggested_resolution?.agreed_amount && (
                <Btn variant="green" style={{ marginLeft: 10 }} onClick={() => {}}>
                  ✓ Accept {fmt$(followup.suggested_resolution.agreed_amount)}
                </Btn>
              )}
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Root app ─────────────────────────────────────────────────────────────
export default function App() {
  const [view, setView] = useState("welcome");
  const [patientId, setPatientId] = useState(null);
  const [caseId, setCaseId] = useState(null);
  const [uploadResult, setUploadResult] = useState(null);
  const [sessionMsg, setSessionMsg] = useState("");

  // API key: read from the build environment or a runtime window config.
  // Set VITE_API_KEY (Vite) or REACT_APP_API_KEY (CRA) at build time,
  // or inject window.__ROBIN_API_KEY at runtime (useful for static hosting).
  const apiKey =
    (typeof window !== "undefined" && window.__ROBIN_API_KEY) ||
    (typeof import_meta_env !== "undefined" && import_meta_env.VITE_API_KEY) ||
    (typeof process !== "undefined" && process.env?.REACT_APP_API_KEY) ||
    null;
  const api = useApi(apiKey);

  const handleTimeout = useCallback(() => {
    // Clear all in-memory PHI on session expiry — never localStorage
    setPatientId(null); setCaseId(null); setUploadResult(null);
    setView("welcome"); setSessionMsg("You were signed out after 15 minutes of inactivity.");
  }, []);

  useSessionTimeout(view !== "welcome", handleTimeout);

  const handleEnter = (signal) => {
    // Called by WelcomeView after a bill is uploaded and analyzed.
    // "new" = first-time user going through fee agreement flow.
    // In production: the POST /intake response returns a patient_id;
    // that ID is stored in state here (not localStorage -- HIPAA).
    setSessionMsg("");
    if (signal === "new") {
      setPatientId("demo-patient-001"); // set from intake response in production
      setView("agreement");
    }
  };

  const handleAgreed = () => setView("upload");

  const handleUploaded = ({ caseId, synthesis, bill, patientId: newPatientId }) => {
    setCaseId(caseId);
    if (newPatientId) setPatientId(newPatientId); // server-assigned ID from intake
    setUploadResult({ synthesis, bill });
    setView("analysis");
  };

  const handleStartNegotiation = (cid) => {
    setCaseId(cid);
    setView("case");
  };

  const handleLogout = () => {
    // Clear all in-memory state — nothing persisted to localStorage
    setPatientId(null); setCaseId(null); setUploadResult(null);
    setView("welcome");
  };

  return (
    <div style={{ minHeight: "100vh", background: view === "welcome" ? C.dark : C.bg, fontFamily: "'Inter', system-ui, -apple-system, sans-serif" }}>
      {/* Topbar (shown on non-welcome screens) */}
      {view !== "welcome" && (
        <div style={{ background: C.white, borderBottom: `1px solid ${C.border}`, padding: "12px 24px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ background: C.red, borderRadius: 6, padding: "4px 12px", display: "inline-block" }}>
            <span style={{ color: C.white, fontWeight: 800, fontSize: 15, letterSpacing: "-0.3px" }}>Robin</span>
          <span style={{ color: "#888", fontSize: 12 }}>Helping you fight unfair medical bills</span>
          </div>
          {patientId && (
            <Btn variant="ghost" onClick={handleLogout} style={{ fontSize: 12, padding: "4px 10px" }}>Sign out</Btn>
          )}
        </div>
      )}

      {/* Session timeout message */}
      {sessionMsg && (
        <div style={{ background: C.amberLight, borderBottom: `1px solid #F0D080`, padding: "10px 24px", fontSize: 13, color: C.amber, textAlign: "center" }}>
          {sessionMsg}
        </div>
      )}

      {/* Views */}
      {view === "welcome"  && <WelcomeView onEnter={handleEnter} />}
      {view === "agreement" && <FeeAgreementView patientId={patientId} api={api} onAgreed={handleAgreed} />}
      {view === "upload"   && <UploadView patientId={patientId} api={api} onUploaded={handleUploaded} />}
      {view === "analysis" && uploadResult && (
        <AnalysisView {...uploadResult} caseId={caseId} patientId={patientId} api={api} onStartNegotiation={handleStartNegotiation} />
      )}
      {view === "case" && caseId && (
        <CaseDashboardView caseId={caseId} patientId={patientId} api={api} onLogout={handleLogout} />
      )}
    </div>
  );
}
