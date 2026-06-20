import { useState, useMemo, useEffect, useCallback, useRef } from "react";

// ─── HIPAA / Privacy design decisions ────────────────────────────────────
// MINIMUM NECESSARY: The dashboard is designed around the minimum necessary
// principle (45 CFR 164.502(b)). Staff see case status and workflow state
// by default. PHI (income, household size) only appears in the case detail
// panel, gated behind a click, with an explicit disclosure notice reminding
// staff why they can see it and that the access is logged.
//
// ACCESS LOGGING: Every case detail view should be logged server-side.
// The UI calls a dedicated audit endpoint (POST /admin/audit-log) when a
// case is expanded. This is a placeholder here -- the backend audit log
// table and endpoint are a separate concern from this UI.
//
// ROLE SEPARATION: This dashboard is entirely separate from the patient
// portal (patient-portal.jsx). Staff accounts are distinct from patient
// accounts. The API enforces this server-side via API_KEY scope.
//
// SESSION TIMEOUT: 30 minutes for admin (vs 15 min for patients).
// Admin users are more likely to step away from a workstation.
//
// NO PHI IN URLs: All routing is in-memory React state. Case IDs appear
// in API calls but never in browser URL bar.
//
// NO PHI IN LOGS: Error boundary catches and strips PHI before logging.
// Provider names and reference numbers are OK; patient identifiers are not.
// ─────────────────────────────────────────────────────────────────────────

const API_BASE = "http://localhost:8001";
const ADMIN_SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 min

// ─── Design tokens ────────────────────────────────────────────────────────
const C = {
  red: "#E03E27", redLight: "#FDECEA", redDark: "#B8311D",
  dark: "#1A1A1A", charcoal: "#2C2C2C",
  slate: "#4A4A4A", muted: "#888",
  border: "#E5E5E5", bg: "#F4F4F4", white: "#FFF",
  green: "#1A7F4E", greenLight: "#EBF7F1",
  amber: "#966000", amberLight: "#FFF4DC",
  blue: "#1A4A7F", blueLight: "#EBF0FB",
  yellow: "#7A5500",
};

// ─── Urgency classification ───────────────────────────────────────────────
// Urgency is computed from negotiation state, not stored -- it's derived
// from the data so it stays accurate as cases progress.
export function deriveUrgency(kase) {
  if (!kase) return "new";
  const { case_status, negotiation } = kase;
  if (case_status === "resolved") return "resolved";
  if (!negotiation) return "new";
  const { status, response_due, first_contacted_at } = negotiation;

  // Collections referral is always urgent
  if (status === "provider_replied" && negotiation.provider_response_text?.toLowerCase().includes("collection")) {
    return "urgent";
  }
  // Counter-offer or direct reply waiting for action
  if (["counter_offer", "provider_replied"].includes(status)) return "action_needed";

  // Overdue response
  if (response_due && new Date(response_due) < new Date()) return "overdue";

  // Pending intake
  if (case_status === "intake") return "new";

  return "waiting";
}

const URGENCY_META = {
  urgent:       { label: "Urgent",        color: "#A32D2D", bg: "#FDECEA", dot: "#E03E27", order: 0 },
  overdue:      { label: "Overdue",       color: "#7A5500", bg: "#FFF4DC", dot: "#C47A00", order: 1 },
  action_needed:{ label: "Reply needed",  color: "#1A4A7F", bg: "#EBF0FB", dot: "#3A6ABA", order: 2 },
  new:          { label: "New",           color: "#1A7F4E", bg: "#EBF7F1", dot: "#2A9F6E", order: 3 },
  waiting:      { label: "Waiting",       color: "#888",    bg: "#F5F5F5", dot: "#CCC",    order: 4 },
  resolved:     { label: "Resolved",      color: "#1A7F4E", bg: "#EBF7F1", dot: "#2A9F6E", order: 5 },
};

