#!/usr/bin/env python3
"""
calibrate_tool.py  —  GameSofa 辨識區塊拖拉校正工具
=====================================================
用法:
    python calibrate_tool.py

功能:
  1. 啟動時手動選擇要截圖的目標視窗
  2. 用視窗顯示截圖，每個辨識框以彩色顯示
  3. 點選框 → 拖拉邊緣/角落 可調整大小
  4. 點選框內部 → 整框移動
  5. 按 [儲存] → 寫入 regions.json（screen_reader.py 啟動時自動讀取）
  6. 按 [重新截圖] → 重新取得目前選定視窗
  7. 按 [選擇視窗] → 重新挑選目標視窗
  8. [花色樣本採集] → 點選任意牌區域，選择花色，自動裁切花色圖標存至 suit_templates/

截圖說明
--------
使用 PrintWindow(PW_RENDERFULLCONTENT) 截圖，完全與 screen_reader.py 一致，
可正確捕捉 Edge/Chrome GPU 合成畫面（解決黑屏問題）。

鍵盤快捷鍵:
  Delete / Backspace → 還原選取框到原始座標
  S                  → 儲存
  R                  → 重新截圖
  W                  → 重新選擇視窗
  T                  → 切換花色樣本採集模式

依賴: tkinter (內建), Pillow
"""

import sys, re, json, os
from pathlib import Path
from tkinter import (
    Tk, Toplevel, Canvas, Frame, Button, Label, StringVar,
    Scrollbar, Listbox, messagebox, BOTH,
    LEFT, RIGHT, TOP, BOTTOM, X, Y, END,
    HORIZONTAL, VERTICAL, NW, ALL, IntVar, Radiobutton
)

try:
    from PIL import Image, ImageTk, ImageFilter
except ImportError:
    print("[ERROR] pip install Pillow")
    sys.exit(1)

SCREEN_READER_PATH = Path("screen_reader.py")
REGIONS_JSON_PATH  = Path("regions.json")
DEBUG_DIR          = Path("debug_crops")
SUIT_TEMPLATE_DIR  = Path("suit_templates")
MAX_DISPLAY_W      = 1280
MAX_DISPLAY_H      = 860
HANDLE_SIZE        = 8
SELF_TITLE         = "GameSofa 辨識區塊校正工具"

SUIT_SYMBOLS = [
    ("heart",   "♥ 紅心",  "#e84040"),
    ("diamond", "♦ 紅菱",  "#ff8c00"),
    ("spade",   "♠ 黑桃",  "#66ccff"),
    ("club",    "♣ 黑梅",  "#aaffaa"),
]

REGION_COLORS = {
    "hole_1":   "#00ff41",
    "hole_2":   "#00ff41",
    "board_1":  "#ff6b6b",
    "board_2":  "#ff6b6b",
    "board_3":  "#ff6b6b",
    "board_4":  "#ff6b6b",
    "board_5":  "#ff6b6b",
    "pot":      "#ffd700",
    "my_stack": "#00cfff",
    "blind":    "#ff9f43",
}

DEFAULT_REGIONS = {
    "hole_1":   (0.5137, 0.7326, 0.5705, 0.8529),
    "hole_2":   (0.5733, 0.7326, 0.6301, 0.8529),
    "board_1":  (0.3122, 0.5080, 0.3955, 0.6417),
    "board_2":  (0.3955, 0.5080, 0.4787, 0.6417),
    "board_3":  (0.4787, 0.5080, 0.5620, 0.6417),
    "board_4":  (0.5620, 0.5080, 0.6452, 0.6417),
    "board_5":  (0.6452, 0.5080, 0.7285, 0.6417),
    "pot":      (0.4163, 0.4412, 0.5866, 0.4880),
    "my_stack": (0.4163, 0.8663, 0.5393, 0.9118),
    "blind":    (0.8136, 0.2246, 0.9082, 0.2674),
}


def load_regions_from_file():
    if REGIONS_JSON_PATH.exists():
        try:
            raw = json.loads(REGIONS_JSON_PATH.read_text(encoding="utf-8"))
            regions = {k: tuple(v) for k, v in raw.items()}
            if regions:
                print(f"[OK] 從 regions.json 讀取 {len(regions)} 個區域")
                return regions
        except Exception as e:
            print(f"[WARN] regions.json 讀取失敗: {e}")

    if SCREEN_READER_PATH.exists():
        src = SCREEN_READER_PATH.read_text(encoding="utf-8")
        for var_name in ("_REGIONS_DEFAULT", "REGIONS_REL"):
            pattern = rf'{re.escape(var_name)}\s*=\s*\{{(.*?)^\}}'
            m = re.search(pattern, src, re.DOTALL | re.MULTILINE)
            if m:
                regions = {}
                for line in m.group(1).splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m2 = re.match(
                        r'"([\w]+)"\s*:\s*\(([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\)',
                        line,
                    )
                    if m2:
                        regions[m2.group(1)] = tuple(float(m2.group(i)) for i in range(2, 6))
                if regions:
                    print(f"[OK] 從 screen_reader.py ({var_name}) 讀取 {len(regions)} 個區域")
                    return regions

    print("[WARN] 無法讀取座標，使用預設值")
    return dict(DEFAULT_REGIONS)


