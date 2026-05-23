"""Monte Carlo equity simulator.

Usage:
    from simulator import MonteCarloEquity
    from engine import Card

    hole = [Card.from_str('Ah'), Card.from_str('Kh')]
    board = [Card.from_str('Qh'), Card.from_str('Jh'), Card.from_str('2c')]
    result = MonteCarloEquity.simulate(hole, board, num_players=3, n=50000)
    print(result)  # {'win': 0.65, 'tie': 0.02, 'lose': 0.33, 'equity': 0.66, ...}
"""
from __future__ import annotations
import random
from typing import List, Optional, Dict
from engine.card import Card, Deck
from engine.hand_evaluator import HandEvaluator
from tqdm import tqdm


class MonteCarloEquity:
    @staticmethod
    def simulate(
        hero_hole: List[Card],
        board: List[Card],
        num_players: int = 2,
        n: int = 10000,
        opponent_range: Optional[List[List[Card]]] = None,
        verbose: bool = False,
    ) -> Dict[str, float]:
        """
        Run n Monte Carlo simulations to estimate hero equity.

        Args:
            hero_hole: Hero's 2 hole cards.
            board: 0-5 known community cards.
            num_players: Total players including hero.
            n: Number of simulations.
            opponent_range: Optional list of possible opponent hole card combos.
            verbose: Show progress bar.

        Returns:
            dict with 'win', 'tie', 'lose', 'equity', 'outs_estimate'
        """
        wins = ties = losses = 0
        known = set(hero_hole + board)
        remaining_board = 5 - len(board)
        iterator = tqdm(range(n), desc="Simulating", leave=False) if verbose else range(n)

        for _ in iterator:
            deck = Deck(exclude=list(known))
            random.shuffle(deck.cards)

            # Deal opponents
            opp_holes: List[List[Card]] = []
            available = list(deck.cards)
            random.shuffle(available)
            for _ in range(num_players - 1):
                if len(available) < 2:
                    break
                opp_hole = available[:2]
                available = available[2:]
                opp_holes.append(opp_hole)

            # Complete board
            sim_board = board + available[:remaining_board]

            # Evaluate
            hero_score = HandEvaluator.hand_rank_int(hero_hole, sim_board)
            opp_scores = [HandEvaluator.hand_rank_int(h, sim_board) for h in opp_holes]

            if not opp_scores:
                wins += 1
                continue

            best_opp = max(opp_scores)
            if hero_score > best_opp:
                wins += 1
            elif hero_score == best_opp:
                ties += 1
            else:
                losses += 1

        total = wins + ties + losses
        win_rate = wins / total
        tie_rate = ties / total
        equity = win_rate + tie_rate / 2

        return {
            'win': round(win_rate, 4),
            'tie': round(tie_rate, 4),
            'lose': round(losses / total, 4),
            'equity': round(equity, 4),
            'simulations': n,
            'num_players': num_players,
        }

    @staticmethod
    def compute_outs(hero_hole: List[Card], board: List[Card]) -> Dict[str, object]:
        """
        Estimate outs and probability of improving on next card.
        Uses equity delta between current street and next street.
        """
        current = MonteCarloEquity.simulate(hero_hole, board, num_players=2, n=5000)
        # Simulate one more card dealt
        outs_count = 0
        known = set(hero_hole + board)
        from engine.card import Card as C, RANKS, SUITS
        deck_remaining = [C(r, s) for r in range(13) for s in range(4) if C(r, s) not in known]

        improve_count = 0
        current_score = HandEvaluator.hand_rank_int(hero_hole, board) if len(board) >= 3 else 0

        for next_card in deck_remaining:
            new_board = board + [next_card]
            new_score = HandEvaluator.hand_rank_int(hero_hole, new_board)
            if new_score > current_score:
                improve_count += 1

        return {
            'current_equity': current['equity'],
            'outs_estimate': improve_count,
            'improvement_prob': round(improve_count / len(deck_remaining), 4) if deck_remaining else 0,
        }
