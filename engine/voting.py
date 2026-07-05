"""Voting state machine for simtank_rpg.

A Proposal is opened when a party member suggests a group action.
All alive members vote yes/no; the initiator auto-votes yes immediately.
Outcome locks the moment it is mathematically certain — no need to wait out
remaining votes.

Thresholds (minimum YES votes to pass):
  4 alive → 3 of 4
  3 alive → 2 of 3
  2 alive → 2 of 2  (unanimous)
  1 alive → auto-pass  (single survivor, no vote needed)

alive_count is frozen at proposal time; mid-vote knockouts don't shift the
threshold (battle-interrupt is out of scope — noted here for when it matters).
"""

from dataclasses import dataclass, field
from typing import Optional


def required_votes(alive_count: int) -> int:
    """Minimum yes-votes to pass a proposal."""
    if alive_count >= 4:
        return 3
    if alive_count == 3:
        return 2
    if alive_count == 2:
        return 2
    return 1  # 1 alive: threshold=1, initiator auto-yes immediately passes


@dataclass
class Proposal:
    initiator: str      # member name who opened this
    direction: str      # 'N' | 'S' | 'E' | 'W'
    steps: int
    threshold: int      # yes-votes required
    alive_count: int    # frozen at proposal time
    votes: dict = field(default_factory=dict)  # member_name → bool (True=yes)

    @property
    def action_desc(self) -> str:
        return f"move {self.direction} {self.steps} step{'s' if self.steps != 1 else ''}"

    @property
    def yes_count(self) -> int:
        return sum(1 for v in self.votes.values() if v)

    @property
    def no_count(self) -> int:
        return sum(1 for v in self.votes.values() if not v)

    @property
    def votes_cast(self) -> int:
        return len(self.votes)

    @property
    def votes_remaining(self) -> int:
        return self.alive_count - self.votes_cast

    def is_locked(self) -> bool:
        """True the moment outcome is mathematically certain."""
        if self.yes_count >= self.threshold:
            return True
        needed = self.threshold - self.yes_count
        if self.votes_remaining < needed:
            return True  # can't reach threshold even with all remaining yes
        return False

    def outcome(self) -> Optional[str]:
        """'passed' | 'failed' | None if still in play."""
        if not self.is_locked():
            return None
        return 'passed' if self.yes_count >= self.threshold else 'failed'

    def has_voted(self, member_name: str) -> bool:
        return member_name in self.votes

    def cast_vote(self, member_name: str, yes: bool) -> None:
        """Record a vote. No-op if member already voted (idempotent)."""
        if member_name not in self.votes:
            self.votes[member_name] = yes


class VotingState:
    """Lifecycle manager for a single open proposal."""

    def __init__(self):
        self._proposal: Optional[Proposal] = None

    @property
    def proposal(self) -> Optional[Proposal]:
        return self._proposal

    @property
    def is_open(self) -> bool:
        return self._proposal is not None

    def open_proposal(self, initiator: str, direction: str, steps: int,
                      alive_count: int) -> tuple[Optional[Proposal], Optional[str]]:
        """Open a new proposal. Initiator auto-votes yes.

        Returns (proposal, outcome):
          - proposal=None, outcome='passed' for alive_count <= 1 (auto-pass)
          - proposal=Proposal, outcome=None normally (vote open)
          - proposal=None, outcome='passed' if alive_count==1 (auto-pass after
            initiator auto-yes hits threshold=1)
        """
        threshold = required_votes(alive_count)
        self._proposal = Proposal(
            initiator=initiator,
            direction=direction,
            steps=steps,
            threshold=threshold,
            alive_count=alive_count,
        )
        self._proposal.cast_vote(initiator, True)   # initiator always votes yes

        # Check immediate resolution (alive_count==1 → threshold=1 → locked at once)
        outcome = self._proposal.outcome()
        if outcome:
            self._proposal = None
            return None, outcome
        return self._proposal, None

    def cast_vote(self, member_name: str, yes: bool) -> Optional[str]:
        """Cast a vote. Returns outcome string if now resolved, else None."""
        if not self._proposal:
            return None
        self._proposal.cast_vote(member_name, yes)
        outcome = self._proposal.outcome()
        if outcome:
            self._proposal = None
            return outcome
        return None

    def abandon(self) -> None:
        """Close without resolution (round cap, nobody can break the stalemate)."""
        self._proposal = None

    def get_display_state(self) -> dict:
        """Snapshot for display / SSE.

        TODO: wire to text panel / SSE event when ready. Pattern mirrors
        whatever the battle loop uses for its display feed.
        """
        if not self._proposal:
            return {"status": "idle"}
        p = self._proposal
        return {
            "status": "open",
            "initiator": p.initiator,
            "action": p.action_desc,
            "direction": p.direction,
            "steps": p.steps,
            "threshold": p.threshold,
            "alive_count": p.alive_count,
            "yes_count": p.yes_count,
            "no_count": p.no_count,
            "votes": dict(p.votes),
            "outcome": p.outcome(),
        }


def available_actions(member_name: str, voting_state: VotingState) -> set:
    """Return the set of action strings valid for this member this turn.

    Structurally prevents illegal moves: VOTE is absent when no proposal is
    open, PROPOSE is absent mid-vote. The parser enforces this too.
    """
    if not voting_state.is_open:
        return {"PROPOSE", "WAIT"}
    if voting_state.proposal.has_voted(member_name):
        return {"WAIT"}
    return {"VOTE", "WAIT"}