def write_regions(regions: dict):
    try:
        data = {k: list(v) for k, v in regions.items()}
        REGIONS_JSON_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[OK] 已寫入 {REGIONS_JSON_PATH}")
    except Exception as e:
        print(f"[ERROR] 寫入 regions.json 失敗: {e}")
        return False

    if not SCREEN_READER_PATH.exists():
        return True

    try:
        src = SCREEN_READER_PATH.read_text(encoding="utf-8")
        lines = ["_REGIONS_DEFAULT = {\n"]
        for group_name, keys in [
            ("手牌", ["hole_1", "hole_2"]),
            ("公共牌", ["board_1", "board_2", "board_3", "board_4", "board_5"]),
            ("底池", ["pot"]),
            ("自己籌碼", ["my_stack"]),
            ("Blind", ["blind"]),
        ]:
            lines.append(f"    # \u2500\u2500 {group_name} \u2500\u2500\n")
            for key in keys:
                if key in regions:
                    v = regions[key]
                    pad = " " * (10 - len(key))
                    lines.append(f'    "{key}":{pad}({v[0]:.4f}, {v[1]:.4f}, {v[2]:.4f}, {v[3]:.4f}),\n')
        lines.append("}\n")
        new_block = "".join(lines)
        new_src = re.sub(
            r'^_REGIONS_DEFAULT\s*=\s*\{.*?^\}\s*$',
            new_block.rstrip(),
            src,
            flags=re.DOTALL | re.MULTILINE,
        )
        if new_src != src:
            SCREEN_READER_PATH.write_text(new_src, encoding="utf-8")
            print(f"[OK] 已同步更新 {SCREEN_READER_PATH} 的 _REGIONS_DEFAULT")
    except Exception as e:
        print(f"[WARN] 同步 screen_reader.py 失敗: {e}")
    return True


def list_visible_windows():
    if sys.platform != "win32":
        return []
    try:
        import win32gui
        windows = []
        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if not title or SELF_TITLE in title:
                return
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                w, h = right - left, bottom - top
            except Exception:
                w, h = 0, 0
            if w < 200 or h < 100:
                return
            windows.append((hwnd, title, w, h))
        win32gui.EnumWindows(cb, None)
        windows.sort(key=lambda x: x[1].lower())
        return windows
    except Exception as e:
        print(f"[WARN] 列舉視窗失敗: {e}")
        return []


