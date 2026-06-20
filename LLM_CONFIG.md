# LLM Configuration Guide

Robin uses language models for five tasks: bill extraction (vision),
EOB extraction (vision), FAP document parsing (text), negotiation letter
drafting (text), and provider response classification (text). One
`LLM_PROVIDER` / `LLM_MODEL` setting covers all five.

## Quick recommendation

**Starting out (zero cost, local):** Ollama + `qwen2.5vl:7b`  
**Scaling up (hosted, no GPU):** Fireworks AI + `qwen2p5-vl-32b-instruct`  
**Maximum quality:** Anthropic + `claude-sonnet-4-6`

---

## Option 1: Local Ollama — free

Best for local dev, privacy-sensitive environments, zero LLM costs.  
Needs 8GB+ VRAM or Apple Silicon (M1/M2/M3, 16GB+ unified memory).

```bash
curl https://ollama.com/install.sh | sh
ollama pull qwen2.5vl:7b
```

```env
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5vl:7b
```

For better quality locally: `ollama pull qwen2.5vl:32b` (needs 24GB+ VRAM).

---

## Option 2: Fireworks AI — hosted, cheap

~$0.20/M input tokens for Qwen2.5-VL-32B. A typical bill extraction
call uses ~2,000 tokens → ~$0.0004 per bill. Get a key at fireworks.ai.

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.fireworks.ai/inference/v1
LLM_API_KEY=fw_your_key_here
LLM_MODEL=accounts/fireworks/models/qwen2p5-vl-32b-instruct
```

---

## Option 3: Together AI

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.together.xyz/v1
LLM_API_KEY=your_together_key
LLM_MODEL=Qwen/Qwen2.5-VL-32B-Instruct
```

Similar pricing to Fireworks. Good fallback if Fireworks is unavailable.

---

## Option 4: OpenRouter — multi-model gateway

One API key, access to 100+ models. Adds ~10% markup over direct pricing.

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-your_key
LLM_MODEL=qwen/qwen2.5-vl-32b-instruct
```

---

## Option 5: Self-hosted vLLM — GPU server

Best for high volume or complete data control.

```bash
pip install vllm
vllm serve Qwen/Qwen2.5-VL-32B-Instruct --host 0.0.0.0 --port 8000
```

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://your-gpu-server:8000/v1
LLM_API_KEY=any_string
LLM_MODEL=Qwen/Qwen2.5-VL-32B-Instruct
```

Qwen2.5-VL-32B needs ~70GB VRAM (two A100 80GBs, or one H100).

---

## Option 6: Anthropic — premium quality

~$3/$15 per million tokens (in/out). ~15× more expensive than open-weight
hosted options, but noticeably better on complex or unusual bills.

```env
LLM_PROVIDER=anthropic
LLM_API_KEY=sk-ant-your_key
LLM_MODEL=claude-sonnet-4-6
```

---

## Cost comparison (June 2026, per bill extraction)

| Option | Model | Per bill | 1K bills/day/month |
|--------|-------|----------|--------------------|
| Ollama local | Qwen2.5-VL-7B | $0 | $0 |
| Fireworks | Qwen2.5-VL-32B | ~$0.0004 | ~$12 |
| Together | Qwen2.5-VL-32B | ~$0.0004 | ~$12 |
| vLLM (A100) | Qwen2.5-VL-32B | ~$0.0001 | ~$3 |
| Anthropic | claude-sonnet-4-6 | ~$0.006 | ~$180 |

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

- **Qwen2.5-VL**: Best open-weight model for medical document OCR and
  structured JSON extraction. Strong at following output schemas exactly.
- **Llama 4 Scout**: Excellent on text tasks; less consistent on vision/OCR.
- **DeepSeek V3.2**: Outstanding text quality, very cheap. No vision support
  — can't do `extract_bill` or `extract_eob`.
- **Kimi K2.5**: Strong reasoning, competitive on text. Vision support is
  newer and less tested for document extraction.

**Practical path:** Ollama + `qwen2.5vl:7b` for development. Fireworks +
`qwen2p5-vl-32b-instruct` for production quality without GPU management.
