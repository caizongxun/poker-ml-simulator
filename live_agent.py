#!/usr/bin/env python3
"""
live_agent.py — Real-time GameSofa AI Advisor
================================================
每秒自動截圖 -> OCR 辨識牌面 -> 建構 feature vector -> DecisionModel 推論
-> 終端機顯示建議動作 (FOLD / CHECK-CALL / RAISE / ALL-IN)

用法:
    python live_agent.py
    python live_agent.py --model checkpoints/supervised_model.pt
    python live_agent.py --interval 1.5 --temperature 0.3

F7  = 啟動/停止自動輪詢
F9  = 單次辨識
F10 = 離開

安裝:
    pip install pillow keyboard pyautogui pytesseract opencv-python pywin32 torch
"""

import argparse, sys, os, time, json, threading
from pathlib import Path
import numpy as np

# ── 引入本機模組 ──────────────────────────────────────────────
# screen_reader 提供 capture_and_analyze + format_results
from screen_reader import (
    capture_and_analyze, format_results,
    get_window_capture, HAS_KEYBOARD
)

try:
    import keyboard
except ImportError:
    keyboard = None

# ── ML 模型 ───────────────────────────────────────────────────
try:
    import torch
    from models.decision_model import DecisionModel
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False
    print("[WARN] torch 未安裝，將只顯示辨識結果不做 AI 推論")
    print("       pip install torch")

# ── 牌面 -> 數值轉換 ──────────────────────────────────────────
RANK_TO_INT = {
    '2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,
    'T':10,'J':11,'Q':12,'K':13,'A':14
}

def card_str_to_rank(card_str: str) -> int:
    """e.g. 'A♥' -> 14, 'K♠' -> 13"""
    if not card_str or len(card_str) < 1:
        return 0
    rank_ch = card_str[0].upper()
    return RANK_TO_INT.get(rank_ch, 0)

def cards_to_feature_segment(hole_cards, board_cards):
    """
    從辨識結果建構簡化 39-dim feature vector（不依賴 GameState）。
    適合即時推論，特徵為近似值。

    dims 說明（與 feature_extractor.py 一致）:
      [0:4]   street one-hot
      [4]     pot odds (近似 0.33)
      [5]     equity estimate (簡化 Chen formula)
      [6]     stack depth (預設 100BB)
      [7]     position (預設 0.5)
      [8]     pot size (從 pot 文字解析)
      [9]     bet ratio (預設 0)
      [10]    active players ratio (預設 0.5)
      [11]    SPR (預設 0.5)
      [12]    fold equity (預設 0.25)
      [13:17] board texture
      [17:33] opponent OPV (zeros)
      [33:39] history features (zeros)
    """
    feat = np.zeros(39, dtype=np.float32)

    # Street one-hot
    n_board = sum(1 for c in board_cards if c)
    if n_board == 0:   street = 0  # preflop
    elif n_board == 3: street = 1  # flop
    elif n_board == 4: street = 2  # turn
    else:              street = 3  # river
    feat[street] = 1.0

    # Pot odds 近似
    feat[4] = 0.33

    # Equity: 簡化 Chen formula
    if hole_cards and len(hole_cards) == 2:
        r1 = card_str_to_rank(hole_cards[0])
        r2 = card_str_to_rank(hole_cards[1])
        if r1 > 0 and r2 > 0:
            hi, lo = max(r1,r2), min(r1,r2)
            score = hi / 2.0
            if r1 == r2: score = max(score*2, 5)   # pocket pair
            gap = hi - lo
            # suited check: 同花色
            s1 = hole_cards[0][-1] if hole_cards[0] else ''
            s2 = hole_cards[1][-1] if hole_cards[1] else ''
            if s1 == s2: score += 2
            score -= max(0, gap-1)
            feat[5] = float(np.clip(score/20.0, 0, 1))

    # Stack / position
    feat[6] = 0.5    # 100BB norm to 200BB = 0.5
    feat[7] = 0.5    # 未知 position
    feat[8] = 0.1    # pot 近似
    feat[9] = 0.0
    feat[10] = 0.5
    feat[11] = 0.5   # SPR
    feat[12] = 0.25  # fold equity

    # Board texture
    valid_board = [c for c in board_cards if c and card_str_to_rank(c) > 0]
    if len(valid_board) >= 3:
        suits  = [c[-1] for c in valid_board if len(c)>=2]
        ranks  = [card_str_to_rank(c) for c in valid_board]
        from collections import Counter
        suit_cnt = Counter(suits)
        rank_cnt = Counter(ranks)
        flush_draw   = float(max(suit_cnt.values(), default=0) >= 3)
        srk = sorted(set(ranks))
        max_c=cur=1
        for i in range(1,len(srk)):
            if srk[i]-srk[i-1]==1: cur+=1; max_c=max(max_c,cur)
            else: cur=1
        straight_draw = float(max_c >= 3)
        paired        = float(max(rank_cnt.values(), default=0) >= 2)
        high_card     = (max(ranks)/14.0) if ranks else 0.0
        feat[13:17] = [flush_draw, straight_draw, paired, high_card]

    # OPV / history: zeros (沒有歷史資料)
    return feat

