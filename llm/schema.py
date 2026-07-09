"""Parse and validate LLM action responses. Always returns a valid action."""

import json
import logging
import re

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"ATTACK", "DEFEND", "SPECIAL", "RUN"}
_FALLBACK = {"action": "DEFEND", "target": None}

_VALID_OW_DIRECTIONS = {"N", "S", "E", "W"}
_OW_MAX_STEPS = 8


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

    if not isinstance(parsed, dict):
        logger.warning("JSON not an object. Raw: %.200s", raw)
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


def parse_overworld_action(raw: str | None, available_actions: set) -> dict | None:
    """Parse and validate an overworld/voting action against available_actions.

    Returns one of:
      {"action": "PROPOSE", "direction": "N"|"S"|"E"|"W", "steps": int 1-8}
      {"action": "VOTE",    "vote": "yes"|"no"}
      {"action": "WAIT"}
    Returns None on any validation failure (caller handles retry + fallback).

    available_actions enforces game-state legality — e.g. "VOTE" is rejected
    when no proposal is open, "PROPOSE" is rejected mid-vote.
    """
    if raw is None:
        return None

    text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", raw.strip(), flags=re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("overworld: JSON parse failed. Raw: %.200s", raw)
        return None

    if not isinstance(parsed, dict):
        logger.warning("overworld: JSON not an object. Raw: %.200s", raw)
        return None

    action = str(parsed.get("action", "")).upper()
    if action not in available_actions:
        logger.warning("overworld: action %r not in %s. Raw: %.200s",
                       action, available_actions, raw)
        return None

    if action == "PROPOSE":
        direction = str(parsed.get("direction", "")).upper()
        if direction not in _VALID_OW_DIRECTIONS:
            logger.warning("overworld: bad direction %r", direction)
            return None
        try:
            steps = int(parsed["steps"])
        except (KeyError, TypeError, ValueError):
            logger.warning("overworld: bad steps in %r", parsed)
            return None
        if not 1 <= steps <= _OW_MAX_STEPS:
            logger.warning("overworld: steps %d out of range 1-%d", steps, _OW_MAX_STEPS)
            return None
        return {"action": "PROPOSE", "direction": direction, "steps": steps}

    if action == "VOTE":
        vote = str(parsed.get("vote", "")).lower()
        if vote not in ("yes", "no"):
            logger.warning("overworld: bad vote %r", vote)
            return None
        return {"action": "VOTE", "vote": vote}

    if action == "WAIT":
        return {"action": "WAIT"}

    return None


_GOAL_TYPES = {"explore", "travel"}
_CHECKPOINT_DECISIONS = {"continue", "abandon", "modify"}


def parse_checkpoint_decision(raw: str | None) -> dict | None:
    """Parse and validate a checkpoint discussion LLM response.

    Expected JSON:
      {"decision": "continue"|"abandon"|"modify",
       "goal_type": "explore"|"travel",   <- required when decision=="modify"
       "target_sx": int,                   <- required when decision=="modify"
       "target_sy": int,                   <- required when decision=="modify"
       "reasoning": "..."}                 <- always optional

    Returns a dict with 'decision' and 'reasoning' keys, plus goal fields when
    decision is 'modify'. Returns None on any validation failure.
    """
    if raw is None:
        return None

    text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", raw.strip(), flags=re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("checkpoint: JSON parse failed. Raw: %.200s", raw)
        return None

    if not isinstance(parsed, dict):
        logger.warning("checkpoint: JSON not an object. Raw: %.200s", raw)
        return None

    decision = str(parsed.get("decision", "")).lower()
    if decision not in _CHECKPOINT_DECISIONS:
        logger.warning("checkpoint: invalid decision %r", decision)
        return None

    reasoning = str(parsed.get("reasoning", ""))[:200]
    result: dict = {"decision": decision, "reasoning": reasoning}

    if decision == "modify":
        goal_type = str(parsed.get("goal_type", "")).lower()
        if goal_type not in _GOAL_TYPES:
            logger.warning("checkpoint: modify missing valid goal_type")
            return None
        try:
            target_sx = int(parsed["target_sx"])
            target_sy = int(parsed["target_sy"])
        except (KeyError, TypeError, ValueError):
            logger.warning("checkpoint: modify missing target coords")
            return None
        result.update({"goal_type": goal_type,
                        "target_sx": target_sx,
                        "target_sy": target_sy})

    return result


def parse_interior_destination(raw: str | None, available_targets: list[str]) -> dict | None:
    """Parse interior navigation choice: {"target": str, "reasoning": str}.

    Returns dict with 'target' and 'reasoning', or None on failure.
    target must be in available_targets.
    """
    if raw is None:
        return None

    text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", raw.strip(), flags=re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("interior: JSON parse failed. Raw: %.200s", raw)
        return None

    if not isinstance(parsed, dict):
        logger.warning("interior: JSON not an object. Raw: %.200s", raw)
        return None

    target = str(parsed.get("target", ""))
    if target not in available_targets:
        logger.warning("interior: target %r not in %s", target, available_targets)
        return None

    reasoning = str(parsed.get("reasoning", ""))[:200]
    return {"target": target, "reasoning": reasoning}


def parse_goal_decision(raw: str | None) -> dict | None:
    """Parse and validate a goal-setting LLM response.

    Expected JSON:
      {"goal_type": "explore"|"travel", "target_sx": int, "target_sy": int,
       "reasoning": "..."}

    Returns a dict with those keys, or None on any validation failure.
    """
    if raw is None:
        return None

    text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", raw.strip(), flags=re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("goal: JSON parse failed. Raw: %.200s", raw)
        return None

    if not isinstance(parsed, dict):
        logger.warning("goal: JSON not an object. Raw: %.200s", raw)
        return None

    goal_type = str(parsed.get("goal_type", "")).lower()
    if goal_type not in _GOAL_TYPES:
        logger.warning("goal: invalid goal_type %r", goal_type)
        return None

    try:
        target_sx = int(parsed["target_sx"])
        target_sy = int(parsed["target_sy"])
    except (KeyError, TypeError, ValueError):
        logger.warning("goal: bad target coords in %r", parsed)
        return None

    reasoning = str(parsed.get("reasoning", ""))[:200]
    return {
        "goal_type": goal_type,
        "target_sx": target_sx,
        "target_sy": target_sy,
        "reasoning": reasoning,
    }
