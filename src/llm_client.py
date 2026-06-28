"""Thin LLM abstraction layer — routes calls to Anthropic or Ollama.

Supports two backends:
  - anthropic: Claude Haiku via Anthropic API (production, paid)
  - ollama: Local model via Ollama OpenAI-compatible API (development, free)

Backend selection: LLM_BACKEND env var or config.LLM_BACKEND.
"""

import json
import logging
import re

import config as cfg

logger = logging.getLogger(__name__)

# ── Token usage accumulator ───────────────────────────────────────────
# Module-level meter. Lives for the process lifetime, so separate CLI
# invocations start fresh automatically. Long-lived processes (dropbox-watch,
# Apify Actor) must call reset_usage() at the start of each logical run —
# see CLAUDE.md / the run entry points in main.py.

_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_model": {}}


def _record_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    """Add one LLM call's token usage to the accumulator."""
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    _USAGE["calls"] += 1
    _USAGE["input_tokens"] += input_tokens
    _USAGE["output_tokens"] += output_tokens
    bm = _USAGE["by_model"].setdefault(
        model, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    )
    bm["calls"] += 1
    bm["input_tokens"] += input_tokens
    bm["output_tokens"] += output_tokens
    if getattr(cfg, "LLM_USAGE_LOG", True):
        logger.debug(
            "LLM usage: model=%s in=%d out=%d", model, input_tokens, output_tokens
        )


