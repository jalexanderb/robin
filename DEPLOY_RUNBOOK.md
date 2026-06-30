# Deploy Runbook — this build

Concrete steps to get a live, testable instance of the current build (the
Claude-backed pipeline + the patient portal in `portal/src`). For the broader
"what are my options" overview, see [DEPLOYMENT.md](DEPLOYMENT.md).

Two pieces deploy independently:

1. **API** — FastAPI + Postgres (`pipeline/`), served by
   `uvicorn api:app --host 0.0.0.0 --port 8001` (see `Dockerfile`).
2. **Portal** — React/Vite static site (`portal/`).

---

## 1. Database

Provision a Postgres 16 instance and set `DATABASE_URL` on the API. Then apply
the schema — easiest first:

**Option A (recommended): let the API set itself up.** Set `AUTO_MIGRATE=true`
on the API service. On boot it applies the schema (idempotently) — no manual
step. You can leave it on or remove it after the first successful deploy.

**Option B: run the script once.**
```bash
cd pipeline && python migrate.py    # uses DATABASE_URL
```

**Option C: apply by hand**, in this order (`bills_schema.sql` references
tables created in `schema.sql`):
```bash
psql "$DATABASE_URL" -f db/schema.sql
psql "$DATABASE_URL" -f db/jobs_schema.sql
psql "$DATABASE_URL" -f db/bills_schema.sql
```

All paths are safe to re-run; the new columns this build adds
(`patients.plan`, `cases.synthesis_json`) come from `bills_schema.sql`.

---

## 2. API (Railway / Render / any Docker host)

Build from the repo root `Dockerfile`. Required environment variables:

| Variable | Value / notes |
|----------|---------------|
| `DATABASE_URL` | full Postgres DSN, e.g. `postgresql://user:pass@host:5432/db` |
| `AUTO_MIGRATE` | `true` to auto-apply the schema on boot (see step 1, option A) |
| `LLM_PROVIDER` | `anthropic` (the default if unset) |
| `ANTHROPIC_API_KEY` | your Anthropic key (default provider is Claude) |
| `LLM_MODEL` | `claude-opus-4-8` (default) — or `claude-sonnet-4-6` to cut cost |
| `API_KEY` | optional bearer token; if set, all requests must send it |
| `CORS_ORIGINS` | the portal's origin, e.g. `https://your-portal.vercel.app` |
| `RETENTION_DAYS` | retention window for the purge sweep (default 365); run `python retention.py` from cron |
| `STORAGE_BACKEND` | `local` (default) or `s3` — use `s3` in production for durable, encrypted blob storage |
| `S3_BUCKET` | required when `STORAGE_BACKEND=s3`: the private bucket name |
| `AWS_REGION` | bucket region, e.g. `us-east-1` |
| `S3_ENDPOINT_URL` | optional; set for non-AWS S3 (Cloudflare R2 / Backblaze B2 / MinIO) |
| `S3_SSE` | server-side encryption: `AES256` (default) or `aws:kms` |
| `S3_SSE_KMS_KEY_ID` | KMS key id/arn, when `S3_SSE=aws:kms` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | S3 credentials (or use an instance/role) |

For `s3`: create a **private** bucket (block all public access), enable
default encryption, scope an IAM user/policy to just that bucket, and
(optionally) add a lifecycle rule mirroring `RETENTION_DAYS`. Blobs are
content-addressed and written with server-side encryption; no app/schema/
frontend changes are needed to switch backends.

Open-weight alternative: set `LLM_PROVIDER=openai_compatible` + `LLM_BASE_URL`
+ `LLM_MODEL` instead of the Anthropic vars (see `LLM_CONFIG.md`).

Smoke test once deployed:

```bash
curl -s https://<api-host>/health         # {"status":"ok","database_reachable":true}
```

> The portal's old hardcoded host `robin-production-542a.up.railway.app`
> currently returns "Application not found" — that deployment is gone. Use a
> fresh host and wire the portal to it (below).

---

## 3. Portal (Vercel)

Root directory `portal/`, framework **Vite** (build `npm run build`, output
`dist/`). Environment variables:

| Variable | Value / notes |
|----------|---------------|
| `VITE_API_BASE` | the API URL from step 2, e.g. `https://<api-host>` |
| `VITE_API_KEY` | only if you set `API_KEY` on the API (sent as a bearer token) |

`src/App.jsx` reads `VITE_API_BASE` (falling back to the old Railway URL only
if unset), so setting it is what points the portal at your API.

> Reminder: a `VITE_*` value is **public** in the client bundle. `VITE_API_KEY`
> matches the API's shared-key model (gate the API + tight CORS); it is not
> per-user auth.

---

## 4. End-to-end smoke test

1. Open the portal URL.
2. Upload any bill image/PDF → answer the triage/income questions → confirm an
   analysis card appears (real data, not the demo mock).
3. Pick a plan → draft a letter → confirm the PDF opens via `GET /letters/...`.
4. Optional: paste a provider response and record an outcome to see the
   savings/fee receipt.

If the analysis card shows Springfield General Hospital / $4,800, the portal
fell back to **demo data** — meaning it couldn't reach the API (check
`VITE_API_BASE` and `CORS_ORIGINS`).

---

## 5. Local dev (full stack)

```bash
# API + Postgres via docker-compose (set the LLM vars first)
export ANTHROPIC_API_KEY=sk-ant-...  LLM_PROVIDER=anthropic  LLM_MODEL=claude-opus-4-8
docker compose up --build
docker compose exec -T postgres psql -U robin -d robin -f - < db/schema.sql
docker compose exec -T postgres psql -U robin -d robin -f - < db/jobs_schema.sql
docker compose exec -T postgres psql -U robin -d robin -f - < db/bills_schema.sql

# Portal
cd portal && npm install
echo 'VITE_API_BASE=http://localhost:8001' > .env.local
npm run dev
```
