"""Evaluate a trained model by running it against baselines.

Usage:
    python -m eval.evaluate --model checkpoints/rl_model.pt --games 10000
"""
from __future__ import annotations
import argparse
import random
import numpy as np
from tqdm import tqdm
from pathlib import Path
from engine.card import Deck
from engine.game_state import GameState, PlayerState, Street
from engine.hand_evaluator import HandEvaluator
from features.feature_extractor import FeatureExtractor
from features.opponent_profile import OpponentProfileVector
from models.decision_model import DecisionModel


class CallStation:
    """Baseline: always call/check."""
    def decide(self, *args, **kwargs): return {'action': 1, 'action_name': 'CALL', 'probs': {}, 'ev': 0}


class RandomAgent:
    """Baseline: random action."""
    def decide(self, *args, **kwargs):
        a = random.randint(0, 4)
        return {'action': a, 'action_name': str(a), 'probs': {}, 'ev': 0}


def heads_up_showdown(model_path: str, games: int = 10000):
    model = DecisionModel.load(model_path)
    extractor = FeatureExtractor(mc_sims=100)
    baselines = {'CallStation': CallStation(), 'Random': RandomAgent()}
    results = {}

    for name, baseline in baselines.items():
        hero_profits = []
        for _ in tqdm(range(games), desc=f"vs {name}", leave=False):
            deck = Deck()
            hero_hole = deck.deal(2)
            opp_hole = deck.deal(2)
            board = deck.deal(5)

            # Quick equity
            eq = MonteCarloEquity.simulate(hero_hole, board[:3], num_players=2, n=200)['equity']

            # Hero action
            p = PlayerState(0, 100, hero_hole, bet=1, total_invested=1)
            state = GameState(
                num_players=2, players=[p, PlayerState(1, 100, opp_hole)],
                board=board[:3], street=Street.FLOP, pot=3, current_bet=1,
                dealer_pos=1, current_player=0
            )
            feat = extractor.extract(state, OpponentProfileVector.default().to_array())
            decision = model.decide(feat, temperature=0.5)
            action = decision['action']

            # Simplified payoff
            hero_score = HandEvaluator.hand_rank_int(hero_hole, board)
            opp_score = HandEvaluator.hand_rank_int(opp_hole, board)

            if action == 0:  # fold
                profit = -1
            elif hero_score > opp_score:
                profit = 3 if action >= 2 else 2
            elif hero_score == opp_score:
                profit = 0
            else:
                profit = -2 if action >= 2 else -1

            hero_profits.append(profit)

        arr = np.array(hero_profits)
        results[name] = {
            'win_rate_bb100': round(arr.mean() * 100 / 2, 2),
            'avg_profit': round(arr.mean(), 4),
            'std': round(arr.std(), 4),
        }
        print(f"  vs {name}: {results[name]}")

    return results


if __name__ == '__main__':
    from simulator.mc_equity import MonteCarloEquity
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, required=True)
    parser.add_argument('--games', type=int, default=10000)
    args = parser.parse_args()
    heads_up_showdown(args.model, args.games)
