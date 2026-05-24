#!/usr/bin/env python3
"""
GameSofa Texas Hold'em Screen Reader
=====================================
按 F9 手動截圖 / F7 切換自動輪詢 -> 辨識手牌 & 公共牌 -> 輸出 last_read.json

座標基準: 相對比例座標 (0.0~1.0)，不再依賴固定視窗尺寸
"""

import sys, os, time, json, re, subprocess, tempfile, threading
from pathlib import Path

# ── Optional imports ──────────────────────────────────────────
try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False
    print("[WARN] keyboard 未安裝")

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
    print("[WARN] opencv-python 未安裝")

try:
    import pytesseract
    HAS_TESS = True
    if sys.platform == "win32":
        pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
except ImportError:
    HAS_TESS = False
    print("[WARN] pytesseract 未安裝")

HAS_WIN32 = False
if sys.platform == "win32":
    try:
        import win32gui, win32ui, win32con
        from ctypes import windll
        HAS_WIN32 = True
    except ImportError:
        print("[WARN] pywin32 未安裝，pip install pywin32")

from PIL import Image, ImageDraw, ImageFilter

# ─────────────────────────────────────────────────────────────
# WINDOW CAPTURE
# ─────────────────────────────────────────────────────────────
WINDOW_KEYWORDS = ["gamesofa", "神來也", "texas", "德州", "poker",
                   "chrome", "firefox", "edge", "msedge"]

class WindowCapture:
    def __init__(self):
        self.hwnd = None
        self.mac_window_id = None
        self.window_title = None
        self._find_window()

    def _find_window(self):
        if sys.platform == "win32":
            self._find_window_win32()
        elif sys.platform == "darwin":
            self._find_window_mac()

    def _find_window_win32(self):
        if not HAS_WIN32:
            return
        candidates = []
        def enum_cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if any(kw in title.lower() for kw in WINDOW_KEYWORDS):
                    candidates.append((hwnd, title))
        win32gui.EnumWindows(enum_cb, None)
        if not candidates:
            print("[WARN] 找不到 GameSofa 視窗，將用全螢幕截圖")
            return
        for hwnd, title in candidates:
            if any(k in title.lower() for k in ["gamesofa","神來也","德州","texas"]):
                self.hwnd, self.window_title = hwnd, title
                print(f"[WIN] 找到視窗: \"{title}\"")
                return
        self.hwnd, self.window_title = candidates[0]
        print(f"[WIN] 使用視窗: \"{self.window_title}\"")

    def _find_window_mac(self):
        try:
            result = subprocess.check_output(
                ["osascript","-e",'tell application "System Events" to get name of every process whose visible is true'],
                text=True)
            procs = [p.strip() for p in result.split(",")]
            for p in procs:
                if any(b in p.lower() for b in ["chrome","safari","firefox","edge"]):
                    self.window_title = p
                    print(f"[MAC] 找到瀏覽器: {p}")
                    return
        except Exception as e:
            print(f"[WARN] macOS 視窗偵測失敗: {e}")

    def list_windows(self):
        if sys.platform == "win32" and HAS_WIN32:
            results = []
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t: results.append((hwnd, t))
            win32gui.EnumWindows(cb, None)
            return results
        return []

    def select_window_interactive(self):
        wins = self.list_windows()
        if not wins:
            print("[INFO] 無法列出視窗")
            return
        print("\n[視窗列表]")
        for i,(hwnd,title) in enumerate(wins[:20]):
            print(f"  {i+1:2d}. {title[:70]}")
        try:
            idx = int(input("\n編號 (0=取消): ").strip()) - 1
            if 0 <= idx < len(wins):
                self.hwnd, self.window_title = wins[idx]
                print(f"[OK] 已選: {self.window_title}")
        except (ValueError, KeyboardInterrupt):
            pass

    def capture(self) -> Image.Image:
        img = None
        if sys.platform == "win32" and HAS_WIN32 and self.hwnd:
            img = self._capture_win32_background()
        elif sys.platform == "darwin" and self.window_title:
            img = self._capture_mac()
        if img is None:
            img = self._capture_fullscreen()
        return img

    def _capture_win32_background(self) -> "Image.Image | None":
        try:
            left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
            w, h = right-left, bottom-top
            if w <= 0 or h <= 0: return None
            hwnd_dc = win32gui.GetWindowDC(self.hwnd)
            mfc_dc  = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bitmap  = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bitmap)
            result = windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), 2)
            if result == 0:
                result = windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), 1)
            bmp_info = bitmap.GetInfo()
            bmp_str  = bitmap.GetBitmapBits(True)
            img = Image.frombuffer("RGB",(bmp_info["bmWidth"],bmp_info["bmHeight"]),
                                   bmp_str,"raw","BGRX",0,1)
            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC(); mfc_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwnd_dc)
            if result == 0:
                print("[WARN] PrintWindow 失敗，改用全螢幕")
                return None
            return img
        except Exception as e:
            print(f"[WARN] win32 截圖失敗: {e}")
            return None

    def _capture_mac(self) -> "Image.Image | None":
        try:
            tmp = tempfile.mktemp(suffix=".png")
            script = f'tell application "{self.window_title}"\nset winID to id of window 1\nend tell\nreturn winID'
            win_id = subprocess.check_output(["osascript","-e",script],text=True).strip()
            subprocess.run(["screencapture","-l",win_id,"-x",tmp],check=True,capture_output=True)
            img = Image.open(tmp); os.unlink(tmp)
            return img
        except Exception as e:
            print(f"[WARN] macOS 截圖失敗: {e}"); return None

    def _capture_fullscreen(self) -> Image.Image:
        if HAS_PYAUTOGUI:
            return pyautogui.screenshot()
        raise RuntimeError("無法截圖：pyautogui 未安裝")

