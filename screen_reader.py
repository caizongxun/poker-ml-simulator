#!/usr/bin/env python3
"""
GameSofa Texas Hold'em Screen Reader v7
=========================================
F6 → 校正模式 (存 debug_crops/ 並印出實際尺寸，方便微調座標)
F7 → 切換自動輪詢
F8 → 重新選擇視窗
F9 → 單次截圖辨識
F10 → 離開

座標說明
--------
REGIONS_REL 用比例座標 (0.0~1.0)，啟動時會優先讀取 regions.json。
若 regions.json 不存在才使用程式內 hardcode 預設值。
用 calibrate_tool.py 調整後儲存，即可同步到此程式。

截圖說明
--------
直接對 hwnd 的 client DC 做 BitBlt，只截網頁內容區，
不含標題列/網址列/書籤列，且不受最大化視窗的負座標影響。
"""

import sys, os, time, json, subprocess, tempfile, threading
from pathlib import Path

# ── Optional imports ──────────────────────────────────────────
try:
    import keyboard; HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False; print("[WARN] keyboard 未安裝")

try:
    import pyautogui; HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False

try:
    import cv2, numpy as np; HAS_CV2 = True
except ImportError:
    HAS_CV2 = False; print("[WARN] opencv-python 未安裝")

try:
    import pytesseract; HAS_TESS = True
    if sys.platform == "win32":
        pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
except ImportError:
    HAS_TESS = False; print("[WARN] pytesseract 未安裝")

HAS_WIN32 = False
if sys.platform == "win32":
    try:
        import win32gui, win32ui, win32con
        from ctypes import windll, wintypes; HAS_WIN32 = True
    except ImportError:
        print("[WARN] pywin32 未安裝，pip install pywin32")

from PIL import Image, ImageDraw

# ─────────────────────────────────────────────────────────────
# WINDOW CAPTURE
# ─────────────────────────────────────────────────────────────
WINDOW_KEYWORDS = ["gamesofa", "神來也", "texas", "德州", "poker",
                   "chrome", "firefox", "edge", "msedge"]


def _capture_client_bitblt(hwnd):
    """
    直接對 hwnd 的 client DC 做 BitBlt 截圖。
    - 只截 client area（不含標題列/網址列/書籤列）
    - 不使用 GetWindowRect，不受最大化視窗負座標影響
    - 不需要 crop 計算
    回傳 PIL.Image 或 None。
    """
    try:
        # GetClientRect 取得 client 寬高
        client_rect = win32gui.GetClientRect(hwnd)
        cw = client_rect[2] - client_rect[0]
        ch = client_rect[3] - client_rect[1]
        if cw <= 0 or ch <= 0:
            return None

        # 取得 client DC（對應網頁內容區）
        client_dc  = win32gui.GetDC(hwnd)
        mfc_dc     = win32ui.CreateDCFromHandle(client_dc)
        save_dc    = mfc_dc.CreateCompatibleDC()
        bitmap     = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, cw, ch)
        save_dc.SelectObject(bitmap)

        # BitBlt 從 client DC 複製到 save_dc（不含視窗邊框/標題列）
        save_dc.BitBlt((0, 0), (cw, ch), mfc_dc, (0, 0), win32con.SRCCOPY)

        bmp_info = bitmap.GetInfo()
        bmp_str  = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_str, "raw", "BGRX", 0, 1
        )
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, client_dc)

        print(f"[INFO] client area (BitBlt): {cw}×{ch}")
        return img

    except Exception as e:
        print(f"[WARN] BitBlt 截圖失敗: {e}")
        return None


