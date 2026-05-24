#!/usr/bin/env python3
"""
GameSofa Texas Hold'em Screen Reader v10.1
===========================================
F6 → 校正模式
F7 → 切換自動輪詢
F8 → 重新選擇視窗
F9 → 單次截圖辨識
F10 → 離開

花色辨識 (v10.1)
--------
優先使用 suit_templates/ 本地樣本做模板匹配：
  suit_templates/
    heart.png, heart_2.png, ...
    diamond.png, diamond_2.png, ...
    spade.png, spade_2.png, ...
    club.png, club_2.png, ...

匹配流程：
1. 從卡牌左側裁出花色圖標區域 (64×64)
   裁切區域: x=0~38%寬， y=33%~80%高  (與 calibrate_tool.py 完全一致)
2. 對每個花色的所有樣本做 cv2.matchTemplate (TM_CCOEFF_NORMED)
3. 取最高分決定花色 (threshold = 0.45)
4. 若分數不足或無樣本 → fallback 回輪廓形狀分析
"""

import sys, os, time, json, subprocess, tempfile, threading, ctypes
from pathlib import Path

# ── Optional imports ──────────────────────────────────
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

# ───────────────────────────────────────────────────────────────
# WINDOW CAPTURE — PrintWindow + PW_RENDERFULLCONTENT
# ───────────────────────────────────────────────────────────────
WINDOW_KEYWORDS = ["gamesofa", "神來也", "texas", "德州", "poker",
                   "chrome", "firefox", "edge", "msedge"]

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

DWMWA_EXTENDED_FRAME_BOUNDS = 9
PW_RENDERFULLCONTENT = 2


def _get_visual_rect(hwnd):
    try:
        rect = _RECT()
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect))
        if hr == 0:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception as e:
        print(f"[WARN] DwmGetWindowAttribute 失敗: {e}")
    return None


def _capture_printwindow(hwnd):
    try:
        win_rect = win32gui.GetWindowRect(hwnd)
        win_w = win_rect[2] - win_rect[0]
        win_h = win_rect[3] - win_rect[1]
        if win_w <= 0 or win_h <= 0:
            return None

        win_dc  = win32gui.GetWindowDC(hwnd)
        mfc_dc  = win32ui.CreateDCFromHandle(win_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap  = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, win_w, win_h)
        save_dc.SelectObject(bitmap)

        result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not result:
            print("[WARN] PrintWindow 回傳 0，嘗試繼續...")

        bmp_info = bitmap.GetInfo()
        bmp_str  = bitmap.GetBitmapBits(True)
        full_img = Image.frombuffer(
            "RGB", (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_str, "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC(); mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, win_dc)

        visual = _get_visual_rect(hwnd) or win_rect
        vis_left, vis_top = visual[0], visual[1]

        pt = _POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))

        client_rect = win32gui.GetClientRect(hwnd)
        cw = client_rect[2] - client_rect[0]
        ch = client_rect[3] - client_rect[1]

        crop_x  = pt.x - vis_left
        crop_y  = pt.y - vis_top
        crop_x2 = crop_x + cw
        crop_y2 = crop_y + ch

        img_w, img_h = full_img.size
        crop_x  = max(0, min(crop_x,  img_w))
        crop_y  = max(0, min(crop_y,  img_h))
        crop_x2 = max(0, min(crop_x2, img_w))
        crop_y2 = max(0, min(crop_y2, img_h))

        if crop_x2 <= crop_x or crop_y2 <= crop_y:
            print(f"[WARN] crop 範圍無效: ({crop_x},{crop_y})-({crop_x2},{crop_y2})，回傳完整截圖")
            return full_img

        client_img = full_img.crop((crop_x, crop_y, crop_x2, crop_y2))
        print(f"[INFO] PrintWindow 截圖成功: window={win_w}×{win_h}, "
              f"client={cw}×{ch}, crop=({crop_x},{crop_y})")
        return client_img

    except Exception as e:
        print(f"[WARN] PrintWindow 截圖失敗: {e}")
        import traceback; traceback.print_exc()
        return None


