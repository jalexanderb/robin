# LLM Configuration Guide

Robin uses language models for five tasks: bill extraction (vision),
EOB extraction (vision), FAP document parsing (text), negotiation letter
drafting (text), and provider response classification (text) — plus the
patient-facing chat (text). One `LLM_PROVIDER` / `LLM_MODEL` setting covers
all of them.

**Robin uses Anthropic Claude by default.** No `LLM_PROVIDER` needed — just
set an API key. Claude is materially more accurate on messy real-world bills,
and on the `anthropic` path `complete_json` uses a forced tool call, so
structured extraction is **guaranteed-valid JSON** — none of the regex /
think-block / fence scraping the open-weight path needs. Given Robin's
business model (20% of what it saves a patient), the higher per-bill cost is
easily justified by avoiding failed or wrong extractions.

## Quick recommendation

**Default (recommended):** Anthropic + `claude-opus-4-8` — set
`ANTHROPIC_API_KEY` and you're done.
**Cost-sensitive at volume:** Anthropic + `claude-sonnet-4-6` — a one-line
change, ~half the cost, still far ahead of open-weight on quality.
**Zero cost / fully local or air-gapped:** Ollama + `qwen3vl:7b` (open-weight).

---

## Option 1: Anthropic Claude — default, premium quality

The out-of-the-box provider. Best quality on complex or unusual bills, and the
only path with guaranteed-valid structured output (forced tool use). Reach
`api.anthropic.com` directly — no GPU, no model hosting.

```env
# LLM_PROVIDER defaults to "anthropic" -- you can omit it
ANTHROPIC_API_KEY=sk-ant-your_key
LLM_MODEL=claude-opus-4-8
```

`ANTHROPIC_API_KEY` is read automatically (no scaffold-specific var needed).
You can also set `LLM_API_KEY` if you prefer.

**Cut cost at volume** by switching the model — nothing else changes:

```env
LLM_MODEL=claude-sonnet-4-6   # ~half the per-bill cost of Opus 4.8
```

---

## Open-weight alternatives

Use these when you want zero LLM cost, full data control, or an air-gapped
deployment. Set `LLM_PROVIDER=openai_compatible` for all of them. Note: on this
path Robin must coax and repair JSON out of the model's text (it has no
structured-output guarantee), so extraction is less reliable than on Claude —
especially for hybrid "reasoning" models that narrate instead of returning JSON.

### Option 2: Local Ollama — free

Best for local dev, privacy-sensitive environments, zero LLM cost.
Needs 8GB+ VRAM or Apple Silicon (M1/M2/M3, 16GB+ unified memory).

```bash
curl https://ollama.com/install.sh | sh
ollama pull qwen3vl:7b
```

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3vl:7b
```

For better quality locally: `ollama pull qwen3vl:32b` (needs 24GB+ VRAM).

### Option 3: Fireworks AI — hosted, cheap

~$0.20/M input tokens for Qwen3-VL-32B. A typical bill extraction call uses
~2,000 tokens → ~$0.0004 per bill. Get a key at fireworks.ai.

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.fireworks.ai/inference/v1
LLM_API_KEY=fw_your_key_here
LLM_MODEL=accounts/fireworks/models/qwen3-vl-32b-instruct
```

### Option 4: Together AI

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.together.xyz/v1
LLM_API_KEY=your_together_key
LLM_MODEL=Qwen/Qwen3-VL-32B-Instruct
```

Similar pricing to Fireworks. Good fallback if Fireworks is unavailable.

### Option 5: OpenRouter — multi-model gateway

One API key, access to 100+ models. Adds ~10% markup over direct pricing.

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-your_key
LLM_MODEL=qwen/qwen3-vl-32b-instruct
```

### Option 6: Self-hosted vLLM — GPU server

Best for high volume or complete data control.

```bash
pip install vllm
vllm serve Qwen/Qwen3-VL-32B-Instruct --host 0.0.0.0 --port 8000
```

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://your-gpu-server:8000/v1
LLM_API_KEY=any_string
LLM_MODEL=Qwen/Qwen3-VL-32B-Instruct
```

Qwen3-VL-32B needs ~70GB VRAM (two A100 80GBs, or one H100).

---

## Cost comparison (June 2026, per bill extraction)

| Option | Model | Per bill | 1K bills/day/month |
|--------|-------|----------|--------------------|
| Anthropic (default) | claude-opus-4-8 | ~$0.010 | ~$300 |
| Anthropic (cost option) | claude-sonnet-4-6 | ~$0.006 | ~$180 |
| Fireworks | Qwen3-VL-32B | ~$0.0004 | ~$12 |
| Together | Qwen3-VL-32B | ~$0.0004 | ~$12 |
| vLLM (A100) | Qwen3-VL-32B | ~$0.0001 | ~$3 |
| Ollama local | Qwen3-VL-7B | $0 | $0 |

Open-weight is ~15–25× cheaper per token, but the reliability cost (failed and
wrong extractions, plus the brittle JSON-repair path) usually outweighs the
savings for a service that bills on results.

---

## Testing your config

```bash
cd pipeline
python3 -c "
import llm_client
result = llm_client.complete_json(
  'Return this JSON exactly: {\"status\": \"ok\"}'
)
print('LLM OK:', result)
"
```

`{'status': 'ok'}` means the LLM is configured correctly.

---

## Model notes (June 2026)

- **claude-opus-4-8** (default): most capable; best on complex/unusual bills,
  letter drafting, and patient Q&A. Structured extraction is guaranteed-valid
  JSON via forced tool use. Note: sampling params (`temperature`/`top_p`) are
  not used on this model — Robin steers via prompting.
- **claude-sonnet-4-6**: strong quality at roughly half the cost; the
  recommended switch when per-bill cost matters at volume.
- **Qwen3-VL**: best open-weight model for medical-document OCR and structured
  JSON extraction. Strong at following output schemas, but no hard guarantee —
  Robin repairs its text output on the `openai_compatible` path.
- **DeepSeek / Kimi / GLM (text)**: capable on text tasks, but vision support
  varies; a text-only model can't do `extract_bill` or `extract_eob`.

**Practical path:** start on the default (Anthropic + `claude-opus-4-8`). If
per-bill cost becomes the constraint at scale, switch `LLM_MODEL` to
`claude-sonnet-4-6`, or move to a hosted/self-hosted Qwen3-VL on the
`openai_compatible` path for the lowest cost.
