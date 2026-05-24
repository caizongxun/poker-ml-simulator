#!/usr/bin/env python3
"""
GameSofa Texas Hold'em Screen Reader
=====================================
按 F9 自動截圖 -> 辨識手牌 & 公共牌 -> 輸出到 advisor HTML

依賴安裝:
    pip install pillow keyboard pyautogui pytesseract opencv-python requests
    # Tesseract OCR: https://github.com/UB-Mannheim/tesseract/wiki (Windows)
    # macOS: brew install tesseract
    # Ubuntu: sudo apt install tesseract-ocr

用法:
    python screen_reader.py
    => 瀏覽器開著 GameSofa，按 F9 擷取
"""

import sys, os, time, json, threading
from pathlib import Path

# ── Optional imports (graceful fallback) ──────────────────────
try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False
    print("[WARN] keyboard 未安裝，將改用手動輸入模式")

try:
    import pyautogui
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    print("[WARN] opencv-python 未安裝，使用 Pillow fallback")

try:
    import pytesseract
    HAS_TESS = True
    # Windows 預設路徑（如有需要請修改）
    if sys.platform == "win32":
        pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
except ImportError:
    HAS_TESS = False
    print("[WARN] pytesseract 未安裝，使用顏色分析 fallback")

from PIL import Image, ImageDraw

# ─────────────────────────────────────────────────────────────
# CONFIGURATION  (依截圖尺寸 1214x915 設定)
# ─────────────────────────────────────────────────────────────
# 截圖尺寸（可自動偵測）
SCREENSHOT_W = 1214
SCREENSHOT_H = 915

# 區域座標 (x1, y1, x2, y2) — 基準為 1214x915
# 灰色工具列高度約 115px，牌桌實際從 y≈115 開始
REGIONS = {
    # ── 手牌 ──
    "hole_1":   (636, 540, 730, 670),
    "hole_2":   (718, 540, 815, 670),
    # ── 公共牌 ──
    "board_1":  (362, 345, 462, 465),
    "board_2":  (470, 345, 570, 465),
    "board_3":  (578, 345, 678, 465),
    "board_4":  (686, 345, 786, 465),
    "board_5":  (794, 345, 894, 465),
    # ── 輔助資訊 ──
    "pot":      (520, 278, 700, 320),
    "my_stack": (476, 670, 640, 712),
    "blind":    (980, 108, 1210, 165),
}

# ─────────────────────────────────────────────────────────────
# SUIT DETECTION BY COLOR (HSV)
# ─────────────────────────────────────────────────────────────

def detect_suit_by_color(crop_img):
    """Return 'red' or 'black' based on dominant non-white pixels."""
    if HAS_CV2:
        arr = np.array(crop_img.convert("RGB"))
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        mask1 = cv2.inRange(hsv, np.array([0,100,80]), np.array([15,255,255]))
        mask2 = cv2.inRange(hsv, np.array([160,100,80]), np.array([180,255,255]))
        red_px = cv2.countNonZero(mask1) + cv2.countNonZero(mask2)
        dark_mask = cv2.inRange(hsv, np.array([0,0,0]), np.array([180,80,80]))
        dark_px = cv2.countNonZero(dark_mask)
        return "red" if red_px > dark_px * 0.3 else "black"
    else:
        arr = crop_img.convert("RGB")
        w, h = arr.size
        red = black = 0
        for y in range(h):
            for x in range(w):
                r, g, b = arr.getpixel((x, y))
                if r > 150 and g < 100 and b < 100:
                    red += 1
                elif r < 80 and g < 80 and b < 80:
                    black += 1
        return "red" if red > black * 0.3 else "black"

# ─────────────────────────────────────────────────────────────
# RANK DETECTION
# ─────────────────────────────────────────────────────────────
RANK_MAP = {
    'a': 'A', '1': 'A',
    '2': '2', '3': '3', '4': '4', '5': '5',
    '6': '6', '7': '7', '8': '8', '9': '9',
    '0': 'T', 't': 'T', '10': 'T',
    'j': 'J', 'q': 'Q', 'k': 'K',
}

