"""Supervised training with full checkpoint/resume support.

Usage:
    python -m training.train_supervised --epochs 50 --samples 100000
    python -m training.train_supervised --epochs 50 --samples 100000 --workers 0   # 穩定單線程
    python -m training.train_supervised --epochs 50 --samples 100000 --reset       # 強制重訓
"""
from __future__ import annotations
import argparse
import random
import traceback
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from pathlib import Path
from tqdm import tqdm
import multiprocessing as mp
import os
import json
import time
import sys

from engine.card import Card, Deck
from engine.game_state import GameState, PlayerState, Street, Action
from features.feature_extractor import FeatureExtractor
from features.opponent_profile import OpponentProfileVector
from simulator.mc_equity import MonteCarloEquity
from models.decision_model import DecisionModel


# ─────────────────────────────────────────────
# Checkpoint utilities
# ─────────────────────────────────────────────

def save_checkpoint(ckpt_dir, epoch, model, optimizer, scheduler, best_val_acc, train_config):
    ckpt_dir = Path(ckpt_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = ckpt_dir / f'epoch_{epoch:04d}.pt'
    torch.save({
        'epoch': epoch,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'scheduler_state_dict': scheduler.state_dict(),
        'best_val_acc': best_val_acc,
        'train_config': train_config,
    }, ckpt_path)
    with open(ckpt_dir / 'latest.json', 'w') as f:
        json.dump({'epoch': epoch, 'path': str(ckpt_path), 'val_acc': best_val_acc}, f, indent=2)
    for old in sorted(ckpt_dir.glob('epoch_*.pt'))[:-3]:
        old.unlink()
    return ckpt_path


def load_latest_checkpoint(ckpt_dir, model, optimizer, scheduler):
    latest_path = Path(ckpt_dir) / 'latest.json'
    if not latest_path.exists():
        return 0, 0.0
    with open(latest_path) as f:
        info = json.load(f)
    ckpt_path = Path(info['path'])
    if not ckpt_path.exists():
        print(f"  [warn] Checkpoint not found: {ckpt_path}, starting fresh.")
        return 0, 0.0
    print(f"  [resume] Loading checkpoint: {ckpt_path}")
    ckpt = torch.load(ckpt_path, map_location='cpu')
    model.load_state_dict(ckpt['model_state_dict'])
    optimizer.load_state_dict(ckpt['optimizer_state_dict'])
    scheduler.load_state_dict(ckpt['scheduler_state_dict'])
    start_epoch = ckpt['epoch'] + 1
    best_val_acc = ckpt.get('best_val_acc', 0.0)
    print(f"  [resume] Resuming from epoch {start_epoch}, best val acc: {best_val_acc:.3f}")
    return start_epoch, best_val_acc


# ─────────────────────────────────────────────
# Dataset cache
# ─────────────────────────────────────────────

def dataset_cache_path(ckpt_dir, n_samples, mc_sims):
    return Path(ckpt_dir) / f'dataset_n{n_samples}_mc{mc_sims}.npz'


def save_dataset_cache(path, X, y, evs):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, X=X, y=y, evs=evs)
    print(f"  [cache] Dataset saved to {path}")


def load_dataset_cache(path):
    path = Path(path)
    if not path.exists():
        return None
    print(f"  [cache] Loading cached dataset from {path}")
    data = np.load(path)
    return data['X'], data['y'], data['evs']


# ─────────────────────────────────────────────
# Data generation  (top-level 可被 pickle)
# ─────────────────────────────────────────────

def _worker_init():
    seed = os.getpid() ^ int(time.time() * 1000) % (2**20)
    random.seed(seed)
    np.random.seed(seed % (2**32))


def _generate_chunk(args: tuple) -> list:
    """Worker: 生成一個 chunk。完整丟出 exception 方便 debug。"""
    chunk_size, num_players_range, mc_sims = args
    _worker_init()
    try:
        extractor = FeatureExtractor(mc_sims=mc_sims)
    except Exception:
        traceback.print_exc()
        return []
    results = []
    for _ in range(chunk_size):
        num_players = random.randint(*num_players_range)
        try:
            feat, label, ev = _generate_single(extractor, num_players, mc_sims)
            results.append((feat, label, ev))
        except Exception:
            pass   # 單個 sample 失敗不印，避免刷屏
    return results


