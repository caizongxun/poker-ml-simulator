"""PPO self-play RL training.

Usage:
    python -m training.train_rl --episodes 100000 --pretrained checkpoints/supervised_model.pt
"""
from __future__ import annotations
import argparse
import random
import numpy as np
from pathlib import Path
from tqdm import tqdm

from engine.card import Card, Deck
from engine.game_state import GameState, PlayerState, Street, Action
from features.feature_extractor import FeatureExtractor
from features.opponent_profile import OpponentProfileVector
from models.decision_model import DecisionModel
from models.rl_agent import PokerRLAgent
from simulator.mc_equity import MonteCarloEquity


def simulate_episode(
    agent: PokerRLAgent,
    opponent: DecisionModel,
    extractor: FeatureExtractor,
    num_players: int = 2,
    big_blind: float = 1.0,
    starting_stack: float = 100.0,
) -> float:
    """
    Simulate one hand. Returns chip reward for agent (hero).
    Simplified: hero vs 1 opponent, heads-up.
    """
    deck = Deck()
    hero_hole = deck.deal(2)
    opp_hole = deck.deal(2)
    board: list = []

    pot = big_blind * 1.5  # SB + BB
    hero_invested = big_blind
    opp_invested = big_blind * 0.5
    hero_stack = starting_stack - hero_invested
    opp_stack = starting_stack - opp_invested

    # Simplified single decision point per street
    total_reward = 0
    for street in [Street.PREFLOP, Street.FLOP, Street.TURN, Street.RIVER]:
        if street == Street.FLOP:
            board += deck.deal(3)
        elif street in [Street.TURN, Street.RIVER]:
            board += deck.deal(1)

        # Hero decides
        p_hero = PlayerState(0, hero_stack, hero_hole, total_invested=hero_invested)
        p_opp = PlayerState(1, opp_stack, opp_hole, total_invested=opp_invested)
        state = GameState(
            num_players=2, players=[p_hero, p_opp], board=board,
            street=street, pot=pot, current_bet=big_blind * 2 if street == Street.PREFLOP else 0,
            dealer_pos=1, current_player=0, big_blind=big_blind
        )
        opv = OpponentProfileVector.default().to_array()
        feat = extractor.extract(state, opv)
        action_idx, log_prob, value = agent.select_action(feat)

        # Resolve action
        bet_size = 0
        if action_idx == 0:  # fold
            agent.buffer.push(feat, action_idx, -hero_invested, log_prob, value, True)
            return -hero_invested
        elif action_idx == 1:  # call
            call_amt = min(state.current_bet - p_hero.bet, hero_stack)
            hero_stack -= call_amt
            pot += call_amt
            hero_invested += call_amt
            bet_size = call_amt
        elif action_idx in [2, 3]:  # raise small / large
            raise_size = pot * (0.5 if action_idx == 2 else 1.0)
            raise_size = min(raise_size, hero_stack)
            hero_stack -= raise_size
            pot += raise_size
            hero_invested += raise_size
            bet_size = raise_size
        elif action_idx == 4:  # all-in
            pot += hero_stack
            hero_invested += hero_stack
            hero_stack = 0

        # Intermediate reward: 0 (only terminal reward)
        agent.buffer.push(feat, action_idx, 0, log_prob, value, False)

    # Showdown
    from engine.hand_evaluator import HandEvaluator
    hero_score = HandEvaluator.hand_rank_int(hero_hole, board[:5])
    opp_score = HandEvaluator.hand_rank_int(opp_hole, board[:5])

    if hero_score > opp_score:
        reward = pot - hero_invested  # net winnings
    elif hero_score == opp_score:
        reward = pot / 2 - hero_invested
    else:
        reward = -hero_invested

    # Update last buffer entry with terminal reward
    if agent.buffer.rewards:
        agent.buffer.rewards[-1] = reward
        agent.buffer.dones[-1] = True

    return reward


def train_rl(
    episodes: int = 100000,
    pretrained: str = None,
    update_every: int = 256,
    snapshot_every: int = 5000,
    save_path: str = 'checkpoints/rl_model.pt',
):
    print("[RL Training] Loading models...")
    model = DecisionModel()
    if pretrained and Path(pretrained).exists():
        model = DecisionModel.load(pretrained)
        print(f"     Loaded pretrained: {pretrained}")

    agent = PokerRLAgent(model=model)
    extractor = FeatureExtractor(mc_sims=200)

    rewards_history = []
    running_reward = 0

    for ep in tqdm(range(episodes), desc="RL episodes"):
        opponent = agent.sample_opponent()
        reward = simulate_episode(agent, opponent, extractor)
        running_reward += reward
        rewards_history.append(reward)

        if (ep + 1) % update_every == 0:
            metrics = agent.update()
            avg = running_reward / update_every
            running_reward = 0
            if (ep + 1) % 5000 == 0:
                print(f"  Ep {ep+1} | AvgReward: {avg:.3f} | "
                      f"PolicyLoss: {metrics['policy_loss']:.4f} | "
                      f"Entropy: {metrics['entropy']:.4f}")

        if (ep + 1) % snapshot_every == 0:
            agent.snapshot_to_pool()

    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    agent.model.save(save_path)
    print(f"     Saved RL model to {save_path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--episodes', type=int, default=100000)
    parser.add_argument('--pretrained', type=str, default=None)
    parser.add_argument('--update_every', type=int, default=256)
    parser.add_argument('--save_path', type=str, default='checkpoints/rl_model.pt')
    args = parser.parse_args()
    train_rl(args.episodes, args.pretrained, args.update_every, args.save_path)