def choose_window_dialog(parent) -> tuple:
    windows = list_visible_windows()
    if not windows:
        messagebox.showerror("找不到視窗", "目前沒有可選擇的可見視窗")
        return None, None

    dlg = Toplevel(parent)
    dlg.title("選擇要校正的目標視窗")
    dlg.geometry("760x520")
    dlg.configure(bg="#1a1a2e")
    dlg.transient(parent)
    dlg.grab_set()

    selected = {"hwnd": None, "title": None}

    Label(dlg, text="請選擇要截圖的目標視窗",
          bg="#16213e", fg="white", font=("Consolas", 12, "bold"), pady=10).pack(fill=X)

    info_var = StringVar(value="雙擊或按下方按鈕確認")
    Label(dlg, textvariable=info_var, bg="#1a1a2e", fg="#aaaaaa", font=("Consolas", 10)).pack(fill=X, pady=(8, 4))

    body = Frame(dlg, bg="#1a1a2e")
    body.pack(fill=BOTH, expand=True, padx=12, pady=8)
    scrollbar = Scrollbar(body, orient=VERTICAL)
    scrollbar.pack(side=RIGHT, fill=Y)
    lb = Listbox(body, bg="#0d0d1a", fg="white", selectbackground="#e94560",
                 font=("Consolas", 10), relief="flat", yscrollcommand=scrollbar.set)
    lb.pack(side=LEFT, fill=BOTH, expand=True)
    scrollbar.config(command=lb.yview)

    for i, (hwnd, title, w, h) in enumerate(windows):
        lb.insert(END, f"[{i+1:02d}] {title}    ({w}x{h})")

    def confirm():
        sel = lb.curselection()
        if not sel:
            messagebox.showwarning("未選擇", "請先選一個視窗", parent=dlg)
            return
        hwnd, title, _, _ = windows[sel[0]]
        selected["hwnd"] = hwnd
        selected["title"] = title
        dlg.destroy()

    def on_select(_=None):
        sel = lb.curselection()
        if not sel:
            return
        hwnd, title, w, h = windows[sel[0]]
        info_var.set(f"已選: {title} | hwnd={hwnd} | {w}x{h}")

    def _refresh(lb=lb, windows=windows, info_var=info_var):
        wins = list_visible_windows()
        windows.clear(); windows.extend(wins)
        lb.delete(0, END)
        for i, (h, t, w, hh) in enumerate(wins):
            lb.insert(END, f"[{i+1:02d}] {t}    ({w}x{hh})")
        info_var.set(f"已重新整理，共 {len(wins)} 個視窗")

    lb.bind("<<ListboxSelect>>", on_select)
    lb.bind("<Double-Button-1>", lambda e: confirm())

    btn_bar = Frame(dlg, bg="#1a1a2e")
    btn_bar.pack(fill=X, padx=12, pady=(0, 12))
    Button(btn_bar, text="重新整理視窗列表", command=_refresh,
           bg="#0f3460", fg="white", relief="flat", padx=12, pady=6,
           font=("Consolas", 10, "bold"), cursor="hand2").pack(side=LEFT)
    Button(btn_bar, text="取消", command=dlg.destroy,
           bg="#444", fg="white", relief="flat", padx=12, pady=6,
           font=("Consolas", 10, "bold"), cursor="hand2").pack(side=RIGHT, padx=(8, 0))
    Button(btn_bar, text="使用此視窗", command=confirm,
           bg="#e94560", fg="white", relief="flat", padx=12, pady=6,
           font=("Consolas", 10, "bold"), cursor="hand2").pack(side=RIGHT)

    dlg.wait_window()
    return selected["hwnd"], selected["title"]


# ───────────────────────────────────────────────────────────────
#  截圖核心
# ───────────────────────────────────────────────────────────────

def capture_hwnd(hwnd):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("screen_reader", SCREEN_READER_PATH)
        sr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sr)
        img = sr._capture_printwindow(hwnd)
        if img is not None:
            print(f"[OK] 截圖成功 (PrintWindow via screen_reader): {img.size}")
            return img
    except Exception as e:
        print(f"[WARN] screen_reader._capture_printwindow 失敗: {e}")

    try:
        import ctypes, ctypes.wintypes, win32gui, win32ui
        PW_RENDERFULLCONTENT = 2
        wr = win32gui.GetWindowRect(hwnd)
        ww, wh = wr[2]-wr[0], wr[3]-wr[1]
        if ww <= 0 or wh <= 0:
            return None
        hdc = win32gui.GetWindowDC(hwnd)
        mdc = win32ui.CreateDCFromHandle(hdc)
        sdc = mdc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mdc, ww, wh)
        sdc.SelectObject(bmp)
        ctypes.windll.user32.PrintWindow(hwnd, sdc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = bmp.GetInfo()
        data = bmp.GetBitmapBits(True)
        full = Image.frombuffer("RGB",(info["bmWidth"],info["bmHeight"]),data,"raw","BGRX",0,1)
        win32gui.DeleteObject(bmp.GetHandle())
        sdc.DeleteDC(); mdc.DeleteDC(); win32gui.ReleaseDC(hwnd, hdc)
        DWMWA = 9
        class R(ctypes.Structure):
            _fields_=[("l","i"),("t","i"),("r","i"),("b","i")]
        vis = R()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd,DWMWA,ctypes.byref(vis),ctypes.sizeof(vis))
        pt = ctypes.wintypes.POINT(0,0)
        ctypes.windll.user32.ClientToScreen(hwnd,ctypes.byref(pt))
        cr = win32gui.GetClientRect(hwnd)
        ox,oy = pt.x-vis.l, pt.y-vis.t
        img = full.crop((ox,oy,ox+cr[2],oy+cr[3]))
        print(f"[OK] 截圖成功 (local fallback): {img.size}")
        return img
    except Exception as e:
        print(f"[WARN] PrintWindow 本地備用失敗: {e}")
        return None


def take_screenshot(hwnd=None):
    if hwnd:
        img = capture_hwnd(hwnd)
        if img:
            return img
    try:
        import pyautogui
        return pyautogui.screenshot()
    except Exception:
        pass
    cal = DEBUG_DIR / "_calibrate.png"
    if cal.exists():
        return Image.open(cal)
    raise RuntimeError("無法截圖")


# ───────────────────────────────────────────────────────────────
#  花色圖標裁切工具
# ───────────────────────────────────────────────────────────────

