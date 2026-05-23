"""Supervised training with full checkpoint/resume support.

功能：
- 每個 epoch 結束後自動儲存 checkpoint（model + optimizer + scheduler + epoch）
- 重新執行自動從最新 checkpoint 繼續，不需要額外參數
- 資料集生成後自動快取到 .npz，下次直接讀取（跳過耗時的 MC 模擬）
- GPU 自動偵測，支援 Colab T4/A100
- concurrent.futures 平行生成資料（修復 Windows/Colab 下 mp.Pool 卡死問題）

Usage:
    # 第一次執行
    python -m training.train_supervised --epochs 50 --samples 100000

    # 中斷後繼續（自動偵測 checkpoint）
    python -m training.train_supervised --epochs 50 --samples 100000

    # 強制重新開始
    python -m training.train_supervised --epochs 50 --samples 100000 --reset

    # 強制單線程（最穩定，速度稍慢）
    python -m training.train_supervised --workers 0
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
import json
import time

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
    latest_path = ckpt_dir / 'latest.json'
    with open(latest_path, 'w') as f:
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
# Dataset cache utilities
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
# Data generation
# ─────────────────────────────────────────────

def _worker_init():
    seed = os.getpid() ^ int(time.time() * 1000) % (2**20)
    random.seed(seed)
    np.random.seed(seed % (2**32))


def _generate_chunk(args: tuple) -> list:
    """頂層函數（可被 pickle），生成一個 chunk 的資料。"""
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


def generate_dataset(
    n_samples, num_players_range=(2, 6), mc_sims=200,
    num_workers=None, chunk_size=100,   # 小 chunk → 更快看到第一個進度
):
    """
    並行生成資料集。

    使用 concurrent.futures.ProcessPoolExecutor 取代 mp.Pool，
    解決 Windows / Colab 下 spawn context 造成 0it/s 卡死的問題。
    num_workers=0 → 純單線程，最穩定。
    """
    if num_workers is None:
        num_workers = max(1, mp.cpu_count() - 1)

    print(f"     Using {num_workers} workers, mc_sims={mc_sims}, chunk_size={chunk_size}")

    n_chunks = (n_samples + chunk_size - 1) // chunk_size
    tasks = [(min(chunk_size, n_samples - i * chunk_size), num_players_range, mc_sims)
             for i in range(n_chunks)]

    X, y, evs = [], [], []
    collected = 0

    # ── 單線程模式 ──────────────────────────────────────
    if num_workers == 0:
        pbar = tqdm(total=n_samples, desc="Generating (single thread)")
        for task in tasks:
            for feat, label, ev in _generate_chunk(task):
                X.append(feat); y.append(label); evs.append(ev)
                collected += 1
                pbar.update(1)
                if collected >= n_samples:
                    break
            if collected >= n_samples:
                break
        pbar.close()

    # ── 多線程模式（concurrent.futures）────────────────
    else:
        # 先試 ProcessPoolExecutor；若環境不支援（如 Jupyter 某些情況）自動降回 ThreadPoolExecutor
        try:
            from concurrent.futures import ProcessPoolExecutor as Executor
            _use_process = True
        except Exception:
            from concurrent.futures import ThreadPoolExecutor as Executor
            _use_process = False

        pbar = tqdm(total=n_samples, desc=f"Generating ({num_workers} workers)")
        try:
            with Executor(max_workers=num_workers) as executor:
                futures = [executor.submit(_generate_chunk, t) for t in tasks]
                for fut in futures:
                    try:
                        chunk_results = fut.result(timeout=120)  # 單個 chunk 最多等 2 分鐘
                    except Exception as e:
                        tqdm.write(f"  [warn] chunk failed: {e}")
                        continue
                    for feat, label, ev in chunk_results:
                        X.append(feat); y.append(label); evs.append(ev)
                        collected += 1
                        pbar.update(1)
                        if collected >= n_samples:
                            break
                    if collected >= n_samples:
                        # 取消未完成的 futures
                        for f in futures:
                            f.cancel()
                        break
        except Exception as e:
            pbar.close()
            print(f"  [warn] Parallel generation failed ({e}), falling back to single thread...")
            # 完全降回單線程補齊剩餘資料
            pbar = tqdm(total=n_samples - collected, desc="Generating (fallback single thread)")
            for task in tasks[collected // chunk_size:]:
                for feat, label, ev in _generate_chunk(task):
                    X.append(feat); y.append(label); evs.append(ev)
                    collected += 1
                    pbar.update(1)
                    if collected >= n_samples:
                        break
                if collected >= n_samples:
                    break
        pbar.close()

    return (
        np.array(X[:n_samples], dtype=np.float32),
        np.array(y[:n_samples], dtype=np.int64),
        np.array(evs[:n_samples], dtype=np.float32)
    )


# ─────────────────────────────────────────────
# Main training loop
# ─────────────────────────────────────────────

def train(
    epochs: int = 50,
    n_samples: int = 100000,
    batch_size: int = 512,
    lr: float = 1e-3,
    mc_sims: int = 200,
    num_workers: int = None,
    ckpt_dir: str = 'checkpoints',
    save_path: str = 'checkpoints/supervised_model.pt',
    reset: bool = False,
):
    ckpt_dir = Path(ckpt_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"  Device : {device}" + (f" ({torch.cuda.get_device_name(0)})" if device.type == 'cuda' else ''))

    # ── 1. Dataset（優先讀快取）──────────────────
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

    # ── 2. Model + optimizer ────────────────────
    train_config = dict(epochs=epochs, n_samples=n_samples, batch_size=batch_size, lr=lr, mc_sims=mc_sims)
    model = DecisionModel(input_dim=39, hidden_dim=256, num_blocks=4).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    ce_loss  = nn.CrossEntropyLoss()
    mse_loss = nn.MSELoss()

    # ── 3. Resume from checkpoint ───────────────
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

    # ── 4. Training loop ─────────────────────────
    print(f"\n[2/3] Training (epoch {start_epoch+1} → {epochs})")
    for epoch in range(start_epoch, epochs):
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

    # ── 5. Final model save ──────────────────────
    print("\n[3/3] Saving final model...")
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    model.cpu().save(save_path)
    print(f"  Saved → {save_path}")
    print(f"  Best val acc: {best_val_acc:.3f}")


# ────────────────────────────────────────────────
if __name__ == '__main__':
    mp.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs',     type=int,   default=50)
    parser.add_argument('--samples',    type=int,   default=100000)
    parser.add_argument('--batch_size', type=int,   default=512)
    parser.add_argument('--lr',         type=float, default=1e-3)
    parser.add_argument('--mc_sims',    type=int,   default=200)
    parser.add_argument('--workers',    type=int,   default=None)
    parser.add_argument('--ckpt_dir',   type=str,   default='checkpoints')
    parser.add_argument('--save_path',  type=str,   default='checkpoints/supervised_model.pt')
    parser.add_argument('--reset',      action='store_true')
    args = parser.parse_args()
    train(
        epochs=args.epochs, n_samples=args.samples, batch_size=args.batch_size,
        lr=args.lr, mc_sims=args.mc_sims, num_workers=args.workers,
        ckpt_dir=args.ckpt_dir, save_path=args.save_path, reset=args.reset
    )
