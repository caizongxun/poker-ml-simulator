"""PPO self-play RL training with checkpoint/resume support.

Usage:
    python -m training.train_rl --episodes 100000 --pretrained checkpoints/supervised_model.pt
    python -m training.train_rl --episodes 100000 --ckpt_dir checkpoints  # resume
    python -m training.train_rl --episodes 100000 --reset                 # restart
"""
from __future__ import annotations
import argparse
import random
import json
import numpy as np
from pathlib import Path
from tqdm import tqdm
import torch

from engine.card import Deck
from engine.game_state import GameState, PlayerState, Street
from features.feature_extractor import FeatureExtractor
from features.opponent_profile import OpponentProfileVector
from models.decision_model import DecisionModel
from models.rl_agent import PokerRLAgent


def save_rl_checkpoint(ckpt_dir: Path, episode: int, agent: PokerRLAgent, rewards_history: list):
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    path = ckpt_dir / f'rl_ep{episode:07d}.pt'
    torch.save({
        'episode': episode,
        'model_state_dict': agent.model.state_dict(),
        'optimizer_state_dict': agent.optimizer.state_dict(),
        'opponent_pool': [m.state_dict() for m in agent.opponent_pool],
        'rewards_tail': rewards_history[-1000:],  # 只存最後 1000 筆
    }, path)
    info = {'episode': episode, 'path': str(path),
            'avg_reward_last1k': float(np.mean(rewards_history[-1000:])) if rewards_history else 0.0}
    (ckpt_dir / 'rl_latest.json').write_text(json.dumps(info, indent=2))
    # 保留最近 3 個
    for old in sorted(ckpt_dir.glob('rl_ep*.pt'))[:-3]:
        old.unlink()
    return path


def load_rl_checkpoint(ckpt_dir: Path, agent: PokerRLAgent):
    latest = ckpt_dir / 'rl_latest.json'
    if not latest.exists():
        return 0, []
    info = json.loads(latest.read_text())
    path = Path(info['path'])
    if not path.exists():
        return 0, []
    print(f"  [resume] Loading RL checkpoint: {path.name}")
    ckpt = torch.load(path, map_location='cpu')
    agent.model.load_state_dict(ckpt['model_state_dict'])
    agent.optimizer.load_state_dict(ckpt['optimizer_state_dict'])
    # Restore opponent pool
    import copy
    for sd in ckpt.get('opponent_pool', []):
        m = copy.deepcopy(agent.model)
        m.load_state_dict(sd)
        agent.opponent_pool.append(m)
    start_ep = ckpt['episode'] + 1
    rewards = ckpt.get('rewards_tail', [])
    print(f"  [resume] Resuming from episode {start_ep}")
    return start_ep, rewards