def detect_rank_ocr(crop_img):
    """Detect card rank using Tesseract OCR on enhanced top-left corner."""
    if not HAS_TESS:
        return "?"
    w, h = crop_img.size
    corner = crop_img.crop((0, 0, int(w*0.45), int(h*0.42)))
    corner = corner.convert("L")
    corner = corner.resize((corner.width*3, corner.height*3), Image.LANCZOS)
    cfg = r'--psm 10 -c tessedit_char_whitelist=AaKkQqJjTt0123456789'
    try:
        text = pytesseract.image_to_string(corner, config=cfg).strip().lower()
        text = ''.join(c for c in text if c.isalnum())
        if text in RANK_MAP:
            return RANK_MAP[text]
        if len(text) >= 2 and text[:2] in RANK_MAP:
            return RANK_MAP[text[:2]]
        if text:
            return RANK_MAP.get(text[0], "?")
        return "?"
    except Exception:
        return "?"

SUIT_FALLBACK = {"red": "\u2665", "black": "\u2660"}

def analyze_card_region(crop_img, region_name):
    result = {"rank": "?", "suit": "?", "color": "unknown", "confidence": 0.0, "region": region_name}
    if is_empty_slot(crop_img):
        result["rank"] = None
        result["suit"] = None
        return result
    color = detect_suit_by_color(crop_img)
    result["color"] = color
    result["suit"] = SUIT_FALLBACK[color]
    rank = detect_rank_ocr(crop_img)
    result["rank"] = rank
    result["confidence"] = 0.9 if rank != "?" else 0.3
    return result

def is_empty_slot(crop_img):
    """Detect if a board slot is empty (mostly teal table background)."""
    if HAS_CV2:
        arr = np.array(crop_img.convert("RGB"))
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        teal_mask = cv2.inRange(hsv, np.array([80,40,80]), np.array([110,200,210]))
        teal_ratio = cv2.countNonZero(teal_mask) / (arr.shape[0] * arr.shape[1])
        return teal_ratio > 0.55
    else:
        arr = crop_img.convert("RGB")
        w, h = arr.size
        teal = total = 0
        for y in range(0, h, 4):
            for x in range(0, w, 4):
                r, g, b = arr.getpixel((x, y))
                total += 1
                if 0 < r < 130 and 120 < g < 220 and 100 < b < 200:
                    teal += 1
        return (teal / max(total, 1)) > 0.5

# ─────────────────────────────────────────────────────────────
# SCALE REGIONS
# ─────────────────────────────────────────────────────────────
def scale_region(region, actual_w, actual_h):
    x1, y1, x2, y2 = region
    sx = actual_w / SCREENSHOT_W
    sy = actual_h / SCREENSHOT_H
    return (int(x1*sx), int(y1*sy), int(x2*sx), int(y2*sy))

# ─────────────────────────────────────────────────────────────
# MAIN CAPTURE
# ─────────────────────────────────────────────────────────────
def capture_and_analyze(save_debug=True, debug_dir="debug_crops"):
    print("\n[SNAP] 截圖中...")
    if HAS_PYAUTOGUI:
        screenshot = pyautogui.screenshot()
    else:
        print("[ERROR] pyautogui 未安裝")
        return None
    actual_w, actual_h = screenshot.size
    print(f"[INFO] 截圖尺寸: {actual_w}x{actual_h}")
    annotated = screenshot.copy()
    draw = ImageDraw.Draw(annotated)
    if save_debug:
        Path(debug_dir).mkdir(exist_ok=True)
    results = {}
    for name, region in REGIONS.items():
        scaled = scale_region(region, actual_w, actual_h)
        x1, y1, x2, y2 = scaled
        crop = screenshot.crop(scaled)
        if save_debug:
            crop.save(f"{debug_dir}/{name}.png")
        if name.startswith(("hole_", "board_")):
            card_info = analyze_card_region(crop, name)
            results[name] = card_info
            color = "#00ff41" if name.startswith("hole") else "#ff6b6b"
            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
            if card_info["rank"] and card_info["rank"] != "?":
                label = f'{card_info["rank"]}{card_info["suit"]}'
            elif card_info["rank"] is None:
                label = "(空)"
            else:
                label = "?"
            draw.text((x1+2, y1+2), label, fill=color)
        else:
            if HAS_TESS:
                gray = crop.convert("L")
                cfg = r'--psm 7 -c tessedit_char_whitelist=0123456789,/'
                try:
                    text = pytesseract.image_to_string(gray, config=cfg).strip()
                except Exception:
                    text = ""
            else:
                text = ""
            results[name] = {"text": text}
            draw.rectangle([x1, y1, x2, y2], outline="#ffd700", width=2)
            draw.text((x1+2, y1+2), name, fill="#ffd700")
    if save_debug:
        annotated.save(f"{debug_dir}/_annotated.png")
        print(f"[DEBUG] 除錯截圖存至 {debug_dir}/")
    return results, annotated

