# Deployment Guide

Two things need to run for Robin to work end-to-end:

1. **The Robin backend** — FastAPI + Postgres. Needs to be on a server
   reachable from the internet. Does NOT need a GPU.

2. **A language model** — handles bill extraction and letter drafting.
   Can be a hosted API (cheapest and easiest) or self-hosted (more control).

These are independent choices. You can mix and match freely.

---

## Do you need a powerful local device?

**No.** The Robin backend is lightweight Python — it runs fine on the
cheapest cloud servers available. The language model is the only
computationally intensive piece, and you don't have to run that yourself.

The options, from simplest to most complex:

```
Simplest: Hosted model API + cheap cloud server
          (Fireworks/Together for the model, Railway/Render for the backend)
          Cost: ~$20-30/month + ~$0.0004 per bill analyzed
          Setup time: ~30 minutes
          No GPU needed anywhere.

Middle:   Local Ollama on your laptop + cloud server for the backend
          (Free model, laptop stays on when processing bills)
          Cost: ~$20/month for the server
          Good for: early testing, low volume

Most control: Cloud GPU instance for the model + cloud server for backend
          (Everything in the cloud, full ownership)
          Cost: ~$100-200/month
          Good for: high volume, regulated environments
```

For a new product testing with real patients, **the simplest option
is the right one.** You can always switch later.

---

## Option A: Hosted model API (recommended to start)

You don't run the model at all. A company runs it for you and you pay
per bill analyzed. Robin's `llm_client.py` already supports this —
it's just a different URL in `.env`.

### Fireworks AI (recommended)

1. Go to **https://fireworks.ai** and create an account
2. Add a credit card (pay-as-you-go, no monthly commitment)
3. Generate an API key from your dashboard
4. Update `.env`:

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.fireworks.ai/inference/v1
LLM_API_KEY=fw_your_key_here
LLM_MODEL=accounts/fireworks/models/qwen2p5-vl-32b-instruct
```

**Cost:** ~$0.20 per million input tokens. A typical bill extraction
uses about 2,000 tokens → **$0.0004 per bill**. Processing 100 bills
costs about $0.04. You'd need to analyze 5,000 bills before spending $2.

### Together AI (alternative)

1. Go to **https://together.ai** and create an account
2. Get an API key — they offer a free $1 credit to start
3. Update `.env`:

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.together.xyz/v1
LLM_API_KEY=your_together_key
LLM_MODEL=Qwen/Qwen2.5-VL-32B-Instruct
```

Same model, similar pricing. Good backup if Fireworks is unavailable.

### OpenRouter (if you want to try multiple models)

OpenRouter routes your request to whichever provider is cheapest or
fastest at that moment. Useful for experimentation.

1. Go to **https://openrouter.ai** and create an account
2. Get an API key
3. Update `.env`:

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-your_key
LLM_MODEL=qwen/qwen2.5-vl-32b-instruct
```

---

## Hosting the Robin backend (FastAPI + Postgres)

The backend is a standard Python web app + Postgres database. Any
cloud platform that can run Docker containers works. No GPU needed.

### Railway (easiest — recommended to start)

Railway is the closest thing to "just deploy it and it works." Postgres
is a built-in service, Docker support is automatic, and deployment is
one command.

**Cost:** ~$5/month for a small instance + ~$5/month for Postgres
= **~$10/month** to start. Scales automatically.

**Steps:**

1. Install the Railway CLI:
   ```bash
   # Mac
   brew install railway

   # or via npm
   npm install -g @railway/cli
   ```

2. Login and create a project:
   ```bash
   railway login
   railway init
   ```

3. Add Postgres:
   ```bash
   railway add --plugin postgresql
   ```
   Railway gives you a `DATABASE_URL` automatically. Copy it.

4. Set your environment variables:
   ```bash
   railway vars set LLM_PROVIDER=openai_compatible
   railway vars set LLM_BASE_URL=https://api.fireworks.ai/inference/v1
   railway vars set LLM_API_KEY=fw_your_key
   railway vars set LLM_MODEL=accounts/fireworks/models/qwen2p5-vl-32b-instruct
   railway vars set API_KEY=$(openssl rand -hex 32)
   railway vars set CORS_ORIGINS=https://your-frontend-domain.com
   ```

5. Deploy:
   ```bash
   railway up
   ```

Railway reads the `Dockerfile` in your project root and builds the
container automatically. Your API will be live at a Railway-provided
URL like `https://robin-api-production.up.railway.app`.