def simulate_episode(agent, opponent, extractor, big_blind=1.0, starting_stack=100.0):
    from engine.hand_evaluator import HandEvaluator
    deck = Deck()
    hero_hole = deck.deal(2)
    opp_hole  = deck.deal(2)
    board: list = []
    pot = big_blind * 1.5
    hero_invested = big_blind
    opp_invested  = big_blind * 0.5
    hero_stack    = starting_stack - hero_invested

    for street in [Street.PREFLOP, Street.FLOP, Street.TURN, Street.RIVER]:
        if   street == Street.FLOP:  board += deck.deal(3)
        elif street in (Street.TURN, Street.RIVER): board += deck.deal(1)

        p_hero = PlayerState(0, hero_stack, hero_hole, total_invested=hero_invested)
        p_opp  = PlayerState(1, starting_stack - opp_invested, opp_hole, total_invested=opp_invested)
        state  = GameState(
            num_players=2, players=[p_hero, p_opp], board=board,
            street=street, pot=pot,
            current_bet=big_blind*2 if street == Street.PREFLOP else 0,
            dealer_pos=1, current_player=0, big_blind=big_blind
        )
        feat = extractor.extract(state, OpponentProfileVector.default().to_array())
        action_idx, log_prob, value = agent.select_action(feat)

        if action_idx == 0:  # fold
            agent.buffer.push(feat, action_idx, -hero_invested, log_prob, value, True)
            return -hero_invested
        elif action_idx == 1:
            call = min(state.current_bet - p_hero.bet, hero_stack)
            hero_stack -= call; pot += call; hero_invested += call
        elif action_idx in (2, 3):
            rs = min(pot * (0.5 if action_idx==2 else 1.0), hero_stack)
            hero_stack -= rs; pot += rs; hero_invested += rs
        else:
            pot += hero_stack; hero_invested += hero_stack; hero_stack = 0

        agent.buffer.push(feat, action_idx, 0, log_prob, value, False)

    hero_score = HandEvaluator.hand_rank_int(hero_hole, board[:5])
    opp_score  = HandEvaluator.hand_rank_int(opp_hole,  board[:5])
    if   hero_score > opp_score: reward = pot - hero_invested
    elif hero_score == opp_score: reward = pot/2 - hero_invested
    else:                         reward = -hero_invested

    if agent.buffer.rewards:
        agent.buffer.rewards[-1] = reward
        agent.buffer.dones[-1]   = True
    return reward


def train_rl(
    episodes=100000, pretrained=None, update_every=256,
    snapshot_every=5000, ckpt_every=2000,
    ckpt_dir='checkpoints', save_path='checkpoints/rl_model.pt',
    reset=False,
):
    ckpt_dir = Path(ckpt_dir)

    model = DecisionModel()
    if pretrained and Path(pretrained).exists():
        model = DecisionModel.load(pretrained)
        print(f"  Loaded pretrained: {pretrained}")

    agent    = PokerRLAgent(model=model)
    extractor = FeatureExtractor(mc_sims=200)
    rewards_history = []
    start_ep = 0

    if not reset:
        start_ep, rewards_history = load_rl_checkpoint(ckpt_dir, agent)

    print(f"  RL training: episode {start_ep+1} → {episodes}")
    running_reward = 0

    for ep in tqdm(range(start_ep, episodes), desc="RL episodes", initial=start_ep, total=episodes):
        opponent = agent.sample_opponent()
        reward   = simulate_episode(agent, opponent, extractor)
        running_reward  += reward
        rewards_history.append(reward)

        if (ep + 1) % update_every == 0:
            metrics = agent.update()
            avg = running_reward / update_every
            running_reward = 0
            if (ep + 1) % 5000 == 0:
                print(f"  Ep {ep+1} | AvgReward: {avg:.3f} | "
                      f"PolicyLoss: {metrics['policy_loss']:.4f} | Entropy: {metrics['entropy']:.4f}")

        if (ep + 1) % snapshot_every == 0:
            agent.snapshot_to_pool()

        if (ep + 1) % ckpt_every == 0:
            p = save_rl_checkpoint(ckpt_dir, ep, agent, rewards_history)
            tqdm.write(f"  [ckpt] {p.name}")

    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    agent.model.save(save_path)
    print(f"  Saved RL model → {save_path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--episodes',    type=int,  default=100000)
    parser.add_argument('--pretrained',  type=str,  default=None)
    parser.add_argument('--update_every',type=int,  default=256)
    parser.add_argument('--ckpt_every',  type=int,  default=2000)
    parser.add_argument('--ckpt_dir',    type=str,  default='checkpoints')
    parser.add_argument('--save_path',   type=str,  default='checkpoints/rl_model.pt')
    parser.add_argument('--reset',       action='store_true')
    args = parser.parse_args()
    train_rl(
        episodes=args.episodes, pretrained=args.pretrained,
        update_every=args.update_every, ckpt_every=args.ckpt_every,
        ckpt_dir=args.ckpt_dir, save_path=args.save_path, reset=args.reset
    )
