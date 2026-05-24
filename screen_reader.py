#!/usr/bin/env python3
"""
GameSofa Texas Hold'em Screen Reader
=====================================
按 F9 手動截圖 / F7 切換自動輪詢 -> 辨識手牌 & 公共牌 -> 輸出 last_read.json

座標基準: Chrome 視窗 1264x952 (GameSofa html5_v2)
實際 tile 尺寸視窗縮放而自動 scale。

安裝:
    pip install pillow keyboard pyautogui pytesseract opencv-python pywin32
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

from PIL import Image, ImageDraw

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

    def get_client_rect(self):
        if sys.platform == "win32" and HAS_WIN32 and self.hwnd:
            try:
                cl = win32gui.GetClientRect(self.hwnd)
                wl = win32gui.GetWindowRect(self.hwnd)
                border_x = (wl[2]-wl[0]-cl[2])//2
                title_h  = (wl[3]-wl[1])-cl[3]-border_x
                return {"border_x":border_x,"title_h":title_h,
                        "client_w":cl[2],"client_h":cl[3]}
            except Exception:
                pass
        return {"border_x":0,"title_h":0,"client_w":0,"client_h":0}

_wc = None
def get_window_capture() -> WindowCapture:
    global _wc
    if _wc is None: _wc = WindowCapture()
    return _wc

# ─────────────────────────────────────────────────────────────
# REGIONS  (基準: 1264x952 Chrome 視窗, GameSofa html5_v2)
# ─────────────────────────────────────────────────────────────
# 座標由截圖量測，含 Chrome 標題列 (~140px) + 書籤列 (~37px)
# client 區域起點約 y=177 (標題列+書籤列+工具列)
# 牌桌有效範圍: x:10~1254, y:177~952

SCREENSHOT_W = 1264   # Chrome 視窗寬
SCREENSHOT_H = 952    # Chrome 視窗高（含標題列）

REGIONS = {
    # ── 手牌 (自己，畫面正中央偏下) ──
    # 截圖可見: 4♥ 在 x≈638~700, 4♦ 在 x≈700~762, y≈615~745
    "hole_1":   (635, 618, 705, 748),
    "hole_2":   (702, 618, 772, 748),

    # ── 公共牌 (牌桌中央, 4張可見) ──
    # 6♠ x≈380~450, 7♦ x≈466~536, 5♥ x≈555~625, K♠ x≈644~720, y≈428~545
    "board_1":  (378, 428, 455, 548),
    "board_2":  (462, 428, 539, 548),
    "board_3":  (550, 428, 627, 548),
    "board_4":  (636, 428, 728, 548),
    "board_5":  (724, 428, 800, 548),

    # ── 底池 (牌桌中央上方，「825」字樣) ──
    "pot":      (530, 362, 730, 398),

    # ── 自己籌碼 (左下角「7,750」) ──
    "my_stack": (490, 755, 670, 795),

    # ── Blind (右上角「25/50」) ──
    # 在牌桌資訊區，不是書籤列
    "blind":    (990, 218, 1145, 258),
}

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
# RANK DETECTION
# ─────────────────────────────────────────────────────────────
RANK_MAP = {
    'a':'A','1':'A','2':'2','3':'3','4':'4','5':'5',
    '6':'6','7':'7','8':'8','9':'9',
    '0':'T','t':'T','10':'T',
    'j':'J','q':'Q','k':'K',
}
SUIT_FALLBACK = {"red":"♥","black":"♠"}

def detect_rank_ocr(crop_img):
    if not HAS_TESS: return "?"
    w,h = crop_img.size
    corner = crop_img.crop((0, 0, int(w*0.45), int(h*0.42)))
    corner = corner.convert("L")
    corner = corner.resize((corner.width*3, corner.height*3), Image.LANCZOS)
    cfg = r'--psm 10 -c tessedit_char_whitelist=AaKkQqJjTt0123456789'
    try:
        text = pytesseract.image_to_string(corner, config=cfg).strip().lower()
        text = ''.join(c for c in text if c.isalnum())
        if text in RANK_MAP: return RANK_MAP[text]
        if len(text)>=2 and text[:2] in RANK_MAP: return RANK_MAP[text[:2]]
        if text: return RANK_MAP.get(text[0], "?")
        return "?"
    except Exception: return "?"

def is_empty_slot(crop_img):
    if HAS_CV2:
        arr = np.array(crop_img.convert("RGB"))
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        teal_mask = cv2.inRange(hsv, np.array([80,40,80]), np.array([110,200,210]))
        teal_ratio = cv2.countNonZero(teal_mask) / (arr.shape[0]*arr.shape[1])
        return teal_ratio > 0.55
    else:
        arr = crop_img.convert("RGB")
        w,h = arr.size
        teal=total=0
        for y in range(0,h,4):
            for x in range(0,w,4):
                r,g,b = arr.getpixel((x,y))
                total+=1
                if 0<r<130 and 120<g<220 and 100<b<200: teal+=1
        return (teal/max(total,1))>0.5

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
    result["confidence"] = 0.9 if rank!="?" else 0.3
    return result

# ─────────────────────────────────────────────────────────────
# SCALE REGIONS
# ─────────────────────────────────────────────────────────────
def scale_region(region, actual_w, actual_h, border_x=0, title_h=0):
    x1,y1,x2,y2 = region
    x1-=border_x; x2-=border_x
    y1-=title_h;  y2-=title_h
    sx = actual_w / SCREENSHOT_W
    sy = actual_h / SCREENSHOT_H
    return (int(x1*sx), int(y1*sy), int(x2*sx), int(y2*sy))

# ─────────────────────────────────────────────────────────────
# MAIN CAPTURE
# ─────────────────────────────────────────────────────────────
def capture_and_analyze(save_debug=True, debug_dir="debug_crops"):
    wc = get_window_capture()
    screenshot = wc.capture()
    actual_w, actual_h = screenshot.size
    cr = wc.get_client_rect()

    annotated = screenshot.copy()
    draw = ImageDraw.Draw(annotated)
    if save_debug:
        Path(debug_dir).mkdir(exist_ok=True)

    results = {}
    for name, region in REGIONS.items():
        scaled = scale_region(region, actual_w, actual_h,
                              border_x=cr["border_x"], title_h=cr["title_h"])
        x1,y1,x2,y2 = scaled
        x1=max(0,x1); y1=max(0,y1)
        x2=min(actual_w,x2); y2=min(actual_h,y2)
        if x2<=x1 or y2<=y1: continue
        crop = screenshot.crop((x1,y1,x2,y2))
        if save_debug: crop.save(f"{debug_dir}/{name}.png")

        if name.startswith(("hole_","board_")):
            card_info = analyze_card_region(crop, name)
            results[name] = card_info
            color = "#00ff41" if name.startswith("hole") else "#ff6b6b"
            draw.rectangle([x1,y1,x2,y2], outline=color, width=2)
            if card_info["rank"] and card_info["rank"]!="?":
                label = f'{card_info["rank"]}{card_info["suit"]}'
            elif card_info["rank"] is None: label="(空)"
            else: label="?"
            draw.text((x1+2,y1+2), label, fill=color)
        else:
            if HAS_TESS:
                gray = crop.convert("L")
                cfg = r'--psm 7 -c tessedit_char_whitelist=0123456789,/'
                try: text = pytesseract.image_to_string(gray, config=cfg).strip()
                except Exception: text=""
            else: text=""
            results[name] = {"text":text}
            draw.rectangle([x1,y1,x2,y2], outline="#ffd700", width=2)
            draw.text((x1+2,y1+2), name, fill="#ffd700")

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
    hole=[]; board=[]
    for i in range(1,3):
        info = results.get(f"hole_{i}",{})
        r,s = info.get("rank","?"), info.get("suit","?")
        conf = info.get("confidence",0)
        if r and r!="?" and s:
            hole.append(f"{r}{s}")
            tag="紅" if info.get("color")=="red" else "黑"
            print(f"  手牌 {i}: {r}{s:4s} ({tag}) 信心={conf:.0%}")
        else:
            print(f"  手牌 {i}: 未辨識")
    for i in range(1,6):
        info = results.get(f"board_{i}",{})
        r,s = info.get("rank"), info.get("suit")
        if r is None:
            print(f"  公共牌 {i}: (空)")
            continue
        if r and r!="?" and s:
            board.append(f"{r}{s}")
            tag="紅" if info.get("color")=="red" else "黑"
            print(f"  公共牌 {i}: {r}{s:4s} ({tag})")
        else:
            print(f"  公共牌 {i}: 未辨識")
    pot   = results.get("pot",{}).get("text","?")
    blind = results.get("blind",{}).get("text","?")
    stack = results.get("my_stack",{}).get("text","?")
    print(f"\n  底池: {pot}  |  Blind: {blind}  |  籌碼: {stack}")
    print(f"  手牌: {'  '.join(hole) if hole else '(無)'}")
    print(f"  公共牌: {'  '.join(board) if board else '(無)'}")

    output = {
        "hole":board,  # 注意: live_agent 讀這個
        "hole_cards": hole,
        "board": board,
        "pot": pot,
        "blind": blind,
        "stack": stack,
        "timestamp": time.strftime("%H:%M:%S")
    }
    # 覆寫 output (修正 key)
    output["hole_cards"] = hole
    with open("last_read.json","w",encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    return output

# ─────────────────────────────────────────────────────────────
# HOTKEY / AUTO-POLL MODE
# ─────────────────────────────────────────────────────────────
_auto_running = False
_auto_thread  = None
_poll_interval = 1.0   # 秒

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
    print("║  GameSofa Screen Reader v3 — 背景截圖模式          ║")
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
        if cmd in ("c","capture"):
            try:
                res, _ = capture_and_analyze(save_debug=True)
                format_results(res)
            except Exception as e: print(f"[ERROR] {e}")
        elif cmd in ("a","auto"): toggle_auto()
        elif cmd in ("w","window"): get_window_capture().select_window_interactive()
        elif cmd in ("q","quit","exit"): print("Bye!"); break
        else: print("未知指令")

# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  GameSofa Screen Reader v3")
    print(f"    keyboard    : {'✓' if HAS_KEYBOARD  else '✗ pip install keyboard'}")
    print(f"    pyautogui   : {'✓' if HAS_PYAUTOGUI else '✗ pip install pyautogui'}")
    print(f"    opencv      : {'✓' if HAS_CV2       else '✗ pip install opencv-python'}")
    print(f"    pytesseract : {'✓' if HAS_TESS      else '✗ pip install pytesseract'}")
    print(f"    pywin32     : {'✓ (背景截圖)' if HAS_WIN32 else '✗ pip install pywin32'}")
    print()
    if HAS_KEYBOARD: run_hotkey_mode()
    else: run_manual_mode()
