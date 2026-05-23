"""Supervised training from MC-simulated labeled data.

資料生成策略：
1. 隨機生成大量手牌局面（street, hole cards, board, num_players, position）
2. 用 Monte Carlo 計算 equity
3. 根據 pot odds + equity + position 自動標記「最佳動作」
4. 訓練 DecisionModel 擬合這個 oracle

Usage:
    python -m training.train_supervised --epochs 50 --samples 100000
"""
from __future__ import annotations
import argparse
import random
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from pathlib import Path
from tqdm import tqdm

from engine.card import Card, Deck
from engine.game_state import GameState, PlayerState, Street, Action
from features.feature_extractor import FeatureExtractor
from features.opponent_profile import OpponentProfileVector
from simulator.mc_equity import MonteCarloEquity
from models.decision_model import DecisionModel


def generate_sample(
    extractor: FeatureExtractor,
    num_players: int,
    mc_sims: int = 300,
) -> tuple:
    """Generate one (feature_vector, action_label, ev) tuple."""
    # Random hand setup
    deck = Deck()
    hole = deck.deal(2)
    street_val = random.choice([Street.PREFLOP, Street.FLOP, Street.TURN, Street.RIVER])
    board_size = {Street.PREFLOP: 0, Street.FLOP: 3, Street.TURN: 4, Street.RIVER: 5}[street_val]
    board = deck.deal(board_size)

    pot = random.uniform(1, 50)
    current_bet = random.uniform(0, pot * 1.5)
    stack = random.uniform(10, 200)
    position = random.randint(0, num_players - 1)
    bb = 1.0

    players = []
    for i in range(num_players):
        p = PlayerState(
            player_id=i,
            stack=stack if i == 0 else random.uniform(10, 200),
            hole_cards=hole if i == 0 else [],
            bet=current_bet if i == 0 else 0,
            total_invested=random.uniform(0, pot / 2)
        )
        players.append(p)

    state = GameState(
        num_players=num_players,
        players=players,
        board=board,
        street=street_val,
        pot=pot,
        current_bet=current_bet,
        dealer_pos=0,
        current_player=0,
        big_blind=bb
    )

    # Equity
    if board_size >= 3:
        eq = MonteCarloEquity.simulate(hole, board, num_players, n=mc_sims)['equity']
    else:
        eq = extractor._preflop_equity_proxy(hole, num_players)

    # Oracle label: simplified EV-based decision
    pot_odds = state.pot_odds
    call_amount = max(current_bet - players[0].bet, 0)
    spr = stack / max(pot, 1)

    # EV of calling
    ev_call = eq * (pot + call_amount) - (1 - eq) * call_amount if call_amount > 0 else 0
    ev_fold = 0
    ev_raise = (eq * (pot * 2) - (1 - eq) * pot * 0.75) * (1 + position / num_players * 0.2)

    if eq < pot_odds - 0.05:  # clear fold
        label = 0  # FOLD
    elif eq > 0.75 and spr < 5:  # strong hand, low SPR -> shove
        label = 4  # ALL_IN
    elif eq > 0.65 and position >= num_players // 2:  # strong + position -> big raise
        label = 3  # RAISE_LARGE
    elif eq > pot_odds + 0.15:  # good equity + fold equity -> small raise
        label = 2  # RAISE_SMALL
    else:  # marginal call
        label = 1  # CALL

    opv = OpponentProfileVector.default().to_array()
    features = extractor.extract(state, opv)
    return features, label, float(ev_call)


def generate_dataset(
    n_samples: int,
    num_players_range: tuple = (2, 6),
    mc_sims: int = 300,
) -> tuple:
    extractor = FeatureExtractor(mc_sims=mc_sims)
    X, y, evs = [], [], []
    desc = f"Generating {n_samples} samples"
    for _ in tqdm(range(n_samples), desc=desc):
        num_players = random.randint(*num_players_range)
        try:
            feat, label, ev = generate_sample(extractor, num_players, mc_sims)
            X.append(feat)
            y.append(label)
            evs.append(ev)
        except Exception:
            continue
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int64), np.array(evs, dtype=np.float32)


def train(
    epochs: int = 50,
    n_samples: int = 100000,
    batch_size: int = 512,
    lr: float = 1e-3,
    save_path: str = 'checkpoints/supervised_model.pt',
):
    print("[1/3] Generating dataset...")
    X, y, evs = generate_dataset(n_samples)
    print(f"     Dataset size: {len(X)}, class distribution: {np.bincount(y)}")

    split = int(0.9 * len(X))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    ev_train, ev_val = evs[:split], evs[split:]

    train_ds = TensorDataset(
        torch.tensor(X_train), torch.tensor(y_train), torch.tensor(ev_train))
    val_ds = TensorDataset(
        torch.tensor(X_val), torch.tensor(y_val), torch.tensor(ev_val))
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size)

    model = DecisionModel(input_dim=39, hidden_dim=256, num_blocks=4)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    ce_loss = nn.CrossEntropyLoss()
    mse_loss = nn.MSELoss()

    print("[2/3] Training...")
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        for xb, yb, evb in train_loader:
            probs, ev_pred = model(xb)
            loss = ce_loss(torch.log(probs + 1e-8), yb) + 0.5 * mse_loss(ev_pred.squeeze(), evb)
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total_loss += loss.item()
        scheduler.step()

        if (epoch + 1) % 10 == 0:
            model.eval()
            correct = 0
            with torch.no_grad():
                for xb, yb, _ in val_loader:
                    probs, _ = model(xb)
                    pred = probs.argmax(dim=-1)
                    correct += (pred == yb).sum().item()
            acc = correct / len(val_ds)
            print(f"  Epoch {epoch+1:3d}/{epochs} | Loss: {total_loss/len(train_loader):.4f} | Val Acc: {acc:.3f}")

    print("[3/3] Saving model...")
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    model.save(save_path)
    print(f"     Saved to {save_path}")
    return model


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--samples', type=int, default=100000)
    parser.add_argument('--batch_size', type=int, default=512)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--save_path', type=str, default='checkpoints/supervised_model.pt')
    args = parser.parse_args()
    train(args.epochs, args.samples, args.batch_size, args.lr, args.save_path)