# ── 推論 ──────────────────────────────────────────────────────
ACTION_NAMES  = ['FOLD', 'CHECK/CALL', 'RAISE_SMALL', 'RAISE_LARGE', 'ALL_IN']
ACTION_COLORS = ['\033[91m','\033[92m','\033[93m','\033[93m','\033[95m']
RESET = '\033[0m'
BOLD  = '\033[1m'

def run_inference(model, hole_cards, board_cards, temperature=1.0):
    feat = cards_to_feature_segment(hole_cards, board_cards)
    result = model.decide(feat, temperature=temperature, greedy=False)
    return result

def print_decision(result, hole_cards, board_cards):
    os.system("cls" if sys.platform=="win32" else "clear")
    print(f"{BOLD}╔{'═'*56}╗{RESET}")
    print(f"{BOLD}║  🃏  GameSofa AI Advisor  {'─'*26} ║{RESET}")
    print(f"{BOLD}╠{'═'*56}╣{RESET}")

    hole_str  = '  '.join(hole_cards)  if hole_cards  else '(未辨識)'
    board_str = '  '.join(board_cards) if board_cards else '(無公共牌)'
    print(f"║  手牌   : {hole_str:<45} ║")
    print(f"║  公共牌 : {board_str:<45} ║")
    print(f"{BOLD}╠{'═'*56}╣{RESET}")

    # 最佳動作
    best_idx  = result['action']
    best_name = result['action_name']
    best_col  = ACTION_COLORS[best_idx]
    ev        = result['ev']
    print(f"║  建議動作: {best_col}{BOLD}{best_name:<10}{RESET}   EV≈{ev:+.3f}{' '*(21-len(best_name))} ║")
    print(f"{BOLD}╠{'═'*56}╣{RESET}")

    # 機率條
    probs = result['probs']
    print(f"║  {'動作':<12} {'機率':>6}  {'條形圖':<24} ║")
    print(f"║  {'─'*12} {'─'*6}  {'─'*24} ║")
    for i, name in enumerate(ACTION_NAMES):
        p = probs.get(name, 0.0)
        bar_len = int(p * 24)
        bar = '█' * bar_len + '░' * (24 - bar_len)
        mark = ' ◄' if i == best_idx else '  '
        col  = ACTION_COLORS[i] if i == best_idx else ''
        rst  = RESET if i == best_idx else ''
        print(f"║  {col}{name:<12} {p:>6.1%}  {bar}{rst}{mark} ║")

    print(f"{BOLD}╚{'═'*56}╝{RESET}")
    print(f"  {time.strftime('%H:%M:%S')}  按 F7=切換自動  F9=單次  F10=離開")