def extract_suit_icon(card_img: Image.Image) -> Image.Image:
    """
    從卡牌圖片裁切左上角花色圖標區域。
    取左上角 35% 寬、下半段 (33%~80% 高)。
    回傳截切後放大到 64x64 的圖片。
    """
    w, h = card_img.size
    x1 = 0
    x2 = int(w * 0.38)
    y1 = int(h * 0.33)
    y2 = int(h * 0.80)
    icon = card_img.crop((x1, y1, x2, y2))
    icon = icon.resize((64, 64), Image.LANCZOS)
    return icon


def save_suit_template(card_img: Image.Image, suit_name: str) -> Path:
    """
    裁切并儲存花色圖標樣本。
    如果已存在，自動編號避免覆蓋。
    suit_name: heart / diamond / spade / club
    """
    SUIT_TEMPLATE_DIR.mkdir(exist_ok=True)
    icon = extract_suit_icon(card_img)

    # 判斷存存量，編號
    existing = sorted(SUIT_TEMPLATE_DIR.glob(f"{suit_name}*.png"))
    if (SUIT_TEMPLATE_DIR / f"{suit_name}.png").exists():
        idx = len(existing) + 1
        path = SUIT_TEMPLATE_DIR / f"{suit_name}_{idx}.png"
    else:
        path = SUIT_TEMPLATE_DIR / f"{suit_name}.png"

    icon.save(str(path))
    print(f"[OK] 花色樣本存至: {path}")
    return path


def get_template_counts() -> dict:
    """\u56de傳各花色現有樣本數量"""
    counts = {}
    for name, _, _ in SUIT_SYMBOLS:
        counts[name] = len(list(SUIT_TEMPLATE_DIR.glob(f"{name}*.png")))
    return counts


# ───────────────────────────────────────────────────────────────
#  花色選擇彈出視窗
# ───────────────────────────────────────────────────────────────

def suit_picker_dialog(parent, card_img: Image.Image, region_name: str) -> str | None:
    """
    顯示卡牌裁切預覽 + 4 個花色按鈕，讓使用者選擇。
    回傳 suit_name str 或 None (取消)。
    """
    dlg = Toplevel(parent)
    dlg.title(f"選擇花色 — {region_name}")
    dlg.configure(bg="#1a1a2e")
    dlg.transient(parent)
    dlg.grab_set()
    dlg.resizable(False, False)

    chosen = {"suit": None}

    # ── 顯示卡牌預覽 ──
    preview_frame = Frame(dlg, bg="#0d0d1a", padx=8, pady=8)
    preview_frame.pack(padx=12, pady=(12, 4))

    # 完整卡牌
    card_resized = card_img.resize((120, 170), Image.LANCZOS)
    card_photo = ImageTk.PhotoImage(card_resized)
    Label(preview_frame, image=card_photo, bg="#0d0d1a", relief="solid", bd=1).pack(side=LEFT, padx=(0, 12))

    # 裁切的花色圖標預覽
    icon = extract_suit_icon(card_img)
    icon_photo = ImageTk.PhotoImage(icon)
    icon_frame = Frame(preview_frame, bg="#0d0d1a")
    icon_frame.pack(side=LEFT)
    Label(icon_frame, text="花色圖標\n(將儲存此區堂)", bg="#0d0d1a", fg="#aaa",
          font=("Consolas", 9), justify="center").pack()
    Label(icon_frame, image=icon_photo, bg="#0d0d1a", relief="solid", bd=1).pack(pady=4)

    Label(dlg, text="請選擇此張牌的花色:",
          bg="#1a1a2e", fg="white", font=("Consolas", 11, "bold")).pack(pady=(4, 0))

    # ── 4 個花色按鈕 ──
    btn_frame = Frame(dlg, bg="#1a1a2e")
    btn_frame.pack(padx=16, pady=10)

    def pick(suit_name):
        chosen["suit"] = suit_name
        dlg.destroy()

    counts = get_template_counts()
    for name, label, color in SUIT_SYMBOLS:
        cnt = counts.get(name, 0)
        btn_text = f"{label}\n(已有 {cnt} 張)"
        Button(
            btn_frame, text=btn_text, command=lambda n=name: pick(n),
            bg="#16213e", fg=color, relief="flat",
            activebackground="#e94560", activeforeground="white",
            font=("Consolas", 12, "bold"),
            width=12, height=3, cursor="hand2",
            bd=2
        ).pack(side=LEFT, padx=6)

    Button(dlg, text="取消", command=dlg.destroy,
           bg="#444", fg="white", relief="flat", padx=20, pady=6,
           font=("Consolas", 10), cursor="hand2").pack(pady=(0, 12))

    # 保持 PhotoImage reference
    dlg._card_photo = card_photo
    dlg._icon_photo = icon_photo

    dlg.wait_window()
    return chosen["suit"]