_wc = None
def get_window_capture() -> WindowCapture:
    global _wc
    if _wc is None: _wc = WindowCapture()
    return _wc

# ─────────────────────────────────────────────────────────────
# REGIONS  ── 相對比例座標 (0.0 ~ 1.0)
# 基準: 1264×952 Chrome 視窗 (含標題列+書籤列)
# 使用比例座標後，任何視窗尺寸都能正確對應
# ─────────────────────────────────────────────────────────────
REGIONS_REL = {
    # ── 手牌 (自己，畫面正中央偏下) ──
    "hole_1":   (0.5024, 0.6492, 0.5578, 0.7857),
    "hole_2":   (0.5554, 0.6492, 0.6108, 0.7857),

    # ── 公共牌 (牌桌中央，最多5張) ──
    "board_1":  (0.2991, 0.4496, 0.3600, 0.5756),
    "board_2":  (0.3655, 0.4496, 0.4264, 0.5756),
    "board_3":  (0.4351, 0.4496, 0.4960, 0.5756),
    "board_4":  (0.5032, 0.4496, 0.5759, 0.5756),
    "board_5":  (0.5728, 0.4496, 0.6329, 0.5756),

    # ── 底池 ──
    "pot":      (0.4193, 0.3803, 0.5775, 0.4181),

    # ── 自己籌碼 ──
    "my_stack": (0.3877, 0.7931, 0.5301, 0.8351),

    # ── Blind (右上角「25/50」) ──
    "blind":    (0.7832, 0.2290, 0.9059, 0.2710),
}

def abs_region(rel, actual_w, actual_h):
    """相對比例 → 絕對像素座標"""
    rx1, ry1, rx2, ry2 = rel
    return (
        int(rx1 * actual_w), int(ry1 * actual_h),
        int(rx2 * actual_w), int(ry2 * actual_h),
    )