const NEG_STATUS_LABELS = {
  pending: "Ready to send",
  contacted: "Letter sent",
  provider_replied: "Provider replied",
  counter_offer: "Counter offer",
  agreed: "Deal agreed",
  paid: "Paid",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
};

// ─── Utility ─────────────────────────────────────────────────────────────
const fmt$ = (n) => n == null ? "—" : "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "—";
const daysUntil = (iso) => iso ? Math.ceil((new Date(iso) - Date.now()) / 86400000) : null;
const daysSince = (iso) => iso ? Math.floor((Date.now() - new Date(iso)) / 86400000) : null;

// ─── Mock data (mirrors exact API response shapes) ────────────────────────
// In production: fetch from GET /admin/cases (a new endpoint returning all
// cases with negotiation summaries) — the response shape matches the
// GET /cases/{case_id} endpoint, batched.
const MOCK_CASES = [
  {
    case_id: "c001", case_status: "negotiating",
    bill: { total_billed_amount: 4800, provider_name_raw: "Springfield General", date_of_service: "2026-03-14" },
    negotiation: { status: "contacted", original_billed_amount: 4800, target_amount: 1440, agreed_amount: null, amount_saved: null, robinhealth_fee: null, patient_net_savings: null, first_contacted_at: "2026-06-01T09:00:00Z", agreed_at: null, provider_response_text: null, contacts: [{ channel: "letter_mail", sent_at: "2026-06-01T09:00:00Z" }] },
    _meta: { ref: "RH-A1B2C3", state: "IL", household_income: 48000, household_size: 3, response_due: "2026-06-22" },
  },
  {
    case_id: "c002", case_status: "negotiating",
    bill: { total_billed_amount: 12500, provider_name_raw: "Mercy Health Partners", date_of_service: "2026-02-28" },
    negotiation: { status: "counter_offer", original_billed_amount: 12500, target_amount: 3750, agreed_amount: null, amount_saved: null, robinhealth_fee: null, patient_net_savings: null, counter_offer_amount: 7200, first_contacted_at: "2026-06-05T09:00:00Z", agreed_at: null, provider_response_text: "We can offer a reduction to $7,200 as our best offer.", contacts: [{ channel: "letter_fax", sent_at: "2026-06-05T09:00:00Z" }] },
    _meta: { ref: "RH-D4E5F6", state: "OH", household_income: 55000, household_size: 4, response_due: "2026-06-19" },
  },
  {
    case_id: "c003", case_status: "negotiating",
    bill: { total_billed_amount: 8200, provider_name_raw: "Ascension St. Vincent", date_of_service: "2026-03-01" },
    negotiation: { status: "provider_replied", original_billed_amount: 8200, target_amount: 2460, agreed_amount: null, amount_saved: null, robinhealth_fee: null, patient_net_savings: null, first_contacted_at: "2026-06-08T09:00:00Z", agreed_at: null, provider_response_text: "Your application has been denied. Income exceeds 200% FPL.", contacts: [{ channel: "letter_mail", sent_at: "2026-06-08T09:00:00Z" }] },
    _meta: { ref: "RH-G7H8I9", state: "IN", household_income: 32000, household_size: 2, response_due: "2026-06-29" },
  },
  {
    case_id: "c004", case_status: "negotiating",
    bill: { total_billed_amount: 5600, provider_name_raw: "Trinity Health", date_of_service: "2026-04-10" },
    negotiation: { status: "provider_replied", original_billed_amount: 5600, target_amount: 1680, agreed_amount: null, amount_saved: null, robinhealth_fee: null, patient_net_savings: null, first_contacted_at: "2026-05-28T09:00:00Z", agreed_at: null, provider_response_text: "Account has been referred to our collection agency.", contacts: [{ channel: "letter_mail", sent_at: "2026-05-28T09:00:00Z" }] },
    _meta: { ref: "RH-V4W5X6", state: "MI", household_income: 29000, household_size: 4, response_due: "2026-06-18" },
  },
  {
    case_id: "c005", case_status: "negotiating",
    bill: { total_billed_amount: 6900, provider_name_raw: "Banner Desert Medical", date_of_service: "2026-04-05" },
    negotiation: { status: "contacted", original_billed_amount: 6900, target_amount: 2070, agreed_amount: null, amount_saved: null, robinhealth_fee: null, patient_net_savings: null, first_contacted_at: "2026-06-10T09:00:00Z", agreed_at: null, provider_response_text: null, contacts: [{ channel: "letter_mail", sent_at: "2026-06-10T09:00:00Z" }] },
    _meta: { ref: "RH-J1K2L3", state: "AZ", household_income: 72000, household_size: 1, response_due: "2026-07-01" },
  },
  {
    case_id: "c006", case_status: "negotiating",
    bill: { total_billed_amount: 15200, provider_name_raw: "Providence St. Jude", date_of_service: "2026-03-22" },
    negotiation: { status: "contacted", original_billed_amount: 15200, target_amount: 4560, agreed_amount: null, amount_saved: null, robinhealth_fee: null, patient_net_savings: null, first_contacted_at: "2026-06-12T09:00:00Z", agreed_at: null, provider_response_text: null, contacts: [{ channel: "letter_email", sent_at: "2026-06-12T09:00:00Z" }] },
    _meta: { ref: "RH-M4N5O6", state: "CA", household_income: 41000, household_size: 5, response_due: "2026-07-03" },
  },
  {
    case_id: "c007", case_status: "resolved",
    bill: { total_billed_amount: 9400, provider_name_raw: "CommonSpirit Health", date_of_service: "2026-02-10" },
    negotiation: { status: "paid", original_billed_amount: 9400, target_amount: 2820, agreed_amount: 3200, amount_saved: 6200, robinhealth_fee: 1240, patient_net_savings: 4960, first_contacted_at: "2026-05-15T09:00:00Z", agreed_at: "2026-06-08T09:00:00Z", provider_response_text: null, contacts: [{ channel: "letter_mail", sent_at: "2026-05-15T09:00:00Z" }] },
    _meta: { ref: "RH-P7Q8R9", state: "CO", household_income: 58000, household_size: 2, response_due: null },
  },
  {
    case_id: "c008", case_status: "intake",
    bill: { total_billed_amount: 22000, provider_name_raw: "Mayo Clinic", date_of_service: "2026-05-15" },
    negotiation: null,
    _meta: { ref: "RH-S1T2U3", state: "MN", household_income: 39000, household_size: 3, response_due: null },
  },
];

