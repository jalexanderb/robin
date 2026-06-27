"""
RobinHealth: generic, provider-agnostic LLM client.

Two backends, selected via LLM_PROVIDER, with every caller in this
scaffold (fap_pipeline.classify_document_quality / extract_eligibility /
run_compliance_checklist, bill_pipeline.extract_bill,
letter_pipeline.draft_letter) going through the exact same complete() /
complete_json() functions regardless of which one is configured -- none
of those five callers know or care which provider is active:

  "openai_compatible" (default) -- any server exposing the OpenAI
      chat-completions shape: vLLM, Ollama, llama.cpp server, or a
      hosted provider (Together, Fireworks, OpenRouter, etc.). This is
      how every current open-weight model gets served.

  "anthropic" -- Anthropic's actual Messages API (api.anthropic.com/v1/
      messages). Deliberately NOT shimmed through the OpenAI-compatible
      path above, because it isn't the same shape: system is a
      top-level request field, not a message in the messages array;
      images are {"type": "image", "source": {"type": "base64", ...}}
      content blocks, not data: URLs; auth is an x-api-key header, not
      a Bearer token; and an anthropic-version header is required.
      Treating it as "just another OpenAI-compatible endpoint" would
      either silently send a malformed request or require a fragile
      reshaping at the call site -- two genuinely different request
      builders, dispatched in one place, is the more honest design.

This split exists so the choice between "an open-weight model I'm
self-hosting or renting" and "Anthropic's API" is a one-line config
change, not a code change, on either side of that decision -- and stays
that way as new models ship from either side. Checked while building the
open-weight side (mid-2026): DeepSeek V3.2/V4, Qwen 3.5/3.7, Llama 4
Scout, GLM-4.7/5, and Kimi K2.5/2.6 are all current contenders, each
leading a different axis (reasoning, cost, or speed) -- not one
universal "best." The same logic applies to Anthropic's own lineup
shipping new Claude models on its own cadence: hardcoding today's
specific model into five call sites would mean editing all five every
time either side of the market moves, exactly the maintenance burden
LLM_MODEL exists to avoid.

SUGGESTED DEFAULTS, NEITHER REQUIRED:
  openai_compatible -> Qwen/Qwen3-VL-32B-Instruct (Apache 2.0). Of the
      open-weight options checked, the consistent standout for
      document/OCR-style extraction specifically -- what extract_bill
      needs -- with text understanding reported on par with text-only
      models of similar size, so one served model can plausibly cover
      every LLM-calling function here (vision and text) instead of
      running two.
  anthropic (the default provider) -> claude-opus-4-8. The most capable
      model, chosen for quality on this scaffold's mix of vision extraction,
      letter drafting, and patient Q&A. For high-volume bill extraction where
      cost matters more than the last increment of quality, set
      LLM_MODEL=claude-sonnet-4-6 -- a one-env-var change. Swap LLM_MODEL
      freely; nothing here depends on this specific one.

ANTHROPIC STRUCTURED OUTPUT:
  complete_json() on the anthropic path forces a single tool call (tool_choice
  -> a one-tool "emit_json" schema) and reads the tool input, which the API
  guarantees is valid JSON. That replaces the regex/think-block scraping the
  openai_compatible path still needs, which is why the anthropic branch carries
  none of it.

Configuration (environment variables):
    LLM_PROVIDER   "openai_compatible" (default) or "anthropic"
    LLM_BASE_URL   provider-specific default if unset: http://localhost:
                   8000/v1 (vLLM/Ollama-style local default) for
                   openai_compatible, https://api.anthropic.com for
                   anthropic
    LLM_API_KEY    bearer token (openai_compatible) or x-api-key
                   (anthropic). For anthropic specifically, ANTHROPIC_
                   API_KEY is checked as a fallback if LLM_API_KEY isn't
                   set, since that's the conventional name Anthropic's
                   own tooling uses -- someone "plugging in" Anthropic
                   likely already has it set and shouldn't need a second,
                   scaffold-specific env var just to reuse it. Many
                   self-hosted openai_compatible servers don't check this
                   at all, but it's sent if set either way.
    LLM_MODEL      model name exactly as the configured provider expects
                   it (e.g. "Qwen/Qwen3-VL-32B-Instruct" or
                   "claude-sonnet-4-6")
    ANTHROPIC_API_VERSION   anthropic-version header value; defaults to
                   "2023-06-01" (the API's request/response schema
                   version, stable since launch -- not tied to specific
                   model releases)

REACHABILITY IN THIS SANDBOX, CHECKED DIRECTLY WHILE BUILDING THIS:
api.anthropic.com is NOT blocked by the egress proxy here, unlike
huggingface.co / api.together.xyz / openrouter.ai (all return HTTP 403).
A real POST to api.anthropic.com/v1/messages, built from this module's
own request-construction logic, got back a real HTTP 401 with body
{"type":"error","error":{"type":"authentication_error","message":
"invalid x-api-key"}} plus a real request_id -- meaning Anthropic's API
fully parsed the request as a valid Messages-API call and rejected it
specifically for the (deliberately fake) key, not for anything about
the request's shape. There's no ANTHROPIC_API_KEY (or any credential)
present anywhere in this environment to get further than that.
complete()/complete_json() are real, tested code for both providers --
request construction and response parsing are covered in
test_pipeline.py by mocking httpx for both, on top of the real
unauthenticated check above -- something the openai_compatible path has
never had real network access to verify at all, since every domain it
could plausibly point at from a generic config is blocked here. Set
either provider's credentials/endpoint for real and the pipeline
functions built on top of this module work without further code
changes.
"""