6. Run the database migrations:
   ```bash
   railway run python pipeline/seed_health_systems.py
   ```

### Render (similar to Railway, slightly cheaper)

1. Go to **https://render.com** and create an account
2. Click "New → Web Service" → connect your GitHub repo
3. Render detects the `Dockerfile` automatically
4. Add environment variables in the Render dashboard
5. Add a Postgres database: "New → PostgreSQL"

**Cost:** ~$7/month for the web service + ~$7/month for Postgres
= **~$14/month**.

Render is a bit more manual than Railway but has a generous free tier
for experimentation (web service sleeps after inactivity on the free plan).

### AWS (if you already have an AWS account)

AWS is more complex than Railway or Render but you may already be paying
for it. Two reasonable options:

**ECS + RDS (production-grade):**
- RDS Postgres: `db.t4g.micro` → ~$15/month
- ECS Fargate (1 vCPU, 2GB RAM): ~$15/month
- Total: ~$30/month
- More setup but standard AWS patterns

**EC2 + managed Postgres:**
- EC2 `t4g.small` (2 vCPU, 2GB): ~$12/month
- RDS `db.t4g.micro`: ~$15/month
- Total: ~$27/month

For AWS, the simplest path is:
```bash
# Install the AWS CLI and configure credentials
aws configure

# Use the provided docker-compose.yml as a reference
# but deploy to ECS using AWS Copilot (simplifies ECS setup)
brew install aws/tap/copilot-cli
copilot init
```

AWS is worth it if you need HIPAA Business Associate Agreements (BAAs)
for handling PHI in production at scale — AWS offers these. Railway
and Render also offer BAAs but on higher-tier plans (~$500/month+).

**For early product development, Railway or Render is fine.** Get a
BAA conversation going with your legal team before you onboard real
patients at any scale.

---

## Putting it together: full stack in 30 minutes

The fastest path to a working Robin deployment:

### What you need
- A Fireworks AI account (free, takes 2 minutes)
- A Railway account (free tier available)
- Your Robin code (this repository)

### Step 1: Get a Fireworks API key (5 minutes)
1. Go to https://fireworks.ai → Sign up
2. Dashboard → API Keys → Create new key
3. Copy the key — you'll use it in step 3

### Step 2: Deploy to Railway (15 minutes)
```bash
# Install Railway CLI
npm install -g @railway/cli

# From your Robin project directory
railway login
railway init --name robin-api

# Add Postgres
railway add --plugin postgresql

# Set environment variables
railway vars set \
  LLM_PROVIDER=openai_compatible \
  LLM_BASE_URL=https://api.fireworks.ai/inference/v1 \
  LLM_API_KEY=fw_your_key_from_step_1 \
  LLM_MODEL=accounts/fireworks/models/qwen2p5-vl-32b-instruct \
  API_KEY=$(openssl rand -hex 32) \
  CORS_ORIGINS="*" \
  LOG_LEVEL=INFO

# Deploy
railway up
```

Railway will show you a deployment URL like:
`https://robin-api-production.up.railway.app`

### Step 3: Seed the hospital data (2 minutes)
```bash
railway run python pipeline/seed_health_systems.py
```

### Step 4: Test it (2 minutes)
```bash
# Health check
curl https://your-railway-url.up.railway.app/health

# Should return: {"status": "ok", "database_reachable": true}
```

