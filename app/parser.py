from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .models import GameSnapshot, GoalEvent, Player, TEAMS

GAME_HREF_RE = re.compile(r"/game/1/(\d+)\.html")
DAY_MATCH_RE = re.compile(r"День\s+(\d+)\s*,\s*№\s*(\d+)", re.I)
DATE_TIME_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})")
PERIOD_SCORE_RE = re.compile(
    r"(\d+)\s*:\s*(\d+)\s*,\s*(\d+)\s*:\s*(\d+)\s*,\s*(\d+)\s*:\s*(\d+)"
)
SCORE_RE = re.compile(r"(?<!\d)(\d+)\s*:\s*(\d+)(?!\d)")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_clock(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
    if not m:
        return None
    mm, ss = map(int, m.groups())
    if ss >= 60:
        return None
    return mm * 60 + ss


def extract_game_links(html: str, base_url: str) -> list[tuple[int, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for a in soup.find_all("a", href=True):
        m = GAME_HREF_RE.search(a.get("href", ""))
        if not m:
            continue
        gid = int(m.group(1))
        if gid in seen:
            continue
        seen.add(gid)
        out.append((gid, urljoin(base_url + "/", a["href"])))
    return out


def _title_teams(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    title = normalize(soup.title.get_text(" ", strip=True) if soup.title else "")
    # e.g. Игра: Ледовые Спартанцы - Хитрые Лисы. 2:5. День 1
    m = re.search(r"Игра:\s*(.+?)\s+-\s+(.+?)\.\s*(?:\d+\s*:\s*\d+|:\.)", title, re.I)
    if m:
        a, b = normalize(m.group(1)), normalize(m.group(2))
        if a in TEAMS and b in TEAMS:
            return a, b
    return None, None


def _main_heading(soup: BeautifulSoup) -> Tag | None:
    for h in soup.find_all(["h1", "h2", "h3"]):
        if DAY_MATCH_RE.search(normalize(h.get_text(" ", strip=True))):
            return h
    return None


def _scheduled_at(soup: BeautifulSoup, main_text: str, tz: ZoneInfo) -> datetime | None:
    # Prefer text around the main Day/№ heading so navigation dates do not win.
    heading = _main_heading(soup)
    if heading:
        pieces = [normalize(heading.get_text(" ", strip=True))]
        node = heading
        for _ in range(8):
            node = node.find_next()
            if not node:
                break
            if isinstance(node, Tag):
                pieces.append(normalize(node.get_text(" ", strip=True)))
            m = DATE_TIME_RE.search(" ".join(pieces))
            if m:
                return datetime.strptime(" ".join(m.groups()), "%d.%m.%Y %H:%M").replace(tzinfo=tz)
    # Fallback anchored to Day/№ in flattened text.
    m = re.search(
        r"День\s+\d+\s*,\s*№\s*\d+\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})",
        main_text,
        re.I,
    )
    if m:
        return datetime.strptime(" ".join(m.groups()), "%d.%m.%Y %H:%M").replace(tzinfo=tz)
    return None


def _parse_events(soup: BeautifulSoup) -> list[GoalEvent]:
    events: list[GoalEvent] = []
    for table in soup.find_all("table"):
        headers = [normalize(x.get_text(" ", strip=True)) for x in table.find_all("th")]
        header_text = " | ".join(headers).lower()
        if not ("период" in header_text and "время" in header_text and "команда" in header_text):
            continue
        if "счет" not in header_text and "счёт" not in header_text:
            continue
        rows = table.find_all("tr")
        for tr in rows[1:]:
            cells = [normalize(x.get_text(" ", strip=True)) for x in tr.find_all(["td", "th"])]
            if len(cells) < 5:
                continue
            # Typical: #, period, time, score, team, author, assist
            offset = 1 if len(cells) >= 6 else 0
            p_raw = cells[offset] if offset < len(cells) else ""
            period: int | str
            if p_raw.isdigit():
                period = int(p_raw)
            else:
                period = p_raw or "B"
            time_raw = cells[offset + 1] if offset + 1 < len(cells) else None
            score_after = cells[offset + 2] if offset + 2 < len(cells) else None
            team = cells[offset + 3] if offset + 3 < len(cells) else None
            author = cells[offset + 4] if offset + 4 < len(cells) else None
            assist = cells[offset + 5] if offset + 5 < len(cells) else None
            events.append(
                GoalEvent(
                    period=period,
                    raw_time=time_raw,
                    absolute_seconds=parse_clock(time_raw),
                    score_after=score_after,
                    team=team or None,
                    author=author or None,
                    assist=assist or None,
                )
            )
        if events:
            break
    return events


def _parse_rosters(soup: BeautifulSoup) -> dict[str, list[Player]]:
    result: dict[str, list[Player]] = {}
    for h in soup.find_all(["h3", "h4", "h5"]):
        txt = normalize(h.get_text(" ", strip=True))
        m = re.search(r"Состав\s+[«\"](.+?)[»\"]", txt, re.I)
        if not m:
            continue
        team = normalize(m.group(1))
        table = h.find_next("table")
        if not table:
            continue
        players: list[Player] = []
        for tr in table.find_all("tr")[1:]:
            cells = [normalize(x.get_text(" ", strip=True)) for x in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            # Usually number, player, role.
            if len(cells) >= 3:
                name, role = cells[-2], cells[-1]
            else:
                name, role = cells[0], cells[1]
            if name:
                players.append(Player(name=name, role=role))
        if players:
            result[team] = players
    return result


def _detect_live_phase(soup: BeautifulSoup, text: str) -> tuple[int | None, int | None, int | None]:
    low = text.lower()
    raw_html = str(soup)
    break_after = None
    if "перерыв" in low:
        if re.search(r"перерыв[^.]{0,30}(?:после\s*)?(?:1|перв)", low):
            break_after = 1
        elif re.search(r"перерыв[^.]{0,30}(?:после\s*)?(?:2|втор)", low):
            break_after = 2
        else:
            break_after = 1

    live_period = None
    patterns = [
        r"(?:ид[её]т|начал(?:ся|ась)|сейчас)\s*(\d)\s*(?:-?й)?\s*период",
        r"(\d)\s*(?:-?й)?\s*период\s*(?:ид[её]т|начал(?:ся|ась))",
        r"период\s*[:№]?\s*(\d)",
    ]
    for pat in patterns:
        m = re.search(pat, low)
        if m and m.group(1) in {"1", "2", "3"}:
            live_period = int(m.group(1))
            break

    # Some versions of the page keep live state in hidden data-* attributes / inline JSON.
    if live_period is None:
        for tag in soup.find_all(True):
            for key, value in tag.attrs.items():
                k = str(key).lower().replace("-", "_")
                if k not in {"data_current_period", "data_live_period", "data_game_period", "current_period", "live_period"}:
                    continue
                value = " ".join(value) if isinstance(value, list) else str(value)
                m = re.search(r"\b([123])\b", value)
                if m:
                    live_period = int(m.group(1))
                    break
            if live_period is not None:
                break
    if live_period is None:
        for pat in (
            r'["\'](?:current[_-]?period|live[_-]?period|game[_-]?period)["\']\s*[:=]\s*["\']?([123])',
            r'\b(?:currentPeriod|livePeriod|gamePeriod)\s*[:=]\s*["\']?([123])',
        ):
            m = re.search(pat, raw_html, re.I)
            if m:
                live_period = int(m.group(1))
                break

    elapsed = None
    timer_candidates: list[str] = []
    for tag in soup.find_all(True):
        ident = " ".join([str(tag.get("id", "")), " ".join(tag.get("class", []))]).lower()
        if any(k in ident for k in ("timer", "clock", "match-time", "game-time", "period-time")):
            timer_candidates.append(normalize(tag.get_text(" ", strip=True)))
        for key, value in tag.attrs.items():
            k = str(key).lower().replace("-", "_")
            if any(x in k for x in ("period_time", "game_time", "match_time", "timer", "clock")):
                timer_candidates.append(" ".join(value) if isinstance(value, list) else str(value))
    for pat in (
        r'["\'](?:period[_-]?time|game[_-]?time|match[_-]?time|timer|clock)["\']\s*[:=]\s*["\'](\d{1,2}:\d{2})',
        r'\b(?:periodTime|gameTime|matchTime)\s*[:=]\s*["\'](\d{1,2}:\d{2})',
    ):
        for m in re.finditer(pat, raw_html, re.I):
            timer_candidates.append(m.group(1))
    for candidate in timer_candidates:
        sec = parse_clock(candidate)
        if sec is not None and sec <= 10 * 60:
            elapsed = sec
            break
    return live_period, elapsed, break_after


def parse_game_page(html: str, url: str, tournament_tz: ZoneInfo) -> GameSnapshot:
    soup = BeautifulSoup(html, "html.parser")
    text = normalize(soup.get_text(" ", strip=True))
    gid_match = GAME_HREF_RE.search(url)
    if not gid_match:
        raise ValueError(f"Cannot determine game id from URL: {url}")
    game_id = int(gid_match.group(1))

    dm = DAY_MATCH_RE.search(text)
    day_no = int(dm.group(1)) if dm else None
    match_no = int(dm.group(2)) if dm else None
    scheduled_at = _scheduled_at(soup, text, tournament_tz)
    team1, team2 = _title_teams(soup)

    # Fallback teams: focus only on content after the main heading to avoid navigation repetition.
    if not team1 or not team2:
        heading = _main_heading(soup)
        local = normalize(" ".join(heading.parent.stripped_strings)) if heading and heading.parent else text
        found = [t for t in TEAMS if t in local]
        if len(found) >= 2:
            team1, team2 = found[0], found[1]

    status = ""
    for phrase in ("Матч завершен", "Новый матч", "Перерыв"):
        if phrase.lower() in text.lower():
            status = phrase
            break
    if not status:
        # Keep a compact status fragment for diagnostics.
        live_m = re.search(r".{0,25}(?:период|матч идет|матч идёт).{0,40}", text, re.I)
        status = normalize(live_m.group(0)) if live_m else "UNKNOWN"

    period_scores: list[tuple[int, int]] = []
    pm = PERIOD_SCORE_RE.search(text)
    if pm:
        nums = list(map(int, pm.groups()))
        period_scores = [(nums[0], nums[1]), (nums[2], nums[3]), (nums[4], nums[5])]

    final_score = None
    title = normalize(soup.title.get_text(" ", strip=True) if soup.title else "")
    fm = re.search(r"\.\s*(\d+)\s*:\s*(\d+)\.\s*День", title)
    if fm:
        final_score = (int(fm.group(1)), int(fm.group(2)))

    events = _parse_events(soup)
    rosters = _parse_rosters(soup)
    live_period, live_elapsed, break_after = _detect_live_phase(soup, text)

    return GameSnapshot(
        game_id=game_id,
        url=url,
        day_no=day_no,
        match_no=match_no,
        scheduled_at=scheduled_at,
        team1=team1,
        team2=team2,
        status_text=status,
        final_score=final_score,
        period_scores=period_scores,
        events=events,
        rosters=rosters,
        live_period=live_period,
        live_elapsed_seconds=live_elapsed,
        break_after_period=break_after,
        raw_text=text,
    )
