#!/usr/bin/env python3
"""
calibrate_tool.py  —  GameSofa 辨識區塊拖拉校正工具
=====================================================
用法:
    python calibrate_tool.py

功能:
  1. 自動截圖 (或載入 debug_crops/_calibrate.png)
  2. 用視窗顯示截圖，每個辨識框以彩色顯示
  3. 點選框 → 拖拉邊緣/角落 可調整大小
  4. 點選框內部 → 整框移動
  5. 按 [儲存並寫入 screen_reader.py] → 自動更新 REGIONS_REL
  6. 按 [重新截圖] → 重新取得畫面

鍵盤快捷鍵:
  Delete / Backspace → 還原選取框到原始座標
  S                  → 儲存
  R                  → 重新截圖

依賴: tkinter (內建), Pillow
"""

import sys, os, json, re, subprocess, tempfile, threading, time
from pathlib import Path
from tkinter import (Tk, Canvas, Frame, Button, Label, StringVar,
                     Scrollbar, Listbox, messagebox, BOTH,
                     LEFT, RIGHT, TOP, BOTTOM, X, Y, END,
                     HORIZONTAL, VERTICAL, NW, ALL)
try:
    from PIL import Image, ImageTk
except ImportError:
    print("[ERROR] pip install Pillow")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
SCREEN_READER_PATH = Path("screen_reader.py")
DEBUG_DIR          = Path("debug_crops")
MAX_DISPLAY_W      = 1280
MAX_DISPLAY_H      = 860
HANDLE_SIZE        = 8

# 校正工具自身標題，截圖時必須排除
SELF_TITLE = "GameSofa 辨識區塊校正工具"

# 區域顏色
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

# ─────────────────────────────────────────────────────────────
# LOAD / WRITE screen_reader.py
# ─────────────────────────────────────────────────────────────
def load_regions_from_file():
    if not SCREEN_READER_PATH.exists():
        print("[WARN] screen_reader.py 不存在，使用預設值")
        return dict(DEFAULT_REGIONS)
    src = SCREEN_READER_PATH.read_text(encoding="utf-8")
    m = re.search(r'REGIONS_REL\s*=\s*\{([^}]+)\}', src, re.DOTALL)
    if not m:
        print("[WARN] 無法解析 REGIONS_REL，使用預設值")
        return dict(DEFAULT_REGIONS)
    regions = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        m2 = re.match(
            r'"([\w]+)"\s*:\s*\(([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\)', line)
        if m2:
            regions[m2.group(1)] = tuple(float(m2.group(i)) for i in range(2,6))
    if not regions:
        return dict(DEFAULT_REGIONS)
    print(f"[OK] 從 screen_reader.py 讀取 {len(regions)} 個區域")
    return regions

def write_regions_to_file(regions: dict):
    if not SCREEN_READER_PATH.exists():
        print("[ERROR] screen_reader.py 不存在"); return False
    src = SCREEN_READER_PATH.read_text(encoding="utf-8")
    lines = ["REGIONS_REL = {\n"]
    for group_name, keys in [
        ("手牌",     ["hole_1","hole_2"]),
        ("公共牌",   ["board_1","board_2","board_3","board_4","board_5"]),
        ("底池",     ["pot"]),
        ("自己籌碼", ["my_stack"]),
        ("Blind",    ["blind"]),
    ]:
        lines.append(f"    # ── {group_name} ──\n")
        for key in keys:
            if key in regions:
                v = regions[key]
                lines.append(f'    "{key}":{" "*(10-len(key))}({v[0]:.4f}, {v[1]:.4f}, {v[2]:.4f}, {v[3]:.4f}),\n')
    lines.append("}\n")
    new_block = "".join(lines)
    new_src = re.sub(r'REGIONS_REL\s*=\s*\{[^}]+\}\s*\n', new_block, src, flags=re.DOTALL)
    if new_src == src:
        new_src = re.sub(r'REGIONS_REL\s*=\s*\{.*?^\}', new_block.rstrip(),
                         src, flags=re.DOTALL|re.MULTILINE)
    SCREEN_READER_PATH.write_text(new_src, encoding="utf-8")
    print(f"[OK] 已更新 {SCREEN_READER_PATH}")
    return True