### Step 5: Point the portal at your backend (2 minutes)

In `portal/patient-portal.jsx`, the `API_BASE` is already set up to
read from an environment variable. If you're deploying the portal
to Vercel or Netlify, add:

```
VITE_API_BASE=https://your-railway-url.up.railway.app
```

Or for a quick test with the portal running locally:
```bash
VITE_API_BASE=https://your-railway-url.up.railway.app npm run dev
```

---

## What about AWS GPU instances for the model?

You can run the model on an AWS GPU instance, but it's almost never
worth it compared to a hosted API at early stage:

| | Hosted API (Fireworks) | AWS GPU (g5.xlarge) |
|--|------------------------|---------------------|
| Cost | ~$0.0004/bill | ~$1.20/hr = ~$876/month |
| Setup | 5 minutes | 2-4 hours |
| Maintenance | None | OS updates, CUDA drivers, model updates |
| Scales to zero | Yes | No (you pay 24/7) |
| Break-even volume | — | ~2M bills/month |

An AWS `g5.xlarge` (1× NVIDIA A10G GPU) costs about $1.20/hour
on-demand, or ~$0.50/hr reserved. At $0.0004 per bill, you'd need to
process about 3,000 bills per hour continuously — 24/7 — for the GPU
instance to be cheaper than Fireworks. That's a very large operation.

**Use a hosted API until you're processing tens of thousands of bills
per month. Switch to self-hosted GPU then.**

---

## Cost summary

| Component | Cheapest option | Cost/month |
|-----------|----------------|-----------|
| Language model | Fireworks AI | ~$0-5 at low volume |
| Backend server | Railway | ~$10 |
| Database | Railway Postgres | included |
| Email delivery | AWS SES | ~$0 (first 62K emails free) |
| Physical mail | Lob | ~$0.75/letter |
| **Total (low volume)** | | **~$10-15/month** |

At 1,000 bills/month analyzed, the model cost is about $0.40 — genuinely
negligible. The $10-15/month is almost entirely the server.

---

## HIPAA considerations before going live with real patients

If you're handling real patient bills, you're handling Protected Health
Information (PHI) under HIPAA. The technical stack is HIPAA-compatible
(the code is designed for it), but you need Business Associate Agreements
(BAAs) with your vendors:

- **Railway/Render:** BAAs available on Business plan (~$20-500/month)
- **AWS:** BAAs available, standard process
- **Fireworks/Together AI:** Check their current BAA offerings — this
  space is evolving. As of mid-2026, some hosted model APIs offer BAAs
  on enterprise tiers.
- **As an alternative to a hosted model API:** Running the model locally
  (Ollama on your own server) means no third party ever sees the bill
  data — the model processes it in memory and the weights are local.
  This is the cleanest HIPAA posture for the model layer.

For early beta with a small number of consenting patients who understand
the product is in development, you can move fast. For a public launch,
get the BAA conversation done first.

---

## Quick decision guide

**"I want to test Robin with a few real bills right now"**
→ Fireworks API key + Railway deployment. 30 minutes, ~$10/month.

**"I don't have a GPU but want everything self-hosted"**
→ Railway for the backend (no GPU needed) + Fireworks for the model.
  Same setup, model data goes to Fireworks' servers.

**"I want zero data leaving my infrastructure"**
→ Railway (or AWS) for the backend + a VPS with an NVIDIA GPU for Ollama/vLLM.
  DigitalOcean, Lambda Labs, and Vast.ai all have GPU VPS options.
  The cheapest: a Lambda Labs `gpu_1x_a10g` at ~$0.75/hr (run it on-demand,
  not 24/7, if volume is low).

**"I want to keep costs at zero for now"**
→ Run the backend locally with `docker compose up -d`. Use `ngrok` to
  expose it temporarily: `ngrok http 8001`. Use Ollama locally for the model.
  Not suitable for real patients but fine for testing the full flow.