class WindowCapture:
    def __init__(self):
        self.hwnd = None; self.window_title = None
        self._find_window()

    def _find_window(self):
        if sys.platform == "win32": self._find_window_win32()
        elif sys.platform == "darwin": self._find_window_mac()

    def _find_window_win32(self):
        if not HAS_WIN32: return
        candidates = []
        def enum_cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if any(kw in title.lower() for kw in WINDOW_KEYWORDS):
                    candidates.append((hwnd, title))
        win32gui.EnumWindows(enum_cb, None)
        if not candidates:
            print("[WARN] 找不到 GameSofa 視窗，將用全螢幕截圖"); return
        for hwnd, title in candidates:
            if any(k in title.lower() for k in ["gamesofa","神來也","德州","texas"]):
                self.hwnd, self.window_title = hwnd, title
                print(f"[WIN] 找到視窗: \"{title}\""); return
        self.hwnd, self.window_title = candidates[0]
        print(f"[WIN] 使用視窗: \"{self.window_title}\"")

    def _find_window_mac(self):
        try:
            result = subprocess.check_output(
                ["osascript","-e",'tell application "System Events" to get name of every process whose visible is true'],
                text=True)
            for p in [x.strip() for x in result.split(",")]:
                if any(b in p.lower() for b in ["chrome","safari","firefox","edge"]):
                    self.window_title = p
                    print(f"[MAC] 找到瀏覽器: {p}"); return
        except Exception as e:
            print(f"[WARN] macOS 視窗偵測失敗: {e}")

    def list_windows(self):
        if sys.platform == "win32" and HAS_WIN32:
            results = []
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t: results.append((hwnd, t))
            win32gui.EnumWindows(cb, None); return results
        return []

    def select_window_interactive(self):
        wins = self.list_windows()
        if not wins: print("[INFO] 無法列出視窗"); return
        print("\n[視窗列表]")
        for i,(hwnd,title) in enumerate(wins[:20]):
            print(f"  {i+1:2d}. {title[:70]}")
        try:
            idx = int(input("\n編號 (0=取消): ").strip()) - 1
            if 0 <= idx < len(wins):
                self.hwnd, self.window_title = wins[idx]
                print(f"[OK] 已選: {self.window_title}")
        except (ValueError, KeyboardInterrupt): pass

    def capture(self) -> Image.Image:
        img = None
        if sys.platform == "win32" and HAS_WIN32 and self.hwnd:
            img = _capture_client_bitblt(self.hwnd)
        elif sys.platform == "darwin" and self.window_title:
            img = self._capture_mac()
        if img is None: img = self._capture_fullscreen()
        return img

    def _capture_mac(self):
        try:
            tmp = tempfile.mktemp(suffix=".png")
            script = f'tell application "{self.window_title}"\nset winID to id of window 1\nend tell\nreturn winID'
            win_id = subprocess.check_output(["osascript","-e",script],text=True).strip()
            subprocess.run(["screencapture","-l",win_id,"-x",tmp],check=True,capture_output=True)
            img = Image.open(tmp); os.unlink(tmp); return img
        except Exception as e:
            print(f"[WARN] macOS 截圖失敗: {e}"); return None

    def _capture_fullscreen(self):
        if HAS_PYAUTOGUI: return pyautogui.screenshot()
        raise RuntimeError("無法截圖：pyautogui 未安裝")

_wc = None
def get_window_capture() -> WindowCapture:
    global _wc
    if _wc is None: _wc = WindowCapture()
    return _wc

# ─────────────────────────────────────────────────────────────
# REGIONS  ── 比例座標 (0.0 ~ 1.0)
# 啟動時優先讀 regions.json（由 calibrate_tool.py 產生）
# 若不存在則使用下方 hardcode 預設值
# ─────────────────────────────────────────────────────────────
_REGIONS_DEFAULT = {
    # ── 手牌 (自己，底部中央固定位置) ──
    "hole_1":   (0.5137, 0.7326, 0.5705, 0.8529),
    "hole_2":   (0.5733, 0.7326, 0.6301, 0.8529),

    # ── 公共牌 (桌面中央，最多5張) ──
    "board_1":  (0.3122, 0.5080, 0.3955, 0.6417),
    "board_2":  (0.3955, 0.5080, 0.4787, 0.6417),
    "board_3":  (0.4787, 0.5080, 0.5620, 0.6417),
    "board_4":  (0.5620, 0.5080, 0.6452, 0.6417),
    "board_5":  (0.6452, 0.5080, 0.7285, 0.6417),

    # ── 底池 ──
    "pot":      (0.4163, 0.4412, 0.5866, 0.4880),

    # ── 自己籌碼 ──
    "my_stack": (0.4163, 0.8663, 0.5393, 0.9118),

    # ── Blind (右上角) ──
    "blind":    (0.8136, 0.2246, 0.9082, 0.2674),
}

