from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

TEAM_NAMES = (
    "Хитрые Лисы",
    "Ледовые Спартанцы",
    "Свирепые Ежи",
    "Стальные Топоры",
    "Меткие Стрелки",
)

CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
SCORE_RE = re.compile(r"(?<!\d)(\d+)\s*[:\-]\s*(\d+)(?!\d)")
PAIR_RE = re.compile(r"(\d+)\s*-\s*(\d+)")


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass(slots=True)
class PariLiveSnapshot:
    team1: str
    team2: str
    tournament_url: str
    event_url: str | None = None
    total_elapsed_seconds: int | None = None
    period: int | None = None
    period_elapsed_seconds: int | None = None
    score: tuple[int, int] | None = None
    period_scores: list[tuple[int, int]] | None = None
    raw_card_text: str = ""

    @property
    def p1_score(self) -> tuple[int, int] | None:
        return self.period_scores[0] if self.period_scores and len(self.period_scores) >= 1 else None

    @property
    def p2_score(self) -> tuple[int, int] | None:
        return self.period_scores[1] if self.period_scores and len(self.period_scores) >= 2 else None

    def oriented(self, team1: str | None, team2: str | None) -> "PariLiveSnapshot | None":
        if team1 == self.team1 and team2 == self.team2:
            return self
        if team1 == self.team2 and team2 == self.team1:
            def rev(x: tuple[int, int] | None) -> tuple[int, int] | None:
                return (x[1], x[0]) if x is not None else None
            return PariLiveSnapshot(
                team1=team1 or self.team2,
                team2=team2 or self.team1,
                tournament_url=self.tournament_url,
                event_url=self.event_url,
                total_elapsed_seconds=self.total_elapsed_seconds,
                period=self.period,
                period_elapsed_seconds=self.period_elapsed_seconds,
                score=rev(self.score),
                period_scores=[rev(x) for x in (self.period_scores or []) if rev(x) is not None],
                raw_card_text=self.raw_card_text,
            )
        return None


def _clock_to_period(sec: int | None) -> tuple[int | None, int | None]:
    if sec is None or sec < 0:
        return None, None
    # Magnitka 3x10. PARI displays accumulated match clock: 0:00..30:00.
    if sec < 10 * 60:
        return 1, sec
    if sec < 20 * 60:
        return 2, sec - 10 * 60
    if sec <= 31 * 60:
        return 3, sec - 20 * 60
    return None, None


def _extract_best_card(anchor: Tag) -> str:
    """Return the smallest ancestor that looks like one live event card.

    PARI changes CSS class names often, so this deliberately relies on semantic text.
    """
    best = _norm(anchor.get_text(" ", strip=True))
    node: Tag | None = anchor
    for _ in range(7):
        if not node or not isinstance(node.parent, Tag):
            break
        node = node.parent
        txt = _norm(node.get_text(" ", strip=True))
        if len(txt) > 2500:
            break
        has_clock = bool(CLOCK_RE.search(txt))
        has_score = bool(re.search(r"(?<!\d)\d+\s*:\s*\d+(?!\d)", txt))
        if has_clock and has_score:
            best = txt
            # Once we have enough context but still a compact card, stop.
            if len(txt) <= 700:
                break
    return best


def _parse_period_scores(card: str, total_sec: int | None, current_score: tuple[int, int] | None) -> list[tuple[int, int]]:
    if current_score is None or total_sec is None:
        return []
    period, _ = _clock_to_period(total_sec)
    # Period results are rendered with hyphens, e.g. (0-0 1-1). Only scan after current score.
    score_m = re.search(r"(?<!\d)\d+\s*:\s*\d+(?!\d)", card)
    tail = card[score_m.end():] if score_m else card
    pairs = [(int(a), int(b)) for a, b in PAIR_RE.findall(tail)]

    # Drop obvious duplicate summary suffixes while preserving order.
    cleaned: list[tuple[int, int]] = []
    for p in pairs:
        if len(cleaned) >= 3:
            break
        if cleaned and p == cleaned[-1] and "…" in tail:
            continue
        cleaned.append(p)

    a, b = current_score
    if period == 1:
        return [(a, b)]
    if period == 2:
        p1 = cleaned[0] if cleaned else None
        if p1:
            return [p1, (max(0, a - p1[0]), max(0, b - p1[1]))]
        return []
    if period == 3:
        if len(cleaned) >= 2:
            p1, p2 = cleaned[0], cleaned[1]
            p3 = (max(0, a - p1[0] - p2[0]), max(0, b - p1[1] - p2[1]))
            return [p1, p2, p3]
        return cleaned[:2]
    return cleaned[:3]


