// Marketing landing page that wraps the chat. Calm, warm, high-contrast, and
// plain-spoken — modeled on the patient-facing pattern used by Goodbill and
// Dollar For (benefit headline + one clear CTA, trust up high, 3-step "how it
// works", pricing, FAQ). The chat remains the primary interface: every CTA
// calls onStart() to open it.

const L = {
  bg: "#FBF9F6", surface: "#FFFFFF", ink: "#1A1A1A", slate: "#43403B",
  muted: "#6B6760", line: "#E7E2DB", red: "#E03E27", redSoft: "#FDECEA",
  green: "#1A7F4E", greenSoft: "#EBF7F1",
};

function Cta({ onStart, children, variant = "solid" }) {
  const solid = variant === "solid";
  return (
    <button
      onClick={onStart}
      style={{
        background: solid ? L.red : "transparent",
        color: solid ? "#fff" : L.red,
        border: solid ? "none" : `1.5px solid ${L.red}`,
        borderRadius: 12, padding: "14px 24px", fontSize: 16, fontWeight: 600,
        cursor: "pointer", lineHeight: 1.2,
      }}
    >
      {children}
    </button>
  );
}

function Step({ n, title, body }) {
  return (
    <div style={{ flex: "1 1 240px", minWidth: 240 }}>
      <div style={{ width: 34, height: 34, borderRadius: "50%", background: L.redSoft, color: L.red, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 12 }}>{n}</div>
      <h3 style={{ fontSize: 18, fontWeight: 700, color: L.ink, margin: "0 0 6px" }}>{title}</h3>
      <p style={{ fontSize: 15, color: L.slate, lineHeight: 1.6, margin: 0 }}>{body}</p>
    </div>
  );
}

function Capability({ title, body }) {
  return (
    <div style={{ background: L.surface, border: `1px solid ${L.line}`, borderRadius: 12, padding: "16px 18px" }}>
      <h3 style={{ fontSize: 15, fontWeight: 700, color: L.ink, margin: "0 0 4px" }}>{title}</h3>
      <p style={{ fontSize: 14, color: L.slate, lineHeight: 1.55, margin: 0 }}>{body}</p>
    </div>
  );
}

function Faq({ q, a }) {
  return (
    <details style={{ borderBottom: `1px solid ${L.line}`, padding: "14px 0" }}>
      <summary style={{ fontSize: 16, fontWeight: 600, color: L.ink, cursor: "pointer", listStyle: "none" }}>{q}</summary>
      <p style={{ fontSize: 15, color: L.slate, lineHeight: 1.65, margin: "8px 0 0" }}>{a}</p>
    </details>
  );
}

