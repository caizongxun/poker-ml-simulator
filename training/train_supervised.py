"""Supervised training from MC-simulated labeled data.

加速策略：
- multiprocessing.Pool 並行生成資料（充分利用所有 CPU 核心）
- mc_sims 預設降到 200（精度損失小，速度提升 1.5x）
- 批量 chunk 生成，tqdm 顯示整體進度
- GPU 自動偵測用於訓練階段

Usage:
    python -m training.train_supervised --epochs 50 --samples 100000
    python -m training.train_supervised --epochs 50 --samples 100000 --workers 0  # 單線程 debug
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
import multiprocessing as mp
import os

from engine.card import Card, Deck
from engine.game_state import GameState, PlayerState, Street, Action
from features.feature_extractor import FeatureExtractor
from features.opponent_profile import OpponentProfileVector
from simulator.mc_equity import MonteCarloEquity
from models.decision_model import DecisionModel


def _worker_init():
    """每個 worker 獨立隨機種子，避免所有 worker 生成相同資料。"""
    seed = os.getpid()
    random.seed(seed)
    np.random.seed(seed % (2**32))


def _generate_chunk(args: tuple) -> list:
    """
    Worker function: 生成 chunk_size 個 sample。
    回傳 list of (features, label, ev)。
    不能用 lambda（pickle 限制），所以用頂層函數。
    """
    chunk_size, num_players_range, mc_sims = args
    _worker_init()
    extractor = FeatureExtractor(mc_sims=mc_sims)
    results = []
    for _ in range(chunk_size):
        num_players = random.randint(*num_players_range)
        try:
            feat, label, ev = _generate_single(extractor, num_players, mc_sims)
            results.append((feat, label, ev))
        except Exception:
            pass
    return results


def _generate_single(
    extractor: FeatureExtractor,
    num_players: int,
    mc_sims: int = 200,
) -> tuple:
    """Generate one (feature_vector, action_label, ev) tuple."""
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

    players = [
        PlayerState(
            player_id=i,
            stack=stack if i == 0 else random.uniform(10, 200),
            hole_cards=hole if i == 0 else [],
            bet=current_bet if i == 0 else 0,
            total_invested=random.uniform(0, pot / 2)
        )
        for i in range(num_players)
    ]

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

    # Oracle label
    pot_odds = state.pot_odds
    call_amount = max(current_bet - players[0].bet, 0)
    spr = stack / max(pot, 1)
    ev_call = eq * (pot + call_amount) - (1 - eq) * call_amount if call_amount > 0 else 0

    if eq < pot_odds - 0.05:
        label = 0
    elif eq > 0.75 and spr < 5:
        label = 4
    elif eq > 0.65 and position >= num_players // 2:
        label = 3
    elif eq > pot_odds + 0.15:
        label = 2
    else:
        label = 1

    opv = OpponentProfileVector.default().to_array()
    features = extractor.extract(state, opv)
    return features, label, float(ev_call)


def generate_dataset(
    n_samples: int,
    num_players_range: tuple = (2, 6),
    mc_sims: int = 200,
    num_workers: int = None,
    chunk_size: int = 500,
) -> tuple:
    """
    並行生成資料集。

    Args:
        n_samples: 目標樣本數
        num_players_range: 玩家數範圍
        mc_sims: 每個樣本的 MC 模擬次數（降低可加速）
        num_workers: 工作線程數，None = 自動偵測 CPU 核心數
        chunk_size: 每個 worker 每次處理的 sample 數
    """
    if num_workers is None:
        num_workers = max(1, mp.cpu_count() - 1)  # 保留 1 核給主程序

    print(f"     Using {num_workers} workers, mc_sims={mc_sims}, chunk_size={chunk_size}")

    # 計算要分派多少 chunks
    n_chunks = (n_samples + chunk_size - 1) // chunk_size
    tasks = [(chunk_size, num_players_range, mc_sims)] * n_chunks

    X, y, evs = [], [], []
    collected = 0

    if num_workers == 0:
        # 單線程模式（debug 用）
        for task in tqdm(tasks, desc=f"Generating (single thread)"):
            for feat, label, ev in _generate_chunk(task):
                X.append(feat); y.append(label); evs.append(ev)
                collected += 1
                if collected >= n_samples:
                    break
            if collected >= n_samples:
                break
    else:
        with mp.Pool(num_workers) as pool:
            pbar = tqdm(total=n_samples, desc=f"Generating ({num_workers} workers)")
            for chunk_results in pool.imap_unordered(_generate_chunk, tasks):
                for feat, label, ev in chunk_results:
                    X.append(feat); y.append(label); evs.append(ev)
                    collected += 1
                    pbar.update(1)
                    if collected >= n_samples:
                        break
                if collected >= n_samples:
                    break
            pbar.close()
            pool.terminate()  # 已夠了就終止剩餘 worker

    return (
        np.array(X[:n_samples], dtype=np.float32),
        np.array(y[:n_samples], dtype=np.int64),
        np.array(evs[:n_samples], dtype=np.float32)
    )


def train(
    epochs: int = 50,
    n_samples: int = 100000,
    batch_size: int = 512,
    lr: float = 1e-3,
    mc_sims: int = 200,
    num_workers: int = None,
    save_path: str = 'checkpoints/supervised_model.pt',
):
    # 自動選 device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"     Device: {device}")
    if device.type == 'cuda':
        print(f"     GPU: {torch.cuda.get_device_name(0)}")

    print("[1/3] Generating dataset...")
    X, y, evs = generate_dataset(n_samples, mc_sims=mc_sims, num_workers=num_workers)
    print(f"     Dataset size: {len(X)}, class distribution: {np.bincount(y)}")

    split = int(0.9 * len(X))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    ev_train, ev_val = evs[:split], evs[split:]

    train_ds = TensorDataset(
        torch.tensor(X_train), torch.tensor(y_train), torch.tensor(ev_train))
    val_ds = TensorDataset(
        torch.tensor(X_val), torch.tensor(y_val), torch.tensor(ev_val))

    # num_workers for DataLoader（GPU 下開多線程 prefetch）
    dl_workers = 4 if device.type == 'cuda' else 0
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True,
                              num_workers=dl_workers, pin_memory=(device.type=='cuda'))
    val_loader = DataLoader(val_ds, batch_size=batch_size,
                            num_workers=dl_workers, pin_memory=(device.type=='cuda'))

    model = DecisionModel(input_dim=39, hidden_dim=256, num_blocks=4).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    ce_loss = nn.CrossEntropyLoss()
    mse_loss = nn.MSELoss()

    print("[2/3] Training...")
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        for xb, yb, evb in train_loader:
            xb, yb, evb = xb.to(device), yb.to(device), evb.to(device)
            probs, ev_pred = model(xb)
            loss = ce_loss(torch.log(probs + 1e-8), yb) + 0.5 * mse_loss(ev_pred.squeeze(), evb)
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total_loss += loss.item()
        scheduler.step()

        if (epoch + 1) % 5 == 0:
            model.eval()
            correct = 0
            with torch.no_grad():
                for xb, yb, _ in val_loader:
                    xb, yb = xb.to(device), yb.to(device)
                    probs, _ = model(xb)
                    pred = probs.argmax(dim=-1)
                    correct += (pred == yb).sum().item()
            acc = correct / len(val_ds)
            print(f"  Epoch {epoch+1:3d}/{epochs} | Loss: {total_loss/len(train_loader):.4f} | Val Acc: {acc:.3f}")

    print("[3/3] Saving model...")
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    # 存 CPU 版本方便跨機器讀取
    cpu_model = model.cpu()
    cpu_model.save(save_path)
    print(f"     Saved to {save_path}")
    return model


if __name__ == '__main__':
    # Windows 必須有這行，否則 multiprocessing spawn 會遞迴啟動
    mp.freeze_support()

    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--samples', type=int, default=100000)
    parser.add_argument('--batch_size', type=int, default=512)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--mc_sims', type=int, default=200,
                        help='MC simulations per sample (lower=faster, default=200)')
    parser.add_argument('--workers', type=int, default=None,
                        help='Parallel workers for data gen (default=cpu_count-1, 0=single thread)')
    parser.add_argument('--save_path', type=str, default='checkpoints/supervised_model.pt')
    args = parser.parse_args()
    train(args.epochs, args.samples, args.batch_size, args.lr,
          args.mc_sims, args.workers, args.save_path)
