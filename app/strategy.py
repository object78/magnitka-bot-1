from __future__ import annotations

from .models import GameSnapshot, ScoredSignal, StrategyCandidate
from .storage import Storage

FOXES = "Хитрые Лисы"
SPARTANS = "Ледовые Спартанцы"
HEDGEHOGS = "Свирепые Ежи"
AXES = "Стальные Топоры"
SHOOTERS = "Меткие Стрелки"


def m3_candidate(game: GameSnapshot) -> StrategyCandidate | None:
    if game.match_no != 3 or not game.team1 or not game.team2:
        return None
    strong = SPARTANS in (game.team1, game.team2) or AXES in (game.team1, game.team2)
    return StrategyCandidate(
        strategy="M3-TB4.5",
        game_id=game.game_id,
        base_score=3 if strong else 2,
        base_reason="M3 + Спартанцы/Топоры" if strong else "M3 базовый",
        min_odds=1.50,
        market="ТБ4,5 за весь матч",
    )


def aplus_candidate(game: GameSnapshot, first_two_total: int | None) -> StrategyCandidate | None:
    if game.match_no not in (3, 5, 6) or not game.team1 or not game.team2:
        return None
    # Hard skip retained from the tested strategy.
    if first_two_total is not None and first_two_total > 10 and SPARTANS in (game.team1, game.team2):
        return None
    base = {3: 3, 5: 2, 6: 1}[game.match_no]
    reason = {3: "A+ №3 HIGH", 5: "A+ №5 NORMAL", 6: "A+ №6 RISK"}[game.match_no]
    return StrategyCandidate(
        strategy="A+ v4",
        game_id=game.game_id,
        base_score=base,
        base_reason=reason,
        min_odds=1.40,
        market="ТБ0,5 во 2-м периоде",
    )


def it_candidate(game: GameSnapshot) -> StrategyCandidate | None:
    if FOXES not in (game.team1, game.team2) or not game.team1 or not game.team2:
        return None
    p1 = game.p1_score()
    if p1 is None:
        return None
    foxes_goals = p1[0] if game.team1 == FOXES else p1[1]
    opp_goals = p1[1] if game.team1 == FOXES else p1[0]
    state = "lead" if foxes_goals > opp_goals else "trail" if foxes_goals < opp_goals else "draw"
    opp = game.team2 if game.team1 == FOXES else game.team1

    if opp == HEDGEHOGS:
        base, reason = 3, "IT vs Ежи CORE+"
    elif opp == AXES and state == "lead":
        base, reason = 3, "IT vs Топоры, Лисы ведут после 1П"
    elif opp == SPARTANS and state != "draw" and game.match_no != 2:
        base, reason = 1, "IT vs Спартанцы RISK"
    else:
        return None

    return StrategyCandidate(
        strategy="IT-L2 v5",
        game_id=game.game_id,
        base_score=base,
        base_reason=reason,
        min_odds=1.60,
        market="ИТБ0,5 Хитрых Лис во 2-м периоде",
        opponent=opp,
        foxes_state_after_p1=state,
    )


def _goalies(game: GameSnapshot, team: str) -> list[str]:
    return [p.name for p in game.rosters.get(team, []) if p.role.strip().lower().startswith("вр")]


def score_candidate(candidate: StrategyCandidate, game: GameSnapshot, storage: Storage) -> ScoredSignal:
    if candidate.strategy == "IT-L2 v5":
        teams = [candidate.opponent] if candidate.opponent else []
    else:
        teams = [t for t in (game.team1, game.team2) if t]

    proxies = []
    for team in teams:
        goalies = _goalies(game, team)
        proxy = storage.goalie_pair_proxy(goalies)
        if proxy is not None:
            proxies.append(proxy)
    gproxy = sum(proxies) / len(proxies) if proxies else None
    boost = gproxy is not None and gproxy >= 2.6
    score = min(4, candidate.base_score + (1 if boost else 0))
    tier = {1: "RISK", 2: "CORE", 3: "HIGH", 4: "ULTRA"}[score]
    return ScoredSignal(candidate=candidate, goalie_proxy=gproxy, gk_boost=boost, score=score, tier=tier)