export default function Landing({ onStart }) {
  const wrap = { maxWidth: 960, margin: "0 auto", padding: "0 20px" };
  return (
    <div style={{ background: L.bg, color: L.ink, fontFamily: "system-ui, -apple-system, sans-serif", minHeight: "100vh" }}>
      {/* Header */}
      <header style={{ borderBottom: `1px solid ${L.line}`, background: L.bg, position: "sticky", top: 0, zIndex: 10 }}>
        <div style={{ ...wrap, display: "flex", alignItems: "center", gap: 12, height: 64 }}>
          <div aria-hidden="true" style={{ width: 30, height: 30, borderRadius: "50%", background: L.red, color: "#fff", fontWeight: 800, display: "flex", alignItems: "center", justifyContent: "center" }}>R</div>
          <span style={{ fontWeight: 700, fontSize: 17 }}>Robin Health</span>
          <nav style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 22 }}>
            <a href="#how" style={{ color: L.slate, textDecoration: "none", fontSize: 14 }}>How it works</a>
            <a href="#pricing" style={{ color: L.slate, textDecoration: "none", fontSize: 14 }}>Pricing</a>
            <button onClick={onStart} style={{ background: L.red, color: "#fff", border: "none", borderRadius: 10, padding: "9px 16px", fontSize: 14, fontWeight: 600, cursor: "pointer" }}>Analyze my bill</button>
          </nav>
        </div>
      </header>

      {/* Hero */}
      <section style={{ ...wrap, padding: "64px 20px 56px", textAlign: "center" }}>
        <p style={{ display: "inline-block", background: L.redSoft, color: L.red, fontSize: 13, fontWeight: 600, padding: "5px 12px", borderRadius: 999, margin: "0 0 20px" }}>AI patient advocate · Beta</p>
        <h1 style={{ fontSize: 44, lineHeight: 1.1, fontWeight: 800, letterSpacing: "-0.5px", margin: "0 0 18px" }}>
          Fight back against confusing medical bills.
        </h1>
        <p style={{ fontSize: 19, color: L.slate, lineHeight: 1.6, maxWidth: 640, margin: "0 auto 28px" }}>
          Robin is your AI health advocate. Share a bill and Robin finds the errors, overcharges, and patient-protection laws on your side — then drafts the letters to lower what you owe. No upfront cost.
        </p>
        <div style={{ display: "flex", gap: 12, justifyContent: "center", flexWrap: "wrap" }}>
          <Cta onStart={onStart}>Analyze my bill — free to start</Cta>
        </div>
        <p style={{ fontSize: 14, color: L.muted, margin: "16px 0 0" }}>
          You'll never pay more than <strong>$50/month</strong> or <strong>20% of what we save you</strong> — and nothing if we don't save you anything.
        </p>
      </section>

      {/* Trust bar */}
      <section style={{ background: L.surface, borderTop: `1px solid ${L.line}`, borderBottom: `1px solid ${L.line}` }}>
        <div style={{ ...wrap, display: "flex", flexWrap: "wrap", gap: 16, justifyContent: "space-between", padding: "18px 20px" }}>
          {[
            "HIPAA-secure & never sold",
            "No upfront cost",
            "We only get paid when you save",
            "Beta — you review everything",
          ].map(t => (
            <span key={t} style={{ fontSize: 14, color: L.slate, fontWeight: 500 }}>✓ {t}</span>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section id="how" aria-labelledby="how-h" style={{ ...wrap, padding: "64px 20px" }}>
        <h2 id="how-h" style={{ fontSize: 30, fontWeight: 800, textAlign: "center", margin: "0 0 40px" }}>How Robin works</h2>
        <div style={{ display: "flex", gap: 28, flexWrap: "wrap" }}>
          <Step n="1" title="Share your bill" body="Snap a photo or upload your bill (and your insurer's EOB, if you have one). It takes a couple of minutes in a simple chat." />
          <Step n="2" title="Robin investigates" body="Robin checks your charity-care eligibility, hunts for overcharges and coding errors, compares prices to fair benchmarks, and checks whether the provider followed laws like the No Surprises Act." />
          <Step n="3" title="Robin takes action" body="Robin drafts and sends the right letters — to your provider or your insurer — and tracks the response, telling you exactly what to do next." />
        </div>
        <div style={{ textAlign: "center", marginTop: 40 }}>
          <Cta onStart={onStart}>Start with your bill</Cta>
        </div>
      </section>

      {/* What Robin can do */}
      <section style={{ background: L.surface, borderTop: `1px solid ${L.line}`, borderBottom: `1px solid ${L.line}` }}>
        <div style={{ ...wrap, padding: "64px 20px" }}>
          <h2 style={{ fontSize: 30, fontWeight: 800, textAlign: "center", margin: "0 0 12px" }}>What Robin looks for</h2>
          <p style={{ fontSize: 16, color: L.slate, textAlign: "center", maxWidth: 620, margin: "0 auto 36px", lineHeight: 1.6 }}>
            Most bills have more than one angle worth fighting. Robin works every one that applies to you.
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 14 }}>
            <Capability title="Charity care & financial assistance" body="Nonprofit hospitals must discount or forgive bills for patients who qualify. Robin checks if you're eligible and applies for you." />
            <Capability title="Billing errors & overcharges" body="Itemized bills routinely hide duplicate charges, services you never got, and coding mistakes. Robin flags them." />
            <Capability title="Fair-price benchmarking" body="Robin compares your charges to Medicare rates and the hospital's own published prices to show what's reasonable." />
            <Capability title="No Surprises Act protections" body="For emergencies and surprise out-of-network bills, federal law limits what you owe. Robin holds providers to it." />
            <Capability title="Insurance claim appeals" body="If your insurer denied or mishandled a claim, Robin drafts a formal appeal asserting your rights." />
            <Capability title="Negotiation & follow-up" body="Robin keeps going — drafting follow-up letters and tracking your case until it's resolved." />
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" aria-labelledby="pricing-h" style={{ ...wrap, padding: "64px 20px" }}>
        <h2 id="pricing-h" style={{ fontSize: 30, fontWeight: 800, textAlign: "center", margin: "0 0 12px" }}>Simple, fair pricing</h2>
        <p style={{ fontSize: 16, color: L.slate, textAlign: "center", maxWidth: 620, margin: "0 auto 36px", lineHeight: 1.6 }}>
          You choose — and you'll never pay more than whichever is lower for you. Analyzing your bill is always free.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
          <div style={{ background: L.surface, border: `1px solid ${L.line}`, borderRadius: 16, padding: "24px" }}>
            <h3 style={{ fontSize: 18, fontWeight: 700, margin: "0 0 6px" }}>Pay-per-win</h3>
            <p style={{ fontSize: 32, fontWeight: 800, margin: "0 0 4px" }}>20%<span style={{ fontSize: 16, fontWeight: 500, color: L.muted }}> of what we save you</span></p>
            <p style={{ fontSize: 14, color: L.muted, margin: "0 0 16px" }}>Capped at $1,000 per bill</p>
            <ul style={{ margin: 0, padding: 0, listStyle: "none", fontSize: 15, color: L.slate, lineHeight: 1.9 }}>
              <li>✓ $0 upfront</li>
              <li>✓ Nothing if we don't save you money</li>
              <li>✓ Best for a single bill</li>
            </ul>
          </div>
          <div style={{ background: L.surface, border: `2px solid ${L.red}`, borderRadius: 16, padding: "24px", position: "relative" }}>
            <span style={{ position: "absolute", top: -12, left: 24, background: L.red, color: "#fff", fontSize: 12, fontWeight: 600, padding: "4px 12px", borderRadius: 999 }}>Best for ongoing or large bills</span>
            <h3 style={{ fontSize: 18, fontWeight: 700, margin: "0 0 6px" }}>Membership</h3>
            <p style={{ fontSize: 32, fontWeight: 800, margin: "0 0 4px" }}>$50<span style={{ fontSize: 16, fontWeight: 500, color: L.muted }}>/month</span></p>
            <p style={{ fontSize: 14, color: L.muted, margin: "0 0 16px" }}>We take 0% of your savings</p>
            <ul style={{ margin: 0, padding: 0, listStyle: "none", fontSize: 15, color: L.slate, lineHeight: 1.9 }}>
              <li>✓ Free until your first win</li>
              <li>✓ Unlimited bills</li>
              <li>✓ Cancel anytime</li>
            </ul>
          </div>
        </div>
        <div style={{ textAlign: "center", marginTop: 36 }}>
          <Cta onStart={onStart}>Get my free analysis</Cta>
        </div>
      </section>

      {/* FAQ */}
      <section style={{ background: L.surface, borderTop: `1px solid ${L.line}` }}>
        <div style={{ maxWidth: 720, margin: "0 auto", padding: "64px 20px" }}>
          <h2 style={{ fontSize: 30, fontWeight: 800, textAlign: "center", margin: "0 0 28px" }}>Questions, answered</h2>
          <Faq q="Do I need to have insurance?" a="No. Robin helps whether you're insured or paying out of pocket. If you have insurance, sharing your Explanation of Benefits lets Robin also check the claim was handled correctly." />
          <Faq q="Is my information private?" a="Yes. Your information is handled under HIPAA, encrypted, and never sold. Nothing sensitive is stored in your browser, and you can ask us to delete your data at any time." />
          <Faq q="Are you lawyers? Is this legal advice?" a="No. Robin is an AI-powered advocacy service, not a law firm, and nothing it produces is legal advice. Robin acts as your authorized representative, and you review and approve everything before it's sent." />
          <Faq q="What does it actually cost?" a="Analyzing your bill is free. After that you choose: pay-per-win (20% of savings, capped at $1,000, and nothing if we save you nothing) or a $50/month membership where we take 0% of your savings. You'll never pay more than whichever is lower for you." />
          <Faq q="It says Beta — what does that mean?" a="Robin is new and improving. Its estimates and drafted letters are a strong starting point, but you should review everything carefully before acting — and Robin will always remind you to." />
        </div>
      </section>

      {/* Final CTA */}
      <section style={{ ...wrap, padding: "72px 20px", textAlign: "center" }}>
        <h2 style={{ fontSize: 32, fontWeight: 800, margin: "0 0 14px" }}>Let's lower that bill.</h2>
        <p style={{ fontSize: 17, color: L.slate, margin: "0 0 28px" }}>It's free to find out what you could save.</p>
        <Cta onStart={onStart}>Analyze my bill</Cta>
      </section>

      {/* Footer */}
      <footer style={{ borderTop: `1px solid ${L.line}`, background: L.bg }}>
        <div style={{ ...wrap, padding: "28px 20px", fontSize: 13, color: L.muted, lineHeight: 1.7 }}>
          <p style={{ margin: "0 0 8px", fontWeight: 600, color: L.slate }}>Robin Health</p>
          <p style={{ margin: 0 }}>
            Robin Health is an AI-enabled patient advocacy service and is in beta. It is not a law firm, and nothing here is legal, medical, or tax advice. Estimates are not guarantees — please review everything carefully before acting. Information is handled under HIPAA and never sold.
          </p>
        </div>
      </footer>
    </div>
  );
}