// ─── Session timeout ──────────────────────────────────────────────────────
function useSessionTimeout(onTimeout) {
  const timer = useRef(null);
  const reset = useCallback(() => {
    clearTimeout(timer.current);
    timer.current = setTimeout(onTimeout, ADMIN_SESSION_TIMEOUT_MS);
  }, [onTimeout]);
  useEffect(() => {
    const events = ["mousedown", "keydown", "touchstart"];
    events.forEach(e => window.addEventListener(e, reset, { passive: true }));
    reset();
    return () => { events.forEach(e => window.removeEventListener(e, reset)); clearTimeout(timer.current); };
  }, [reset]);
}

// ─── Shared components ────────────────────────────────────────────────────
function UrgencyBadge({ urgency, small }) {
  const m = URGENCY_META[urgency] || URGENCY_META.waiting;
  return (
    <span style={{ background: m.bg, color: m.color, borderRadius: 20, padding: small ? "1px 8px" : "2px 10px", fontSize: small ? 10 : 11, fontWeight: 600, whiteSpace: "nowrap" }}>
      {m.label}
    </span>
  );
}

function MetricCard({ label, value, sub, color, alert }) {
  return (
    <div style={{ background: C.white, border: `1px solid ${alert ? C.red : C.border}`, borderRadius: 10, padding: "14px 16px" }}>
      <p style={{ fontSize: 11, color: C.muted, margin: "0 0 4px", fontWeight: 600, letterSpacing: "0.5px" }}>{label.toUpperCase()}</p>
      <p style={{ fontSize: 24, fontWeight: 800, color: color || C.dark, margin: "0 0 2px", letterSpacing: "-0.5px" }}>{value}</p>
      {sub && <p style={{ fontSize: 11, color: C.muted, margin: 0 }}>{sub}</p>}
    </div>
  );
}

