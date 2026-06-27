# Robin — Patient Portal

The patient-facing web app for **RobinHealth**, an AI-enabled medical-bill
advocacy service. Patients drop in a bill, Robin analyzes it, estimates what
they could save, answers their questions, and drafts a negotiation letter to
the provider.

Built with React + Vite. The chat UI lives in [`src/App.jsx`](src/App.jsx).

## Running locally

```bash
npm install
npm run dev      # dev server with HMR
npm run build    # production build to dist/
npm run preview  # serve the production build
npm run lint     # eslint
```

## Backend

The portal talks to the Robin API (the `pipeline/` service). The base URL is
set in [`src/App.jsx`](src/App.jsx) via `API_BASE`. Endpoints used:

- `POST /intake` — upload a bill; returns the structured analysis.
- `POST /chat` — free-form patient Q&A, answered by the configured LLM
  (Claude by default). The app sends the user's question plus a compact
  summary of their current case so Robin answers about *their* bill rather
  than in generalities. See `ROBIN_CHAT_SYSTEM` / the `/chat` handler in
  `pipeline/api.py`.

The LLM provider is configured server-side (`LLM_PROVIDER` / `LLM_MODEL`); see
[`../LLM_CONFIG.md`](../LLM_CONFIG.md). Out of the box the backend uses
Anthropic Claude.

## Privacy

No PHI is stored in `localStorage` — all state is in React memory and clears
on refresh. The session times out after 15 minutes of inactivity.

> Robin is in **beta**. Estimates and drafted letters should be reviewed
> carefully before acting on them.