class WindowCapture:
    def __init__(self):
        self.hwnd = None; self.window_title = None
        self._find_window()

    def _find_window(self):
        if sys.platform == "win32":    self._find_window_win32()
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
            img = _capture_printwindow(self.hwnd)
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

# ───────────────────────────────────────────────────────────────
# REGIONS  —— 比例座標 (0.0 ~ 1.0)
# ───────────────────────────────────────────────────────────────
_REGIONS_DEFAULT = {
    # ── 手牌 ──
    "hole_1":   (0.5137, 0.7326, 0.5705, 0.8529),
    "hole_2":   (0.5733, 0.7326, 0.6301, 0.8529),
    # ── 公共牌 ──
    "board_1":  (0.3122, 0.5080, 0.3955, 0.6417),
    "board_2":  (0.3955, 0.5080, 0.4787, 0.6417),
    "board_3":  (0.4787, 0.5080, 0.5620, 0.6417),
    "board_4":  (0.5620, 0.5080, 0.6452, 0.6417),
    "board_5":  (0.6452, 0.5080, 0.7285, 0.6417),
    # ── 底池 ──
    "pot":      (0.4163, 0.4412, 0.5866, 0.4880),
    # ── 自己籌碼 ──
    "my_stack": (0.4163, 0.8663, 0.5393, 0.9118),
    # ── Blind ──
    "blind":    (0.8136, 0.2246, 0.9082, 0.2674),
}

REGIONS_JSON = Path("regions.json")

def _load_regions() -> dict:
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
    return dict(_REGIONS_DEFAULT)

REGIONS_REL = _load_regions()

def abs_region(rel, actual_w, actual_h):
    rx1, ry1, rx2, ry2 = rel
    return (int(rx1*actual_w), int(ry1*actual_h),
            int(rx2*actual_w), int(ry2*actual_h))


# ───────────────────────────────────────────────────────────────
# SUIT TEMPLATE MATCHING v10.1
# ───────────────────────────────────────────────────────────────
TEMPLATES_DIR = Path("suit_templates")

SUIT_FILES = {
    "heart":   "♥",
    "diamond": "♦",
    "spade":   "♠",
    "club":    "♣",
}

_template_cache: dict = {}
_template_cache_mtime: dict = {}


def _load_templates() -> dict:
    global _template_cache, _template_cache_mtime
    if not HAS_CV2 or not TEMPLATES_DIR.exists():
        return {}

    updated = False
    result = {}
    for suit_name in SUIT_FILES:
        imgs = []
        idx = 1
        while True:
            fname = TEMPLATES_DIR / (f"{suit_name}.png" if idx == 1 else f"{suit_name}_{idx}.png")
            if not fname.exists():
                break
            mtime = fname.stat().st_mtime
            cache_key = str(fname)
            if cache_key not in _template_cache or _template_cache_mtime.get(cache_key) != mtime:
                arr = cv2.imread(str(fname), cv2.IMREAD_GRAYSCALE)
                if arr is not None:
                    _template_cache[cache_key] = arr
                    _template_cache_mtime[cache_key] = mtime
                    updated = True
            cached = _template_cache.get(cache_key)
            if cached is not None:
                imgs.append(cached)
            idx += 1
        if imgs:
            result[suit_name] = imgs

    if updated:
        total = sum(len(v) for v in result.values())
        print(f"[TMPL] 載入花色樣本: {total} 張 — " +
              ", ".join(f"{k}:{len(v)}" for k, v in result.items()))
    return result


