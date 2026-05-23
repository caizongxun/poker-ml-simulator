"""Feature engineering for the ML decision model.

Feature vector (39 dims):
    [0:4]   Street one-hot (preflop/flop/turn/river)
    [4]     Pot odds (0-1)
    [5]     Equity estimate (0-1, from MC sim)
    [6]     Stack depth in BBs (normalized)
    [7]     Position (0=BB, 1=SB, 2=BTN, ... normalized)
    [8]     Pot size normalized by BB
    [9]     Bet size relative to pot
    [10]    Num active players (normalized)
    [11]    SPR (Stack-to-Pot Ratio)
    [12]    Fold equity estimate
    [13:17] Board texture (flush draw possible, straight draw, paired, high card)
    [17:33] OPV (Opponent Profile Vector, 16 dims) - see opponent_profile.py
    [33:39] Hand history features (num bets this street, etc.)
"""
from __future__ import annotations
import numpy as np
from typing import List, Optional
from engine.game_state import GameState, Street
from engine.card import Card
from simulator.mc_equity import MonteCarloEquity
from .opponent_profile import OpponentProfileVector


class FeatureExtractor:
    def __init__(self, mc_sims: int = 500):
        """mc_sims: MC simulations for real-time equity estimate (lower = faster)."""
        self.mc_sims = mc_sims

    def extract(self, state: GameState, opponent_opv: Optional[np.ndarray] = None) -> np.ndarray:
        """Extract full feature vector from a GameState."""
        feat = np.zeros(39, dtype=np.float32)
        hero = state.players[state.current_player]

        # Street one-hot
        feat[int(state.street)] = 1.0

        # Pot odds
        feat[4] = state.pot_odds

        # Equity (MC)
        if len(state.board) >= 3:
            eq_result = MonteCarloEquity.simulate(
                hero.hole_cards, state.board,
                num_players=len(state.active_players),
                n=self.mc_sims
            )
            feat[5] = eq_result['equity']
        elif state.street == Street.PREFLOP and hero.hole_cards:
            # Preflop equity approximation (Chen formula proxy)
            feat[5] = self._preflop_equity_proxy(hero.hole_cards, len(state.active_players))

        # Stack / position / pot
        feat[6] = min(state.stack_depth / 200, 1.0)  # normalize to 200bb
        feat[7] = state.to_feature_dict()['position'] / max(state.num_players - 1, 1)
        feat[8] = min(state.pot / (state.big_blind * 100), 1.0)
        feat[9] = state.to_feature_dict()['bet_ratio']
        feat[10] = len(state.active_players) / state.num_players
        spr = hero.stack / state.pot if state.pot > 0 else 10
        feat[11] = min(spr / 20, 1.0)

        # Fold equity (simplified: position * stack_pressure)
        feat[12] = feat[7] * min(1.0, 1 / (spr + 0.1))

        # Board texture
        if len(state.board) >= 3:
            feat[13:17] = self._board_texture(state.board)

        # Opponent Profile Vector (16 dims)
        if opponent_opv is not None:
            feat[17:33] = opponent_opv[:16]
        else:
            feat[17:33] = OpponentProfileVector.default().to_array()

        # Hand history features
        feat[33:39] = self._history_features(state)

        return feat

    def _preflop_equity_proxy(self, hole: List[Card], num_players: int) -> float:
        """Chen formula approximation for preflop hand strength."""
        r1, r2 = sorted([c.rank for c in hole], reverse=True)
        score = max(r1, r2) / 2  # base rank score
        if r1 == r2:  # pocket pair
            score = max(score * 2, 5)
        suited = hole[0].suit == hole[1].suit
        gap = r1 - r2
        if suited:
            score += 2
        if gap == 0:
            score += 0
        elif gap == 1:
            score += 1
        elif gap == 2:
            score -= 1
        else:
            score -= 2
        # Normalize to 0-1, adjust for num players
        raw = min(score / 20, 1.0)
        return raw ** (num_players - 1)

    def _board_texture(self, board: List[Card]) -> np.ndarray:
        """4-dim board texture vector."""
        suits = [c.suit for c in board]
        ranks = [c.rank for c in board]
        from collections import Counter
        suit_cnt = Counter(suits)
        rank_cnt = Counter(ranks)

        flush_draw = float(max(suit_cnt.values()) >= 3)
        # Straight draw: any 3+ consecutive ranks
        sorted_ranks = sorted(set(ranks))
        max_consec = 1
        cur = 1
        for i in range(1, len(sorted_ranks)):
            if sorted_ranks[i] - sorted_ranks[i-1] == 1:
                cur += 1
                max_consec = max(max_consec, cur)
            else:
                cur = 1
        straight_draw = float(max_consec >= 3)
        paired = float(max(rank_cnt.values()) >= 2)
        high_card = board[-1].rank / 12.0  # highest card normalized

        return np.array([flush_draw, straight_draw, paired, high_card], dtype=np.float32)

    def _history_features(self, state: GameState) -> np.ndarray:
        """6-dim hand history aggregation."""
        hist = state.hand_history
        if not hist:
            return np.zeros(6, dtype=np.float32)
        from engine.game_state import Action
        cur_street_acts = [h for h in hist if h.get('street') == int(state.street)]
        bets = sum(1 for a in cur_street_acts if a.get('action') in [Action.RAISE, Action.ALL_IN])
        calls = sum(1 for a in cur_street_acts if a.get('action') == Action.CALL)
        folds = sum(1 for a in cur_street_acts if a.get('action') == Action.FOLD)
        total_raised = sum(a.get('amount', 0) for a in cur_street_acts if a.get('action') == Action.RAISE)
        total_acts = len(cur_street_acts)
        return np.array([
            min(bets / 3, 1.0),
            min(calls / 3, 1.0),
            min(folds / 5, 1.0),
            min(total_raised / (state.pot + 1), 1.0),
            min(total_acts / 10, 1.0),
            float(len(hist) > 0)
        ], dtype=np.float32)