REGIONS_JSON = Path("regions.json")

def _load_regions() -> dict:
    """優先讀 regions.json，失敗則用 hardcode 預設值。"""
    if REGIONS_JSON.exists():
        try:
            raw = json.loads(REGIONS_JSON.read_text(encoding="utf-8"))
            regions = {k: tuple(v) for k, v in raw.items()}
            print(f"[OK] 從 regions.json 載入 {len(regions)} 個區域")
            return regions
        except Exception as e:
            print(f"[WARN] regions.json 讀取失敗 ({e})，使用預設值")
    else:
        print("[INFO] regions.json 不存在，使用 hardcode 預設值")
        print("[INFO] 請執行 calibrate_tool.py 校正後儲存，座標會自動同步")
    return dict(_REGIONS_DEFAULT)

REGIONS_REL = _load_regions()

def abs_region(rel, actual_w, actual_h):
    rx1, ry1, rx2, ry2 = rel
    return (int(rx1*actual_w), int(ry1*actual_h),
            int(rx2*actual_w), int(ry2*actual_h))

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
        dark_px = cv2.countNonZero(cv2.inRange(hsv, np.array([0,0,0]), np.array([180,80,80])))
        return "red" if red_px > dark_px * 0.3 else "black"
    else:
        arr = crop_img.convert("RGB"); w, h = arr.size; red = black = 0
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
    'a':'A', '1':'A',
    '2':'2', '3':'3', '4':'4', '5':'5',
    '6':'6', '7':'7', '8':'8', '9':'9',
    '0':'10', 't':'10', '10':'10',
    'j':'J', 'q':'Q', 'k':'K',
}
SUIT_FALLBACK = {"red":"♥", "black":"♠"}

def preprocess_for_ocr(crop_img):
    w, h = crop_img.size
    corner = crop_img.crop((0, 0, int(w*0.45), int(h*0.40)))
    corner = corner.resize((corner.width*4, corner.height*4), Image.LANCZOS)
    corner = corner.convert("L")
    if HAS_CV2:
        arr = np.array(corner)
        _, binary = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if binary.mean() < 127: binary = 255 - binary
        kernel = np.ones((2,2), np.uint8)
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
        if text in RANK_MAP:       return RANK_MAP[text]
        if len(text)>=2 and text[:2] in RANK_MAP: return RANK_MAP[text[:2]]
        if text:                   return RANK_MAP.get(text[0], "?")
        return "?"
    except Exception: return "?"

# ─────────────────────────────────────────────────────────────
# EMPTY SLOT DETECTION
# ─────────────────────────────────────────────────────────────
def is_empty_slot(crop_img):
    if HAS_CV2:
        arr = np.array(crop_img.convert("RGB"))
        total = arr.shape[0] * arr.shape[1]
        hsv   = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        teal  = cv2.countNonZero(cv2.inRange(hsv,
                    np.array([75, 25, 30]), np.array([115, 255, 230])))
        white = cv2.countNonZero(cv2.inRange(arr,
                    np.array([210,210,210]), np.array([255,255,255])))
        tr = teal  / total
        wr = white / total
        if tr > 0.45:               return True
        if wr < 0.15 and tr > 0.15: return True
        return False
    else:
        arr = crop_img.convert("RGB"); w, h = arr.size
        teal = white = total = 0
        for y in range(0,h,3):
            for x in range(0,w,3):
                r,g,b = arr.getpixel((x,y)); total += 1
                if 0<r<150 and 100<g<220 and 80<b<210: teal  += 1
                if r>210 and g>210 and b>210:           white += 1
        t = max(total,1)
        if teal/t > 0.45:                return True
        if white/t < 0.15 and teal/t > 0.15: return True
        return False

