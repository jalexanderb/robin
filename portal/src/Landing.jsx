import { useEffect } from "react";

// Marketing landing page that wraps the live chat (passed in as chatSlot).
// Calm, warm, high-contrast, plain-spoken — modeled on the patient-facing
// pattern from Goodbill / Dollar For.

const L = {
  bg: "#FBF9F6", surface: "#FFFFFF", ink: "#1A1A1A", slate: "#1A1A1A",
  muted: "#3A3733", line: "#E7E2DB", red: "#E03E27", redSoft: "#FDECEA",
  green: "#1A7F4E", greenSoft: "#EBF7F1", dark: "#211E1B",
};

function Cta({ onStart, children, variant = "solid" }) {
  const solid = variant === "solid";
  return (
    <button
      className="cta"
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

function Eyebrow({ children }) {
  return (
    <p style={{ textTransform: "uppercase", letterSpacing: "1.5px", fontSize: 12, fontWeight: 700, color: L.red, margin: "0 0 10px", textAlign: "center" }}>
      {children}
    </p>
  );
}

function Stat({ value, label, source }) {
  return (
    <div style={{ flex: "1 1 200px", minWidth: 170, textAlign: "center", padding: "10px 12px" }}>
      <p style={{ fontSize: 38, fontWeight: 800, color: "#fff", margin: "0 0 8px", letterSpacing: "-1px" }}>{value}</p>
      <p style={{ fontSize: 15, color: "#fff", opacity: 0.85, lineHeight: 1.45, margin: 0 }}>{label}</p>
      <p style={{ fontSize: 11, color: "#fff", opacity: 0.45, margin: "8px 0 0", textTransform: "uppercase", letterSpacing: "0.5px" }}>{source}</p>
    </div>
  );
}

function Step({ n, title, body }) {
  return (
    <div style={{ flex: "1 1 240px", minWidth: 240 }}>
      <div style={{ width: 36, height: 36, borderRadius: "50%", background: L.redSoft, color: L.red, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 12 }}>{n}</div>
      <h3 style={{ fontSize: 18, fontWeight: 700, color: L.ink, margin: "0 0 6px" }}>{title}</h3>
      <p style={{ fontSize: 15, color: L.slate, lineHeight: 1.6, margin: 0 }}>{body}</p>
    </div>
  );
}

function Capability({ icon, title, body }) {
  return (
    <div className="card" style={{ background: L.surface, border: `1px solid ${L.line}`, borderRadius: 12, padding: "20px" }}>
      <div aria-hidden="true" style={{ width: 40, height: 40, borderRadius: 10, background: L.redSoft, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 20, marginBottom: 12 }}>{icon}</div>
      <h3 style={{ fontSize: 16, fontWeight: 700, color: L.ink, margin: "0 0 6px" }}>{title}</h3>
      <p style={{ fontSize: 14.5, color: L.slate, lineHeight: 1.55, margin: 0 }}>{body}</p>
    </div>
  );
}

function Faq({ q, a }) {
  return (
    <details style={{ borderBottom: `1px solid ${L.line}`, padding: "14px 0" }}>
      <summary style={{ fontSize: 16, fontWeight: 600, color: L.ink, cursor: "pointer" }}>{q}</summary>
      <p style={{ fontSize: 15, color: L.slate, lineHeight: 1.65, margin: "8px 0 0" }}>{a}</p>
    </details>
  );
}

export default function Landing({ chatSlot }) {
  const wrap = { maxWidth: 1080, margin: "0 auto", padding: "0 20px" };
  const scrollToChat = () =>
    document.getElementById("robin-chat")?.scrollIntoView({ behavior: "smooth", block: "center" });
  const goHome = () => window.scrollTo({ top: 0, behavior: "smooth" });

  // Reveal-on-scroll (respects reduced-motion / no IntersectionObserver).
  useEffect(() => {
    const els = document.querySelectorAll(".reveal");
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !("IntersectionObserver" in window)) {
      els.forEach((el) => el.classList.add("is-visible"));
      return;
    }
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => {
        if (e.isIntersecting) { e.target.classList.add("is-visible"); io.unobserve(e.target); }
      }),
      { threshold: 0.12 }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  return (
    <div style={{ background: L.bg, color: L.ink, fontFamily: "system-ui, -apple-system, sans-serif", minHeight: "100vh" }}>
      <style>{`
        .cta { transition: filter .15s ease, transform .1s ease; }
        .cta:hover { filter: brightness(1.06); }
        .cta:active { transform: scale(0.985); }
        .card { transition: transform .18s ease, box-shadow .18s ease; }
        .card:hover { transform: translateY(-2px); box-shadow: 0 12px 30px rgba(0,0,0,0.08); }
        .reveal { opacity: 0; transform: translateY(14px); transition: opacity .5s ease, transform .5s ease; }
        .reveal.is-visible { opacity: 1; transform: none; }
        a:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid ${L.red}; outline-offset: 3px; border-radius: 6px; }
        @media (max-width: 640px) { .nav-links { display: none !important; } }
        @media (prefers-reduced-motion: reduce) {
          .reveal { opacity: 1; transform: none; transition: none; }
          .card, .cta { transition: none; }
        }
      `}</style>

      {/* Header */}
      <header style={{ borderBottom: `1px solid ${L.line}`, background: L.bg, position: "sticky", top: 0, zIndex: 10 }}>
        <div style={{ ...wrap, display: "flex", alignItems: "center", gap: 12, height: 64 }}>
          <button onClick={goHome} aria-label="Robin Health — home" style={{ display: "flex", alignItems: "center", gap: 12, background: "none", border: "none", padding: 0, cursor: "pointer" }}>
            <div aria-hidden="true" style={{ width: 30, height: 30, borderRadius: "50%", background: L.red, color: "#fff", fontWeight: 800, display: "flex", alignItems: "center", justifyContent: "center" }}>R</div>
            <span style={{ fontWeight: 700, fontSize: 17, color: L.ink }}>Robin Health</span>
          </button>
          <nav style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 22 }}>
            <span className="nav-links" style={{ display: "flex", gap: 22 }}>
              <a href="#how" style={{ color: L.slate, textDecoration: "none", fontSize: 14 }}>How it works</a>
              <a href="#pricing" style={{ color: L.slate, textDecoration: "none", fontSize: 14 }}>Pricing</a>
            </span>
            <button onClick={scrollToChat} style={{ background: L.red, color: "#fff", border: "none", borderRadius: 10, padding: "9px 16px", fontSize: 14, fontWeight: 600, cursor: "pointer" }}>Analyze my bill</button>
          </nav>
        </div>
      </header>

      {/* Hero: copy on the left, the live chat embedded on the right */}
      <section style={{ ...wrap, padding: "56px 20px 48px" }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 40, alignItems: "center", justifyContent: "center" }}>
          <div style={{ flex: "1 1 360px", minWidth: 300, maxWidth: 520 }}>
            <p style={{ display: "inline-block", background: L.redSoft, color: L.red, fontSize: 13, fontWeight: 600, padding: "5px 12px", borderRadius: 999, margin: "0 0 20px" }}>AI patient advocate · Beta</p>
            <h1 style={{ fontSize: "clamp(32px, 5vw, 46px)", lineHeight: 1.1, fontWeight: 800, color: L.ink, letterSpacing: "-0.5px", margin: "0 0 18px" }}>
              Fight back against confusing medical bills.
            </h1>
            <p style={{ fontSize: 19, color: L.slate, lineHeight: 1.6, margin: "0 0 28px" }}>
              Robin is your AI powered healthcare advocate. Share a bill and Robin finds the errors, overcharges, and patient-protection laws on your side — then drafts the letters to lower what you owe.
            </p>
            <Cta onStart={scrollToChat}>Analyze my bill — free to start</Cta>
            <p style={{ fontSize: 14, color: L.muted, margin: "16px 0 0" }}>
              Pricing: <strong>$50/month</strong> or <strong>20%</strong> of what we save.
            </p>
          </div>
          <div
            id="robin-chat"
            style={{ flex: "0 0 auto", width: 420, maxWidth: "100%", height: 620, maxHeight: "80vh", borderRadius: 18, overflow: "hidden", border: `1px solid ${L.line}`, boxShadow: "0 24px 60px rgba(0,0,0,0.18)" }}
          >
            {chatSlot}
          </div>
        </div>
      </section>

      {/* The problem — sourced stats */}
      <section className="reveal" style={{ background: L.dark }}>
        <div style={{ ...wrap, padding: "52px 20px" }}>
          <h2 style={{ color: "#fff", textAlign: "center", fontSize: 28, fontWeight: 800, margin: "0 0 8px" }}>The deck is stacked against patients</h2>
          <p style={{ color: "#fff", opacity: 0.6, textAlign: "center", fontSize: 15, margin: "0 0 32px" }}>You shouldn't have to fight a confusing, error-prone system alone.</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "center" }}>
            <Stat value="100M+" label="Americans carry medical debt" source="KFF" />
            <Stat value="$74B" label="borrowed to pay medical bills in a single year" source="Gallup, 2024" />
            <Stat value="45%" label="of insured adults were billed for care they thought was covered" source="Commonwealth Fund" />
            <Stat value="58%" label="fear a health emergency could put them in debt" source="Gallup" />
          </div>
        </div>
      </section>

      {/* How it works */}
      <section id="how" aria-labelledby="how-h" className="reveal" style={{ ...wrap, padding: "64px 20px" }}>
        <Eyebrow>How it works</Eyebrow>
        <h2 id="how-h" style={{ fontSize: 30, fontWeight: 800, color: L.ink, textAlign: "center", margin: "0 0 40px" }}>Three simple steps to save you money</h2>
        <div style={{ display: "flex", gap: 28, flexWrap: "wrap" }}>
          <Step n="1" title="Share your bill" body="Snap a photo or upload your bill (and your insurer's EOB, if you have one). It takes a couple of minutes in a simple chat." />
          <Step n="2" title="Robin investigates" body="Robin checks your charity-care eligibility, hunts for overcharges and coding errors, compares prices to fair benchmarks, and checks whether the provider followed laws like the No Surprises Act." />
          <Step n="3" title="Robin takes action" body="Robin drafts and sends the right letters — to your provider or your insurer — and tracks the response, telling you exactly what to do next." />
        </div>
        <div style={{ textAlign: "center", marginTop: 40 }}>
          <Cta onStart={scrollToChat}>Start with your bill</Cta>
        </div>
      </section>

      {/* What Robin can do */}
      <section className="reveal" style={{ background: L.surface, borderTop: `1px solid ${L.line}`, borderBottom: `1px solid ${L.line}` }}>
        <div style={{ ...wrap, padding: "64px 20px" }}>
          <Eyebrow>What Robin looks for</Eyebrow>
          <h2 style={{ fontSize: 30, fontWeight: 800, color: L.ink, textAlign: "center", margin: "0 0 36px" }}>Fighting back against an unfair system</h2>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 14 }}>
            <Capability icon="🤝" title="Charity care & financial assistance" body="Nonprofit hospitals must discount or forgive bills for patients who qualify. Robin checks if you're eligible and helps you apply." />
            <Capability icon="🔍" title="Billing errors & overcharges" body="Itemized bills routinely hide duplicate charges, services you never got, and coding mistakes. Robin flags them." />
            <Capability icon="📊" title="Fair-price benchmarking" body="Robin compares your charges to Medicare rates and the hospital's own published prices to show what's reasonable." />
            <Capability icon="🛡️" title="No Surprises Act protections" body="For emergencies and surprise out-of-network bills, federal law limits what you owe. Robin helps hold providers to it." />
            <Capability icon="📨" title="Insurance claim appeals" body="If your insurer denied or mishandled a claim, Robin drafts a formal appeal asserting your rights." />
            <Capability icon="🔁" title="Negotiation & follow-up" body="Robin keeps going — drafting follow-up letters and tracking your case until it's resolved." />
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" aria-labelledby="pricing-h" className="reveal" style={{ ...wrap, padding: "64px 20px" }}>
        <Eyebrow>Pricing</Eyebrow>
        <h2 id="pricing-h" style={{ fontSize: 30, fontWeight: 800, color: L.ink, textAlign: "center", margin: "0 0 12px" }}>Simple, fair pricing</h2>
        <p style={{ fontSize: 16, color: L.slate, textAlign: "center", maxWidth: 620, margin: "0 auto 36px", lineHeight: 1.6 }}>
          You choose — and you'll never pay more than whichever is lower for you. Analyzing your bill is always free.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
          <div className="card" style={{ background: L.surface, border: `1px solid ${L.line}`, borderRadius: 16, padding: "24px" }}>
            <h3 style={{ fontSize: 18, fontWeight: 700, color: L.ink, margin: "0 0 6px" }}>Pay-per-win</h3>
            <p style={{ fontSize: 32, fontWeight: 800, color: L.ink, margin: "0 0 4px" }}>20%<span style={{ fontSize: 16, fontWeight: 500, color: L.muted }}> of what we save you</span></p>
            <p style={{ fontSize: 14, color: L.muted, margin: "0 0 16px" }}>Capped at $1,000 per bill</p>
            <ul style={{ margin: 0, padding: 0, listStyle: "none", fontSize: 15, color: L.slate, lineHeight: 1.9 }}>
              <li>✓ $0 upfront</li>
              <li>✓ Nothing if we don't save you money</li>
              <li>✓ Best for a single bill</li>
            </ul>
          </div>
          <div className="card" style={{ background: L.surface, border: `2px solid ${L.red}`, borderRadius: 16, padding: "24px", position: "relative" }}>
            <span style={{ position: "absolute", top: -12, left: 24, background: L.red, color: "#fff", fontSize: 12, fontWeight: 600, padding: "4px 12px", borderRadius: 999 }}>Most popular</span>
            <h3 style={{ fontSize: 18, fontWeight: 700, color: L.ink, margin: "0 0 6px" }}>Membership</h3>
            <p style={{ fontSize: 32, fontWeight: 800, color: L.ink, margin: "0 0 4px" }}>$50<span style={{ fontSize: 16, fontWeight: 500, color: L.muted }}>/month</span></p>
            <p style={{ fontSize: 14, color: L.muted, margin: "0 0 16px" }}>We take 0% of your savings</p>
            <ul style={{ margin: 0, padding: 0, listStyle: "none", fontSize: 15, color: L.slate, lineHeight: 1.9 }}>
              <li>✓ Unlimited bills</li>
              <li>✓ Cancel anytime</li>
            </ul>
          </div>
        </div>
        <p style={{ textAlign: "center", fontSize: 14, color: L.muted, margin: "22px auto 0", maxWidth: 680, lineHeight: 1.6 }}>
          Example: on a $4,800 bill reduced to $1,440, pay-per-win would be $672 — or stay on the $50/month plan and pay $0 of your savings.
        </p>
        <div style={{ textAlign: "center", marginTop: 28 }}>
          <Cta onStart={scrollToChat}>Get my free analysis</Cta>
        </div>
      </section>

      {/* FAQ */}
      <section className="reveal" style={{ background: L.surface, borderTop: `1px solid ${L.line}` }}>
        <div style={{ maxWidth: 720, margin: "0 auto", padding: "64px 20px" }}>
          <Eyebrow>FAQ</Eyebrow>
          <h2 style={{ fontSize: 30, fontWeight: 800, color: L.ink, textAlign: "center", margin: "0 0 28px" }}>Questions, answered</h2>
          <Faq q="Do I need to have insurance?" a="No. Robin helps whether you're insured or paying out of pocket. If you have insurance, sharing your Explanation of Benefits lets Robin also check the claim was handled correctly." />
          <Faq q="Is my information private?" a="Your information is used only to analyze and advocate for your bill — nothing else — and you can ask us to delete it at any time. Nothing sensitive is stored in your browser; it clears when you close the tab." />
          <Faq q="Are you lawyers? Is this legal advice?" a="No. Robin is an AI-powered advocacy service, not a law firm, and nothing it produces is legal advice. Robin can help you with negotiating with vendors, and you review and approve everything before it's sent." />
          <Faq q="What does it actually cost?" a="Analyzing your bill is free. After that you choose: pay-per-win (20% of savings, capped at $1,000, and nothing if we save you nothing) or a $50/month membership where we take 0% of your savings. You'll never pay more than whichever is lower for you." />
          <Faq q="It says Beta — what does that mean?" a="Robin is new and improving. Our service is starting with estimates and drafting letters, but we are still in active testing and everything should be reviewed carefully before you decide to act. Our vision is to build a comprehensive suite of services to help the American people get the most from a broken health system while paying the least." />
        </div>
      </section>

      {/* Final CTA */}
      <section className="reveal" style={{ ...wrap, padding: "72px 20px", textAlign: "center" }}>
        <h2 style={{ fontSize: 32, fontWeight: 800, color: L.ink, margin: "0 0 14px" }}>Let's lower that bill.</h2>
        <p style={{ fontSize: 17, color: L.slate, margin: "0 0 28px" }}>It's free to find out what you could save.</p>
        <Cta onStart={scrollToChat}>Analyze my bill</Cta>
      </section>

      {/* Footer */}
      <footer style={{ borderTop: `1px solid ${L.line}`, background: L.bg }}>
        <div style={{ ...wrap, padding: "32px 20px" }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 20, justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div aria-hidden="true" style={{ width: 28, height: 28, borderRadius: "50%", background: L.red, color: "#fff", fontWeight: 800, fontSize: 13, display: "flex", alignItems: "center", justifyContent: "center" }}>R</div>
              <span style={{ fontWeight: 700, fontSize: 15, color: L.ink }}>Robin Health</span>
            </div>
            <nav style={{ display: "flex", flexWrap: "wrap", gap: 20, fontSize: 14 }}>
              <a href="#how" style={{ color: L.slate, textDecoration: "none" }}>How it works</a>
              <a href="#pricing" style={{ color: L.slate, textDecoration: "none" }}>Pricing</a>
              <a href="/privacy.html" style={{ color: L.slate, textDecoration: "none" }}>Privacy</a>
              <a href="/consumer-health-privacy.html" style={{ color: L.slate, textDecoration: "none" }}>Health Data Privacy</a>
              <a href="mailto:advocacy@robinhealth.com" style={{ color: L.slate, textDecoration: "none" }}>Contact</a>
            </nav>
          </div>
          <hr style={{ border: "none", borderTop: `1px solid ${L.line}`, margin: "0 0 16px" }} />
          <p style={{ fontSize: 13, color: L.muted, lineHeight: 1.7, margin: 0 }}>
            © {new Date().getFullYear()} Robin Health. Robin is an AI-enabled patient advocacy service and is in beta. It is not a law firm, and nothing here is legal, medical, or tax advice. Estimates are not guarantees — please review everything carefully before acting. Your information is used only to work on your bill.
          </p>
        </div>
      </footer>
    </div>
  );
}
