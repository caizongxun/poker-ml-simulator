#!/usr/bin/env python3
"""
GameSofa Texas Hold'em Screen Reader
=====================================
按 F9 自動截圖 -> 辨識手牌 & 公共牌 -> 輸出到 advisor HTML

新增 WindowCapture 模組：
  - Windows: win32gui PrintWindow API，視窗不需在前景
  - macOS:   screencapture -l <windowid>，視窗不需在前景
  - fallback: pyautogui 全螢幕截圖（需視窗在前景）

依賴安裝:
    pip install pillow keyboard pyautogui pytesseract opencv-python
    # Windows 背景截圖額外需要:
    pip install pywin32
    # Tesseract OCR: https://github.com/UB-Mannheim/tesseract/wiki (Windows)
    # macOS: brew install tesseract
    # Ubuntu: sudo apt install tesseract-ocr

用法:
    python screen_reader.py
    => 自動尋找 GameSofa 視窗，按 F9 擷取（不需切換到前景）
"""

import sys, os, time, json, re, subprocess, tempfile
from pathlib import Path

# ── Optional imports ───────────────────────────────────────────
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
    if sys.platform == "win32":
        pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
except ImportError:
    HAS_TESS = False
    print("[WARN] pytesseract 未安裝，使用顏色分析 fallback")

# Windows 背景截圖依賴
HAS_WIN32 = False
if sys.platform == "win32":
    try:
        import win32gui, win32ui, win32con
        from ctypes import windll
        HAS_WIN32 = True
    except ImportError:
        print("[WARN] pywin32 未安裝，將使用全螢幕截圖\n       pip install pywin32")

from PIL import Image, ImageDraw

# ─────────────────────────────────────────────────────────────
# WINDOW CAPTURE MODULE
# 核心功能：不需視窗在前景即可截圖
# ─────────────────────────────────────────────────────────────

# GameSofa 視窗標題關鍵字（部分符合即可）
WINDOW_KEYWORDS = ["gamesofa", "texas", "hold", "poker", "chrome", "firefox", "edge"]

