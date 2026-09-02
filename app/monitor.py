from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .models import GameSnapshot, ScoredSignal
from .source import MagnitkaSource
from .storage import Storage
from .strategy import FOXES, aplus_candidate, it_candidate, m3_candidate, score_candidate
from .telegram import TelegramGateway

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Watch:
    strategy: str
    game_id: int
    target_seconds: int
    prep_sent: bool = False
    warning_sent: bool = False
    done: bool = False
    p2_detected_at_iso: str | None = None


class Monitor:
    def __init__(
        self,
        cfg: Config,
        source: MagnitkaSource,
        storage: Storage,
        telegram: TelegramGateway,
    ):
        self.cfg = cfg
        self.source = source
        self.storage = storage
        self.telegram = telegram
        self.games: dict[int, GameSnapshot] = {}
        self.last_schedule_refresh: datetime | None = None
        self.watches: dict[tuple[int, str], Watch] = {}
        self._status = "Запуск..."

    def now(self) -> datetime:
        return datetime.now(self.cfg.tournament_tz)

    def status_text(self) -> str:
        return self._status

    def today_text(self) -> str:
        now = self.now()
        snaps = sorted(
            [g for g in self.games.values() if g.scheduled_at and g.scheduled_at.date() == now.date()],
            key=lambda g: g.match_no or 99,
        )
        if not snaps:
            return f"📅 {now:%d.%m.%Y}: расписание пока не загружено."
        lines = [f"📅 Магнитка {now:%d.%m.%Y}"]
        for g in snaps:
            lines.append(f"№{g.match_no} {g.scheduled_at:%H:%M} — {g.team1} — {g.team2}")
        return "\n".join(lines)

    async def _notify_once(self, key: str, text: str) -> None:
        if self.storage.notified(key):
            return
        await self.telegram.broadcast(text)
        self.storage.mark_notified(key)

    async def refresh_schedule(self, force: bool = False) -> None:
        now = self.now()
        if (
            not force
            and self.last_schedule_refresh
            and (now - self.last_schedule_refresh).total_seconds() < self.cfg.schedule_refresh_seconds
        ):
            return
        snaps = await self.source.discover_games_for_date(now.date())
        for s in snaps:
            self.games[s.game_id] = s
        self.last_schedule_refresh = now
        if snaps:
            self._status = f"✅ Расписание загружено: {len(snaps)} матчей на {now:%d.%m.%Y}."
        else:
            self._status = f"⏳ На {now:%d.%m.%Y} дневные матчи не найдены."

    def _today_games(self) -> list[GameSnapshot]:
        today = self.now().date()
        return sorted(
            [g for g in self.games.values() if g.scheduled_at and g.scheduled_at.date() == today],
            key=lambda g: g.match_no or 99,
        )

    async def _refresh_relevant_games(self) -> None:
        now = self.now()
        for g in self._today_games():
            if not g.scheduled_at:
                continue
            # Fetch close-to-start, live, or not-yet-confirmed-finished games.
            delta = (now - g.scheduled_at).total_seconds()
            if -15 * 60 <= delta <= 90 * 60 and not (g.finished and delta > 45 * 60):
                try:
                    self.games[g.game_id] = await self.source.fetch_game(g.url)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Live fetch failed for %s: %s", g.game_id, exc)

    def _first_two_total(self) -> int | None:
        gs = {g.match_no: g for g in self._today_games()}
        if 1 not in gs or 2 not in gs:
            return None
        totals = [gs[i].total_regulation_goals() for i in (1, 2)]
        if any(x is None for x in totals):
            return None
        return int(totals[0] + totals[1])

    def _score(self, candidate, game: GameSnapshot) -> ScoredSignal:
        return score_candidate(candidate, game, self.storage)

    def _fmt_proxy(self, signal: ScoredSignal) -> str:
        return "нет данных" if signal.goalie_proxy is None else f"{signal.goalie_proxy:.2f}"

    async def _m3_pregame(self) -> None:
        now = self.now()
        for game in self._today_games():
            cand = m3_candidate(game)
            if not cand or not game.scheduled_at:
                continue
            secs = (game.scheduled_at - now).total_seconds()
            if not (0 <= secs <= self.cfg.pregame_m3_minutes * 60):
                continue
            sig = self._score(cand, game)
            first2 = self._first_two_total()
            context = ""
            if first2 is not None:
                context = f"\nПервые 2 матча: {first2} голов" + (" (≥10 TEST/BOOST)" if first2 >= 10 else "")
            await self._notify_once(
                f"{now.date()}:M3:{game.game_id}:pregame",
                f"🔴 M3 — {sig.tier}\n"
                f"№3 {game.scheduled_at:%H:%M} | {game.team1} — {game.team2}\n\n"
                f"СТАВКА: {cand.market}\nБрать от: {cand.min_odds:.2f}\n"
                f"База: {cand.base_reason}\nGK proxy: {self._fmt_proxy(sig)}"
                + (" → BOOST" if sig.gk_boost else "")
                + context,
            )

    def _period_elapsed(self, game: GameSnapshot, watch: Watch) -> int | None:
        if game.live_period != 2:
            return None
        if game.live_elapsed_seconds is not None:
            return game.live_elapsed_seconds
        now = self.now()
        key = f"p2_start:{game.game_id}"
        stored = self.storage.get_state(key)
        if stored:
            try:
                dt = datetime.fromisoformat(stored)
                return max(0, int((now - dt).total_seconds()))
            except Exception:  # noqa: BLE001
                pass
        # First observed P2 tick. Poll latency is bounded by LIVE_POLL_SECONDS.
        self.storage.set_state(key, now.isoformat())
        return 0

    async def _prep_period_watches(self) -> None:
        first2 = self._first_two_total()
        for game in self._today_games():
            # We want preparation during break after P1. If the site skips a visible break state,
            # the fallback is first detection of P2 and the message still arrives immediately.
            p2_just_started = game.live_period == 2 and (game.live_elapsed_seconds is None or game.live_elapsed_seconds <= 15)
            in_break = game.break_after_period == 1
            if not (in_break or p2_just_started):
                continue

            ac = aplus_candidate(game, first2)
            if ac:
                key = (game.game_id, ac.strategy)
                watch = self.watches.setdefault(key, Watch(ac.strategy, game.game_id, 4 * 60 + 50))
                if not watch.prep_sent:
                    sig = self._score(ac, game)
                    await self._notify_once(
                        f"{self.now().date()}:{game.game_id}:Aplus:prep",
                        f"🟡 A+ — ГОТОВИМСЯ | {sig.tier}\n"
                        f"{game.team1} — {game.team2}\n"
                        f"Все предварительные условия выполнены.\n\n"
                        f"Ждём 0:0 во 2П до 4:50.\n"
                        f"Если 0:0 сохранится → {ac.market} от {ac.min_odds:.2f}.\n"
                        f"GK proxy: {self._fmt_proxy(sig)}" + (" → BOOST" if sig.gk_boost else ""),
                    )
                    watch.prep_sent = True

            ic = it_candidate(game)
            if ic:
                key = (game.game_id, ic.strategy)
                watch = self.watches.setdefault(key, Watch(ic.strategy, game.game_id, 60))
                if not watch.prep_sent:
                    sig = self._score(ic, game)
                    await self._notify_once(
                        f"{self.now().date()}:{game.game_id}:IT:prep",
                        f"🟡 IT-L2 — ГОТОВИМСЯ | {sig.tier}\n"
                        f"{game.team1} — {game.team2}\n"
                        f"Условия по сопернику и счёту 1П выполнены.\n\n"
                        f"Ждём до 1:00 второго периода.\n"
                        f"Если Лисы ещё не забьют → {ic.market} от {ic.min_odds:.2f}.\n"
                        f"Важно: гол соперника не отменяет IT-L2.\n"
                        f"GK proxy соперника: {self._fmt_proxy(sig)}" + (" → BOOST" if sig.gk_boost else ""),
                    )
                    watch.prep_sent = True

    async def _run_watches(self) -> None:
        first2 = self._first_two_total()
        for key, watch in list(self.watches.items()):
            if watch.done:
                continue
            game = self.games.get(watch.game_id)
            if not game:
                continue
            elapsed = self._period_elapsed(game, watch)
            if elapsed is None:
                continue

            if watch.strategy == "A+ v4":
                cand = aplus_candidate(game, first2)
                if not cand:
                    watch.done = True
                    continue
                p2 = game.p2_score()
                if sum(p2) > 0 and elapsed < watch.target_seconds:
                    if self.cfg.send_cancel_messages:
                        await self._notify_once(
                            f"{self.now().date()}:{game.game_id}:Aplus:cancel",
                            f"⚪ A+ — ОТМЕНА\n{game.team1} — {game.team2}\n"
                            f"Во 2П уже есть гол ({p2[0]}:{p2[1]}) до точки 4:50. Ставки нет.",
                        )
                    watch.done = True
                    continue
                if (
                    not watch.warning_sent
                    and elapsed >= watch.target_seconds - self.cfg.target_warning_seconds
                    and sum(p2) == 0
                    and elapsed < watch.target_seconds
                ):
                    await self._notify_once(
                        f"{self.now().date()}:{game.game_id}:Aplus:warning",
                        f"🟠 A+ — {self.cfg.target_warning_seconds} сек до проверки\n"
                        f"2П пока 0:0. Подготовь рынок ТБ0,5 2П.",
                    )
                    watch.warning_sent = True
                if elapsed >= watch.target_seconds:
                    p2 = game.p2_score()
                    if sum(p2) == 0:
                        sig = self._score(cand, game)
                        await self._notify_once(
                            f"{self.now().date()}:{game.game_id}:Aplus:bet",
                            f"🚨 A+ — СТАВКА СЕЙЧАС | {sig.tier}\n"
                            f"{game.team1} — {game.team2}\n"
                            f"2П: ~4:50 | счёт периода 0:0\n\n"
                            f"➡️ {cand.market}\nБрать от: {cand.min_odds:.2f}\n"
                            f"GK proxy: {self._fmt_proxy(sig)}" + (" → BOOST" if sig.gk_boost else ""),
                        )
                    watch.done = True

            elif watch.strategy == "IT-L2 v5":
                cand = it_candidate(game)
                if not cand:
                    watch.done = True
                    continue
                fox_goals = game.p2_goals_for(FOXES)
                if fox_goals > 0 and elapsed < watch.target_seconds:
                    if self.cfg.send_cancel_messages:
                        await self._notify_once(
                            f"{self.now().date()}:{game.game_id}:IT:cancel",
                            f"⚪ IT-L2 — ОТМЕНА\n{game.team1} — {game.team2}\n"
                            f"Хитрые Лисы уже забили во 2П до 1:00. Ставки нет.",
                        )
                    watch.done = True
                    continue
                if (
                    not watch.warning_sent
                    and elapsed >= watch.target_seconds - self.cfg.target_warning_seconds
                    and fox_goals == 0
                    and elapsed < watch.target_seconds
                ):
                    await self._notify_once(
                        f"{self.now().date()}:{game.game_id}:IT:warning",
                        f"🟠 IT-L2 — {self.cfg.target_warning_seconds} сек до проверки\n"
                        f"Лисы пока без гола во 2П. Подготовь ИТБ0,5 Лис 2П.",
                    )
                    watch.warning_sent = True
                if elapsed >= watch.target_seconds:
                    if game.p2_goals_for(FOXES) == 0:
                        sig = self._score(cand, game)
                        await self._notify_once(
                            f"{self.now().date()}:{game.game_id}:IT:bet",
                            f"🚨 IT-L2 — СТАВКА СЕЙЧАС | {sig.tier}\n"
                            f"{game.team1} — {game.team2}\n"
                            f"2П: ~1:00 | Лисы: 0 голов в периоде\n\n"
                            f"➡️ {cand.market}\nБрать от: {cand.min_odds:.2f}\n"
                            f"GK proxy соперника: {self._fmt_proxy(sig)}" + (" → BOOST" if sig.gk_boost else ""),
                        )
                    watch.done = True

    async def _maybe_send_day_start(self) -> None:
        if not self.cfg.send_day_start_message:
            return
        gs = self._today_games()
        if not gs or not gs[0].scheduled_at:
            return
        now = self.now()
        first = gs[0].scheduled_at
        mins = (first - now).total_seconds() / 60
        if 0 <= mins <= self.cfg.start_monitor_minutes_before:
            await self._notify_once(
                f"{now.date()}:day-start",
                "🟢 Магнитка бот 1 начал мониторинг дня\n\n" + self.today_text(),
            )

    async def _update_goalie_history_if_day_finished(self) -> None:
        games = self._today_games()
        if len(games) < 6 or not all(g.finished for g in games):
            return
        day = self.now().date().isoformat()
        teams = sorted({t for g in games for t in (g.team1, g.team2) if t})
        for team in teams:
            if self.storage.processed_team_day(day, team):
                continue
            team_games = [g for g in games if team in (g.team1, g.team2)]
            if len(team_games) != 3:
                continue
            ga2 = ga = 0
            for g in team_games:
                p2 = g.p2_score()
                periods = g.period_scores[:3]
                if len(periods) < 3:
                    break
                if g.team1 == team:
                    ga2 += p2[1]
                    ga += sum(b for a, b in periods)
                else:
                    ga2 += p2[0]
                    ga += sum(a for a, b in periods)
            else:
                # Daily roster is normally stable; use the first game where roster is present.
                goalies = []
                for g in team_games:
                    plist = g.rosters.get(team, [])
                    goalies = [p.name for p in plist if p.role.lower().startswith("вр")]
                    if goalies:
                        break
                if goalies:
                    self.storage.add_team_day_goalie_result(day, team, goalies, ga2, ga)

    async def run(self) -> None:
        await self.refresh_schedule(force=True)
        while True:
            try:
                now = self.now()
                await self.refresh_schedule()
                games = self._today_games()
                if not games:
                    await asyncio.sleep(self.cfg.idle_poll_seconds)
                    continue

                first = games[0].scheduled_at
                last = games[-1].scheduled_at
                assert first and last
                active_from = first - timedelta(minutes=self.cfg.start_monitor_minutes_before)
                active_until = last + timedelta(minutes=90)
                if now < active_from or now > active_until:
                    self._status = f"💤 Ожидание. Окно мониторинга: {active_from:%H:%M}–{active_until:%H:%M} (время турнира)."
                    await asyncio.sleep(self.cfg.idle_poll_seconds)
                    continue

                await self._maybe_send_day_start()
                await self._refresh_relevant_games()
                await self._m3_pregame()
                await self._prep_period_watches()
                await self._run_watches()
                await self._update_goalie_history_if_day_finished()
                self._status = f"🟢 Мониторинг активен {now:%d.%m %H:%M:%S}; матчей сегодня: {len(games)}."
                await asyncio.sleep(self.cfg.live_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Monitor loop error: %s", exc)
                self._status = f"⚠️ Ошибка мониторинга: {exc}"
                await asyncio.sleep(10)