def _generate_single(extractor, num_players, mc_sims=200):
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
        num_players=num_players, players=players, board=board,
        street=street_val, pot=pot, current_bet=current_bet,
        dealer_pos=0, current_player=0, big_blind=bb
    )

    eq = (MonteCarloEquity.simulate(hole, board, num_players, n=mc_sims)['equity']
          if board_size >= 3 else extractor._preflop_equity_proxy(hole, num_players))

    pot_odds = state.pot_odds
    call_amount = max(current_bet - players[0].bet, 0)
    spr = stack / max(pot, 1)
    ev_call = eq * (pot + call_amount) - (1 - eq) * call_amount if call_amount > 0 else 0

    if eq < pot_odds - 0.05:                        label = 0
    elif eq > 0.75 and spr < 5:                     label = 4
    elif eq > 0.65 and position >= num_players // 2: label = 3
    elif eq > pot_odds + 0.15:                      label = 2
    else:                                           label = 1

    features = extractor.extract(state, OpponentProfileVector.default().to_array())
    return features, label, float(ev_call)


def _detect_best_pool():
    """
    偵測最適合當前環境的 multiprocessing context。

    - Linux/Colab: 'fork'  → worker 不需重新 import，啟動最快
    - Windows    : 'spawn' → 唯一選擇
    - macOS 3.8+: 'spawn' 是預設，但 'fork' 更快
    """
    plat = sys.platform
    if plat == 'win32':
        return 'spawn'
    return 'fork'  # Linux (Colab) + macOS 都用 fork


def generate_dataset(
    n_samples, num_players_range=(2, 6), mc_sims=200,
    num_workers=None, chunk_size=200,
):
    if num_workers is None:
        num_workers = max(1, mp.cpu_count() - 1)

    # Colab 建議 2，本地多核可用 cpu_count-1
    print(f"     Using {num_workers} workers, mc_sims={mc_sims}, chunk_size={chunk_size}")

    n_chunks = (n_samples + chunk_size - 1) // chunk_size
    tasks = [(min(chunk_size, n_samples - i * chunk_size), num_players_range, mc_sims)
             for i in range(n_chunks)]
    X, y, evs = [], [], []
    collected = 0

    # ── 單線程 ───────────────────────────────────────
    if num_workers == 0:
        pbar = tqdm(total=n_samples, desc="Generating (single thread)")
        for task in tasks:
            for feat, label, ev in _generate_chunk(task):
                X.append(feat); y.append(label); evs.append(ev)
                collected += 1
                pbar.update(1)
                if collected >= n_samples: break
            if collected >= n_samples: break
        pbar.close()
        return (
            np.array(X[:n_samples], dtype=np.float32),
            np.array(y[:n_samples], dtype=np.int64),
            np.array(evs[:n_samples], dtype=np.float32)
        )

    # ── 多線程：優先用 fork (Colab/Linux)，失敗自動降回單線程 ──
    ctx_name = _detect_best_pool()
    print(f"     mp context: {ctx_name}")

    pbar = tqdm(total=n_samples, desc=f"Generating ({num_workers} workers, {ctx_name})")
    failed_chunks = 0
    success = False

    try:
        ctx = mp.get_context(ctx_name)
        with ctx.Pool(num_workers) as pool:
            success = True
            for chunk_results in pool.imap_unordered(_generate_chunk, tasks):
                if not chunk_results:   # worker 回傳空列表→此 chunk 全部失敗
                    failed_chunks += 1
                    if failed_chunks <= 3:  # 只印前幾次
                        tqdm.write(f"  [warn] empty chunk (worker error, see traceback above)")
                    continue
                for feat, label, ev in chunk_results:
                    X.append(feat); y.append(label); evs.append(ev)
                    collected += 1
                    pbar.update(1)
                    if collected >= n_samples: break
                if collected >= n_samples:
                    pool.terminate()
                    break
    except Exception as e:
        tqdm.write(f"  [error] Pool failed: {e}")
        traceback.print_exc()
    finally:
        pbar.close()

    # ── 如果資料不夠，單線程補齊 ───────────────────
    if collected < n_samples:
        remaining = n_samples - collected
        print(f"  [fallback] Single-thread: generating remaining {remaining} samples...")
        pbar2 = tqdm(total=remaining, desc="Generating (fallback)")
        for task in tasks:
            if collected >= n_samples: break
            for feat, label, ev in _generate_chunk(task):
                X.append(feat); y.append(label); evs.append(ev)
                collected += 1
                pbar2.update(1)
                if collected >= n_samples: break
        pbar2.close()

    return (
        np.array(X[:n_samples], dtype=np.float32),
        np.array(y[:n_samples], dtype=np.int64),
        np.array(evs[:n_samples], dtype=np.float32)
    )


# ─────────────────────────────────────────────
# Main training loop
# ─────────────────────────────────────────────

