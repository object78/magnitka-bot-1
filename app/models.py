from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

TEAMS = (
    "Хитрые Лисы",
    "Ледовые Спартанцы",
    "Свирепые Ежи",
    "Стальные Топоры",
    "Меткие Стрелки",
)


@dataclass(slots=True)
class Player:
    name: str
    role: str


@dataclass(slots=True)
class GoalEvent:
    period: int | str
    raw_time: str | None
    absolute_seconds: int | None
    score_after: str | None
    team: str | None
    author: str | None = None
    assist: str | None = None

    @property
    def is_goal(self) -> bool:
        if self.score_after is None:
            return False
        import re
        return bool(re.search(r"\d+\s*:\s*\d+", self.score_after))

    @property
    def period_elapsed_seconds(self) -> int | None:
        """Convert MG Open's usually-continuous 0:00..30:00 clock to local period seconds.

        Historical protocols normally report P2 as 10:xx..19:xx and P3 as 20:xx..29:xx.
        Some pages can already contain a local 0:xx..9:xx value, so both forms are accepted.
        Impossible/anomalous values are ignored for live clock anchoring.
        """
        if not isinstance(self.period, int) or self.absolute_seconds is None:
            return None
        sec = self.absolute_seconds
        if not 0 <= sec <= 30 * 60:
            return None
        if self.period == 1:
            return sec if sec <= 10 * 60 else None
        if self.period == 2:
            if 10 * 60 <= sec <= 20 * 60:
                return sec - 10 * 60
            if 0 <= sec <= 10 * 60:
                return sec
            return None
        if self.period == 3:
            if 20 * 60 <= sec <= 30 * 60:
                return sec - 20 * 60
            if 0 <= sec <= 10 * 60:
                return sec
            return None
        return None

    @property
    def signature(self) -> str:
        return "|".join(
            [
                str(self.period),
                str(self.raw_time or ""),
                str(self.score_after or ""),
                str(self.team or ""),
                str(self.author or ""),
            ]
        )


@dataclass(slots=True)
class GameSnapshot:
    game_id: int
    url: str
    day_no: int | None
    match_no: int | None
    scheduled_at: datetime | None
    team1: str | None
    team2: str | None
    status_text: str
    final_score: tuple[int, int] | None = None
    period_scores: list[tuple[int, int]] = field(default_factory=list)
    events: list[GoalEvent] = field(default_factory=list)
    rosters: dict[str, list[Player]] = field(default_factory=dict)
    live_period: int | None = None
    live_elapsed_seconds: int | None = None
    break_after_period: int | None = None
    raw_text: str = ""

    @property
    def finished(self) -> bool:
        return "матч завершен" in self.status_text.lower()

    @property
    def new_match(self) -> bool:
        return "новый матч" in self.status_text.lower()

    def team_names(self) -> tuple[str | None, str | None]:
        return self.team1, self.team2

    def p1_score(self) -> tuple[int, int] | None:
        if len(self.period_scores) >= 1:
            return self.period_scores[0]
        score = self._period_score_from_events(1)
        if score is not None:
            return score
        if self.break_after_period == 1 or self.live_period in (2, 3) or self.finished:
            return (0, 0)
        return None

    def p2_score(self) -> tuple[int, int]:
        if len(self.period_scores) >= 2:
            return self.period_scores[1]
        return self._period_score_from_events(2) or (0, 0)

    def p2_goals_for(self, team: str) -> int:
        if team not in (self.team1, self.team2):
            return 0
        a, b = self.p2_score()
        return a if team == self.team1 else b

    def total_regulation_goals(self) -> int | None:
        if len(self.period_scores) >= 3:
            return sum(a + b for a, b in self.period_scores[:3])
        if self.final_score is not None and self.finished:
            return sum(self.final_score)
        return None

    def _period_score_from_events(self, period: int) -> tuple[int, int] | None:
        if not self.team1 or not self.team2:
            return None
        a = b = 0
        seen = False
        for ev in self.events:
            if ev.period != period or not ev.is_goal:
                continue
            seen = True
            if ev.team == self.team1:
                a += 1
            elif ev.team == self.team2:
                b += 1
        return (a, b) if seen else None


@dataclass(slots=True)
class StrategyCandidate:
    strategy: Literal["M3-TB4.5", "A+ v4", "IT-L2 v5"]
    game_id: int
    base_score: int
    base_reason: str
    min_odds: float
    market: str
    opponent: str | None = None
    foxes_state_after_p1: str | None = None


@dataclass(slots=True)
class ScoredSignal:
    candidate: StrategyCandidate
    goalie_proxy: float | None
    gk_boost: bool
    score: int
    tier: Literal["RISK", "CORE", "HIGH", "ULTRA"]
