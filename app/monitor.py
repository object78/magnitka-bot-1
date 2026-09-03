from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from .config import Config
from .models import GameSnapshot, ScoredSignal
from .source import MagnitkaSource
from .storage import Storage
from .strategy import FOXES, HEDGEHOGS, aplus_candidate, it_candidate, m3_candidate, score_candidate
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


class Monitor:
    def __init__(self, cfg: Config, source: MagnitkaSource, storage: Storage, telegram: TelegramGateway):
        self.cfg = cfg
        self.source = source
        self.storage = storage
        self.telegram = telegram
        self.games: dict[int, GameSnapshot] = {}
        self.last_schedule_refresh: datetime | None = None
        self.watches: dict[tuple[int, str], Watch] = {}
        self._status = "Запуск..."
        self._last_debug_signature: dict[int, str] = {}

    def now(self) -> datetime:
        return datetime.now(self.cfg.tournament_tz)

    def status_text(self) -> str:
        return self._status

    def today_text(self) -> str:
        now = self.now()
        snaps = self._today_games()
        if not snaps:
            return f"📅 {now:%d.%m.%Y}: расписание пока не загружено."
        lines = [f"📅 Магнитка {now:%d.%m.%Y}"]
        for g in snaps:
            lines.append(f"№{g.match_no} {g.scheduled_at:%H:%M} — {g.team1} — {g.team2}")
        return "\n".join(lines)

    def debug_text(self) -> str:
        now = self.now()
        lines = [f"🧪 LIVE debug {now:%d.%m %H:%M:%S} (Магнитогорск)"]
        for g in self._today_games():
            start, source = self._p2_start_info(g)
            p1 = g.p1_score()
            p2 = g.p2_score()
            start_txt = start.strftime("%H:%M:%S") if start else "?"
            lines.append(
                f"№{g.match_no} id={g.game_id} {g.team1}—{g.team2}\n"
                f" status={g.status_text}; liveP={g.live_period}; timer={g.live_elapsed_seconds}; "
                f"P1={p1}; P2={p2}; events={len(g.events)}; P2start≈{start_txt} [{source}]"
            )
        return "\n".join(lines)

    async def _notify_once(self, key: str, text: str) -> None:
        if self.storage.notified(key):
            return
        await self.telegram.broadcast(text)
        self.storage.mark_notified(key)

    async def _notify_bet_once(self, key: str, text: str, game: GameSnapshot, sig: ScoredSignal) -> None:
        if self.storage.notified(key):
            return
        # Statistics and ROI are shown only on explicit /stats request.
        await self.telegram.broadcast(text)
        self.storage.mark_notified(key)
        cand = sig.candidate
        self.storage.record_bet(
            event_key=key,
            strategy=cand.strategy,
            game_id=game.game_id,
            market=cand.market,
            min_odds=cand.min_odds,
            stake=self.cfg.flat_stake,
            tier=sig.tier,
            team1=game.team1,
            team2=game.team2,
            placed_at=self.now(),
        )

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
            old = self.games.get(s.game_id)
            # Do not overwrite a fresher live snapshot with a normal calendar-discovery fetch.
            if old is None or not old.scheduled_at or now < old.scheduled_at - timedelta(minutes=15) or old.finished:
                self.games[s.game_id] = s
        self.last_schedule_refresh = now
        self._status = (
            f"✅ Расписание загружено: {len(snaps)} матчей на {now:%d.%m.%Y}."
            if snaps
            else f"⏳ На {now:%d.%m.%Y} дневные матчи не найдены."
        )

    def _today_games(self) -> list[GameSnapshot]:
        today = self.now().date()
        return sorted(
            [g for g in self.games.values() if g.scheduled_at and g.scheduled_at.date() == today],
            key=lambda g: g.match_no or 99,
        )

    def _save_p2_anchor(self, game: GameSnapshot, start: datetime, source: str) -> None:
        if not game.scheduled_at:
            return
        offset = int((start - game.scheduled_at).total_seconds())
        if not (self.cfg.p2_offset_min_seconds <= offset <= self.cfg.p2_offset_max_seconds):
            return
        key = f"p2_start:{game.game_id}"
        current = self.storage.get_state(key)
        # Explicit page timers are strongest; otherwise keep the first good event anchor.
        if current and current.get("source") == "explicit-timer" and source != "explicit-timer":
            return
        self.storage.set_state(key, {"iso": start.isoformat(), "source": source, "offset": offset})
        day_key = f"p2_offsets:{game.scheduled_at.date().isoformat()}"
        samples = self.storage.get_state(day_key, {}) or {}
        samples[str(game.game_id)] = offset
        self.storage.set_state(day_key, samples)
        log.info("P2 anchor game=%s start=%s offset=%ss source=%s", game.game_id, start.isoformat(), offset, source)

    def _learn_live_timing(self, old: GameSnapshot | None, new: GameSnapshot, observed_at: datetime) -> None:
        if not new.scheduled_at:
            return
        if new.live_period == 2 and new.live_elapsed_seconds is not None:
            self._save_p2_anchor(new, observed_at - timedelta(seconds=new.live_elapsed_seconds), "explicit-timer")
            return
        if new.live_period == 2 and new.live_elapsed_seconds is None:
            if not self.storage.get_state(f"p2_start:{new.game_id}"):
                self._save_p2_anchor(new, observed_at, "explicit-period")

        old_sigs = {e.signature for e in old.events} if old else set()
        new_p2 = [
            e for e in new.events
            if e.period == 2 and e.is_goal and e.period_elapsed_seconds is not None and e.signature not in old_sigs
        ]
        if new_p2:
            # Use the newest just-appeared P2 event. With 5s polling this estimates start within a few seconds.
            ev = max(new_p2, key=lambda e: e.period_elapsed_seconds or 0)
            local = ev.period_elapsed_seconds or 0
            self._save_p2_anchor(new, observed_at - timedelta(seconds=local), "new-p2-event")

    def _p2_start_info(self, game: GameSnapshot) -> tuple[datetime | None, str]:
        stored = self.storage.get_state(f"p2_start:{game.game_id}")
        if stored:
            try:
                if isinstance(stored, str):
                    return datetime.fromisoformat(stored), "stored-legacy"
                return datetime.fromisoformat(stored["iso"]), str(stored.get("source", "stored"))
            except Exception:  # noqa: BLE001
                pass
        if not game.scheduled_at:
            return None, "none"
        day_key = f"p2_offsets:{game.scheduled_at.date().isoformat()}"
        samples = self.storage.get_state(day_key, {}) or {}
        offsets = [
            int(v) for gid, v in samples.items()
            if str(gid) != str(game.game_id)
            and self.cfg.p2_offset_min_seconds <= int(v) <= self.cfg.p2_offset_max_seconds
        ]
        if offsets:
            return game.scheduled_at + timedelta(seconds=int(median(offsets))), "day-calibration"
        return game.scheduled_at + timedelta(seconds=self.cfg.p2_default_offset_seconds), "default-estimate"

    async def _refresh_relevant_games(self) -> None:
        now = self.now()
        for g in self._today_games():
            if not g.scheduled_at:
                continue
            delta = (now - g.scheduled_at).total_seconds()
            if -15 * 60 <= delta <= 90 * 60 and not (g.finished and delta > 45 * 60):
                try:
                    old = self.games.get(g.game_id)
                    new = await self.source.fetch_game(g.url, fresh=True)
                    self._learn_live_timing(old, new, now)
                    self.games[g.game_id] = new
                    sig = f"{new.status_text}|{new.live_period}|{new.live_elapsed_seconds}|{new.p1_score()}|{new.p2_score()}|{len(new.events)}"
                    if self._last_debug_signature.get(new.game_id) != sig:
                        self._last_debug_signature[new.game_id] = sig
                        start, source = self._p2_start_info(new)
                        log.info(
                            "LIVE game=%s no=%s status=%s liveP=%s timer=%s p1=%s p2=%s events=%s p2start=%s source=%s",
                            new.game_id, new.match_no, new.status_text, new.live_period, new.live_elapsed_seconds,
                            new.p1_score(), new.p2_score(), len(new.events), start.isoformat() if start else None, source,
                        )
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
            await self._notify_bet_once(
                f"{now.date()}:M3:{game.game_id}:pregame",
                f"🔴 M3 — {sig.tier}\n"
                f"№3 {game.scheduled_at:%H:%M} | {game.team1} — {game.team2}\n\n"
                f"СТАВКА: {cand.market}\nБрать от: {cand.min_odds:.2f}\n"
                f"База: {cand.base_reason}\nGK proxy: {self._fmt_proxy(sig)}"
                + (" → BOOST" if sig.gk_boost else "") + context,
                game, sig,
            )

    def _period_elapsed(self, game: GameSnapshot) -> int | None:
        now = self.now()
        if game.live_period == 2 and game.live_elapsed_seconds is not None:
            return game.live_elapsed_seconds
        start, _ = self._p2_start_info(game)
        if start and now >= start:
            return max(0, int((now - start).total_seconds()))
        return None

    def _near_p2_start(self, game: GameSnapshot) -> bool:
        now = self.now()
        if game.break_after_period == 1:
            return True
        if game.live_period == 2 and (game.live_elapsed_seconds is None or game.live_elapsed_seconds <= 20):
            return True
        start, _ = self._p2_start_info(game)
        if not start:
            return False
        delta = (start - now).total_seconds()
        return -20 <= delta <= self.cfg.p2_prep_lead_seconds

    def _p1_complete_enough(self, game: GameSnapshot) -> bool:
        if game.break_after_period == 1 or game.live_period in (2, 3) or game.finished:
            return True
        start, source = self._p2_start_info(game)
        # An event/explicit anchor is enough. A pure default estimate is not trusted for Axes/Spartans P1-state rules.
        return bool(start and self.now() >= start and source not in {"default-estimate"})

    async def _prep_period_watches(self) -> None:
        first2 = self._first_two_total()
        for game in self._today_games():
            if not game.scheduled_at or self.now() < game.scheduled_at:
                continue
            near = self._near_p2_start(game)

            ac = aplus_candidate(game, first2)
            if ac:
                key = (game.game_id, ac.strategy)
                watch = self.watches.setdefault(key, Watch(ac.strategy, game.game_id, 4 * 60 + 50))
                if near and not watch.prep_sent:
                    sig = self._score(ac, game)
                    start, source = self._p2_start_info(game)
                    await self._notify_once(
                        f"{self.now().date()}:{game.game_id}:Aplus:prep",
                        f"🟡 A+ — ГОТОВИМСЯ | {sig.tier}\n"
                        f"{game.team1} — {game.team2}\n"
                        f"Все предварительные условия выполнены.\n\n"
                        f"Ждём 0:0 во 2П до 4:50.\n"
                        f"Если 0:0 сохранится → {ac.market} от {ac.min_odds:.2f}.\n"
                        f"GK proxy: {self._fmt_proxy(sig)}" + (" → BOOST" if sig.gk_boost else "")
                        + f"\nLIVE clock: {source}",
                    )
                    watch.prep_sent = True

            # Hedgehogs do not depend on the P1 score, so candidate may be known before the break.
            raw_ic = it_candidate(game, assume_p1_complete=self._p1_complete_enough(game))
            if raw_ic:
                # For Axes/Spartans do not act on a partial P1 score before the period is actually complete.
                if raw_ic.opponent != HEDGEHOGS and not self._p1_complete_enough(game):
                    continue
                key = (game.game_id, raw_ic.strategy)
                watch = self.watches.setdefault(key, Watch(raw_ic.strategy, game.game_id, 60))
                if near and not watch.prep_sent:
                    sig = self._score(raw_ic, game)
                    _, source = self._p2_start_info(game)
                    await self._notify_once(
                        f"{self.now().date()}:{game.game_id}:IT:prep",
                        f"🟡 IT-L2 — ГОТОВИМСЯ | {sig.tier}\n"
                        f"{game.team1} — {game.team2}\n"
                        f"Предварительные условия выполнены.\n\n"
                        f"Ждём до 1:00 второго периода.\n"
                        f"Если Лисы ещё не забьют → {raw_ic.market} от {raw_ic.min_odds:.2f}.\n"
                        f"Важно: гол соперника не отменяет IT-L2.\n"
                        f"GK proxy соперника: {self._fmt_proxy(sig)}" + (" → BOOST" if sig.gk_boost else "")
                        + f"\nLIVE clock: {source}",
                    )
                    watch.prep_sent = True

    async def _run_watches(self) -> None:
        first2 = self._first_two_total()
        for watch in list(self.watches.values()):
            if watch.done:
                continue
            game = self.games.get(watch.game_id)
            if not game:
                continue
            elapsed = self._period_elapsed(game)
            if elapsed is None:
                continue
            # If bot was restarted long after the decision point, never emit a stale betting signal.
            if elapsed > watch.target_seconds + 90:
                watch.done = True
                log.warning("Watch expired without live action game=%s strategy=%s elapsed=%ss", game.game_id, watch.strategy, elapsed)
                continue

            if watch.strategy == "A+ v4":
                cand = aplus_candidate(game, first2)
                if not cand:
                    watch.done = True
                    continue
                p2 = game.p2_score()
                # Exact event clock has priority for cancellation.
                early_p2_goal = any(
                    e.period == 2 and e.is_goal and e.period_elapsed_seconds is not None
                    and e.period_elapsed_seconds <= watch.target_seconds for e in game.events
                )
                if early_p2_goal or (sum(p2) > 0 and elapsed < watch.target_seconds):
                    if self.cfg.send_cancel_messages:
                        await self._notify_once(
                            f"{self.now().date()}:{game.game_id}:Aplus:cancel",
                            f"⚪ A+ — ОТМЕНА\n{game.team1} — {game.team2}\n"
                            f"Во 2П есть гол до точки 4:50. Ставки нет.",
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
                    if sum(game.p2_score()) == 0:
                        sig = self._score(cand, game)
                        _, source = self._p2_start_info(game)
                        await self._notify_bet_once(
                            f"{self.now().date()}:{game.game_id}:Aplus:bet",
                            f"🚨 A+ — СТАВКА СЕЙЧАС | {sig.tier}\n"
                            f"{game.team1} — {game.team2}\n"
                            f"2П: ~4:50 | счёт периода 0:0\n\n"
                            f"➡️ {cand.market}\nБрать от: {cand.min_odds:.2f}\n"
                            f"GK proxy: {self._fmt_proxy(sig)}" + (" → BOOST" if sig.gk_boost else "")
                            + f"\nLIVE clock: {source}",
                            game, sig,
                        )
                    watch.done = True

            elif watch.strategy == "IT-L2 v5":
                cand = it_candidate(game, assume_p1_complete=True)
                if not cand:
                    watch.done = True
                    continue
                early_fox_goal = any(
                    e.period == 2 and e.is_goal and e.team == FOXES
                    and e.period_elapsed_seconds is not None and e.period_elapsed_seconds <= watch.target_seconds
                    for e in game.events
                )
                fox_goals = game.p2_goals_for(FOXES)
                if early_fox_goal or (fox_goals > 0 and elapsed < watch.target_seconds):
                    if self.cfg.send_cancel_messages:
                        await self._notify_once(
                            f"{self.now().date()}:{game.game_id}:IT:cancel",
                            f"⚪ IT-L2 — ОТМЕНА\n{game.team1} — {game.team2}\n"
                            f"Хитрые Лисы забили во 2П до 1:00. Ставки нет.",
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
                        _, source = self._p2_start_info(game)
                        await self._notify_bet_once(
                            f"{self.now().date()}:{game.game_id}:IT:bet",
                            f"🚨 IT-L2 — СТАВКА СЕЙЧАС | {sig.tier}\n"
                            f"{game.team1} — {game.team2}\n"
                            f"2П: ~1:00 | Лисы: 0 голов в периоде\n\n"
                            f"➡️ {cand.market}\nБрать от: {cand.min_odds:.2f}\n"
                            f"GK proxy соперника: {self._fmt_proxy(sig)}" + (" → BOOST" if sig.gk_boost else "")
                            + f"\nLIVE clock: {source}",
                            game, sig,
                        )
                    watch.done = True

    def _p2_ended(self, game: GameSnapshot) -> bool:
        if game.finished or game.live_period == 3 or game.break_after_period == 2:
            return True
        if len(game.period_scores) >= 3:
            return True
        return any(e.period == 3 for e in game.events)

    def _result_message(self, bet, game: GameSnapshot | None) -> str:
        won = str(bet["result"]) == "W"
        icon = "✅" if won else "❌"
        title = "СТАВКА ПРОШЛА" if won else "СТАВКА НЕ ПРОШЛА"
        strategy = str(bet["strategy"])
        tier = str(bet["tier"] or "")
        teams = f"{bet['team1'] or '?'} — {bet['team2'] or '?'}"
        lines = [f"{icon} {title}", f"{strategy}" + (f" • {tier}" if tier else ""), teams, f"Рынок: {bet['market']}"]

        if game is not None:
            if strategy == "M3-TB4.5":
                total = game.total_regulation_goals()
                if game.final_score is not None:
                    lines.append(f"Итог матча: {game.final_score[0]}:{game.final_score[1]}")
                if total is not None:
                    lines.append(f"Тотал 3П: {total}")
            elif strategy in ("A+ v4", "IT-L2 v5"):
                p2 = game.p2_score()
                lines.append(f"Счёт 2П: {p2[0]}:{p2[1]}")

        # No money, win rate or ROI here. Those are available only through /stats.
        return "\n".join(lines)

    async def _notify_settled_results(self) -> None:
        for bet in self.storage.pending_result_notifications():
            game = self.games.get(int(bet["game_id"]))
            await self.telegram.broadcast(self._result_message(bet, game))
            self.storage.mark_result_notified(str(bet["event_key"]))

    async def _settle_open_bets(self) -> None:
        for bet in self.storage.open_bets():
            game = self.games.get(int(bet["game_id"]))
            if not game:
                continue
            strategy = str(bet["strategy"])
            if strategy == "M3-TB4.5":
                if not game.finished:
                    continue
                total = game.total_regulation_goals()
                if total is not None:
                    self.storage.settle_bet(str(bet["event_key"]), total >= 5, self.now())
            elif strategy == "A+ v4":
                if sum(game.p2_score()) > 0:
                    self.storage.settle_bet(str(bet["event_key"]), True, self.now())
                elif self._p2_ended(game):
                    self.storage.settle_bet(str(bet["event_key"]), False, self.now())
            elif strategy == "IT-L2 v5":
                if game.p2_goals_for(FOXES) > 0:
                    self.storage.settle_bet(str(bet["event_key"]), True, self.now())
                elif self._p2_ended(game):
                    self.storage.settle_bet(str(bet["event_key"]), False, self.now())

        # Results are delivered automatically, but performance/ROI remain request-only via /stats.
        await self._notify_settled_results()

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
                await self._settle_open_bets()
                await self._update_goalie_history_if_day_finished()
                self._status = f"🟢 Мониторинг активен {now:%d.%m %H:%M:%S}; матчей сегодня: {len(games)}. /debug — live диагностика."
                await asyncio.sleep(self.cfg.live_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Monitor loop error: %s", exc)
                self._status = f"⚠️ Ошибка мониторинга: {exc}"
                await asyncio.sleep(10)
