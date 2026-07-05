"""Provider-agnostic LLM client."""

import json
import logging
import urllib.request
from dataclasses import dataclass

import secrets

logger = logging.getLogger(__name__)


def _ask_ollama(prompt: str, system: str) -> str | None:
    url = f"{secrets.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    payload = {
        "model": secrets.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secrets.OLLAMA_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["message"]["content"]
    except Exception as exc:
        logger.warning("Ollama error: %s", exc)
        return None


def _ask_mistral(prompt: str, system: str) -> str | None:
    payload = {
        "model": secrets.MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        "https://api.mistral.ai/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secrets.MISTRAL_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("Mistral error: %s", exc)
        return None


def ask(prompt: str, system: str) -> str | None:
    if secrets.PROVIDER == "mistral":
        return _ask_mistral(prompt, system)
    return _ask_ollama(prompt, system)


@dataclass
class LLMDecision:
    result: dict
    raw_first: str | None   # raw response from first attempt
    raw_retry: str | None   # raw response from retry; None if no retry happened
    used_fallback: bool     # True when both attempts failed validation

    @property
    def was_real(self) -> bool:
        """True if we got a valid parse from the LLM on either attempt."""
        return not self.used_fallback


def ask_with_retry(prompt: str, system: str, validator, reprompt_suffix: str,
                   fallback: dict) -> LLMDecision:
    """Call the LLM, validate, retry once on bad output, then fall back.

    Args:
        validator:       callable(raw: str | None) → dict | None
        reprompt_suffix: appended to the prompt on the corrective retry
        fallback:        returned as result if both attempts fail validation

    Never raises. Always returns an LLMDecision.
    """
    raw_first = ask(prompt, system)
    result = validator(raw_first)
    if result is not None:
        return LLMDecision(result=result, raw_first=raw_first,
                           raw_retry=None, used_fallback=False)

    correction = prompt + "\n\n---\n" + reprompt_suffix
    raw_retry = ask(correction, system)
    result = validator(raw_retry)
    if result is not None:
        return LLMDecision(result=result, raw_first=raw_first,
                           raw_retry=raw_retry, used_fallback=False)

    return LLMDecision(result=fallback, raw_first=raw_first,
                       raw_retry=raw_retry, used_fallback=True)