# ─────────────────────────────────────────────────────────────
# SUIT DETECTION
# ─────────────────────────────────────────────────────────────
def detect_suit_by_color(crop_img):
    if HAS_CV2:
        arr = np.array(crop_img.convert("RGB"))
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        m1 = cv2.inRange(hsv, np.array([0,100,80]),   np.array([15,255,255]))
        m2 = cv2.inRange(hsv, np.array([160,100,80]), np.array([180,255,255]))
        red_px  = cv2.countNonZero(m1) + cv2.countNonZero(m2)
        dark_m  = cv2.inRange(hsv, np.array([0,0,0]),  np.array([180,80,80]))
        dark_px = cv2.countNonZero(dark_m)
        return "red" if red_px > dark_px * 0.3 else "black"
    else:
        arr = crop_img.convert("RGB")
        w, h = arr.size
        red = black = 0
        for y in range(h):
            for x in range(w):
                r,g,b = arr.getpixel((x,y))
                if r>150 and g<100 and b<100: red+=1
                elif r<80 and g<80 and b<80:  black+=1
        return "red" if red > black*0.3 else "black"

# ─────────────────────────────────────────────────────────────
# RANK DETECTION  (加強 OCR 前處理)
# ─────────────────────────────────────────────────────────────
RANK_MAP = {
    'a':'A','1':'A','2':'2','3':'3','4':'4','5':'5',
    '6':'6','7':'7','8':'8','9':'9',
    '0':'T','t':'T','10':'T',
    'j':'J','q':'Q','k':'K',
}
SUIT_FALLBACK = {"red":"♥","black":"♠"}

def preprocess_for_ocr(crop_img):
    """裁左上角 + 放大 + 二值化 + 去雜訊"""
    w, h = crop_img.size
    corner = crop_img.crop((0, 0, int(w * 0.45), int(h * 0.40)))
    # 放大 4x
    corner = corner.resize((corner.width * 4, corner.height * 4), Image.LANCZOS)
    # 轉灰階
    corner = corner.convert("L")
    # Otsu 二值化（用 numpy/cv2，否則用簡單閾值）
    if HAS_CV2:
        arr = np.array(corner)
        _, binary = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # 白底黑字：若背景偏暗則反轉
        if binary.mean() < 127:
            binary = 255 - binary
        # 輕微 erosion 去雜點
        kernel = np.ones((2, 2), np.uint8)
        binary = cv2.erode(binary, kernel, iterations=1)
        corner = Image.fromarray(binary)
    else:
        corner = corner.point(lambda p: 255 if p > 128 else 0, "1")
    return corner

def detect_rank_ocr(crop_img):
    if not HAS_TESS: return "?"
    corner = preprocess_for_ocr(crop_img)
    cfg = r'--psm 10 -c tessedit_char_whitelist=AaKkQqJjTt0123456789'
    try:
        text = pytesseract.image_to_string(corner, config=cfg).strip().lower()
        text = ''.join(c for c in text if c.isalnum())
        if text in RANK_MAP: return RANK_MAP[text]
        if len(text) >= 2 and text[:2] in RANK_MAP: return RANK_MAP[text[:2]]
        if text: return RANK_MAP.get(text[0], "?")
        return "?"
    except Exception: return "?"

# ─────────────────────────────────────────────────────────────
# EMPTY SLOT DETECTION  (修正 HSV 範圍 + 加入白色牌面檢查)
# ─────────────────────────────────────────────────────────────
def is_empty_slot(crop_img):
    """
    判斷是否為空牌槽（顯示桌面背景而非牌面）
    GameSofa 桌面顏色: 深藍綠 (teal/dark-teal)
    牌面: 白色為主
    """
    if HAS_CV2:
        arr = np.array(crop_img.convert("RGB"))
        total_px = arr.shape[0] * arr.shape[1]

        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)

        # 偵測桌面 teal（含深色到亮色範圍，GameSofa 深藍綠約 H=160~200→HSV H=80~100）
        teal1 = cv2.inRange(hsv, np.array([75,  30,  40]), np.array([110, 255, 220]))
        # 深藍綠偏暗版
        teal2 = cv2.inRange(hsv, np.array([85,  20,  20]), np.array([115, 180, 120]))
        teal_px = cv2.countNonZero(teal1) + cv2.countNonZero(teal2)
        teal_ratio = teal_px / total_px

        # 白色牌面像素（白底）
        white_mask = cv2.inRange(arr,
                                  np.array([200, 200, 200]),
                                  np.array([255, 255, 255]))
        white_ratio = cv2.countNonZero(white_mask) / total_px

        # 如果 teal 佔多 OR 白色非常少 → 空槽
        if teal_ratio > 0.40:
            return True
        if white_ratio < 0.10 and teal_ratio > 0.20:
            return True
        return False
    else:
        arr = crop_img.convert("RGB")
        w, h = arr.size
        teal = white = total = 0
        for y in range(0, h, 3):
            for x in range(0, w, 3):
                r, g, b = arr.getpixel((x, y))
                total += 1
                if 0 < r < 140 and 100 < g < 220 and 80 < b < 200:
                    teal += 1
                if r > 200 and g > 200 and b > 200:
                    white += 1
        t = max(total, 1)
        if teal / t > 0.40: return True
        if white / t < 0.10 and teal / t > 0.20: return True
        return False

