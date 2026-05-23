"""PPO-based RL agent for self-play training.

突破性設計：
1. Dual-head Actor-Critic：actor 輸出混合策略，critic 估計 chip EV
2. 對手池（Opponent Pool）：每 N episodes 更新一個歷史版本的 opponent，
   避免 overfitting 當前 opponent（防止 cyclic best response）
3. KL 懲罰項：懲罰偏離 GTO baseline 太多（從 CFR pre-training 取得），
   控制 exploitative vs GTO 的 trade-off
"""
from __future__ import annotations
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from typing import List, Tuple, Dict, Optional
from .decision_model import DecisionModel


class RolloutBuffer:
    def __init__(self):
        self.states: List[np.ndarray] = []
        self.actions: List[int] = []
        self.rewards: List[float] = []
        self.log_probs: List[float] = []
        self.values: List[float] = []
        self.dones: List[bool] = []

    def push(self, state, action, reward, log_prob, value, done):
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.log_probs.append(log_prob)
        self.values.append(value)
        self.dones.append(done)

    def clear(self):
        self.__init__()

    def get_tensors(self, gamma=0.99, gae_lambda=0.95) -> Dict[str, torch.Tensor]:
        """Compute GAE advantages and returns."""
        rewards = np.array(self.rewards)
        values = np.array(self.values)
        dones = np.array(self.dones, dtype=np.float32)

        advantages = np.zeros_like(rewards)
        last_adv = 0
        for t in reversed(range(len(rewards))):
            next_val = values[t+1] if t+1 < len(values) else 0
            delta = rewards[t] + gamma * next_val * (1-dones[t]) - values[t]
            advantages[t] = last_adv = delta + gamma * gae_lambda * (1-dones[t]) * last_adv

        returns = advantages + values
        return {
            'states': torch.tensor(np.stack(self.states), dtype=torch.float32),
            'actions': torch.tensor(self.actions, dtype=torch.long),
            'old_log_probs': torch.tensor(self.log_probs, dtype=torch.float32),
            'advantages': torch.tensor(advantages, dtype=torch.float32),
            'returns': torch.tensor(returns, dtype=torch.float32),
        }


class PokerRLAgent:
    def __init__(
        self,
        model: Optional[DecisionModel] = None,
        lr: float = 3e-4,
        clip_eps: float = 0.2,
        entropy_coef: float = 0.01,
        value_coef: float = 0.5,
        kl_coef: float = 0.05,   # KL penalty weight vs GTO baseline
        gto_model: Optional[DecisionModel] = None,  # pre-trained GTO reference
    ):
        self.model = model or DecisionModel()
        self.optimizer = optim.Adam(self.model.parameters(), lr=lr)
        self.clip_eps = clip_eps
        self.entropy_coef = entropy_coef
        self.value_coef = value_coef
        self.kl_coef = kl_coef
        self.gto_model = gto_model
        self.buffer = RolloutBuffer()
        self.opponent_pool: List[DecisionModel] = []  # historical opponents
        self.opponent_pool_max = 10

    def select_action(self, features: np.ndarray) -> Tuple[int, float, float]:
        """Returns (action_idx, log_prob, value)."""
        self.model.eval()
        with torch.no_grad():
            x = torch.tensor(features, dtype=torch.float32).unsqueeze(0)
            action_probs, ev = self.model(x)
            probs = action_probs[0]
            dist = torch.distributions.Categorical(probs)
            action = dist.sample()
            log_prob = dist.log_prob(action)
        return int(action.item()), float(log_prob.item()), float(ev[0].item())

    def update(self, epochs: int = 4, batch_size: int = 64) -> Dict[str, float]:
        """PPO update step."""
        self.model.train()
        data = self.buffer.get_tensors()
        states = data['states']
        actions = data['actions']
        old_log_probs = data['old_log_probs']
        advantages = (data['advantages'] - data['advantages'].mean()) / (data['advantages'].std() + 1e-8)
        returns = data['returns']

        metrics = {'policy_loss': 0, 'value_loss': 0, 'entropy': 0, 'kl': 0}
        n = len(states)
        for _ in range(epochs):
            idx = torch.randperm(n)
            for start in range(0, n, batch_size):
                batch_idx = idx[start:start+batch_size]
                b_states = states[batch_idx]
                b_actions = actions[batch_idx]
                b_old_lp = old_log_probs[batch_idx]
                b_adv = advantages[batch_idx]
                b_ret = returns[batch_idx]

                action_probs, ev = self.model(b_states)
                dist = torch.distributions.Categorical(action_probs)
                log_probs = dist.log_prob(b_actions)
                entropy = dist.entropy().mean()

                ratio = torch.exp(log_probs - b_old_lp)
                surr1 = ratio * b_adv
                surr2 = torch.clamp(ratio, 1-self.clip_eps, 1+self.clip_eps) * b_adv
                policy_loss = -torch.min(surr1, surr2).mean()

                value_loss = nn.MSELoss()(ev.squeeze(), b_ret)

                # KL penalty vs GTO baseline
                kl_loss = torch.tensor(0.0)
                if self.gto_model is not None:
                    with torch.no_grad():
                        gto_probs, _ = self.gto_model(b_states)
                    kl_loss = torch.distributions.kl_divergence(
                        torch.distributions.Categorical(action_probs),
                        torch.distributions.Categorical(gto_probs)
                    ).mean()

                total_loss = (policy_loss
                              + self.value_coef * value_loss
                              - self.entropy_coef * entropy
                              + self.kl_coef * kl_loss)

                self.optimizer.zero_grad()
                total_loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), 0.5)
                self.optimizer.step()

                metrics['policy_loss'] += policy_loss.item()
                metrics['value_loss'] += value_loss.item()
                metrics['entropy'] += entropy.item()
                metrics['kl'] += kl_loss.item()

        self.buffer.clear()
        return {k: v / epochs for k, v in metrics.items()}

    def snapshot_to_pool(self):
        """Save current model to opponent pool for future self-play."""
        import copy
        if len(self.opponent_pool) >= self.opponent_pool_max:
            self.opponent_pool.pop(0)
        self.opponent_pool.append(copy.deepcopy(self.model))

    def sample_opponent(self) -> DecisionModel:
        """Sample a random opponent from pool (or current model if pool empty)."""
        if not self.opponent_pool:
            return self.model
        return np.random.choice(self.opponent_pool)
