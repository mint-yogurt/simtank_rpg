"""Parse and validate LLM action responses. Always returns a valid action."""

import json
import logging
import re

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"ATTACK", "DEFEND", "SPECIAL", "RUN"}
_FALLBACK = {"action": "DEFEND", "target": None}


def parse_action(
    raw: str | None,
    special_target: str | None,
    enemy_name: str,
    party_names: list[str],
    special_name: str = "",
) -> dict:
    if raw is None:
        return _FALLBACK.copy()

    text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", raw.strip(), flags=re.DOTALL).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("JSON parse failed. Raw: %.200s", raw)
        return _FALLBACK.copy()

    action = str(parsed.get("action", "")).upper()
    if action not in VALID_ACTIONS:
        if special_name and action == special_name.upper():
            action = "SPECIAL"
        else:
            logger.warning("Invalid action %r. Raw: %.200s", action, raw)
            return _FALLBACK.copy()

    target = parsed.get("target")

    # "" and None both mean ally-targeting (unfilled placeholder)
    if special_target in ("", None):
        special_target = "ally"

    if action == "ATTACK":
        target = enemy_name
    elif action == "SPECIAL":
        if special_target == "enemy":
            target = enemy_name
        elif special_target == "ally":
            if target not in party_names:
                target = party_names[0] if party_names else None
        else:
            target = None
    else:  # DEFEND, RUN
        target = None

    return {"action": action, "target": target}