class WindowCapture:
    """跨平台視窗截圖，支援背景截圖（Windows win32 / macOS screencapture）。"""

    def __init__(self):
        self.hwnd = None          # Windows HWND
        self.mac_window_id = None # macOS CGWindowID
        self.window_title = None
        self._find_window()

    # ── 尋找視窗 ──────────────────────────────────────────────

    def _find_window(self):
        if sys.platform == "win32":
            self._find_window_win32()
        elif sys.platform == "darwin":
            self._find_window_mac()
        else:
            print("[INFO] Linux: 使用全螢幕截圖模式")

    def _find_window_win32(self):
        """列舉所有視窗，找到標題含關鍵字的視窗。"""
        if not HAS_WIN32:
            return
        candidates = []
        def enum_cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).lower()
                for kw in WINDOW_KEYWORDS:
                    if kw in title:
                        candidates.append((hwnd, win32gui.GetWindowText(hwnd)))
                        break
        win32gui.EnumWindows(enum_cb, None)

        if not candidates:
            print("[WARN] 找不到 GameSofa 視窗，將使用全螢幕截圖")
            print("       請確認瀏覽器已開啟 GameSofa 頁面")
            return

        # 優先選擇標題含 gamesofa 的
        for hwnd, title in candidates:
            if "gamesofa" in title.lower():
                self.hwnd = hwnd
                self.window_title = title
                print(f"[WIN] 找到視窗: \"{title}\" (HWND={hwnd})")
                return
        # fallback: 選第一個
        self.hwnd, self.window_title = candidates[0]
        print(f"[WIN] 使用視窗: \"{self.window_title}\" (HWND={self.hwnd})")

    def _find_window_mac(self):
        """用 osascript 找到含關鍵字的瀏覽器視窗 ID。"""
        try:
            result = subprocess.check_output(
                ["osascript", "-e",
                 'tell application "System Events" to get name of every process whose visible is true'],
                text=True
            )
            procs = [p.strip() for p in result.split(",")]
            browsers = [p for p in procs if any(b in p.lower() for b in ["chrome","safari","firefox","edge"])]
            if browsers:
                self.window_title = browsers[0]
                print(f"[MAC] 找到瀏覽器: {browsers[0]}")
        except Exception as e:
            print(f"[WARN] macOS 視窗偵測失敗: {e}")

    def list_windows(self):
        """列出所有可見視窗，供使用者手動選擇。"""
        if sys.platform == "win32" and HAS_WIN32:
            results = []
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t:
                        results.append((hwnd, t))
            win32gui.EnumWindows(cb, None)
            return results
        return []

    def select_window_interactive(self):
        """互動式選擇視窗。"""
        wins = self.list_windows()
        if not wins:
            print("[INFO] 無法列出視窗")
            return
        print("\n[視窗列表] 請選擇要截圖的視窗:")
        for i, (hwnd, title) in enumerate(wins[:20]):
            print(f"  {i+1:2d}. {title[:70]}")
        try:
            idx = int(input("\n輸入編號 (0=取消): ").strip()) - 1
            if 0 <= idx < len(wins):
                self.hwnd, self.window_title = wins[idx]
                print(f"[OK] 已選擇: {self.window_title}")
        except (ValueError, KeyboardInterrupt):
            pass

    # ── 截圖方法 ──────────────────────────────────────────────

    def capture(self) -> Image.Image:
        """截圖：優先使用背景截圖，fallback 到全螢幕。"""
        img = None
        if sys.platform == "win32" and HAS_WIN32 and self.hwnd:
            img = self._capture_win32_background()
        elif sys.platform == "darwin" and self.window_title:
            img = self._capture_mac()
        if img is None:
            img = self._capture_fullscreen()
        return img

    def _capture_win32_background(self) -> Image.Image | None:
        """
        Windows PrintWindow API — 向視窗索取畫面 bitmap，
        完全不需要視窗在前景或最小化恢復。
        """
        try:
            # 取得視窗尺寸
            left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
            w = right - left
            h = bottom - top
            if w <= 0 or h <= 0:
                return None

            # 建立裝置 context 和 bitmap
            hwnd_dc   = win32gui.GetWindowDC(self.hwnd)
            mfc_dc    = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc   = mfc_dc.CreateCompatibleDC()
            bitmap    = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bitmap)

            # PW_RENDERFULLCONTENT=2 可截到 Chrome/Edge 的 GPU 渲染內容
            result = windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), 2)
            if result == 0:
                # fallback: PW_CLIENTONLY=1
                result = windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), 1)

            bmp_info = bitmap.GetInfo()
            bmp_str  = bitmap.GetBitmapBits(True)
            img = Image.frombuffer(
                "RGB",
                (bmp_info["bmWidth"], bmp_info["bmHeight"]),
                bmp_str, "raw", "BGRX", 0, 1
            )
            # 清理
            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwnd_dc)

            if result == 0:
                print("[WARN] PrintWindow 回傳 0，影像可能是空白，改用全螢幕截圖")
                return None
            print(f"[WIN32] 背景截圖成功 {img.size}（視窗: {self.window_title}）")
            return img
        except Exception as e:
            print(f"[WARN] win32 截圖失敗: {e}")
            return None

    def _capture_mac(self) -> Image.Image | None:
        """macOS: 用 screencapture 指定視窗截圖（需 Accessibility 權限）。"""
        try:
            tmp = tempfile.mktemp(suffix=".png")
            # 先用 AppleScript 取得視窗 ID
            script = f'''
            tell application "{self.window_title}"
                set winID to id of window 1
            end tell
            return winID
            '''
            win_id = subprocess.check_output(
                ["osascript", "-e", script], text=True
            ).strip()
            subprocess.run(
                ["screencapture", "-l", win_id, "-x", tmp],
                check=True, capture_output=True
            )
            img = Image.open(tmp)
            os.unlink(tmp)
            print(f"[MAC] 背景截圖成功 {img.size}")
            return img
        except Exception as e:
            print(f"[WARN] macOS 截圖失敗: {e}")
            return None

    def _capture_fullscreen(self) -> Image.Image:
        """Fallback: pyautogui 全螢幕截圖（需視窗在前景）。"""
        if HAS_PYAUTOGUI:
            print("[SNAP] 全螢幕截圖（視窗需在前景）")
            return pyautogui.screenshot()
        raise RuntimeError("無法截圖：pyautogui 未安裝")

    def get_client_rect(self):
        """取得視窗客戶區相對座標（扣除標題列/邊框）。"""
        if sys.platform == "win32" and HAS_WIN32 and self.hwnd:
            try:
                cl = win32gui.GetClientRect(self.hwnd)
                wl = win32gui.GetWindowRect(self.hwnd)
                # 標題列高度 = window_top_to_client_top
                border_x = (wl[2]-wl[0] - cl[2]) // 2
                title_h  = (wl[3]-wl[1]) - cl[3] - border_x
                return {"border_x": border_x, "title_h": title_h,
                        "client_w": cl[2], "client_h": cl[3]}
            except Exception:
                pass
        return {"border_x": 0, "title_h": 0, "client_w": 0, "client_h": 0}

# 全域 WindowCapture 實例
_wc = None

def get_window_capture() -> WindowCapture:
    global _wc
    if _wc is None:
        _wc = WindowCapture()
    return _wc

# ─────────────────────────────────────────────────────────────
# CONFIGURATION  (依截圖尺寸 1214x915 設定)
# ─────────────────────────────────────────────────────────────
SCREENSHOT_W = 1214
SCREENSHOT_H = 915