# ───────────────────────────────────────────────────────────────
#  主應用程式
# ───────────────────────────────────────────────────────────────

class CalibrateApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(SELF_TITLE)
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(True, True)

        self.target_hwnd = None
        self.target_title = None
        self.screenshot = None
        self.photo = None
        self.scale = 1.0
        self.img_w = 1
        self.img_h = 1

        self.regions = {}
        self.orig_regions = {}
        self.selected = None
        self.drag_mode = None
        self.drag_start = None
        self.drag_box_start = None

        # 花色樣本採集模式
        self.suit_collect_mode = False

        self._build_ui()
        self._load()

    def _build_ui(self):
        toolbar = Frame(self.root, bg="#16213e", pady=6)
        toolbar.pack(side=TOP, fill=X)
        btn = dict(
            bg="#0f3460", fg="white", relief="flat", padx=10, pady=4,
            font=("Consolas", 10, "bold"), cursor="hand2",
            activebackground="#e94560", activeforeground="white"
        )
        Button(toolbar, text="🪟 選擇視窗 (W)",    command=self._choose_window, **btn).pack(side=LEFT, padx=4)
        Button(toolbar, text="📷 重新截圖 (R)",    command=self._retake,        **btn).pack(side=LEFT, padx=4)
        Button(toolbar, text="💾 儲存 regions.json (S)", command=self._save, **btn).pack(side=LEFT, padx=4)
        Button(toolbar, text="↺ 還原選取框 (Del)",   command=self._reset_selected, **btn).pack(side=LEFT, padx=4)
        Button(toolbar, text="↺↺ 全部還原",         command=self._reset_all,      **btn).pack(side=LEFT, padx=4)

        # 花色樣本採集模式切換按鈕
        self.suit_btn_var = StringVar(value="🃏 花色樣本採集 OFF (T)")
        self.suit_btn = Button(
            toolbar, textvariable=self.suit_btn_var,
            command=self._toggle_suit_mode,
            bg="#1a5c1a", fg="#aaffaa", relief="flat", padx=10, pady=4,
            font=("Consolas", 10, "bold"), cursor="hand2",
            activebackground="#2a8c2a", activeforeground="white"
        )
        self.suit_btn.pack(side=LEFT, padx=8)

        self.status_var = StringVar(value="載入中...")
        Label(toolbar, textvariable=self.status_var, bg="#16213e", fg="#aaa",
              font=("Consolas", 9)).pack(side=RIGHT, padx=12)

        legend = Frame(self.root, bg="#0f3460", width=160)
        legend.pack(side=LEFT, fill=Y)
        Label(legend, text=" 區域列表", bg="#0f3460", fg="white",
              font=("Consolas", 10, "bold"), anchor="w").pack(fill=X, pady=(8, 0))

        self.listbox = Listbox(
            legend, bg="#1a1a2e", fg="white", selectbackground="#e94560",
            font=("Consolas", 10), relief="flat", width=16, activestyle="none"
        )
        self.listbox.pack(fill=BOTH, expand=True, padx=4, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

        Label(legend, text=" 選取框座標", bg="#0f3460", fg="#aaa",
              font=("Consolas", 9), anchor="w").pack(fill=X)
        self.coord_var = StringVar(value="-")
        Label(legend, textvariable=self.coord_var, bg="#0f3460", fg="#ffcc00",
              font=("Consolas", 8), justify=LEFT, anchor="w", wraplength=150).pack(fill=X, padx=4, pady=4)

        # 花色樣本統計
        Label(legend, text=" 花色樣本數", bg="#0f3460", fg="#aaa",
              font=("Consolas", 9), anchor="w").pack(fill=X)
        self.suit_count_var = StringVar(value="-")
        Label(legend, textvariable=self.suit_count_var, bg="#0f3460", fg="#ffcc00",
              font=("Consolas", 9), justify=LEFT, anchor="w", wraplength=150).pack(fill=X, padx=4, pady=2)
        self._update_suit_counts()

        canvas_frame = Frame(self.root, bg="#1a1a2e")
        canvas_frame.pack(side=LEFT, fill=BOTH, expand=True)
        hbar = Scrollbar(canvas_frame, orient=HORIZONTAL)
        vbar = Scrollbar(canvas_frame, orient=VERTICAL)
        hbar.pack(side=BOTTOM, fill=X)
        vbar.pack(side=RIGHT, fill=Y)
        self.canvas = Canvas(canvas_frame, bg="#0d0d1a", cursor="crosshair",
                             xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        hbar.config(command=self.canvas.xview)
        vbar.config(command=self.canvas.yview)

        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",        self._on_drag)
        self.canvas.bind("<ButtonRelease-1>",  self._on_release)
        self.canvas.bind("<Motion>",           self._on_motion)

        self.root.bind("<Delete>",   lambda e: self._reset_selected())
        self.root.bind("<BackSpace>",lambda e: self._reset_selected())
        self.root.bind("s", lambda e: self._save())
        self.root.bind("S", lambda e: self._save())
        self.root.bind("r", lambda e: self._retake())
        self.root.bind("R", lambda e: self._retake())
        self.root.bind("w", lambda e: self._choose_window())
        self.root.bind("W", lambda e: self._choose_window())
        self.root.bind("t", lambda e: self._toggle_suit_mode())
        self.root.bind("T", lambda e: self._toggle_suit_mode())

    def _toggle_suit_mode(self):
        self.suit_collect_mode = not self.suit_collect_mode
        if self.suit_collect_mode:
            self.suit_btn_var.set("🃏 花色樣本採集 ON (T)")
            self.suit_btn.config(bg="#e94560", fg="white")
            self.canvas.config(cursor="plus")
            self.status_var.set("🃏 花色採集模式: 點擊任意張牌區域在畫面上")
        else:
            self.suit_btn_var.set("🃏 花色樣本採集 OFF (T)")
            self.suit_btn.config(bg="#1a5c1a", fg="#aaffaa")
            self.canvas.config(cursor="crosshair")
            self.status_var.set("已結束花色採集模式")

    def _update_suit_counts(self):
        counts = get_template_counts()
        lines = []
        for name, label, _ in SUIT_SYMBOLS:
            lines.append(f" {label}: {counts.get(name,0)} 張")
        self.suit_count_var.set("\n".join(lines))

    def _load(self):
        self.regions = load_regions_from_file()
        self.orig_regions = {k: tuple(v) for k, v in self.regions.items()}
        self._update_listbox()
        self._choose_window(initial=True)

    def _choose_window(self, initial=False):
        hwnd, title = choose_window_dialog(self.root)
        if hwnd is None:
            if initial:
                self.status_var.set("尚未選擇目標視窗")
            return
        self.target_hwnd = hwnd
        self.target_title = title
        self._retake()

    def _retake(self):
        self.status_var.set("截圖中...")
        self.root.update()
        try:
            img = take_screenshot(self.target_hwnd)
        except Exception as e:
            messagebox.showerror("截圖失敗", str(e))
            self.status_var.set("截圖失敗")
            return
        self.screenshot = img
        self.img_w, self.img_h = img.size
        cw = self.canvas.winfo_width() or MAX_DISPLAY_W
        ch = self.canvas.winfo_height() or MAX_DISPLAY_H
        self.scale = min(1.0, cw / self.img_w, ch / self.img_h)
        dw, dh = int(self.img_w * self.scale), int(self.img_h * self.scale)
        self.photo = ImageTk.PhotoImage(img.resize((dw, dh), Image.LANCZOS))
        self.canvas.config(scrollregion=(0, 0, dw, dh))
        target = self.target_title or "全螢幕"
        self.status_var.set(f"目標: {target} | {self.img_w}×{self.img_h} | 縮放: {self.scale:.2f}x")
        self._redraw()

    def _update_listbox(self):
        self.listbox.delete(0, END)
        for i, name in enumerate(self.regions):
            self.listbox.insert(END, f"  {name}")
            self.listbox.itemconfig(i, fg=REGION_COLORS.get(name, "#fff"))

    def _redraw(self):
        c = self.canvas
        c.delete(ALL)
        if self.photo:
            c.create_image(0, 0, anchor=NW, image=self.photo, tags="bg")
        for name, rel in self.regions.items():
            x1, y1, x2, y2 = self._r2c(rel)
            clr = REGION_COLORS.get(name, "#fff")
            width = 3 if name == self.selected else 2
            c.create_rectangle(x1, y1, x2, y2, outline=clr, width=width, fill="", tags=("region", name))
            if name == self.selected and not self.suit_collect_mode:
                self._draw_handles(x1, y1, x2, y2, clr, name)
            c.create_text(x1+4, y1+3, text=name, anchor=NW, fill="#000", font=("Consolas", 8, "bold"))
            c.create_text(x1+3, y1+2, text=name, anchor=NW, fill=clr,   font=("Consolas", 8, "bold"))

    def _draw_handles(self, x1, y1, x2, y2, clr, name):
        h = HANDLE_SIZE // 2
        mx, my = (x1+x2)//2, (y1+y2)//2
        for tag, cx, cy in [
            ("nw",x1,y1),("n",mx,y1),("ne",x2,y1),("e",x2,my),
            ("se",x2,y2),("s",mx,y2),("sw",x1,y2),("w",x1,my),
        ]:
            self.canvas.create_rectangle(cx-h, cy-h, cx+h, cy+h, fill=clr, outline="white", width=1,
                                         tags=("handle", f"handle_{name}_{tag}"))

    def _r2c(self, rel):
        s = self.scale
        return rel[0]*self.img_w*s, rel[1]*self.img_h*s, rel[2]*self.img_w*s, rel[3]*self.img_h*s

    def _c2r(self, cx, cy):
        return cx/(self.img_w*self.scale), cy/(self.img_h*self.scale)

    def _clamp(self, v):
        return max(0.0, min(1.0, v))

    def _cxy(self, e):
        return self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)

    def _hit_handle(self, cx, cy):
        for item in self.canvas.find_overlapping(cx-3, cy-3, cx+3, cy+3):
            for t in self.canvas.gettags(item):
                if t.startswith("handle_"):
                    parts = t[len("handle_"):].rsplit("_", 1)
                    if len(parts) == 2:
                        return parts[0], parts[1]
        return None

    def _hit_region(self, cx, cy):
        for item in reversed(self.canvas.find_overlapping(cx-2, cy-2, cx+2, cy+2)):
            tags = self.canvas.gettags(item)
            for key in ("region", "label"):
                if key in tags:
                    for t in tags:
                        if t in self.regions:
                            return t
        for name, rel in self.regions.items():
            x1, y1, x2, y2 = self._r2c(rel)
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                return name
        return None

    def _on_press(self, e):
        cx, cy = self._cxy(e)
        self.drag_start = (cx, cy)

        # 花色採集模式：點擊到幸狀區域即觸發選擇
        if self.suit_collect_mode:
            self._collect_suit_at(cx, cy)
            return

        hit = self._hit_handle(cx, cy)
        if hit:
            name, d = hit
            self.selected = name
            self.drag_mode = f"resize_{d}"
            self.drag_box_start = list(self.regions[name])
            self._redraw(); self._update_coord_label()
            return
        name = self._hit_region(cx, cy)
        if name:
            self.selected = name
            self.drag_mode = "move"
            self.drag_box_start = list(self.regions[name])
        else:
            self.selected = None
            self.drag_mode = None
        self._redraw(); self._update_coord_label()

    def _collect_suit_at(self, cx, cy):
        """
        花色採集模式下，點擊畫面任意一張牌區域圖，
        裁切該區域圖片，彈出花色選擇對話框。
        """
        if self.screenshot is None:
            messagebox.showwarning("尚未截圖", "請先按 R 重新截圖")
            return

        # 先連截圖中的區域圖
        name = self._hit_region(cx, cy)
        if name:
            rel = self.regions[name]
            x1, y1, x2, y2 = (
                int(rel[0]*self.img_w), int(rel[1]*self.img_h),
                int(rel[2]*self.img_w), int(rel[3]*self.img_h)
            )
            card_img = self.screenshot.crop((x1, y1, x2, y2))
            region_label = name
        else:
            # 點到空白處，以點擊中心為核心手動裁切一小區。
            # 預設切 hole_1 或 board_1 大小的區域
            default_w = int(0.07 * self.img_w)
            default_h = int(0.14 * self.img_h)
            rx = cx / self.scale
            ry = cy / self.scale
            x1 = max(0, int(rx - default_w/2))
            y1 = max(0, int(ry - default_h/2))
            x2 = min(self.img_w, x1 + default_w)
            y2 = min(self.img_h, y1 + default_h)
            card_img = self.screenshot.crop((x1, y1, x2, y2))
            region_label = f"({int(rx)},{int(ry)})"

        # 彈出花色選擇
        suit = suit_picker_dialog(self.root, card_img, region_label)
        if suit is None:
            return

        # 儲存樣本
        path = save_suit_template(card_img, suit)
        self._update_suit_counts()
        counts = get_template_counts()
        total = sum(counts.values())
        self.status_var.set(
            f"✅ 已儲存 {suit} 樣本 → {path.name} | 總樣本數: {total}"
        )

    def _on_drag(self, e):
        if self.suit_collect_mode or not self.drag_mode or not self.selected:
            return
        cx, cy = self._cxy(e)
        bx1, by1, bx2, by2 = self.drag_box_start
        dx, dy = self._c2r(cx - self.drag_start[0], cy - self.drag_start[1])

        if self.drag_mode == "move":
            bw, bh = bx2-bx1, by2-by1
            nx1 = self._clamp(bx1+dx)
            ny1 = self._clamp(by1+dy)
            nx2 = self._clamp(nx1+bw)
            ny2 = self._clamp(ny1+bh)
            if nx2 >= 1.0: nx1 = 1.0-bw; nx2 = 1.0
            if ny2 >= 1.0: ny1 = 1.0-bh; ny2 = 1.0
            self.regions[self.selected] = (nx1, ny1, nx2, ny2)
        else:
            d = self.drag_mode[len("resize_"):]
            rx, ry = self._c2r(cx, cy)
            rx, ry = self._clamp(rx), self._clamp(ry)
            nx1, ny1, nx2, ny2 = bx1, by1, bx2, by2
            MIN = 0.01
            if 'n' in d: ny1 = min(ry, by2-MIN)
            if 's' in d: ny2 = max(ry, by1+MIN)
            if 'w' in d: nx1 = min(rx, bx2-MIN)
            if 'e' in d: nx2 = max(rx, bx1+MIN)
            self.regions[self.selected] = (nx1, ny1, nx2, ny2)

        self._redraw(); self._update_coord_label()

    def _on_release(self, e):
        self.drag_mode = None
        self._update_coord_label()

    def _on_motion(self, e):
        if self.suit_collect_mode:
            cx, cy = self._cxy(e)
            name = self._hit_region(cx, cy)
            if name and name.startswith(("hole_", "board_")):
                self.canvas.config(cursor="plus")
            else:
                self.canvas.config(cursor="tcross")
            return
        cx, cy = self._cxy(e)
        hit = self._hit_handle(cx, cy)
        if hit:
            _, d = hit
            self.canvas.config(cursor={
                "nw":"size_nw_se","se":"size_nw_se",
                "ne":"size_ne_sw","sw":"size_ne_sw",
                "n":"size_ns","s":"size_ns",
                "e":"size_we","w":"size_we",
            }.get(d, "fleur"))
        elif self._hit_region(cx, cy):
            self.canvas.config(cursor="fleur")
        else:
            self.canvas.config(cursor="crosshair")

    def _on_listbox_select(self, e):
        sel = self.listbox.curselection()
        if not sel:
            return
        name = list(self.regions.keys())[sel[0]]
        self.selected = name
        self._redraw(); self._update_coord_label()
        if self.screenshot:
            rel = self.regions[name]
            cx = (rel[0]+rel[2])/2 * self.img_w * self.scale
            cy = (rel[1]+rel[3])/2 * self.img_h * self.scale
            tw, th = int(self.img_w*self.scale), int(self.img_h*self.scale)
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            self.canvas.xview_moveto(max(0, (cx-cw//2)/tw))
            self.canvas.yview_moveto(max(0, (cy-ch//2)/th))

    def _update_coord_label(self):
        if not self.selected or self.selected not in self.regions:
            self.coord_var.set("-"); return
        v = self.regions[self.selected]
        px1, py1 = int(v[0]*self.img_w), int(v[1]*self.img_h)
        px2, py2 = int(v[2]*self.img_w), int(v[3]*self.img_h)
        self.coord_var.set(
            f"比例:\n  x1={v[0]:.4f} y1={v[1]:.4f}\n  x2={v[2]:.4f} y2={v[3]:.4f}\n"
            f"像素 ({self.img_w}\u00d7{self.img_h}):\n  ({px1},{py1})-({px2},{py2})\n"
            f"大小: {px2-px1}\u00d7{py2-py1}"
        )
        names = list(self.regions.keys())
        if self.selected in names:
            idx = names.index(self.selected)
            self.listbox.selection_clear(0, END)
            self.listbox.selection_set(idx)
            self.listbox.see(idx)

    def _reset_selected(self):
        if self.selected and self.selected in self.orig_regions:
            self.regions[self.selected] = tuple(self.orig_regions[self.selected])
            self._redraw(); self._update_coord_label()
            self.status_var.set(f"已還原: {self.selected}")

    def _reset_all(self):
        if messagebox.askyesno("全部還原", "確定要還原所有區域到原始値？"):
            self.regions = {k: tuple(v) for k, v in self.orig_regions.items()}
            self._redraw(); self._update_coord_label()
            self.status_var.set("已還原全部區域")

    def _save(self):
        if not messagebox.askyesno("儲存", "確定要將新座標寫入 regions.json？"):
            return
        if write_regions(self.regions):
            self.orig_regions = {k: tuple(v) for k, v in self.regions.items()}
            self.status_var.set("✅ 已儲存到 regions.json")
            messagebox.showinfo("儲存成功",
                "座標已寫入 regions.json!\n"
                "下次執行 screen_reader.py 會自動載入新座標。"
            )
        else:
            messagebox.showerror("儲存失敗", "無法寫入 regions.json")


if __name__ == "__main__":
    root = Tk()
    root.geometry(f"{MAX_DISPLAY_W + 200}x{MAX_DISPLAY_H + 80}")
    CalibrateApp(root)
    root.mainloop()