# ----------------------------------------------------------------
# 重要：此裁切區域必須與 calibrate_tool.py 的 extract_suit_icon() 完全一致
#   calibrate_tool.py extract_suit_icon:
#     x1=0, x2=int(w*0.38), y1=int(h*0.33), y2=int(h*0.80)
# ----------------------------------------------------------------
def _extract_suit_roi(card_img_rgb: np.ndarray) -> np.ndarray | None:
    """
    從卡牌圖片裁出花色圖標 ROI，與 calibrate_tool.py 完全一致。
    區域: x=0~38%寬， y=33%~80%高
    回傳 64×64 灰階圖。
    """
    h, w = card_img_rgb.shape[:2]
    x1, x2 = 0,          int(w * 0.38)
    y1, y2 = int(h * 0.33), int(h * 0.80)
    roi = card_img_rgb[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    roi_gray    = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
    roi_resized = cv2.resize(roi_gray, (64, 64), interpolation=cv2.INTER_AREA)
    return roi_resized


def detect_suit_by_template(card_img: Image.Image, color: str):
    """
    用模板匹配辨識花色。
    回傳 (suit_symbol, detail_str) 或 (None, reason) 若無法判斷。
    """
    if not HAS_CV2:
        return None, "no_cv2"

    templates = _load_templates()
    if not templates:
        return None, "no_templates"

    arr = np.array(card_img.convert("RGB"))
    roi = _extract_suit_roi(arr)
    if roi is None:
        return None, "no_roi"

    if color == "red":
        candidates = {k: v for k, v in templates.items() if k in ("heart", "diamond")}
    else:
        candidates = {k: v for k, v in templates.items() if k in ("spade", "club")}

    if not candidates:
        return None, f"no_{color}_templates"

    best_suit  = None
    best_score = -1.0
    score_log  = []

    for suit_name, tmpl_list in candidates.items():
        suit_best = -1.0
        for tmpl in tmpl_list:
            tmpl_resized = cv2.resize(tmpl, (64, 64), interpolation=cv2.INTER_AREA)
            result = cv2.matchTemplate(roi, tmpl_resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > suit_best:
                suit_best = max_val
        score_log.append(f"{suit_name}={suit_best:.3f}")
        if suit_best > best_score:
            best_score = suit_best
            best_suit  = suit_name

    detail = "tmpl:" + " ".join(score_log)

    THRESHOLD = 0.45
    if best_score < THRESHOLD:
        return None, f"{detail} <thresh({THRESHOLD})"

    suit_symbol = SUIT_FILES[best_suit]
    return suit_symbol, f"{detail} ✓{best_suit}({best_score:.3f})"


# ───────────────────────────────────────────────────────────────
# SUIT DETECTION FALLBACK — 輪廓形狀分析 (v9 邏輯，保留為備援)
# ───────────────────────────────────────────────────────────────

def _extract_suit_symbol_mask(crop_rgb_arr, color):
    h, w = crop_rgb_arr.shape[:2]
    sym_y1, sym_y2 = h // 3, h
    sym_x1, sym_x2 = 0, w // 3
    sym = crop_rgb_arr[sym_y1:sym_y2, sym_x1:sym_x2]
    if sym.size == 0:
        return None, None
    gray = cv2.cvtColor(sym, cv2.COLOR_RGB2GRAY)
    if color == "red":
        r = sym[:, :, 0]; g = sym[:, :, 1]; b = sym[:, :, 2]
        mask = ((r.astype(int) - g.astype(int) > 30) &
                (r.astype(int) - b.astype(int) > 30) &
                (r > 100)).astype(np.uint8) * 255
    else:
        mask = (gray < 100).astype(np.uint8) * 255
    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  kernel)
    return mask, sym


def _shape_features(mask):
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(cnt)
    if area < 20:
        return None
    perimeter = cv2.arcLength(cnt, True)
    x, y, bw, bh = cv2.boundingRect(cnt)
    aspect_ratio = bw / bh if bh > 0 else 1.0
    circularity  = (4 * np.pi * area / (perimeter ** 2)) if perimeter > 0 else 0
    extent       = area / (bw * bh) if bw * bh > 0 else 0
    hull_area    = cv2.contourArea(cv2.convexHull(cnt))
    convexity    = area / hull_area if hull_area > 0 else 1.0
    top_third    = mask[:mask.shape[0]//3, :]
    top_cnts, _  = cv2.findContours(top_third, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    top_count    = len([c for c in top_cnts if cv2.contourArea(c) > 10])
    return dict(aspect_ratio=aspect_ratio, circularity=circularity,
                extent=extent, convexity=convexity, top_count=top_count, area=area)


def detect_suit_by_shape(crop_img: Image.Image, color: str):
    if not HAS_CV2:
        return ("♥" if color == "red" else "♠"), "no_cv2"
    arr = np.array(crop_img.convert("RGB"))
    mask, _ = _extract_suit_symbol_mask(arr, color)
    if mask is None:
        return ("♥" if color == "red" else "♠"), "no_mask"
    feats = _shape_features(mask)
    if feats is None:
        return ("♥" if color == "red" else "♠"), "no_contour"

    ar, circ, conv, ext, top = (feats["aspect_ratio"], feats["circularity"],
                                 feats["convexity"], feats["extent"], feats["top_count"])
    detail = f"shape:ar={ar:.2f} circ={circ:.2f} conv={conv:.2f} ext={ext:.2f} top={top}"

    if color == "red":
        sh, sd = 0, 0
        if top >= 2: sh += 3
        else:        sd += 2
        if conv < 0.82: sh += 2
        else:           sd += 2
        if ar < 0.82:   sd += 2
        elif ar > 1.0:  sh += 1
        if circ > 0.60: sh += 1
        elif circ < 0.45: sd += 1
        suit = "♥" if sh >= sd else "♦"
        return suit, f"{detail} H={sh} D={sd}"
    else:
        ss, sc = 0, 0
        if ar >= 1.05:  sc += 2
        elif ar < 0.90: ss += 2
        else:           ss += 1
        if circ > 0.65: sc += 2
        elif circ < 0.45: ss += 2
        else:           sc += 1
        if conv < 0.80: ss += 2
        else:           sc += 1
        if ext > 0.72:  sc += 1
        elif ext < 0.58: ss += 1
        suit = "♠" if ss >= sc else "♣"
        return suit, f"{detail} S={ss} C={sc}"


# ───────────────────────────────────────────────────────────────
# SUIT DETECTION 主入口
# ───────────────────────────────────────────────────────────────

def detect_suit_by_color(crop_img: Image.Image) -> str:
    if HAS_CV2:
        arr = np.array(crop_img.convert("RGB"))
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        m1 = cv2.inRange(hsv, np.array([0,  100, 80]),  np.array([15, 255, 255]))
        m2 = cv2.inRange(hsv, np.array([160,100, 80]),  np.array([180,255, 255]))
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


def detect_suit(crop_img: Image.Image):
    """
    主入口：
    1. 顏色判斷 (red/black)
    2. 模板匹配 (suit_templates/)
    3. fallback → 輪廓形狀分析
    回傳 (color, suit_symbol, detail)
    """
    color = detect_suit_by_color(crop_img)
    suit, detail = detect_suit_by_template(crop_img, color)
    if suit is not None:
        return color, suit, detail
    suit, detail = detect_suit_by_shape(crop_img, color)
    return color, suit, f"[fallback] {detail}"


# ───────────────────────────────────────────────────────────────
# RANK DETECTION
# ───────────────────────────────────────────────────────────────
RANK_MAP = {
    'a':'A', '1':'A',
    '2':'2', '3':'3', '4':'4', '5':'5',
    '6':'6', '7':'7', '8':'8', '9':'9',
    '0':'10', 't':'10', '10':'10',
    'j':'J', 'q':'Q', 'k':'K',
}

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
        if text in RANK_MAP:                    return RANK_MAP[text]
        if len(text)>=2 and text[:2] in RANK_MAP: return RANK_MAP[text[:2]]
        if text:                                return RANK_MAP.get(text[0], "?")
        return "?"
    except Exception: return "?"


# ───────────────────────────────────────────────────────────────
# EMPTY SLOT DETECTION
# ───────────────────────────────────────────────────────────────
def is_empty_slot(crop_img):
    if HAS_CV2:
        arr   = np.array(crop_img.convert("RGB"))
        total = arr.shape[0] * arr.shape[1]
        hsv   = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        teal  = cv2.countNonZero(cv2.inRange(hsv,
                    np.array([75, 25, 30]), np.array([115, 255, 230])))
        white = cv2.countNonZero(cv2.inRange(arr,
                    np.array([210,210,210]), np.array([255,255,255])))
        tr = teal  / total
        wr = white / total
        if tr > 0.45:                return True
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
        if teal/t > 0.45:                    return True
        if white/t < 0.15 and teal/t > 0.15: return True
        return False


def analyze_card_region(crop_img, region_name):
    result = {"rank":"?","suit":"?","color":"unknown","confidence":0.0,"region":region_name}
    if is_empty_slot(crop_img):
        result["rank"] = None; result["suit"] = None; return result

    color, suit, suit_detail = detect_suit(crop_img)
    result["color"] = color
    result["suit"]  = suit

    rank = detect_rank_ocr(crop_img)
    result["rank"]        = rank
    result["confidence"]  = 0.9 if rank != "?" else 0.3
    result["suit_detail"] = suit_detail
    return result


# ───────────────────────────────────────────────────────────────
# MAIN CAPTURE
# ───────────────────────────────────────────────────────────────
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


# ───────────────────────────────────────────────────────────────
# CALIBRATE MODE (F6)
# ───────────────────────────────────────────────────────────────
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
        print("  校正截圖存至 debug_crops/_calibrate.png")

        templates = _load_templates()
        if templates:
            print("\n  [花色樣本] suit_templates/")
            for suit_name, suit_sym in SUIT_FILES.items():
                count = len(templates.get(suit_name, []))
                print(f"    {suit_sym} {suit_name:<8}: {count} 張樣本")
        else:
            print("\n  [花色樣本] suit_templates/ 尚無樣本 → 使用輪廓分析 fallback")
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback; traceback.print_exc()


# ───────────────────────────────────────────────────────────────
# OUTPUT FORMATTER
# ───────────────────────────────────────────────────────────────
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
            sd  = info.get("suit_detail", "")
            print(f"  手牌 {i}: {r}{s:5s} ({tag}) 信心={conf:.0%} [{sd}]")
        else:
            print(f"  手牌 {i}: 未辨識")
    for i in range(1,6):
        info = results.get(f"board_{i}", {})
        r, s = info.get("rank"), info.get("suit")
        if r is None: print(f"  公共牌 {i}: (空)"); continue
        if r and r != "?" and s:
            board.append(f"{r}{s}")
            tag = "紅" if info.get("color")=="red" else "黑"
            sd  = info.get("suit_detail", "")
            print(f"  公共牌 {i}: {r}{s:5s} ({tag}) [{sd}]")
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


# ───────────────────────────────────────────────────────────────
# HOTKEY / AUTO-POLL MODE
# ───────────────────────────────────────────────────────────────
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
    print("║  GameSofa Screen Reader v10.1 — 模板匹配花色辨識        ║")
    print("╠" + "═"*54 + "╣")
    wc = get_window_capture()
    if wc.window_title:
        print(f"║  視窗: {wc.window_title[:46]:<46} ║")
    src = "regions.json" if REGIONS_JSON.exists() else "hardcode 預設值"
    print(f"║  座標來源: {src:<43} ║")
    templates = _load_templates()
    total_tmpl = sum(len(v) for v in templates.values())
    tmpl_src   = f"suit_templates/ ({total_tmpl} 張)" if total_tmpl else "無樣本 → 輪廓分析"
    print(f"║  花色辨識: {tmpl_src:<43} ║")
    print("║  F6  → 校正模式                                    ║")
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


# ───────────────────────────────────────────────────────────────
# ENTRY POINT
# ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  GameSofa Screen Reader v10.1 (模板匹配花色辨識 + PrintWindow GPU 截圖)")
    print(f"    keyboard    : {'✓' if HAS_KEYBOARD  else '✗ pip install keyboard'}")
    print(f"    pyautogui   : {'✓' if HAS_PYAUTOGUI else '✗ pip install pyautogui'}")
    print(f"    opencv      : {'✓' if HAS_CV2       else '✗ pip install opencv-python'}")
    print(f"    pytesseract : {'✓' if HAS_TESS      else '✗ pip install pytesseract'}")
    print(f"    pywin32     : {'✓ (PrintWindow GPU 截圖)' if HAS_WIN32 else '✗ pip install pywin32'}")
    print()
    if HAS_KEYBOARD: run_hotkey_mode()
    else: run_manual_mode()
