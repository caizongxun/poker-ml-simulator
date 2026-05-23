"""Supervised decision model.

輸入: 39-dim 特徵向量 (FeatureExtractor 輸出)
輸出: 混合策略機率 [fold_prob, call_prob, raise_small_prob, raise_large_prob, all_in_prob]

突破性設計：輸出混合策略（不是 argmax），讓模型在生產環境中做隨機化決策，
防止被對手利用（防 exploit）。
"""
from __future__ import annotations
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Tuple, Dict


class ResidualBlock(nn.Module):
    def __init__(self, dim: int, dropout: float = 0.1):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim),
            nn.LayerNorm(dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(dim, dim),
            nn.LayerNorm(dim),
        )
        self.act = nn.GELU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.act(x + self.net(x))


class DecisionModel(nn.Module):
    """
    MLP with residual blocks.
    Input:  [batch, 39] feature vector
    Output: [batch, 5]  mixed strategy (softmax probabilities)
            [batch, 1]  EV estimate
    """
    NUM_ACTIONS = 5  # fold, check/call, raise_small, raise_large, all_in

    def __init__(self, input_dim: int = 39, hidden_dim: int = 256, num_blocks: int = 4,
                 dropout: float = 0.1):
        super().__init__()
        self.embed = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
        )
        self.blocks = nn.ModuleList(
            [ResidualBlock(hidden_dim, dropout) for _ in range(num_blocks)]
        )
        # Action head: mixed strategy distribution
        self.action_head = nn.Linear(hidden_dim, self.NUM_ACTIONS)
        # Value head: EV estimate
        self.value_head = nn.Sequential(
            nn.Linear(hidden_dim, 64),
            nn.GELU(),
            nn.Linear(64, 1)
        )

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Returns:
            action_probs: [batch, 5] softmax probabilities
            ev_estimate:  [batch, 1] expected value
        """
        h = self.embed(x)
        for block in self.blocks:
            h = block(h)
        action_logits = self.action_head(h)
        action_probs = F.softmax(action_logits, dim=-1)
        ev = self.value_head(h)
        return action_probs, ev

    def decide(
        self,
        features: np.ndarray,
        temperature: float = 1.0,
        greedy: bool = False
    ) -> Dict[str, object]:
        """
        Make a decision from a feature vector.

        Args:
            features: (39,) numpy array
            temperature: Controls randomness (1=training, 0.1=exploitation)
            greedy: If True, always pick the highest prob action

        Returns:
            dict with 'action', 'action_name', 'probs', 'ev'
        """
        self.eval()
        with torch.no_grad():
            x = torch.tensor(features, dtype=torch.float32).unsqueeze(0)
            action_probs, ev = self(x)
            probs = action_probs[0].cpu().numpy()

            if temperature != 1.0:
                # Temperature scaling
                logits = np.log(probs + 1e-8) / temperature
                probs = np.exp(logits - np.max(logits))
                probs /= probs.sum()

            if greedy:
                action_idx = int(np.argmax(probs))
            else:
                action_idx = int(np.random.choice(len(probs), p=probs))

        names = ['FOLD', 'CHECK/CALL', 'RAISE_SMALL', 'RAISE_LARGE', 'ALL_IN']
        return {
            'action': action_idx,
            'action_name': names[action_idx],
            'probs': {names[i]: round(float(p), 4) for i, p in enumerate(probs)},
            'ev': round(float(ev[0].item()), 4)
        }

    def save(self, path: str):
        torch.save(self.state_dict(), path)

    @classmethod
    def load(cls, path: str, **kwargs) -> 'DecisionModel':
        model = cls(**kwargs)
        model.load_state_dict(torch.load(path, map_location='cpu'))
        return model