REGIONS = {
    "hole_1":   (636, 540, 730, 670),
    "hole_2":   (718, 540, 815, 670),
    "board_1":  (362, 345, 462, 465),
    "board_2":  (470, 345, 570, 465),
    "board_3":  (578, 345, 678, 465),
    "board_4":  (686, 345, 786, 465),
    "board_5":  (794, 345, 894, 465),
    "pot":      (520, 278, 700, 320),
    "my_stack": (476, 670, 640, 712),
    "blind":    (980, 108, 1210, 165),
}

# ─────────────────────────────────────────────────────────────
# SUIT DETECTION BY COLOR (HSV)
# ─────────────────────────────────────────────────────────────

def detect_suit_by_color(crop_img):
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
def scale_region(region, actual_w, actual_h, border_x=0, title_h=0):
    """
    將基準座標縮放到實際截圖尺寸。
    border_x / title_h: win32 視窗模式下扣除邊框偏移。
    """
    x1, y1, x2, y2 = region
    # 扣除標題列和邊框
    x1 -= border_x; x2 -= border_x
    y1 -= title_h;  y2 -= title_h
    sx = actual_w / SCREENSHOT_W
    sy = actual_h / SCREENSHOT_H
    return (int(x1*sx), int(y1*sy), int(x2*sx), int(y2*sy))

# ─────────────────────────────────────────────────────────────
# MAIN CAPTURE
# ─────────────────────────────────────────────────────────────
def capture_and_analyze(save_debug=True, debug_dir="debug_crops"):
    wc = get_window_capture()

    # 使用 WindowCapture（支援背景截圖）
    screenshot = wc.capture()
    actual_w, actual_h = screenshot.size
    cr = wc.get_client_rect()
    print(f"[INFO] 截圖尺寸: {actual_w}x{actual_h}")

    annotated = screenshot.copy()
    draw = ImageDraw.Draw(annotated)
    if save_debug:
        Path(debug_dir).mkdir(exist_ok=True)

    results = {}
    for name, region in REGIONS.items():
        scaled = scale_region(
            region, actual_w, actual_h,
            border_x=cr["border_x"],
            title_h=cr["title_h"]
        )
        x1, y1, x2, y2 = scaled
        # 邊界保護
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
    pot_text   = results.get("pot", {}).get("text", "?")
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

def on_f8():
    """F8: 互動式重新選擇視窗。"""
    wc = get_window_capture()
    wc.select_window_interactive()

def run_hotkey_mode():
    print("\n" + "╔"+"═"*52+"╗")
    print("║  GameSofa Screen Reader — 背景截圖模式          ║")
    print("╠"+"═"*52+"╣")
    wc = get_window_capture()
    if wc.window_title:
        title_short = wc.window_title[:40]
        print(f"║  視窗: {title_short:<44} ║")
    print("║  F8  → 重新選擇視窗                              ║")
    print("║  F9  → 截圖並辨識（背景視窗，不需切換前景）      ║")
    print("║  F10 → 離開程式                                   ║")
    print("╚"+"═"*52+"╝")
    keyboard.add_hotkey("f8",  on_f8)
    keyboard.add_hotkey("f9",  on_f9)
    keyboard.add_hotkey("f10", lambda: (print("\n[BYE] 離開"), os._exit(0)))
    keyboard.wait()

def run_manual_mode():
    print("\n[Manual Mode] 輸入指令:")
    print("  c / capture → 截圖辨識")
    print("  w / window  → 選擇視窗")
    print("  q / quit    → 離開")
    while True:
        cmd = input("\n> ").strip().lower()
        if cmd in ("c", "capture"):
            try:
                res, img = capture_and_analyze(save_debug=True)
                format_results(res)
            except Exception as e:
                print(f"[ERROR] {e}")
        elif cmd in ("w", "window"):
            get_window_capture().select_window_interactive()
        elif cmd in ("q", "quit", "exit"):
            print("Bye!")
            break
        else:
            print("未知指令，輸入 c 截圖 / w 選視窗 / q 離開")

# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  GameSofa Texas Hold'em Screen Reader v2")
    print("  依賴檢查:")
    print(f"    keyboard    : {'✓' if HAS_KEYBOARD    else '✗ pip install keyboard'}")
    print(f"    pyautogui   : {'✓' if HAS_PYAUTOGUI   else '✗ pip install pyautogui'}")
    print(f"    opencv      : {'✓' if HAS_CV2         else '✗ pip install opencv-python'}")
    print(f"    pytesseract : {'✓' if HAS_TESS        else '✗ pip install pytesseract'}")
    print(f"    pywin32     : {'✓ (背景截圖)' if HAS_WIN32 else '✗ pip install pywin32  ← Windows 背景截圖需要'}")
    print()

    if not HAS_PYAUTOGUI and not HAS_WIN32:
        print("[FATAL] 缺少截圖依賴，請執行:")
        print("  pip install pyautogui pillow keyboard opencv-python pytesseract pywin32")
        sys.exit(1)

    if HAS_KEYBOARD:
        run_hotkey_mode()
    else:
        run_manual_mode()
