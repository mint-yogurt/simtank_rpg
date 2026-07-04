"""Provider-agnostic LLM client."""

import json
import logging
import urllib.request

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
