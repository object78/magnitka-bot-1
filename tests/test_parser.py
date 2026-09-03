from pathlib import Path
from zoneinfo import ZoneInfo

from app.parser import parse_game_page

FIX = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Asia/Yekaterinburg")


def test_finished_page():
    html = (FIX / "game_finished.html").read_text(encoding="utf-8")
    s = parse_game_page(html, "https://mg-open.org/game/1/4946.html", TZ)
    assert s.game_id == 4946
    assert s.match_no == 1
    assert s.team1 == "Ледовые Спартанцы"
    assert s.team2 == "Хитрые Лисы"
    assert s.period_scores == [(1, 2), (1, 0), (0, 3)]
    assert s.finished
    assert len(s.rosters["Ледовые Спартанцы"]) == 2


def test_live_timer():
    html = (FIX / "game_live_p2.html").read_text(encoding="utf-8")
    s = parse_game_page(html, "https://mg-open.org/game/1/6000.html", TZ)
    assert s.live_period == 2
    assert s.live_elapsed_seconds == 260
    assert s.p2_score() == (0, 0)


def test_scoreless_p1_is_known_once_p2_started():
    html = (FIX / "game_live_p2.html").read_text(encoding="utf-8")
    # Remove the only P1 goal row; live_period=2 makes P1 0:0 knowable.
    html = html.replace('<tr><td>1</td><td>1</td><td>05:30</td><td>1:0</td><td>Хитрые Лисы</td><td>A</td><td></td></tr>', '')
    s = parse_game_page(html, "https://mg-open.org/game/1/6001.html", TZ)
    assert s.p1_score() == (0, 0)


def test_continuous_event_clock_converts_to_period_local_seconds():
    from app.models import GoalEvent
    p2 = GoalEvent(period=2, raw_time="10:29", absolute_seconds=10*60+29, score_after="0:1", team="Свирепые Ежи")
    p3 = GoalEvent(period=3, raw_time="26:45", absolute_seconds=26*60+45, score_after="2:5", team="Хитрые Лисы")
    assert p2.period_elapsed_seconds == 29
    assert p3.period_elapsed_seconds == 6*60+45


def test_hidden_live_attributes_are_detected():
    html = '''
    <html><head><title>Игра: Хитрые Лисы - Свирепые Ежи. :.</title></head>
    <body>
      <h2>День 4, № 4</h2><div>03.09.2026 12:00</div>
      <div data-current-period="2" data-period-time="00:37"></div>
      <h4>Состав «Хитрые Лисы»</h4><table><tr><th>#</th><th>Игрок</th><th>Амплуа</th></tr></table>
    </body></html>
    '''
    s = parse_game_page(html, "https://mg-open.org/game/1/5129.html", TZ)
    assert s.live_period == 2
    assert s.live_elapsed_seconds == 37