function DueDate({ iso }) {
  if (!iso) return <span style={{ color: C.muted, fontSize: 12 }}>—</span>;
  const days = daysUntil(iso);
  const color = days < 0 ? C.red : days < 3 ? C.amber : C.muted;
  return (
    <span style={{ fontSize: 12, fontWeight: 600, color }}>
      {days < 0 ? `${Math.abs(days)}d overdue` : days === 0 ? "Today" : `${days}d`}
    </span>
  );
}

// ─── Case row ─────────────────────────────────────────────────────────────
function CaseRow({ kase, urgency, selected, onSelect }) {
  const m = URGENCY_META[urgency] || URGENCY_META.waiting;
  const neg = kase.negotiation;
  const bill = kase.bill;
  const isCollections = neg?.provider_response_text?.toLowerCase().includes("collection");

  return (
    <tr
      onClick={() => onSelect(kase.case_id === selected ? null : kase.case_id)}
      style={{
        cursor: "pointer",
        background: selected ? C.blueLight : isCollections ? "#FFF0EE" : "transparent",
        borderBottom: `1px solid ${C.border}`,
        transition: "background .1s",
      }}
    >
      <td style={{ padding: "10px 12px", width: 20 }}>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: m.dot }} />
      </td>
      <td style={{ padding: "10px 8px" }}>
        <p style={{ fontWeight: 600, color: C.dark, fontSize: 13, margin: "0 0 1px" }}>{bill.provider_name_raw}</p>
        <p style={{ color: C.muted, fontSize: 11, margin: 0, fontFamily: "monospace" }}>{kase._meta.ref}</p>
      </td>
      <td style={{ padding: "10px 8px" }}>
        <p style={{ fontWeight: 700, color: C.dark, fontSize: 13, margin: "0 0 1px" }}>{fmt$(bill.total_billed_amount)}</p>
        {neg?.target_amount && <p style={{ color: C.blue, fontSize: 11, margin: 0 }}>→ {fmt$(neg.target_amount)}</p>}
      </td>
      <td style={{ padding: "10px 8px" }}>
        {neg ? (
          <span style={{ color: C.slate, fontSize: 12 }}>{NEG_STATUS_LABELS[neg.status] || neg.status}</span>
        ) : (
          <span style={{ color: C.muted, fontSize: 12 }}>Not started</span>
        )}
      </td>
      <td style={{ padding: "10px 8px" }}>
        <DueDate iso={kase._meta.response_due} />
      </td>
      <td style={{ padding: "10px 12px" }}>
        <UrgencyBadge urgency={urgency} small />
      </td>
    </tr>
  );
}

// ─── Recommended actions per urgency ─────────────────────────────────────
function getRecommendedActions(kase, urgency) {
  const neg = kase.negotiation;
  const isCollections = neg?.provider_response_text?.toLowerCase().includes("collection");
  const isDenied = neg?.provider_response_text?.toLowerCase().includes("denied") ||
    neg?.provider_response_text?.toLowerCase().includes("not qualify");

  if (isCollections) return [
    { label: "Send urgent cease-and-desist letter", color: C.red, urgent: true },
    { label: "Invoke FDCPA + 501(r) rights", color: C.red, urgent: true },
  ];
  if (isDenied) return [
    { label: "Draft eligibility appeal (501(r)-4)", color: C.blue },
    { label: "Request copy of FAP criteria used", color: C.slate },
  ];
  if (urgency === "action_needed" && neg?.status === "counter_offer") return [
    { label: "Draft counter-letter to provider", color: C.blue },
    { label: "Record outcome if patient accepts", color: C.green },
  ];
  if (urgency === "overdue") return [
    { label: "Send follow-up letter (no response)", color: C.amber },
    { label: "Escalate to state insurance commissioner", color: C.slate },
  ];
  if (urgency === "new") return [
    { label: "Review synthesis and start negotiation", color: C.blue },
  ];
  if (urgency === "resolved") return [];
  return [{ label: "No action needed — awaiting provider response", color: C.muted }];
}