from __future__ import annotations

import base64
import json
import os

import httpx


_DEFAULT_MODEL_BY_PROVIDER = {
    "openai_compatible": "Qwen/Qwen3-VL-32B-Instruct",  # suggested, not required -- see module docstring
    "anthropic": "claude-opus-4-8",  # default: most capable model; override via LLM_MODEL (e.g. claude-sonnet-4-6 to cut cost)
}
_DEFAULT_BASE_URL_BY_PROVIDER = {
    "openai_compatible": "http://localhost:8000/v1",  # vLLM/Ollama-style local default
    "anthropic": "https://api.anthropic.com",
}
_DEFAULT_ANTHROPIC_API_VERSION = "2023-06-01"


def _provider() -> str:
    # Default to Anthropic: Claude's quality and -- via forced tool-use in
    # complete_json -- guaranteed-valid structured output are worth it for this
    # workload (bill extraction, letter drafting, patient Q&A). Set
    # LLM_PROVIDER=openai_compatible to use a self-hosted/open-weight model.
    return os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()


def _base_url() -> str:
    provider = _provider()
    default = _DEFAULT_BASE_URL_BY_PROVIDER.get(provider, _DEFAULT_BASE_URL_BY_PROVIDER["openai_compatible"])
    return os.environ.get("LLM_BASE_URL", default)


def _api_key() -> str | None:
    key = os.environ.get("LLM_API_KEY")
    if key:
        return key
    if _provider() == "anthropic":
        # Fallback to the conventional Anthropic env var name -- see
        # module docstring for why.
        return os.environ.get("ANTHROPIC_API_KEY")
    return None


def _model() -> str:
    provider = _provider()
    default = _DEFAULT_MODEL_BY_PROVIDER.get(provider, _DEFAULT_MODEL_BY_PROVIDER["openai_compatible"])
    return os.environ.get("LLM_MODEL", default)


def complete(
    prompt: str,
    images: list[tuple[bytes, str]] | None = None,
    system: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.0,
) -> str:
    """
    Send one user turn (optionally with images) to the configured LLM
    provider and return the response text.

    Dispatches on LLM_PROVIDER ("openai_compatible" or "anthropic" --
    see module docstring) to one of two request/response shapes. Every
    caller in this scaffold goes through this exact function and never
    needs to know which provider is active.

    images is a list of (raw_bytes, media_type) tuples, e.g.
    [(png_bytes, "image/png")].
    """
    if _provider() == "anthropic":
        return _complete_anthropic(prompt, images, system, max_tokens, temperature)
    return _complete_openai_compatible(prompt, images, system, max_tokens, temperature)


