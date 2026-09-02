from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import GameSnapshot, Player
from app.strategy import aplus_candidate, it_candidate, m3_candidate

TZ = ZoneInfo("Asia/Yekaterinburg")


def game(no: int, a: str, b: str, p1=(0, 0)) -> GameSnapshot:
    return GameSnapshot(
        game_id=1,
        url="https://mg-open.org/game/1/1.html",
        day_no=1,
        match_no=no,
        scheduled_at=datetime(2026, 9, 2, 11, 0, tzinfo=TZ),
        team1=a,
        team2=b,
        status_text="Перерыв",
        period_scores=[p1],
        rosters={a: [Player("G1", "Вр"), Player("G2", "Вр")], b: [Player("G3", "Вр"), Player("G4", "Вр")]},
    )


def test_m3_strong():
    c = m3_candidate(game(3, "Ледовые Спартанцы", "Свирепые Ежи"))
    assert c and c.base_score == 3


def test_aplus_hard_skip():
    c = aplus_candidate(game(5, "Ледовые Спартанцы", "Меткие Стрелки"), 11)
    assert c is None


def test_it_vs_hedgehogs_any_p1_state():
    c = it_candidate(game(4, "Хитрые Лисы", "Свирепые Ежи", p1=(0, 2)))
    assert c and c.base_score == 3


def test_it_vs_axes_requires_foxes_lead():
    assert it_candidate(game(3, "Хитрые Лисы", "Стальные Топоры", p1=(1, 0))) is not None
    assert it_candidate(game(3, "Хитрые Лисы", "Стальные Топоры", p1=(0, 1))) is None