def train(
    epochs=50, n_samples=100000, batch_size=512,
    lr=1e-3, mc_sims=200, num_workers=None,
    ckpt_dir='checkpoints', save_path='checkpoints/supervised_model.pt',
    reset=False,
):
    ckpt_dir = Path(ckpt_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"  Device : {device}" + (f" ({torch.cuda.get_device_name(0)})" if device.type == 'cuda' else ''))

    # ── 1. Dataset ──
    print("\n[1/3] Dataset")
    cache_path = dataset_cache_path(ckpt_dir, n_samples, mc_sims)
    cached = None if reset else load_dataset_cache(cache_path)

    if cached is not None:
        X, y, evs = cached
        print(f"     Loaded from cache: {len(X)} samples")
    else:
        print(f"     Generating {n_samples} samples...")
        X, y, evs = generate_dataset(n_samples, mc_sims=mc_sims, num_workers=num_workers)
        save_dataset_cache(cache_path, X, y, evs)

    print(f"     Class distribution: {np.bincount(y)}")

    split = int(0.9 * len(X))
    train_ds = TensorDataset(torch.tensor(X[:split]), torch.tensor(y[:split]), torch.tensor(evs[:split]))
    val_ds   = TensorDataset(torch.tensor(X[split:]), torch.tensor(y[split:]), torch.tensor(evs[split:]))
    dl_w = 4 if device.type == 'cuda' else 0
    pin  = device.type == 'cuda'
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True,  num_workers=dl_w, pin_memory=pin)
    val_loader   = DataLoader(val_ds,   batch_size=batch_size, shuffle=False, num_workers=dl_w, pin_memory=pin)

    # ── 2. Model ──
    train_config = dict(epochs=epochs, n_samples=n_samples, batch_size=batch_size, lr=lr, mc_sims=mc_sims)
    model = DecisionModel(input_dim=39, hidden_dim=256, num_blocks=4).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    ce_loss  = nn.CrossEntropyLoss()
    mse_loss = nn.MSELoss()

    # ── 3. Resume ──
    start_epoch = 0
    best_val_acc = 0.0
    if not reset:
        start_epoch, best_val_acc = load_latest_checkpoint(ckpt_dir, model, optimizer, scheduler)
        model = model.to(device)
        for state in optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device)

    if start_epoch >= epochs:
        print(f"\n  Already finished {epochs} epochs. Use --reset to retrain.")
        return

    # ── 4. Train ──
    print(f"\n[2/3] Training (epoch {start_epoch+1} → {epochs})")
    for epoch in range(start_epoch, epochs):
        model.train()
        total_loss = 0
        for xb, yb, evb in train_loader:
            xb, yb, evb = xb.to(device), yb.to(device), evb.to(device)
            probs, ev_pred = model(xb)
            loss = ce_loss(torch.log(probs + 1e-8), yb) + 0.5 * mse_loss(ev_pred.squeeze(), evb)
            optimizer.zero_grad(); loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total_loss += loss.item()
        scheduler.step()

        model.eval()
        correct = 0
        with torch.no_grad():
            for xb, yb, _ in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                probs, _ = model(xb)
                correct += (probs.argmax(dim=-1) == yb).sum().item()
        val_acc = correct / len(val_ds)
        if val_acc > best_val_acc:
            best_val_acc = val_acc

        avg_loss = total_loss / len(train_loader)
        print(f"  Epoch {epoch+1:3d}/{epochs} | Loss: {avg_loss:.4f} | Val Acc: {val_acc:.3f} | Best: {best_val_acc:.3f}")
        ckpt_path = save_checkpoint(ckpt_dir, epoch, model, optimizer, scheduler, best_val_acc, train_config)
        print(f"  [ckpt] Saved → {ckpt_path.name}")

    # ── 5. Final save ──
    print("\n[3/3] Saving final model...")
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    model.cpu().save(save_path)
    print(f"  Saved → {save_path}  |  Best val acc: {best_val_acc:.3f}")


# ─────────────────────────────────────────────
if __name__ == '__main__':
    mp.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs',     type=int,   default=50)
    parser.add_argument('--samples',    type=int,   default=100000)
    parser.add_argument('--batch_size', type=int,   default=512)
    parser.add_argument('--lr',         type=float, default=1e-3)
    parser.add_argument('--mc_sims',    type=int,   default=200)
    parser.add_argument('--workers',    type=int,   default=None,
                        help='0=single thread (safest), None=auto (cpu_count-1)')
    parser.add_argument('--ckpt_dir',   type=str,   default='checkpoints')
    parser.add_argument('--save_path',  type=str,   default='checkpoints/supervised_model.pt')
    parser.add_argument('--reset',      action='store_true')
    args = parser.parse_args()
    train(
        epochs=args.epochs, n_samples=args.samples, batch_size=args.batch_size,
        lr=args.lr, mc_sims=args.mc_sims, num_workers=args.workers,
        ckpt_dir=args.ckpt_dir, save_path=args.save_path, reset=args.reset
    )
