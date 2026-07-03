"""
secrets.py - API keys and private config. GITIGNORED. Do not commit.

Fill in the values below. Import via `import secrets` (or
`from secrets import MISTRAL_API_KEY`). Note: within this project this shadows
Python's stdlib `secrets` module, which isn't needed here.
"""

# --- Hosted LLM providers (free tiers) ---
MISTRAL_API_KEY = ""              # https://console.mistral.ai
AI_HORDE_API_KEY = "0000000000"   # "0000000000" = anonymous, low priority

# --- Local LLM ---
OLLAMA_BASE_URL = "http://localhost:11434"  # no key needed

# --- Misc ---
# GITHUB_TOKEN = ""