def analyze_card_region(crop_img, region_name):
    result = {"rank":"?","suit":"?","color":"unknown","confidence":0.0,"region":region_name}
    if is_empty_slot(crop_img):
        result["rank"] = None; result["suit"] = None
        return result
    color = detect_suit_by_color(crop_img)
    result["color"] = color
    result["suit"]  = SUIT_FALLBACK[color]
    rank = detect_rank_ocr(crop_img)
    result["rank"]  = rank
    result["confidence"] = 0.9 if rank != "?" else 0.3
    return result

# ─────────────────────────────────────────────────────────────
# MAIN CAPTURE
# ─────────────────────────────────────────────────────────────
def capture_and_analyze(save_debug=True, debug_dir="debug_crops"):
    wc = get_window_capture()
    screenshot = wc.capture()
    actual_w, actual_h = screenshot.size

    annotated = screenshot.copy()
    draw = ImageDraw.Draw(annotated)
    if save_debug:
        Path(debug_dir).mkdir(exist_ok=True)

    results = {}
    for name, rel in REGIONS_REL.items():
        x1, y1, x2, y2 = abs_region(rel, actual_w, actual_h)
        x1 = max(0, x1); y1 = max(0, y1)
        x2 = min(actual_w, x2); y2 = min(actual_h, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        crop = screenshot.crop((x1, y1, x2, y2))
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
            draw.text((x1 + 2, y1 + 2), label, fill=color)
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
            draw.text((x1 + 2, y1 + 2), name, fill="#ffd700")

    if save_debug:
        annotated.save(f"{debug_dir}/_annotated.png")
        print(f"[DEBUG] 除錯截圖存至 {debug_dir}/")
    return results, annotated

# ─────────────────────────────────────────────────────────────
# OUTPUT FORMATTER
# ─────────────────────────────────────────────────────────────
def format_results(results):
    print("\n" + "="*52)
    print("  辨識結果")
    print("="*52)
    hole = []; board = []
    for i in range(1, 3):
        info = results.get(f"hole_{i}", {})
        r, s = info.get("rank", "?"), info.get("suit", "?")
        conf = info.get("confidence", 0)
        if r and r != "?" and s:
            hole.append(f"{r}{s}")
            tag = "紅" if info.get("color") == "red" else "黑"
            print(f"  手牌 {i}: {r}{s:4s} ({tag}) 信心={conf:.0%}")
        else:
            print(f"  手牌 {i}: 未辨識")
    for i in range(1, 6):
        info = results.get(f"board_{i}", {})
        r, s = info.get("rank"), info.get("suit")
        if r is None:
            print(f"  公共牌 {i}: (空)")
            continue
        if r and r != "?" and s:
            board.append(f"{r}{s}")
            tag = "紅" if info.get("color") == "red" else "黑"
            print(f"  公共牌 {i}: {r}{s:4s} ({tag})")
        else:
            print(f"  公共牌 {i}: 未辨識")
    pot   = results.get("pot",      {}).get("text", "?")
    blind = results.get("blind",    {}).get("text", "?")
    stack = results.get("my_stack", {}).get("text", "?")
    print(f"\n  底池: {pot}  |  Blind: {blind}  |  籌碼: {stack}")
    print(f"  手牌: {'  '.join(hole) if hole else '(無)'}")
    print(f"  公共牌: {'  '.join(board) if board else '(無)'}")

    output = {
        "hole_cards": hole,
        "board":      board,
        "pot":        pot,
        "blind":      blind,
        "stack":      stack,
        "timestamp":  time.strftime("%H:%M:%S"),
    }
    with open("last_read.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    return output

# ─────────────────────────────────────────────────────────────
# HOTKEY / AUTO-POLL MODE
# ─────────────────────────────────────────────────────────────
_auto_running = False
_auto_thread  = None
_poll_interval = 1.0

def _auto_poll_loop():
    global _auto_running
    while _auto_running:
        try:
            res, _ = capture_and_analyze(save_debug=False)
            format_results(res)
        except Exception as e:
            print(f"[AUTO ERR] {e}")
        time.sleep(_poll_interval)

def toggle_auto():
    global _auto_running, _auto_thread
    if _auto_running:
        _auto_running = False
        print("[F7] 自動輪詢 ▶ 已停止")
    else:
        _auto_running = True
        _auto_thread = threading.Thread(target=_auto_poll_loop, daemon=True)
        _auto_thread.start()
        print(f"[F7] 自動輪詢 ▶ 啟動 (每 {_poll_interval}s)")

def on_f9():
    print("\n[F9] 手動截圖")
    try:
        res, _ = capture_and_analyze(save_debug=True)
        format_results(res)
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback; traceback.print_exc()

def on_f8():
    get_window_capture().select_window_interactive()

def run_hotkey_mode():
    print("\n╔" + "═"*54 + "╗")
    print("║  GameSofa Screen Reader v4 — 比例座標版              ║")
    print("╠" + "═"*54 + "╣")
    wc = get_window_capture()
    if wc.window_title:
        print(f"║  視窗: {wc.window_title[:46]:<46} ║")
    print("║  F7  → 切換自動輪詢 (每秒辨識)                     ║")
    print("║  F8  → 重新選擇視窗                                 ║")
    print("║  F9  → 單次截圖辨識                                 ║")
    print("║  F10 → 離開                                         ║")
    print("╚" + "═"*54 + "╝")
    keyboard.add_hotkey("f7",  toggle_auto)
    keyboard.add_hotkey("f8",  on_f8)
    keyboard.add_hotkey("f9",  on_f9)
    keyboard.add_hotkey("f10", lambda: (print("\n[BYE]"), os._exit(0)))
    keyboard.wait()

def run_manual_mode():
    print("\n[Manual Mode]  c=截圖  a=切換自動  w=選視窗  q=離開")
    while True:
        cmd = input("\n> ").strip().lower()
        if cmd in ("c", "capture"):
            try:
                res, _ = capture_and_analyze(save_debug=True)
                format_results(res)
            except Exception as e:
                print(f"[ERROR] {e}")
        elif cmd in ("a", "auto"):
            toggle_auto()
        elif cmd in ("w", "window"):
            get_window_capture().select_window_interactive()
        elif cmd in ("q", "quit", "exit"):
            print("Bye!"); break
        else:
            print("未知指令")

# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  GameSofa Screen Reader v4 (比例座標版)")
    print(f"    keyboard    : {'✓' if HAS_KEYBOARD  else '✗ pip install keyboard'}")
    print(f"    pyautogui   : {'✓' if HAS_PYAUTOGUI else '✗ pip install pyautogui'}")
    print(f"    opencv      : {'✓' if HAS_CV2       else '✗ pip install opencv-python'}")
    print(f"    pytesseract : {'✓' if HAS_TESS      else '✗ pip install pytesseract'}")
    print(f"    pywin32     : {'✓ (背景截圖)' if HAS_WIN32 else '✗ pip install pywin32'}")
    print()
    if HAS_KEYBOARD:
        run_hotkey_mode()
    else:
        run_manual_mode()