def _complete_openai_compatible(
    prompt: str,
    images: list[tuple[bytes, str]] | None,
    system: str | None,
    max_tokens: int,
    temperature: float,
) -> str:
    """
    OpenAI-compatible chat-completions shape. Images are encoded as
    inline base64 data URLs, the format most vision-capable
    open-model servers (vLLM-served Qwen-VL and similar) accept.
    """
    content: list[dict] | str
    if images:
        content = [{"type": "text", "text": prompt}]
        for raw_bytes, media_type in images:
            encoded = base64.b64encode(raw_bytes).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            })
    else:
        content = prompt

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})

    headers = {"Content-Type": "application/json"}
    if _api_key():
        headers["Authorization"] = f"Bearer {_api_key()}"

    body: dict = {
        "model": _model(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    # Disable thinking mode for Qwen3.7 Plus and similar hybrid reasoning
    # models -- when thinking is enabled they narrate rather than output JSON.
    # This is a Fireworks/Qwen3 extension to the OpenAI-compatible API.
    model_name = _model().lower()
    if "qwen3" in model_name or "qwq" in model_name or "deepseek-r" in model_name:
        body["thinking"] = {"type": "disabled"}

    response = httpx.post(
        f"{_base_url()}/chat/completions",
        headers=headers,
        json=body,
        timeout=120.0,
    )
    response.raise_for_status()
    data = response.json()
    # Log the full response for debugging (truncated to 500 chars)
    import logging
    logging.getLogger("robinhealth.llm").info(
        "LLM raw response: %s", str(data)[:2000]
    )
    content_raw = data["choices"][0]["message"]["content"]
    # Some models return None content when they only emit tool_calls
    # or when the response is in a different field
    if content_raw is None:
        # Check for reasoning_content field (some Fireworks models)
        content_raw = (
            data["choices"][0]["message"].get("reasoning_content") or
            data["choices"][0].get("text") or
            ""
        )
    return content_raw


def _complete_anthropic(
    prompt: str,
    images: list[tuple[bytes, str]] | None,
    system: str | None,
    max_tokens: int,
    temperature: float,
) -> str:
    """
    Anthropic Messages API, free-text response. temperature is accepted for
    signature parity with the openai_compatible path but deliberately NOT
    forwarded -- the default model (claude-opus-4-8) and the rest of the
    Opus 4.7+/Fable family reject temperature/top_p/top_k with a 400. Steer
    determinism via the prompt instead.
    """
    data = _anthropic_request(prompt, images, system, max_tokens)
    blocks = data["content"]
    return "".join(block["text"] for block in blocks if block.get("type") == "text")


def _anthropic_request(
    prompt: str,
    images: list[tuple[bytes, str]] | None,
    system: str | None,
    max_tokens: int,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
) -> dict:
    """
    Build and send one Anthropic Messages API request (POST
    {base_url}/v1/messages), returning the parsed response body. Shared by
    _complete_anthropic (free text) and _complete_anthropic_json (forced
    tool call). See module docstring for how this shape differs from the
    OpenAI-compatible one (system placement, image block shape, auth header,
    required api-version header).
    """
    content: list[dict] | str
    if images:
        content = [{"type": "text", "text": prompt}]
        for raw_bytes, media_type in images:
            encoded = base64.b64encode(raw_bytes).decode("ascii")
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": encoded},
            })
    else:
        content = prompt

    headers = {
        "Content-Type": "application/json",
        "anthropic-version": os.environ.get("ANTHROPIC_API_VERSION", _DEFAULT_ANTHROPIC_API_VERSION),
    }
    if _api_key():
        headers["x-api-key"] = _api_key()

    # No temperature/top_p/top_k and no thinking config: sampling params 400
    # on the default model, and thinking is off by default on Opus 4.7+ --
    # right for deterministic extraction/classification.
    body: dict = {
        "model": _model(),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if system:
        # Top-level field, not a message -- the one shape difference most
        # likely to silently misbehave (rather than cleanly error) if this
        # were ever accidentally routed through the openai_compatible builder:
        # a "system" message in the messages array is ignored by Anthropic's
        # API rather than rejected.
        body["system"] = system
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice

    response = httpx.post(
        f"{_base_url()}/v1/messages",
        headers=headers,
        json=body,
        timeout=120.0,
    )
    response.raise_for_status()
    return response.json()


# Single-tool schema used to force valid JSON out of the Anthropic path. The
# payload goes under "data" (rather than being the tool input directly)
# because a forced tool call's input must be a JSON *object*, while some
# callers (e.g. fap_pipeline.run_compliance_checklist) expect a top-level
# array -- the "data" envelope carries either shape unchanged.
_EMIT_JSON_TOOL = {
    "name": "emit_json",
    "description": (
        "Return your answer. Put the complete JSON value the user's "
        "instructions ask for -- an object or an array, matching the "
        "requested schema exactly -- as the value of the `data` field. Do "
        "not rename, wrap, or omit any fields inside it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"data": {}},  # empty schema -> any JSON value
        "required": ["data"],
    },
}


def _complete_anthropic_json(
    prompt: str,
    images: list[tuple[bytes, str]] | None = None,
    system: str | None = None,
    max_tokens: int = 2000,
    **_ignored,
) -> dict | list:
    """
    Anthropic structured output via a forced tool call. The API guarantees the
    tool input is valid JSON, so there is nothing to scrape, strip, or repair
    -- the entire fragile text-parsing path in complete_json is bypassed.
    """
    data = _anthropic_request(
        prompt, images, system, max_tokens,
        tools=[_EMIT_JSON_TOOL],
        tool_choice={"type": "tool", "name": "emit_json"},
    )
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_json":
            return block["input"]["data"]
    # Forced tool_choice means we should never get here; treat as a hard error
    # rather than silently returning something unparseable downstream.
    raise ValueError("Anthropic response contained no emit_json tool_use block")


def complete_json(prompt: str, **kwargs) -> dict | list:
    """
    Return parsed JSON from the configured provider.

    anthropic: forced tool call -> the API guarantees valid JSON (no
        scraping). See _complete_anthropic_json.

    openai_compatible: open-weight models have no such guarantee, so we coax
        and repair text output by:
        1. Adding a system message telling the model to output JSON only
        2. Stripping <think>...</think> blocks from reasoning models
        3. Stripping ```json fences
        4. Scanning for the JSON object/array and parsing it
    """
    if _provider() == "anthropic":
        return _complete_anthropic_json(prompt, **kwargs)

    import re

    # Inject a system message that forces JSON-only output.
    # This overrides any existing system kwarg.
    json_system = (
        "You are a data extraction assistant. "
        "You MUST respond with valid JSON only. "
        "Do not include any explanation, markdown, code fences, "
        "or text before or after the JSON. "
        "Output ONLY the raw JSON object or array."
    )
    existing_system = kwargs.get("system", "")
    kwargs["system"] = (json_system + " " + existing_system).strip()

    text = complete(prompt, **kwargs).strip()

    # Strip <think>...</think> blocks (Qwen3.7 Plus, DeepSeek R1, etc.)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Strip ```json / ``` fences
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    # Try to find valid JSON by scanning from the last { or [
    # Reasoning models often write prose first then JSON at the end
    # Try last occurrence first (most likely to be the actual JSON output)
    for start_char in ["{", "["]:
        last_pos = text.rfind(start_char)
        if last_pos != -1:
            candidate = text[last_pos:]
            try:
                return json.loads(candidate.strip())
            except (json.JSONDecodeError, ValueError):
                pass

    # Fall back to first occurrence
    match = re.search(r"[\\[{]", text)
    if match:
        candidate = text[match.start():]
        try:
            return json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # Last resort: try the whole text
    return json.loads(text.strip())