# ─────────────────────────────────────────────────────────────
# FIND TARGET WINDOW (GameSofa, 排除自身)
# ─────────────────────────────────────────────────────────────
GAME_KEYWORDS = ["gamesofa", "神來也", "texas", "德州", "poker"]

def _find_game_hwnd():
    """尋找 GameSofa 視窗 hwnd，排除校正工具自身"""
    if sys.platform != "win32": return None
    try:
        import win32gui
        candidates = []
        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd): return
            title = win32gui.GetWindowText(hwnd)
            # 排除校正工具自身
            if SELF_TITLE in title: return
            if any(kw in title.lower() for kw in GAME_KEYWORDS):
                candidates.append((hwnd, title))
        win32gui.EnumWindows(cb, None)
        if not candidates: return None
        # 優先選 GameSofa 游戲視窗
        for hwnd, title in candidates:
            if any(k in title.lower() for k in ["gamesofa","神來也","德州","texas"]):
                print(f"[OK] 找到目標視窗: \"{title}\"")
                return hwnd
        hwnd, title = candidates[0]
        print(f"[OK] 使用視窗: \"{title}\"")
        return hwnd
    except Exception as e:
        print(f"[WARN] 視窗偵測失敗: {e}")
        return None

def _capture_hwnd(hwnd) -> "Image.Image | None":
    """PrintWindow 截指定 hwnd"""
    try:
        import win32gui, win32ui
        from ctypes import windll
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right-left, bottom-top
        if w <= 0 or h <= 0: return None
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc  = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp     = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if result == 0:
            result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 1)
        info = bmp.GetInfo(); data = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB",(info["bmWidth"],info["bmHeight"]),
                               data,"raw","BGRX",0,1)
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC(); mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        if result == 0: return None
        return img
    except Exception as e:
        print(f"[WARN] PrintWindow 失敗: {e}")
        return None

# ─────────────────────────────────────────────────────────────
# TAKE SCREENSHOT  (與 calibrate_tool 自身完全分離)
#
# 截圖步驟:
#   1. 先找 GameSofa 視窗 (hwnd)，排除校正工具
#   2. 找到 → PrintWindow 截圖
#   3. 找不到 → 用 pyautogui 全螢幕
#   4. 還是不行 → 載入上次的 _calibrate.png
# ─────────────────────────────────────────────────────────────
def take_screenshot() -> Image.Image:
    # Step 1: 找 GameSofa hwnd (排除自身)
    hwnd = _find_game_hwnd()
    if hwnd:
        img = _capture_hwnd(hwnd)
        if img:
            print(f"[OK] 截圖成功 (PrintWindow): {img.size}")
            return img
        print("[WARN] PrintWindow 失敗，嘗試 pyautogui")

    # Step 2: pyautogui 全螢幕
    try:
        import pyautogui
        img = pyautogui.screenshot()
        print(f"[OK] 截圖成功 (pyautogui 全螢幕): {img.size}")
        return img
    except Exception as e:
        print(f"[WARN] pyautogui 失敗: {e}")

    # Step 3: 載入上次 calibrate.png
    cal = DEBUG_DIR / "_calibrate.png"
    if cal.exists():
        print(f"[INFO] 載入上次校正截圖: {cal}")
        return Image.open(cal)

    raise RuntimeError(
        "找不到 GameSofa 視窗，且 pyautogui 不可用。\n"
        "請先執行 screen_reader.py，按 F6 校正一次後再開啟此工具。"
    )

