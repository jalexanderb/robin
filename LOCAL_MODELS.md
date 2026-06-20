# Running Open-Source Models Locally with Robin

You do **not** need HuggingFace's API or any cloud service to use open-source
models. The models themselves are free to download and run on your own
hardware. HuggingFace is just a model hosting site — like GitHub for code —
and you can download from it once and run locally forever with no API calls,
no account, and no per-token cost.

This guide covers the two local setups: **Ollama** (easiest, for development
and moderate workloads) and **vLLM** (for production throughput on a GPU
server). Ollama is where almost everyone should start.

---

## Which hardware do you have?

| Setup | Recommended path |
|-------|-----------------|
| Mac with Apple Silicon (M1/M2/M3/M4) | [Ollama on Mac](#1-ollama-on-mac-recommended-for-most-people) |
| Windows or Linux PC with a modern NVIDIA GPU (8GB+ VRAM) | [Ollama on Windows/Linux](#2-ollama-on-windowslinux) |
| Linux server with 24GB+ VRAM (A10G, 3090, 4090, etc.) | [Ollama large model](#running-the-32b-model-for-better-quality) |
| Cloud GPU instance or on-prem GPU cluster | [vLLM for production](#3-vllm-for-production-gpu-servers) |
| No dedicated GPU / low RAM | [Use a hosted API instead](LLM_CONFIG.md) |

The model Robin uses for bill extraction (`qwen2.5vl:7b`) needs about
**6 GB of RAM/VRAM** in practice. If your machine has 8GB or more, you
can run it. Apple Silicon is ideal because it shares RAM between CPU and
GPU, so a 16GB MacBook effectively has a 16GB "GPU".

---

## 1. Ollama on Mac (recommended for most people)

Ollama is a free app that manages downloading and running models locally.
It's the equivalent of Docker but for language models — one command to
pull a model, one command to run it.

### Step 1: Install Ollama

Go to **https://ollama.com** and download the Mac app. It installs like
any other app — drag to Applications. Once running, you'll see a llama
icon in your menu bar.

Alternatively, from Terminal:
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### Step 2: Pull the model

Open Terminal and run:
```bash
ollama pull qwen2.5vl:7b
```

This downloads the model from Ollama's servers (about 5 GB). It only
needs to happen once — the model is stored on your machine and reused
forever. You can see what you've downloaded with:
```bash
ollama list
```

### Step 3: Verify it's working

```bash
ollama run qwen2.5vl:7b "What is 2 + 2?"
```

You should see the model respond. Type `/bye` to exit the chat.

### Step 4: Configure Robin to use it

Edit your `.env` file:
```env
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5vl:7b
```

Ollama runs as a background service automatically when you open the app.
It listens on port 11434. Robin's `llm_client.py` speaks to it over
the standard OpenAI-compatible API that Ollama exposes on that port.

### Step 5: Test Robin's connection to the model

```bash
cd pipeline
python3 -c "
import llm_client
result = llm_client.complete_json(
    'Return this JSON exactly, no other text: {\"status\": \"ok\", \"model\": \"local\"}'
)
print('Connected:', result)
"
```

Expected output: `Connected: {'status': 'ok', 'model': 'local'}`

If you see a connection error, make sure the Ollama app is running
(check your menu bar for the llama icon).

---

## 2. Ollama on Windows/Linux

### Windows

Download the installer from **https://ollama.com/download/windows**.
Run it — it installs Ollama as a Windows service that starts automatically.

Then open PowerShell or Command Prompt:
```powershell
ollama pull qwen2.5vl:7b
ollama run qwen2.5vl:7b "test"
```

The rest of the setup is identical to Mac. Ollama uses your NVIDIA GPU
automatically if you have CUDA drivers installed (which you almost
certainly do if you have an NVIDIA GPU and play games or use ML tools).

To check if Ollama is using your GPU:
```powershell
ollama run qwen2.5vl:7b "hi"
# In a second terminal:
nvidia-smi
# Look for "ollama" in the process list
```

### Linux (Ubuntu/Debian)

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

This installs Ollama as a systemd service. It starts automatically and
survives reboots.

```bash
# Pull and test
ollama pull qwen2.5vl:7b
ollama run qwen2.5vl:7b "test"

# Check service status
systemctl status ollama
```

If you have an NVIDIA GPU, install CUDA drivers first:
```bash
# Ubuntu 22.04/24.04
sudo apt install nvidia-cuda-toolkit
# Then reboot, then install Ollama
```

Ollama detects CUDA automatically once drivers are installed.

### Linux without a GPU (CPU-only)

The model will still work but will be slow — roughly 1-3 tokens per
second on a modern CPU, versus 30-60+ on a GPU. For development and
testing this is fine; for handling real patient bills in production,
you'd want either a GPU or a hosted API (see `LLM_CONFIG.md`).

```bash
# Same install as above — Ollama falls back to CPU automatically
ollama pull qwen2.5vl:7b
```

---

## Running the 32B model for better quality

The 7B model works well for development. The 32B model is noticeably
better at extracting data from complex, multi-page, or poorly-scanned
bills — which matters in production.

```bash
ollama pull qwen2.5vl:32b
```

Hardware requirements:
- **Mac:** 32GB+ unified memory (M1 Pro/Max/Ultra, M2 Max/Ultra, M3 Max/Ultra, M4 Max)
- **NVIDIA GPU:** 24GB+ VRAM (RTX 3090, 3090 Ti, 4090, A5000, A6000)
- **Multiple GPUs:** Ollama supports multi-GPU automatically (e.g. two 3090s)

Update `.env` to use it:
```env
LLM_MODEL=qwen2.5vl:32b
```

No other changes needed — Robin's code doesn't care which model size
you're using.

---

## Using Robin with Docker Compose (recommended)

If you're using the provided `docker-compose.yml`, Ollama runs as a
container alongside the API. The API and Ollama talk to each other over
Docker's internal network.

```bash
# Start everything
docker compose up -d

# Pull the model into the Ollama container (once)
docker compose exec ollama ollama pull qwen2.5vl:7b

# Verify
docker compose exec ollama ollama list

# Watch logs
docker compose logs -f api
```

The model is stored in the `ollama_models` Docker volume, so it
persists across container restarts and doesn't need to be re-downloaded.

**For GPU passthrough in Docker (NVIDIA):**

Uncomment the `deploy: resources:` block in `docker-compose.yml`:
```yaml
ollama:
  image: ollama/ollama:latest
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
```

Then install the NVIDIA Container Toolkit:
```bash
# Ubuntu
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker
```

**For Apple Silicon with Docker:**

Ollama inside Docker on Apple Silicon can't access the GPU (Docker on
Mac runs in a Linux VM). Instead, run Ollama natively on your Mac and
point the Docker container at your host:

```env
# In .env
LLM_BASE_URL=http://host.docker.internal:11434/v1
```

Then remove or comment out the `ollama:` service from `docker-compose.yml`
entirely and just run the Ollama Mac app normally.

---

## 3. vLLM for production (GPU servers)

Ollama is optimized for ease of use; vLLM is optimized for throughput.
Use vLLM when you're processing hundreds or thousands of bills per day
and need to minimize latency and maximize GPU utilization.

vLLM downloads models from HuggingFace automatically the first time
you serve them. After that, they're cached locally.

### Setup on a GPU server (Ubuntu)

```bash
# Python 3.10-3.12 required
pip install vllm

# Serve the model (downloads ~65GB on first run)
vllm serve Qwen/Qwen2.5-VL-32B-Instruct \
  --host 0.0.0.0 \
  --port 8000 \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90
```

Then in Robin's `.env`:
```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://your-server-ip:8000/v1
LLM_MODEL=Qwen/Qwen2.5-VL-32B-Instruct
LLM_API_KEY=any_string   # vLLM accepts any key by default
```

### Hardware sizing for vLLM

| Model | Minimum VRAM | Recommended setup |
|-------|-------------|-------------------|
| Qwen2.5-VL-7B | 16GB | 1× A10G (24GB) or 1× RTX 4090 (24GB) |
| Qwen2.5-VL-32B | 70GB | 2× A100 80GB, or 1× H100 80GB |

Cloud GPU options (June 2026 pricing):
- **Lambda Labs:** A10G at ~$0.75/hr, A100 at ~$1.99/hr
- **RunPod:** A10G at ~$0.69/hr, A100 at ~$1.89/hr
- **Vast.ai:** Often cheaper than the above for spot instances
- **AWS/GCP/Azure:** More expensive but more reliable SLAs

For Robin's workload (bill extraction is the heaviest task at ~2,000
tokens per call), an A10G handles about 30 extractions per minute with
the 7B model. A 32B model on two A100s does about 15 per minute but
with noticeably better accuracy.

### Keeping models cached between restarts

vLLM downloads to `~/.cache/huggingface/hub/` by default. Mount this
as a persistent volume in Docker or just make sure the path isn't
ephemeral on your cloud instance:

```bash
# Check what's cached
ls ~/.cache/huggingface/hub/

# Set a custom cache path
export HF_HOME=/data/hf_cache
vllm serve Qwen/Qwen2.5-VL-32B-Instruct ...
```

---

## How to verify a model works with Robin's tasks

Once any model is running (Ollama or vLLM), run these checks in order:

### Check 1: Basic connectivity
```bash
cd pipeline
python3 -c "
import llm_client
print(llm_client.complete('Say hello in one word.'))
"
```

### Check 2: JSON output (critical — Robin depends on this)
```bash
python3 -c "
import llm_client
result = llm_client.complete_json(
    'Return JSON with keys name and value. Name is robin, value is 42. No markdown, just JSON.'
)
print(result)
assert result == {'name': 'robin', 'value': 42}
print('JSON output: OK')
"
```

### Check 3: Vision (bill extraction — most important task)
```bash
python3 -c "
from PIL import Image
import io, base64, llm_client

# Create a tiny test 'bill' image
img = Image.new('RGB', (400, 200), color='white')
from PIL import ImageDraw
d = ImageDraw.Draw(img)
d.text((10, 10), 'INVOICE', fill='black')
d.text((10, 40), 'Total: \$1,234.56', fill='black')
d.text((10, 70), 'CPT: 99213', fill='black')
buf = io.BytesIO()
img.save(buf, format='PNG')
img_bytes = buf.getvalue()

result = llm_client.complete_json(
    'Extract: {\"total\": <number>, \"cpt_code\": \"<string>\"}. JSON only.',
    images=[(img_bytes, 'image/png')]
)
print('Vision result:', result)
print('Vision: OK' if 'total' in result else 'Vision: model may not support images')
" 2>/dev/null || echo "(install Pillow first: pip install Pillow)"
```

If Check 3 fails with an error about images not being supported, the
model you're using doesn't support vision. Switch to `qwen2.5vl:7b`
(the "vl" suffix means Vision-Language).

---

## Switching between models

You can switch models at any time without restarting the API — just
change `LLM_MODEL` in `.env` and restart the API process:

```bash
# Pull a new model
ollama pull qwen2.5vl:32b

# Update .env
LLM_MODEL=qwen2.5vl:32b

# Restart the API (docker compose)
docker compose restart api

# Or if running directly
pkill -f "uvicorn" && uvicorn pipeline.api:app --port 8001 --reload
```

---

## Troubleshooting

**"connection refused" on port 11434**
Ollama isn't running. On Mac: click the llama icon in the menu bar to
open the app. On Linux: `systemctl start ollama`.

**Model responds slowly**
- On Mac: check Activity Monitor → Window → GPU History to see if
  the model is using the GPU (should show near-100% usage during inference)
- On Linux: `nvidia-smi` and look for high GPU utilization
- If no GPU usage: your CUDA drivers may not be installed, or Ollama
  can't see the GPU. Try `ollama run qwen2.5vl:7b "hi"` in a terminal
  and watch `nvidia-smi` in another terminal

**"model not found" error**
Run `ollama list` to see what's downloaded. If `qwen2.5vl:7b` isn't
there, run `ollama pull qwen2.5vl:7b`.

**JSON parsing errors from the model**
The model returned something that isn't valid JSON (maybe it added a
preamble like "Here is the JSON:"). `complete_json()` in `llm_client.py`
already strips markdown code fences, but some models are less reliable
at following JSON-only instructions. Try:
- A larger model: `ollama pull qwen2.5vl:32b`
- Adding "Respond with JSON only. No explanation. No markdown." to the
  prompt (already the case in Robin's prompts, but worth checking)

**"CUDA out of memory"**
The model is too large for your GPU. Options:
- Use a smaller model: `ollama pull qwen2.5vl:7b` instead of 32b
- Reduce context length: add `--max-model-len 16384` to the vLLM command
- Quantized version: `ollama pull qwen2.5vl:7b-q4_0` (half the VRAM, slightly lower quality)

**Bill extraction gives wrong results**
- The 7B model sometimes struggles with very poor quality scans. Try
  the 32B model for better OCR accuracy.
- Make sure the image is right-side-up and reasonably legible.
- PDFs with embedded text (not scanned) extract much more reliably.

---

## Quick reference: what goes in `.env`

```env
# Local Ollama (Mac app or Linux service)
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5vl:7b

# Local Ollama inside Docker (Ollama running as container)
LLM_PROVIDER=ollama
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=qwen2.5vl:7b

# Local Ollama, but API is in Docker and Ollama is native on Mac
LLM_PROVIDER=ollama
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen2.5vl:7b

# Self-hosted vLLM on a GPU server
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://gpu-server-ip:8000/v1
LLM_MODEL=Qwen/Qwen2.5-VL-32B-Instruct
LLM_API_KEY=any_string
```