# ─────────────────────────────────────────────────────────────
# OUTPUT FORMATTER
# ─────────────────────────────────────────────────────────────
def format_results(results):
    print("\n" + "="*50)
    print("  辨識結果")
    print("="*50)
    hole = []
    board = []
    for i in range(1, 3):
        key = f"hole_{i}"
        info = results.get(key, {})
        r, s = info.get("rank", "?"), info.get("suit", "?")
        conf = info.get("confidence", 0)
        if r and r != "?" and s:
            card_str = f"{r}{s}"
            hole.append(card_str)
            color_tag = "紅" if info.get("color") == "red" else "黑"
            print(f"  手牌 {i}: {card_str:4s} ({color_tag}) 信心={conf:.0%}")
        else:
            print(f"  手牌 {i}: 未辨識")
    for i in range(1, 6):
        key = f"board_{i}"
        info = results.get(key, {})
        r, s = info.get("rank"), info.get("suit")
        if r is None:
            print(f"  公共牌 {i}: (空)")
            continue
        if r and r != "?" and s:
            card_str = f"{r}{s}"
            board.append(card_str)
            color_tag = "紅" if info.get("color") == "red" else "黑"
            print(f"  公共牌 {i}: {card_str:4s} ({color_tag})")
        else:
            print(f"  公共牌 {i}: 未辨識")
    pot_text = results.get("pot", {}).get("text", "?")
    blind_text = results.get("blind", {}).get("text", "?")
    stack_text = results.get("my_stack", {}).get("text", "?")
    print(f"\n  底池: {pot_text}  |  Blind: {blind_text}  |  我的籌碼: {stack_text}")
    print("\n  ── 可複製到 Advisor HTML ──")
    print(f"  手牌: {'  '.join(hole) if hole else '(無)'}")
    print(f"  公共牌: {'  '.join(board) if board else '(無)'}")
    output = {
        "hole": hole,
        "board": board,
        "pot": pot_text,
        "blind": blind_text,
        "stack": stack_text,
        "timestamp": time.strftime("%H:%M:%S")
    }
    json_path = Path("last_read.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n  結果已存至: {json_path.absolute()}")
    return output

# ─────────────────────────────────────────────────────────────
# HOTKEY MODE
# ─────────────────────────────────────────────────────────────
def on_f9():
    print("\n[F9] 偵測到快捷鍵！")
    try:
        res, img = capture_and_analyze(save_debug=True)
        format_results(res)
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback; traceback.print_exc()

def run_hotkey_mode():
    print("\n" + "╔"+"═"*48+"╗")
    print("║  GameSofa Screen Reader — 快捷鍵模式        ║")
    print("╠"+"═"*48+"╣")
    print("║  F9  → 截圖並辨識手牌 / 公共牌             ║")
    print("║  F10 → 離開程式                            ║")
    print("╚"+"═"*48+"╝")
    print("\n[READY] 等待按鍵中... 請切換到瀏覽器後按 F9")
    keyboard.add_hotkey("f9", on_f9)
    keyboard.add_hotkey("f10", lambda: (print("\n[BYE] 離開"), os._exit(0)))
    keyboard.wait()

def run_manual_mode():
    print("\n[Manual Mode] 輸入指令:")
    print("  c / capture → 截圖辨識")
    print("  q / quit    → 離開")
    while True:
        cmd = input("\n> ").strip().lower()
        if cmd in ("c", "capture"):
            try:
                res, img = capture_and_analyze(save_debug=True)
                format_results(res)
            except Exception as e:
                print(f"[ERROR] {e}")
        elif cmd in ("q", "quit", "exit"):
            print("Bye!")
            break
        else:
            print("未知指令，輸入 c 截圖 或 q 離開")

# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  GameSofa Texas Hold'em Screen Reader")
    print("  依賴檢查:")
    print(f"    keyboard    : {'✓' if HAS_KEYBOARD else '✗ pip install keyboard'}")
    print(f"    pyautogui   : {'✓' if HAS_PYAUTOGUI else '✗ pip install pyautogui'}")
    print(f"    opencv      : {'✓' if HAS_CV2 else '✗ pip install opencv-python'}")
    print(f"    pytesseract : {'✓' if HAS_TESS else '✗ pip install pytesseract'}")
    if not HAS_PYAUTOGUI:
        print("\n[FATAL] 缺少 pyautogui，請先執行:")
        print("  pip install pyautogui pillow keyboard opencv-python pytesseract")
        sys.exit(1)
    if HAS_KEYBOARD:
        run_hotkey_mode()
    else:
        run_manual_mode()