def analyze_card_region(crop_img, region_name):
    result = {"rank":"?","suit":"?","color":"unknown","confidence":0.0,"region":region_name}
    if is_empty_slot(crop_img):
        result["rank"] = None; result["suit"] = None; return result
    color          = detect_suit_by_color(crop_img)
    result["color"] = color
    result["suit"]  = SUIT_FALLBACK[color]
    rank            = detect_rank_ocr(crop_img)
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
    if save_debug: Path(debug_dir).mkdir(exist_ok=True)

    results = {}
    for name, rel in REGIONS_REL.items():
        x1,y1,x2,y2 = abs_region(rel, actual_w, actual_h)
        x1=max(0,x1); y1=max(0,y1)
        x2=min(actual_w,x2); y2=min(actual_h,y2)
        if x2<=x1 or y2<=y1: continue
        crop = screenshot.crop((x1,y1,x2,y2))
        if save_debug: crop.save(f"{debug_dir}/{name}.png")

        if name.startswith(("hole_","board_")):
            card_info = analyze_card_region(crop, name)
            results[name] = card_info
            clr = "#00ff41" if name.startswith("hole") else "#ff6b6b"
            draw.rectangle([x1,y1,x2,y2], outline=clr, width=2)
            r, s = card_info["rank"], card_info["suit"]
            if r and r != "?": label = f"{r}{s}"
            elif r is None:    label = "(空)"
            else:              label = "?"
            draw.text((x1+2, y1+2), label, fill=clr)
        else:
            text = ""
            if HAS_TESS:
                gray = crop.convert("L")
                cfg  = r'--psm 7 -c tessedit_char_whitelist=0123456789,/'
                try: text = pytesseract.image_to_string(gray, config=cfg).strip()
                except Exception: pass
            results[name] = {"text": text}
            draw.rectangle([x1,y1,x2,y2], outline="#ffd700", width=2)
            draw.text((x1+2,y1+2), name, fill="#ffd700")

    if save_debug:
        annotated.save(f"{debug_dir}/_annotated.png")
        print(f"[DEBUG] 截圖尺寸 (client area): {actual_w}×{actual_h}")
        print(f"[DEBUG] 除錯圖存至 {debug_dir}/_annotated.png")
    return results, annotated

# ─────────────────────────────────────────────────────────────
# CALIBRATE MODE (F6)
# ─────────────────────────────────────────────────────────────
def on_f6_calibrate():
    print("\n[F6] 校正模式 — 截圖並印出各區域像素座標")
    try:
        wc = get_window_capture()
        screenshot = wc.capture()
        actual_w, actual_h = screenshot.size
        print(f"  client area 尺寸: {actual_w} × {actual_h}")
        print(f"  {'Region':<12} {'x1':>6} {'y1':>6} {'x2':>6} {'y2':>6}  (像素)")
        print(f"  {'-'*48}")
        for name, rel in REGIONS_REL.items():
            x1,y1,x2,y2 = abs_region(rel, actual_w, actual_h)
            print(f"  {name:<12} {x1:>6} {y1:>6} {x2:>6} {y2:>6}")
        Path("debug_crops").mkdir(exist_ok=True)
        annotated = screenshot.copy()
        draw = ImageDraw.Draw(annotated)
        for name, rel in REGIONS_REL.items():
            x1,y1,x2,y2 = abs_region(rel, actual_w, actual_h)
            clr = "#00ff41" if "hole" in name else ("#ff6b6b" if "board" in name else "#ffd700")
            draw.rectangle([x1,y1,x2,y2], outline=clr, width=2)
            draw.text((x1+2,y1+2), name, fill=clr)
        annotated.save("debug_crops/_calibrate.png")
        print("  校正截圖存至 debug_crops/_calibrate.png，請開啟確認框位置")
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback; traceback.print_exc()