def _record_response_usage(model: str, response) -> None:
    """Pull token counts off a backend response and record them.

    Handles both shapes: Anthropic (input_tokens/output_tokens) and
    OpenAI-compatible — Ollama/OpenRouter (prompt_tokens/completion_tokens).
    Silently no-ops if the response carries no usage object.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    in_tok = getattr(usage, "input_tokens", None)
    out_tok = getattr(usage, "output_tokens", None)
    if in_tok is None and out_tok is None:
        in_tok = getattr(usage, "prompt_tokens", 0)
        out_tok = getattr(usage, "completion_tokens", 0)
    _record_usage(model, in_tok, out_tok)


def get_usage() -> dict:
    """Return an immutable snapshot of the current token meter."""
    import copy
    return copy.deepcopy(_USAGE)


def reset_usage() -> None:
    """Zero the token meter. Call at the start of each logical run."""
    _USAGE["calls"] = 0
    _USAGE["input_tokens"] = 0
    _USAGE["output_tokens"] = 0
    _USAGE["by_model"] = {}


def estimate_cost_usd(usage: dict | None = None) -> float:
    """Convert token usage to USD using cfg.LLM_PRICING (per 1M tokens).

    Unknown models fall back to cfg.LLM_PRICING_DEFAULT. Pass a snapshot
    from get_usage(), or omit to price the current accumulator.
    """
    if usage is None:
        usage = _USAGE
    pricing = getattr(cfg, "LLM_PRICING", {})
    default = getattr(cfg, "LLM_PRICING_DEFAULT", (1.0, 5.0))
    total = 0.0
    by_model = usage.get("by_model") or {}
    if by_model:
        for model, m in by_model.items():
            in_rate, out_rate = pricing.get(model, default)
            if model not in pricing:
                logger.debug("No LLM_PRICING for model %s — using default rate", model)
            total += (m["input_tokens"] / 1_000_000) * in_rate
            total += (m["output_tokens"] / 1_000_000) * out_rate
    else:
        in_rate, out_rate = default
        total += (usage.get("input_tokens", 0) / 1_000_000) * in_rate
        total += (usage.get("output_tokens", 0) / 1_000_000) * out_rate
    return total


# ── Backend dispatch ──────────────────────────────────────────────────


def chat_json(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
    model: str | None = None,
) -> dict | None:
    """Send prompt, get parsed JSON response. Routes to configured backend.

    Returns parsed dict on success, None on failure.

    model: optional per-call Anthropic model override (e.g. a higher-quality
    model for accuracy-critical extraction). Defaults to config.LLM_MODEL.
    Ignored by the ollama / openrouter backends (they have their own model config).
    """
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend == "ollama":
        return _chat_ollama(prompt, system, max_tokens)
    elif backend == "openrouter":
        return _chat_openrouter(prompt, system, max_tokens)
    else:
        return _chat_anthropic(prompt, system, max_tokens, api_key, model)


def chat_json_async(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
    model: str | None = None,
):
    """Async version — returns a coroutine. For llm_parser.py compatibility."""
    import asyncio
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend == "ollama":
        return _chat_ollama_async(prompt, system, max_tokens)
    elif backend == "openrouter":
        return _chat_openrouter_async(prompt, system, max_tokens)
    else:
        return _chat_anthropic_async(prompt, system, max_tokens, api_key, model)


# ── Anthropic backend ────────────────────────────────────────────────


def _chat_anthropic(
    prompt: str, system: str, max_tokens: int, api_key: str | None,
    model: str | None = None,
) -> dict | None:
    """Call Claude via Anthropic API (sync). model overrides config.LLM_MODEL."""
    import anthropic

    key = api_key or cfg.ANTHROPIC_API_KEY
    if not key:
        logger.warning("No Anthropic API key — skipping LLM call")
        return None

    model = model or getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        _record_response_usage(model, response)
        result_text = response.content[0].text.strip()
        return _parse_json(result_text)
    except anthropic.NotFoundError as e:
        # Almost always a bad/retired model id. Surface loudly: silently returning
        # None here makes downstream extraction (e.g. heir maps) collapse to empty.
        logger.error("Anthropic rejected model %r (NotFoundError): %s", model, e)
        return None
    except Exception as e:
        logger.warning("Anthropic LLM call failed (model=%s): %s", model, e)
        return None


async def _chat_anthropic_async(
    prompt: str, system: str, max_tokens: int, api_key: str | None,
    model: str | None = None,
) -> dict | None:
    """Call Claude via Anthropic API (async). model overrides config.LLM_MODEL."""
    import anthropic

    key = api_key or cfg.ANTHROPIC_API_KEY
    if not key:
        logger.warning("No Anthropic API key — skipping LLM call")
        return None

    model = model or getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        _record_response_usage(model, response)
        result_text = response.content[0].text.strip()
        return _parse_json(result_text)
    except anthropic.NotFoundError as e:
        logger.error("Anthropic rejected model %r (NotFoundError): %s", model, e)
        return None
    except Exception as e:
        logger.warning("Anthropic async LLM call failed (model=%s): %s", model, e)
        return None


# ── Ollama backend ───────────────────────────────────────────────────


def _chat_ollama(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call local Ollama model via OpenAI-compatible API (sync)."""
    from openai import OpenAI

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434/v1/")
    model = getattr(cfg, "OLLAMA_MODEL", "qwen2.5:7b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(base_url=base_url, api_key="ollama")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        _record_response_usage(model, response)
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            # Retry once with explicit JSON instruction appended
            logger.debug("Ollama JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            _record_response_usage(model, response)
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("Ollama LLM call failed: %s", e)
        return None


async def _chat_ollama_async(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call local Ollama model via OpenAI-compatible API (async)."""
    from openai import AsyncOpenAI

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434/v1/")
    model = getattr(cfg, "OLLAMA_MODEL", "qwen2.5:7b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key="ollama")
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        _record_response_usage(model, response)
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("Ollama async JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            _record_response_usage(model, response)
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("Ollama async LLM call failed: %s", e)
        return None


# ── OpenRouter backend ──────────────────────────────────────────────


def _chat_openrouter(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call OpenRouter model via OpenAI-compatible API (sync)."""
    from openai import OpenAI

    api_key = getattr(cfg, "OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("No OpenRouter API key — skipping LLM call")
        return None

    base_url = getattr(cfg, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = getattr(cfg, "OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        _record_response_usage(model, response)
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("OpenRouter JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            _record_response_usage(model, response)
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("OpenRouter LLM call failed: %s", e)
        return None


async def _chat_openrouter_async(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call OpenRouter model via OpenAI-compatible API (async)."""
    from openai import AsyncOpenAI

    api_key = getattr(cfg, "OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("No OpenRouter API key — skipping LLM call")
        return None

    base_url = getattr(cfg, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = getattr(cfg, "OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        _record_response_usage(model, response)
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("OpenRouter async JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            _record_response_usage(model, response)
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("OpenRouter async LLM call failed: %s", e)
        return None


# ── JSON parsing ─────────────────────────────────────────────────────


def _parse_json(text: str) -> dict | None:
    """Parse JSON from LLM response, stripping markdown fences."""
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"items": result}  # Wrap list in dict for consistency
        return None
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.debug("Failed to parse JSON from LLM response: %.200s", text)
        return None