def parse_pari_tournament(html: str, tournament_url: str) -> list[PariLiveSnapshot]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[PariLiveSnapshot] = []
    seen: set[tuple[str, str]] = set()

    for a in soup.find_all("a", href=True):
        label = _norm(a.get_text(" ", strip=True)).replace("—", "-")
        teams = [t for t in TEAM_NAMES if t in label]
        if len(teams) != 2:
            continue
        # Preserve the order as rendered by PARI.
        pos = sorted(((label.find(t), t) for t in teams))
        team1, team2 = pos[0][1], pos[1][1]
        key = (team1, team2)
        if key in seen:
            continue

        card = _extract_best_card(a)
        # Remove leading tournament/market clutter before team name when possible.
        idx = card.find(team1)
        if idx >= 0:
            card = card[idx:]

        # Find first plausible accumulated hockey clock after the teams. Avoid '3x10'.
        clock = None
        for m in CLOCK_RE.finditer(card):
            mm, ss = int(m.group(1)), int(m.group(2))
            if ss < 60 and 0 <= mm <= 31:
                clock = mm * 60 + ss
                break
        period, local = _clock_to_period(clock)
        # We only need live cards. Sidebar/prematch duplicates often have the same team link but no clock.
        if clock is None or period is None:
            continue

        # Current score: first colon score after the clock.
        score = None
        search_from = 0
        if clock is not None:
            cm = CLOCK_RE.search(card)
            search_from = cm.end() if cm else 0
        sm = re.search(r"(?<!\d)(\d+)\s*:\s*(\d+)(?!\d)", card[search_from:])
        if sm:
            score = (int(sm.group(1)), int(sm.group(2)))
            score_abs_end = search_from + sm.end()
            score_prefix = card[:score_abs_end]
        else:
            score_prefix = card

        ps = _parse_period_scores(card, clock, score)
        out.append(
            PariLiveSnapshot(
                team1=team1,
                team2=team2,
                tournament_url=tournament_url,
                event_url=urljoin(tournament_url, a.get("href", "")) or None,
                total_elapsed_seconds=clock,
                period=period,
                period_elapsed_seconds=local,
                score=score,
                period_scores=ps,
                raw_card_text=card[:1200],
            )
        )
        seen.add(key)
    return out


def discover_tournament_url(html: str, base_url: str, day_no: int | None = None) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        txt = _norm(a.get_text(" ", strip=True))
        if "Магнитка Оупен" not in txt or "Дневной Турнир" not in txt:
            continue
        href = urljoin(base_url.rstrip("/") + "/", a["href"])
        m = re.search(r"Дневной Турнир\s*№\s*(\d+)", txt, re.I)
        num = int(m.group(1)) if m else -1
        if day_no is not None and num == day_no:
            return href
        candidates.append((num, href))
    if not candidates:
        return None
    # Usually the current tournament has the largest № in the live category list.
    candidates.sort(reverse=True)
    return candidates[0][1]


class PariLiveSource:
    def __init__(
        self,
        base_url: str = "https://pari.ru",
        discovery_path: str = "/live/hockey/country/russia",
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.discovery_url = urljoin(self.base_url + "/", discovery_path.lstrip("/"))
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        self.tournament_url: str | None = None
        self.last_error: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str) -> str:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self.client.get(url, params={"_mb": str(__import__("time").time_ns())})
                r.raise_for_status()
                return r.text
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
        assert last is not None
        raise last

    async def discover(self, day_no: int | None = None, force: bool = False) -> str | None:
        if self.tournament_url and not force:
            return self.tournament_url

        # Magnitka Open is listed by PARI under Hockey -> Russia, not Cyberhockey.
        # Keep the configured path first for compatibility, but always fall back to
        # the Russia live page so an old BotHost env value cannot silently disable live.
        discovery_urls: list[str] = [self.discovery_url]
        russia_url = urljoin(self.base_url + "/", "live/hockey/country/russia")
        if russia_url not in discovery_urls:
            discovery_urls.append(russia_url)

        for discovery_url in discovery_urls:
            try:
                html = await self._get(discovery_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("PARI discovery fetch failed for %s: %s", discovery_url, exc)
                continue
            url = discover_tournament_url(html, self.base_url, day_no=day_no)
            if url:
                self.tournament_url = url
                return url
        return None

    async def fetch_live(self, day_no: int | None = None) -> list[PariLiveSnapshot]:
        try:
            url = await self.discover(day_no=day_no)
            if not url:
                self.last_error = "турнир Магнитка Оупен не найден в PARI live"
                return []
            html = await self._get(url)
            rows = parse_pari_tournament(html, url)
            # If page turned stale/empty after a day change, rediscover once.
            if not rows:
                self.tournament_url = None
                url = await self.discover(day_no=day_no, force=True)
                if url:
                    html = await self._get(url)
                    rows = parse_pari_tournament(html, url)
            self.last_error = None
            return rows
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("PARI live fetch failed: %s", exc)
            return []
