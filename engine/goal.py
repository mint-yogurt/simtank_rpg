"""Persistent goal state for party navigation in simtank_rpg.

A Goal survives across ticks and screens. It is replaced only when:
  - no active goal exists
  - status transitions to 'completed' or 'abandoned'
  - a checkpoint discussion (JOB 11b) results in abandoning/changing it
"""

from dataclasses import dataclass, field


@dataclass
class Goal:
    goal_type: str      # 'explore' | 'travel'
    target_sx: int      # target screen x (column)
    target_sy: int      # target screen y (row, N decreases)
    reasoning: str      # summary from LLM deliberation
    status: str = field(default='active')   # 'active' | 'completed' | 'abandoned'
    abandon_reason: str = field(default='')

    def is_active(self) -> bool:
        return self.status == 'active'

    def complete(self) -> None:
        self.status = 'completed'

    def abandon(self, reason: str = '') -> None:
        self.status = 'abandoned'
        self.abandon_reason = reason

    def at_target(self, sx: int, sy: int) -> bool:
        return sx == self.target_sx and sy == self.target_sy

    def summary(self) -> str:
        suffix = f' [{self.abandon_reason}]' if self.abandon_reason else ''
        return (f"{self.goal_type} → ({self.target_sx},{self.target_sy})"
                f" [{self.status}{suffix}]: {self.reasoning}")

    def to_dict(self) -> dict:
        return {
            "goal_type":     self.goal_type,
            "target_sx":     self.target_sx,
            "target_sy":     self.target_sy,
            "reasoning":     self.reasoning,
            "status":        self.status,
            "abandon_reason": self.abandon_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Goal":
        g = cls(
            goal_type=d["goal_type"],
            target_sx=d["target_sx"],
            target_sy=d["target_sy"],
            reasoning=d["reasoning"],
            status=d.get("status", "active"),
            abandon_reason=d.get("abandon_reason", ""),
        )
        return g