# ─────────────────────────────────────────────────────────────
# GUI APPLICATION
# ─────────────────────────────────────────────────────────────
class CalibrateApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(SELF_TITLE)
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(True, True)

        self.screenshot: Image.Image = None
        self.photo: ImageTk.PhotoImage = None
        self.scale = 1.0
        self.img_w = 1; self.img_h = 1

        self.regions: dict = {}
        self.orig_regions: dict = {}

        self.selected    = None
        self.drag_mode   = None
        self.drag_start  = None
        self.drag_box_start = None

        self._build_ui()
        self._load()

    # ── UI ─────────────────────────────────────────────────────
    def _build_ui(self):
        toolbar = Frame(self.root, bg="#16213e", pady=6)
        toolbar.pack(side=TOP, fill=X)
        btn = dict(bg="#0f3460", fg="white", relief="flat", padx=12, pady=4,
                   font=("Consolas",10,"bold"), cursor="hand2",
                   activebackground="#e94560", activeforeground="white")
        Button(toolbar, text="📷 重新截圖 (R)",
               command=self._retake, **btn).pack(side=LEFT, padx=4)
        Button(toolbar, text="💾 儲存並寫入 screen_reader.py (S)",
               command=self._save, **btn).pack(side=LEFT, padx=4)
        Button(toolbar, text="↺ 還原選取框 (Del)",
               command=self._reset_selected, **btn).pack(side=LEFT, padx=4)
        Button(toolbar, text="↺↺ 全部還原",
               command=self._reset_all, **btn).pack(side=LEFT, padx=4)
        self.status_var = StringVar(value="載入中...")
        Label(toolbar, textvariable=self.status_var, bg="#16213e", fg="#aaa",
              font=("Consolas",9)).pack(side=RIGHT, padx=12)

        legend = Frame(self.root, bg="#0f3460", width=160)
        legend.pack(side=LEFT, fill=Y)
        Label(legend, text=" 區域列表", bg="#0f3460", fg="white",
              font=("Consolas",10,"bold"), anchor="w").pack(fill=X, pady=(8,0))
        self.listbox = Listbox(legend, bg="#1a1a2e", fg="white",
                               selectbackground="#e94560", font=("Consolas",10),
                               relief="flat", width=16, activestyle="none")
        self.listbox.pack(fill=BOTH, expand=True, padx=4, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self._on_listbox_select)
        Label(legend, text=" 選取框座標", bg="#0f3460", fg="#aaa",
              font=("Consolas",9), anchor="w").pack(fill=X)
        self.coord_var = StringVar(value="-")
        Label(legend, textvariable=self.coord_var, bg="#0f3460", fg="#ffcc00",
              font=("Consolas",8), justify=LEFT, anchor="w",
              wraplength=150).pack(fill=X, padx=4, pady=4)

        cf = Frame(self.root, bg="#1a1a2e")
        cf.pack(side=LEFT, fill=BOTH, expand=True)
        hbar = Scrollbar(cf, orient=HORIZONTAL); hbar.pack(side=BOTTOM, fill=X)
        vbar = Scrollbar(cf, orient=VERTICAL);   vbar.pack(side=RIGHT,  fill=Y)
        self.canvas = Canvas(cf, bg="#0d0d1a", cursor="crosshair",
                             xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        hbar.config(command=self.canvas.xview)
        vbar.config(command=self.canvas.yview)

        self.canvas.bind("<ButtonPress-1>",  self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>",          self._on_motion)
        self.root.bind("<Delete>",    lambda e: self._reset_selected())
        self.root.bind("<BackSpace>", lambda e: self._reset_selected())
        self.root.bind("s", lambda e: self._save())
        self.root.bind("S", lambda e: self._save())
        self.root.bind("r", lambda e: self._retake())
        self.root.bind("R", lambda e: self._retake())

    # ── 載入 / 截圖 ───────────────────────────────────────────
    def _load(self):
        self.regions = load_regions_from_file()
        self.orig_regions = {k: tuple(v) for k, v in self.regions.items()}
        self._update_listbox()
        self._retake()

    def _retake(self):
        self.status_var.set("截圖中..."); self.root.update()
        try:
            img = take_screenshot()
        except Exception as e:
            messagebox.showerror("截圖失敗", str(e))
            self.status_var.set("截圖失敗")
            return
        self.screenshot = img
        self.img_w, self.img_h = img.size
        cw = self.canvas.winfo_width()  or MAX_DISPLAY_W
        ch = self.canvas.winfo_height() or MAX_DISPLAY_H
        self.scale = min(1.0, cw/self.img_w, ch/self.img_h)
        dw = int(self.img_w * self.scale)
        dh = int(self.img_h * self.scale)
        self.photo = ImageTk.PhotoImage(img.resize((dw,dh), Image.LANCZOS))
        self.canvas.config(scrollregion=(0, 0, dw, dh))
        self.status_var.set(f"截圖: {self.img_w}×{self.img_h}  縮放: {self.scale:.2f}x")
        self._redraw()

    def _update_listbox(self):
        self.listbox.delete(0, END)
        for i, name in enumerate(self.regions):
            self.listbox.insert(END, f"  {name}")
            self.listbox.itemconfig(i, fg=REGION_COLORS.get(name, "#fff"))

    # ── 繪製 ─────────────────────────────────────────────────
    def _redraw(self):
        c = self.canvas; c.delete(ALL)
        if self.photo:
            c.create_image(0, 0, anchor=NW, image=self.photo, tags="bg")
        for name, rel in self.regions.items():
            x1, y1, x2, y2 = self._r2c(rel)
            clr = REGION_COLORS.get(name, "#fff")
            w   = 3 if name == self.selected else 2
            c.create_rectangle(x1, y1, x2, y2, outline=clr, width=w,
                               fill="", tags=("region", name))
            if name == self.selected:
                self._draw_handles(x1, y1, x2, y2, clr, name)
            # label shadow
            c.create_text(x1+4, y1+3, text=name, anchor=NW, fill="#000",
                          font=("Consolas",8,"bold"), tags=("label",name))
            c.create_text(x1+3, y1+2, text=name, anchor=NW, fill=clr,
                          font=("Consolas",8,"bold"), tags=("label",name))

    def _draw_handles(self, x1, y1, x2, y2, clr, name):
        h = HANDLE_SIZE // 2
        mx, my = (x1+x2)//2, (y1+y2)//2
        for tag, cx, cy in [
            ("nw",x1,y1),("n",mx,y1),("ne",x2,y1),("e",x2,my),
            ("se",x2,y2),("s",mx,y2),("sw",x1,y2),("w",x1,my),
        ]:
            self.canvas.create_rectangle(
                cx-h, cy-h, cx+h, cy+h,
                fill=clr, outline="white", width=1,
                tags=("handle", f"handle_{name}_{tag}")
            )

    # ── 座標轉換 ─────────────────────────────────────────────
    def _r2c(self, rel):
        """ratio -> canvas px"""
        s = self.scale; w = self.img_w; h = self.img_h
        return (rel[0]*w*s, rel[1]*h*s, rel[2]*w*s, rel[3]*h*s)

    def _c2r(self, cx, cy):
        """canvas px -> ratio"""
        return cx/(self.img_w*self.scale), cy/(self.img_h*self.scale)

    def _clamp(self, v): return max(0.0, min(1.0, v))

    # ── 滑鼠事件 ─────────────────────────────────────────────
    def _cxy(self, e):
        return self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)

    def _hit_handle(self, cx, cy):
        for item in self.canvas.find_overlapping(cx-3, cy-3, cx+3, cy+3):
            for t in self.canvas.gettags(item):
                if t.startswith("handle_"):
                    parts = t[len("handle_"):].rsplit("_", 1)
                    if len(parts) == 2: return parts[0], parts[1]
        return None

    def _hit_region(self, cx, cy):
        for item in reversed(self.canvas.find_overlapping(cx-2,cy-2,cx+2,cy+2)):
            tags = self.canvas.gettags(item)
            for key in ("region", "label"):
                if key in tags:
                    for t in tags:
                        if t in self.regions: return t
        for name, rel in self.regions.items():
            x1,y1,x2,y2 = self._r2c(rel)
            if x1<=cx<=x2 and y1<=cy<=y2: return name
        return None

    def _on_press(self, e):
        cx, cy = self._cxy(e)
        self.drag_start = (cx, cy)
        hit = self._hit_handle(cx, cy)
        if hit:
            name, d = hit
            self.selected = name
            self.drag_mode = f"resize_{d}"
            self.drag_box_start = list(self.regions[name])
            self._redraw(); self._update_coord_label(); return
        name = self._hit_region(cx, cy)
        if name:
            self.selected = name
            self.drag_mode = "move"
            self.drag_box_start = list(self.regions[name])
        else:
            self.selected = None; self.drag_mode = None
        self._redraw(); self._update_coord_label()

    def _on_drag(self, e):
        if not self.drag_mode or not self.selected: return
        cx, cy = self._cxy(e)
        bx1, by1, bx2, by2 = self.drag_box_start
        dx, dy = self._c2r(cx - self.drag_start[0], cy - self.drag_start[1])

        if self.drag_mode == "move":
            bw, bh = bx2-bx1, by2-by1
            nx1 = self._clamp(bx1+dx); ny1 = self._clamp(by1+dy)
            nx2 = self._clamp(nx1+bw); ny2 = self._clamp(ny1+bh)
            if nx2 >= 1.0: nx1 = 1.0-bw; nx2 = 1.0
            if ny2 >= 1.0: ny1 = 1.0-bh; ny2 = 1.0
            self.regions[self.selected] = (nx1, ny1, nx2, ny2)
        else:
            d = self.drag_mode[len("resize_"):]
            rx, ry = self._c2r(cx, cy)
            rx = self._clamp(rx); ry = self._clamp(ry)
            nx1,ny1,nx2,ny2 = bx1,by1,bx2,by2
            MIN = 0.01
            if 'n' in d: ny1 = min(ry, by2-MIN)
            if 's' in d: ny2 = max(ry, by1+MIN)
            if 'w' in d: nx1 = min(rx, bx2-MIN)
            if 'e' in d: nx2 = max(rx, bx1+MIN)
            self.regions[self.selected] = (nx1, ny1, nx2, ny2)

        self._redraw(); self._update_coord_label()

    def _on_release(self, e):
        self.drag_mode = None; self._update_coord_label()

    def _on_motion(self, e):
        cx, cy = self._cxy(e)
        hit = self._hit_handle(cx, cy)
        if hit:
            _,d = hit
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

    # ── listbox ───────────────────────────────────────────────
    def _on_listbox_select(self, e):
        sel = self.listbox.curselection()
        if not sel: return
        name = list(self.regions.keys())[sel[0]]
        self.selected = name
        self._redraw(); self._update_coord_label()
        if self.screenshot:
            rel = self.regions[name]
            cx = (rel[0]+rel[2])/2 * self.img_w * self.scale
            cy = (rel[1]+rel[3])/2 * self.img_h * self.scale
            tw = int(self.img_w*self.scale); th = int(self.img_h*self.scale)
            cw = self.canvas.winfo_width(); ch = self.canvas.winfo_height()
            self.canvas.xview_moveto(max(0,(cx-cw//2)/tw))
            self.canvas.yview_moveto(max(0,(cy-ch//2)/th))

    def _update_coord_label(self):
        if not self.selected or self.selected not in self.regions:
            self.coord_var.set("-"); return
        v = self.regions[self.selected]
        px1=int(v[0]*self.img_w); py1=int(v[1]*self.img_h)
        px2=int(v[2]*self.img_w); py2=int(v[3]*self.img_h)
        self.coord_var.set(
            f"比例:\n  x1={v[0]:.4f} y1={v[1]:.4f}\n  x2={v[2]:.4f} y2={v[3]:.4f}\n"
            f"像素 ({self.img_w}×{self.img_h}):\n  ({px1},{py1})-({px2},{py2})\n"
            f"大小: {px2-px1}×{py2-py1}"
        )
        names = list(self.regions.keys())
        if self.selected in names:
            idx = names.index(self.selected)
            self.listbox.selection_clear(0,END)
            self.listbox.selection_set(idx)
            self.listbox.see(idx)

    # ── 操作 ────────────────────────────────────────────────
    def _reset_selected(self):
        if self.selected and self.selected in self.orig_regions:
            self.regions[self.selected] = tuple(self.orig_regions[self.selected])
            self._redraw(); self._update_coord_label()
            self.status_var.set(f"已還原: {self.selected}")

    def _reset_all(self):
        if messagebox.askyesno("全部還原", "確定要還原所有區域到原始值？"):
            self.regions = {k:tuple(v) for k,v in self.orig_regions.items()}
            self._redraw(); self._update_coord_label()
            self.status_var.set("已還原全部區域")

    def _save(self):
        if not messagebox.askyesno("儲存","確定要將新座標寫入 screen_reader.py？"): return
        if write_regions_to_file(self.regions):
            self.orig_regions = {k:tuple(v) for k,v in self.regions.items()}
            self.status_var.set("✅ 已儲存到 screen_reader.py")
            messagebox.showinfo("儲存成功",
                "座標已更新！\n下次執行 screen_reader.py 會使用新座標。")
        else:
            messagebox.showerror("儲存失敗","無法寫入 screen_reader.py")

# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = Tk()
    root.geometry(f"{MAX_DISPLAY_W+200}x{MAX_DISPLAY_H+80}")
    CalibrateApp(root)
    root.mainloop()