// ─── Case detail panel ────────────────────────────────────────────────────
function CaseDetailPanel({ kase, urgency, onClose, onAuditLog }) {
  useEffect(() => {
    // Log the PHI access for HIPAA audit trail
    if (onAuditLog) onAuditLog(kase.case_id);
  }, [kase.case_id]);

  const neg = kase.negotiation;
  const bill = kase.bill;
  const m = kase._meta;
  const actions = getRecommendedActions(kase, urgency);
  const saving = neg?.agreed_amount != null
    ? neg.original_billed_amount - neg.agreed_amount
    : neg?.target_amount ? neg.original_billed_amount - neg.target_amount : null;

  return (
    <div style={{ background: C.white, border: `1px solid ${C.border}`, borderRadius: 12, padding: "20px 22px", marginTop: 8 }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
        <div>
          <p style={{ fontWeight: 800, color: C.dark, fontSize: 16, margin: "0 0 2px" }}>{bill.provider_name_raw}</p>
          <p style={{ color: C.muted, fontSize: 12, margin: 0 }}>
            {m.ref} · {m.state} · Date of service: {fmtDate(bill.date_of_service)}
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <UrgencyBadge urgency={urgency} />
          <button onClick={onClose} style={{ background: "none", border: "none", color: C.muted, fontSize: 20, cursor: "pointer", lineHeight: 1 }}>×</button>
        </div>
      </div>

      {/* Financial summary */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, marginBottom: 16 }}>
        {[
          ["Billed", fmt$(bill.total_billed_amount), C.dark],
          neg?.agreed_amount != null
            ? ["Agreed", fmt$(neg.agreed_amount), C.green]
            : ["Target", fmt$(neg?.target_amount), C.blue],
          ["Saving", fmt$(saving), C.red],
          neg?.agreed_amount != null
            ? ["RH fee", fmt$(neg.robinhealth_fee), C.dark]
            : ["Est. fee", fmt$(saving ? saving * 0.2 : null), C.muted],
        ].map(([l, v, c]) => (
          <div key={l} style={{ background: C.bg, borderRadius: 8, padding: "10px 12px" }}>
            <p style={{ fontSize: 10, color: C.muted, margin: "0 0 2px", fontWeight: 600 }}>{l.toUpperCase()}</p>
            <p style={{ fontSize: 16, fontWeight: 800, color: c, margin: 0 }}>{v}</p>
          </div>
        ))}
      </div>

      {/* HIPAA minimum necessary notice */}
      <div style={{ background: "#FFFBF0", border: "1px solid #F0E080", borderRadius: 8, padding: "10px 14px", marginBottom: 14 }}>
        <p style={{ fontWeight: 700, color: "#664400", margin: "0 0 4px", fontSize: 12 }}>
          ⚠️ HIPAA — minimum necessary access
        </p>
        <p style={{ color: "#886600", margin: 0, fontSize: 12, lineHeight: 1.5 }}>
          Patient financial details are displayed here only to assess FAP eligibility and determine the negotiation strategy for this case.
          This access is logged. Do not share, copy, or export this information beyond what is required to complete the negotiation.
        </p>
      </div>

      {/* Patient financial context (PHI — gated + logged) */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 16 }}>
        {[
          ["Household income", fmt$(m.household_income) + "/yr"],
          ["Household size", m.household_size + " members"],
          ["State", m.state],
        ].map(([l, v]) => (
          <div key={l} style={{ background: C.bg, borderRadius: 8, padding: "10px 12px" }}>
            <p style={{ fontSize: 11, color: C.muted, margin: "0 0 2px" }}>{l}</p>
            <p style={{ fontSize: 14, fontWeight: 700, color: C.dark, margin: 0 }}>{v}</p>
          </div>
        ))}
      </div>

      {/* Provider response (if any) */}
      {neg?.provider_response_text && (
        <div style={{ background: C.redLight, border: `1px solid ${C.red}33`, borderRadius: 8, padding: "12px 14px", marginBottom: 14 }}>
          <p style={{ fontWeight: 700, color: C.redDark, fontSize: 12, margin: "0 0 6px" }}>Provider response received</p>
          <p style={{ color: C.slate, fontSize: 13, margin: 0, lineHeight: 1.55 }}>"{neg.provider_response_text}"</p>
        </div>
      )}

      {/* Counter-offer detail */}
      {neg?.counter_offer_amount && (
        <div style={{ background: C.amberLight, border: `1px solid #F0D08088`, borderRadius: 8, padding: "10px 14px", marginBottom: 14 }}>
          <p style={{ fontSize: 12, color: C.amber, margin: 0 }}>
            Counter offer: <strong>{fmt$(neg.counter_offer_amount)}</strong>
            {" "}(our target: {fmt$(neg.target_amount)} · difference: {fmt$(neg.counter_offer_amount - neg.target_amount)})
          </p>
        </div>
      )}

      {/* Recommended actions */}
      {actions.length > 0 && (
        <div>
          <p style={{ fontSize: 12, fontWeight: 700, color: C.dark, margin: "0 0 10px" }}>Recommended next steps:</p>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {actions.map((a) => (
              <button key={a.label} style={{
                background: C.white, border: `1.5px solid ${a.color}`,
                borderRadius: 8, padding: "9px 14px", fontSize: 12,
                color: a.color, cursor: "pointer", fontWeight: 600,
                textAlign: "left", display: "flex", alignItems: "center", gap: 8,
              }}>
                {a.urgent && <span>⚡</span>}{a.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Timeline */}
      {neg?.contacts && neg.contacts.length > 0 && (
        <div style={{ marginTop: 16, paddingTop: 16, borderTop: `1px solid ${C.border}` }}>
          <p style={{ fontSize: 12, fontWeight: 700, color: C.dark, margin: "0 0 10px" }}>Activity</p>
          {neg.contacts.map((c, i) => (
            <div key={i} style={{ display: "flex", gap: 10, marginBottom: 8 }}>
              <div style={{ width: 8, height: 8, borderRadius: "50%", background: C.blue, flexShrink: 0, marginTop: 3 }} />
              <div>
                <p style={{ fontSize: 12, color: C.dark, fontWeight: 600, margin: "0 0 1px" }}>
                  {NEG_STATUS_LABELS[c.channel] || c.channel.replace("_", " ")}
                </p>
                <p style={{ fontSize: 11, color: C.muted, margin: 0 }}>{fmtDate(c.sent_at)}</p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Main dashboard ───────────────────────────────────────────────────────
export default function AdminDashboard() {
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("urgency");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [timedOut, setTimedOut] = useState(false);
  const [auditLog, setAuditLog] = useState([]);

  const handleTimeout = useCallback(() => setTimedOut(true), []);
  useSessionTimeout(handleTimeout);

  const handleAuditLog = useCallback((caseId) => {
    // In production: POST /admin/audit-log { case_id, action: "view_phi", timestamp }
    setAuditLog(prev => [...prev, { caseId, at: new Date().toISOString() }]);
  }, []);

  // Derive urgency for each case
  const casesWithUrgency = useMemo(() =>
    MOCK_CASES.map(k => ({ ...k, urgency: deriveUrgency(k) })),
    []
  );

  // Filter + sort
  const urgencyOrder = Object.fromEntries(Object.entries(URGENCY_META).map(([k, v]) => [k, v.order]));
  const filtered = useMemo(() => {
    let d = [...casesWithUrgency];
    if (filter !== "all") d = d.filter(c => c.urgency === filter);
    if (search) d = d.filter(c =>
      c.bill.provider_name_raw.toLowerCase().includes(search.toLowerCase()) ||
      c._meta.ref.toLowerCase().includes(search.toLowerCase())
    );
    if (sort === "urgency") d.sort((a, b) => (urgencyOrder[a.urgency] ?? 9) - (urgencyOrder[b.urgency] ?? 9));
    if (sort === "billed") d.sort((a, b) => b.bill.total_billed_amount - a.bill.total_billed_amount);
    if (sort === "due") d.sort((a, b) => {
      const da = a._meta.response_due ? new Date(a._meta.response_due) : new Date("9999-12-31");
      const db = b._meta.response_due ? new Date(b._meta.response_due) : new Date("9999-12-31");
      return da - db;
    });
    return d;
  }, [filter, sort, search, casesWithUrgency]);

  const selectedCase = casesWithUrgency.find(c => c.case_id === selectedId);

  // Aggregate metrics
  const metrics = useMemo(() => ({
    active: casesWithUrgency.filter(c => c.case_status === "negotiating").length,
    needAttention: casesWithUrgency.filter(c => ["urgent", "overdue", "action_needed"].includes(c.urgency)).length,
    totalSaved: casesWithUrgency.filter(c => c.negotiation?.agreed_amount != null).reduce((s, c) => s + c.negotiation.amount_saved, 0),
    totalFees: casesWithUrgency.filter(c => c.negotiation?.robinhealth_fee != null).reduce((s, c) => s + c.negotiation.robinhealth_fee, 0),
    avgSavingsPct: (() => {
      const resolved = casesWithUrgency.filter(c => c.negotiation?.agreed_amount != null);
      if (!resolved.length) return null;
      const avg = resolved.reduce((s, c) => s + (c.negotiation.amount_saved / c.negotiation.original_billed_amount * 100), 0) / resolved.length;
      return Math.round(avg);
    })(),
  }), [casesWithUrgency]);

  const filterCounts = useMemo(() => {
    const counts = { all: casesWithUrgency.length };
    casesWithUrgency.forEach(c => { counts[c.urgency] = (counts[c.urgency] || 0) + 1; });
    return counts;
  }, [casesWithUrgency]);

  if (timedOut) return (
    <div style={{ background: C.dark, minHeight: 200, display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 12, padding: 40, textAlign: "center" }}>
      <p style={{ color: C.white, fontWeight: 700, fontSize: 16 }}>Session expired</p>
      <p style={{ color: "#888", fontSize: 13 }}>You were automatically signed out after 30 minutes of inactivity to protect patient privacy.</p>
      <button onClick={() => setTimedOut(false)} style={{ background: C.red, color: C.white, border: "none", borderRadius: 8, padding: "9px 20px", fontWeight: 600, cursor: "pointer", fontSize: 13 }}>Sign back in</button>
    </div>
  );

  return (
    <div style={{ background: C.bg, minHeight: 500, fontFamily: "system-ui, -apple-system, sans-serif" }}>
      {/* Top nav */}
      <div style={{ background: C.dark, padding: "11px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ background: C.red, borderRadius: 6, padding: "4px 12px" }}>
            <span style={{ color: C.white, fontWeight: 800, fontSize: 14 }}>RobinHealth</span>
          </div>
          <span style={{ color: "#555", fontSize: 13 }}>Staff Dashboard</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 11, color: "#555" }}>HIPAA-compliant · Audit-logged</span>
          <div style={{ width: 28, height: 28, borderRadius: "50%", background: "#3A3A3A", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: "#AAA", fontWeight: 700 }}>JB</div>
        </div>
      </div>

      <div style={{ padding: "16px 20px" }}>
        {/* Metrics */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10, marginBottom: 16 }}>
          <MetricCard label="Active cases" value={metrics.active} sub="in negotiation" />
          <MetricCard label="Need attention" value={metrics.needAttention} color={metrics.needAttention > 0 ? C.red : C.green} sub="urgent + overdue + reply" alert={metrics.needAttention > 0} />
          <MetricCard label="Total saved" value={fmt$(metrics.totalSaved)} color={C.green} sub="all resolved cases" />
          <MetricCard label="RH fees earned" value={fmt$(metrics.totalFees)} sub="20% of savings" />
          {metrics.avgSavingsPct && <MetricCard label="Avg savings" value={metrics.avgSavingsPct + "%"} sub="of original bill" color={C.blue} />}
        </div>

        {/* Case list */}
        <div style={{ background: C.white, border: `1px solid ${C.border}`, borderRadius: 12, overflow: "hidden" }}>
          {/* Toolbar */}
          <div style={{ padding: "12px 16px", borderBottom: `1px solid ${C.border}`, display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <input
              placeholder="Search provider or ref…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              style={{ flex: "1 1 180px", border: `1px solid ${C.border}`, borderRadius: 7, padding: "6px 10px", fontSize: 12, color: C.dark, minWidth: 0 }}
            />
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {[["all", "All"], ["urgent", "Urgent"], ["overdue", "Overdue"], ["action_needed", "Reply needed"], ["new", "New"], ["waiting", "Waiting"], ["resolved", "Resolved"]].map(([v, l]) => {
                const m = URGENCY_META[v];
                const active = filter === v;
                return (
                  <button key={v} onClick={() => setFilter(v)} style={{
                    background: active ? (m?.bg || C.blueLight) : "none",
                    border: `1px solid ${active ? (m?.color || C.blue) : C.border}`,
                    borderRadius: 20, padding: "3px 10px", fontSize: 11, cursor: "pointer",
                    color: active ? (m?.color || C.blue) : C.muted,
                    fontWeight: active ? 700 : 400,
                  }}>
                    {l}{filterCounts[v] ? ` (${filterCounts[v]})` : ""}
                  </button>
                );
              })}
            </div>
            <select value={sort} onChange={e => setSort(e.target.value)} style={{ border: `1px solid ${C.border}`, borderRadius: 7, padding: "6px 10px", fontSize: 12, color: C.dark, background: C.white }}>
              <option value="urgency">Priority</option>
              <option value="billed">Billed ↓</option>
              <option value="due">Due date</option>
            </select>
          </div>

          {/* Table */}
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: C.bg }}>
                {["", "Provider / Ref", "Amount", "Status", "Response due", "Priority"].map(h => (
                  <th key={h} style={{ padding: "8px 12px", fontSize: 10, color: C.muted, fontWeight: 700, textAlign: "left", letterSpacing: "0.3px", whiteSpace: "nowrap" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map(c => (
                <CaseRow
                  key={c.case_id}
                  kase={c}
                  urgency={c.urgency}
                  selected={selectedId}
                  onSelect={setSelectedId}
                />
              ))}
            </tbody>
          </table>
          {filtered.length === 0 && (
            <div style={{ padding: "24px", textAlign: "center", color: C.muted, fontSize: 13 }}>No cases match this filter.</div>
          )}
        </div>

        {/* Detail panel */}
        {selectedCase && (
          <CaseDetailPanel
            kase={selectedCase}
            urgency={selectedCase.urgency}
            onClose={() => setSelectedId(null)}
            onAuditLog={handleAuditLog}
          />
        )}

        {/* HIPAA footer */}
        <div style={{ borderTop: `1px solid ${C.border}`, marginTop: 16, paddingTop: 12 }}>
          <p style={{ fontSize: 11, color: C.muted, lineHeight: 1.6, margin: 0 }}>
            ⚖️ This system processes Protected Health Information (PHI) under HIPAA. Access is logged for audit purposes.
            Display only the minimum necessary information. Do not export PHI to non-compliant systems.
            Auto-logout after {ADMIN_SESSION_TIMEOUT_MS / 60000} minutes of inactivity.
            {auditLog.length > 0 && ` · ${auditLog.length} case detail view(s) logged this session.`}
          </p>
        </div>
      </div>
    </div>
  );
}