# ── 主迴圈 ────────────────────────────────────────────────────
_running   = False
_poll_th   = None

def one_shot(model, temperature, save_debug=False):
    try:
        results, _ = capture_and_analyze(save_debug=save_debug)
        out = format_results(results)
        hole  = out.get("hole_cards", [])
        board = out.get("board", [])
        if not HAS_TORCH or model is None:
            return
        if not hole:
            print("[SKIP] 手牌未辨識，跳過推論")
            return
        result = run_inference(model, hole, board, temperature)
        print_decision(result, hole, board)
        # 寫入 last_decision.json 供其他程式讀取
        with open("last_decision.json","w",encoding="utf-8") as f:
            json.dump({"hole":hole,"board":board,**result,
                       "timestamp":time.strftime("%H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERR] {e}")
        import traceback; traceback.print_exc()

def toggle_auto(model, interval, temperature):
    global _running, _poll_th
    if _running:
        _running = False
        print("[F7] 自動輪詢 ▶ 已停止")
    else:
        _running = True
        def loop():
            while _running:
                one_shot(model, temperature, save_debug=False)
                time.sleep(interval)
        _poll_th = threading.Thread(target=loop, daemon=True)
        _poll_th.start()
        print(f"[F7] 自動輪詢 ▶ 啟動 (每 {interval}s)")

def main():
    parser = argparse.ArgumentParser(description="GameSofa AI Advisor")
    parser.add_argument("--model",       default="checkpoints/supervised_model.pt",
                        help="模型路徑")
    parser.add_argument("--interval",    type=float, default=1.0,
                        help="自動輪詢間隔秒數 (預設 1.0)")
    parser.add_argument("--temperature", type=float, default=0.5,
                        help="決策溫度 0.1=保守 1.0=多樣 (預設 0.5)")
    parser.add_argument("--auto",        action="store_true",
                        help="啟動後立即開始自動輪詢")
    args = parser.parse_args()

    # 載入模型
    model = None
    if HAS_TORCH:
        model_path = Path(args.model)
        if model_path.exists():
            print(f"[MODEL] 載入: {model_path}")
            try:
                model = DecisionModel.load(str(model_path))
                model.eval()
                print("[MODEL] 載入成功 ✓")
            except Exception as e:
                print(f"[WARN] 模型載入失敗: {e}")
                model = None
        else:
            print(f"[WARN] 找不到模型: {model_path}")
            print("       請先訓練: python -m training.train_supervised")
            print("       或指定路徑: python live_agent.py --model <path>")

    wc = get_window_capture()
    print(f"\n[AGENT] 視窗: {wc.window_title or '(全螢幕)'}")
    print(f"[AGENT] 模型: {'✓ ' + str(args.model) if model else '✗ 僅辨識模式'}")
    print(f"[AGENT] 溫度: {args.temperature}  間隔: {args.interval}s")

    if args.auto:
        toggle_auto(model, args.interval, args.temperature)

    if HAS_KEYBOARD and keyboard:
        print("\n快捷鍵: F7=切換自動  F8=選視窗  F9=單次截圖  F10=離開\n")
        keyboard.add_hotkey("f7", lambda: toggle_auto(model, args.interval, args.temperature))
        keyboard.add_hotkey("f8", lambda: get_window_capture().select_window_interactive())
        keyboard.add_hotkey("f9", lambda: one_shot(model, args.temperature, save_debug=True))
        keyboard.add_hotkey("f10",lambda: (print("\n[BYE]"), os._exit(0)))
        keyboard.wait()
    else:
        print("\n[Manual] c=截圖推論  a=切換自動  q=離開")
        while True:
            cmd = input("\n> ").strip().lower()
            if cmd in ("c","capture"):
                one_shot(model, args.temperature, save_debug=True)
            elif cmd in ("a","auto"):
                toggle_auto(model, args.interval, args.temperature)
            elif cmd in ("q","quit"):
                print("Bye!"); break

if __name__ == "__main__":
    main()