# ─────────────────────────────────────────────────────────────
# OUTPUT FORMATTER
# ─────────────────────────────────────────────────────────────
def format_results(results):
    print("\n" + "="*52)
    print("  辨識結果")
    print("="*52)
    hole = []; board = []
    for i in range(1,3):
        info = results.get(f"hole_{i}", {})
        r, s, conf = info.get("rank","?"), info.get("suit","?"), info.get("confidence",0)
        if r and r != "?" and s:
            hole.append(f"{r}{s}")
            tag = "紅" if info.get("color")=="red" else "黑"
            print(f"  手牌 {i}: {r}{s:5s} ({tag}) 信心={conf:.0%}")
        else:
            print(f"  手牌 {i}: 未辨識")
    for i in range(1,6):
        info = results.get(f"board_{i}", {})
        r, s = info.get("rank"), info.get("suit")
        if r is None: print(f"  公共牌 {i}: (空)"); continue
        if r and r != "?" and s:
            board.append(f"{r}{s}")
            tag = "紅" if info.get("color")=="red" else "黑"
            print(f"  公共牌 {i}: {r}{s:5s} ({tag})")
        else:
            print(f"  公共牌 {i}: 未辨識")
    pot   = results.get("pot",{}).get("text","?")
    blind = results.get("blind",{}).get("text","?")
    stack = results.get("my_stack",{}).get("text","?")
    print(f"\n  底池: {pot}  |  Blind: {blind}  |  籌碼: {stack}")
    print(f"  手牌:   {'  '.join(hole)  if hole  else '(無)'}")
    print(f"  公共牌: {'  '.join(board) if board else '(無)'}")
    output = {
        "hole_cards": hole, "board": board,
        "pot": pot, "blind": blind, "stack": stack,
        "timestamp": time.strftime("%H:%M:%S"),
    }
    with open("last_read.json","w",encoding="utf-8") as f:
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
        _auto_running = False; print("[F7] 自動輪詢 ▶ 已停止")
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
        print(f"[ERROR] {e}"); import traceback; traceback.print_exc()

def run_hotkey_mode():
    print("\n╔" + "═"*54 + "╗")
    print("║  GameSofa Screen Reader v7 — regions.json 優先讀取  ║")
    print("╠" + "═"*54 + "╣")
    wc = get_window_capture()
    if wc.window_title:
        print(f"║  視窗: {wc.window_title[:46]:<46} ║")
    src = "regions.json" if REGIONS_JSON.exists() else "hardcode 預設值"
    print(f"║  座標來源: {src:<43} ║")
    print("║  F6  → 校正模式 (存 debug_crops/_calibrate.png)    ║")
    print("║  F7  → 切換自動輪詢 (每秒辨識)                     ║")
    print("║  F8  → 重新選擇視窗                                 ║")
    print("║  F9  → 單次截圖辨識                                 ║")
    print("║  F10 → 離開                                         ║")
    print("╚" + "═"*54 + "╝")
    keyboard.add_hotkey("f6",  on_f6_calibrate)
    keyboard.add_hotkey("f7",  toggle_auto)
    keyboard.add_hotkey("f8",  lambda: get_window_capture().select_window_interactive())
    keyboard.add_hotkey("f9",  on_f9)
    keyboard.add_hotkey("f10", lambda: (print("\n[BYE]"), os._exit(0)))
    keyboard.wait()

def run_manual_mode():
    print("\n[Manual Mode]  c=截圖  cal=校正  a=自動  w=選視窗  q=離開")
    while True:
        cmd = input("\n> ").strip().lower()
        if cmd in ("c","capture"):
            try: res, _ = capture_and_analyze(save_debug=True); format_results(res)
            except Exception as e: print(f"[ERROR] {e}")
        elif cmd in ("cal","calibrate"): on_f6_calibrate()
        elif cmd in ("a","auto"):   toggle_auto()
        elif cmd in ("w","window"): get_window_capture().select_window_interactive()
        elif cmd in ("q","quit","exit"): print("Bye!"); break
        else: print("未知指令")

# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  GameSofa Screen Reader v7 (regions.json 優先讀取)")
    print(f"    keyboard    : {'✓' if HAS_KEYBOARD  else '✗ pip install keyboard'}")
    print(f"    pyautogui   : {'✓' if HAS_PYAUTOGUI else '✗ pip install pyautogui'}")
    print(f"    opencv      : {'✓' if HAS_CV2       else '✗ pip install opencv-python'}")
    print(f"    pytesseract : {'✓' if HAS_TESS      else '✗ pip install pytesseract'}")
    print(f"    pywin32     : {'✓ (BitBlt 背景截圖)' if HAS_WIN32 else '✗ pip install pywin32'}")
    print()
    if HAS_KEYBOARD: run_hotkey_mode()
    else: run_manual_mode()
