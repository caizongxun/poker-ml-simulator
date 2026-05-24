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
                     Scrollbar, Listbox, messagebox, filedialog, BOTH,
                     LEFT, RIGHT, TOP, BOTTOM, X, Y, END, HORIZONTAL,
                     VERTICAL, NW, N, S, E, W, ALL)
try:
    from PIL import Image, ImageTk, ImageDraw
except ImportError:
    print("[ERROR] pip install Pillow")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
SCREEN_READER_PATH = Path("screen_reader.py")
DEBUG_DIR          = Path("debug_crops")
MAX_DISPLAY_W      = 1280   # 顯示最大寬度
MAX_DISPLAY_H      = 860    # 顯示最大高度
HANDLE_SIZE        = 8      # 拖拉控制點大小(px)

# 區域顏色定義
REGION_COLORS = {
    "hole_1":   "#00ff41",  # 亮綠
    "hole_2":   "#00ff41",
    "board_1":  "#ff6b6b",  # 紅
    "board_2":  "#ff6b6b",
    "board_3":  "#ff6b6b",
    "board_4":  "#ff6b6b",
    "board_5":  "#ff6b6b",
    "pot":      "#ffd700",  # 金
    "my_stack": "#00cfff",  # 水藍
    "blind":    "#ff9f43",  # 橙
}

# 預設 REGIONS_REL (fallback，會從 screen_reader.py 讀取)
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
# LOAD REGIONS FROM screen_reader.py
# ─────────────────────────────────────────────────────────────
def load_regions_from_file():
    """從 screen_reader.py 解析 REGIONS_REL 字典"""
    if not SCREEN_READER_PATH.exists():
        print("[WARN] screen_reader.py 不存在，使用預設值")
        return dict(DEFAULT_REGIONS)
    src = SCREEN_READER_PATH.read_text(encoding="utf-8")
    # 找 REGIONS_REL = { ... }
    m = re.search(
        r'REGIONS_REL\s*=\s*\{([^}]+)\}',
        src, re.DOTALL
    )
    if not m:
        print("[WARN] 無法解析 REGIONS_REL，使用預設值")
        return dict(DEFAULT_REGIONS)
    body = m.group(1)
    regions = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        # "hole_1": (0.51, 0.73, 0.57, 0.85),
        m2 = re.match(
            r'"([\w]+)"\s*:\s*\(([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\)',
            line
        )
        if m2:
            name = m2.group(1)
            vals = tuple(float(m2.group(i)) for i in range(2,6))
            regions[name] = vals
    if not regions:
        return dict(DEFAULT_REGIONS)
    print(f"[OK] 從 screen_reader.py 讀取 {len(regions)} 個區域")
    return regions

# ─────────────────────────────────────────────────────────────
# WRITE REGIONS TO screen_reader.py
# ─────────────────────────────────────────────────────────────
def write_regions_to_file(regions: dict):
    """將新的比例座標寫回 screen_reader.py 的 REGIONS_REL"""
    if not SCREEN_READER_PATH.exists():
        print("[ERROR] screen_reader.py 不存在")
        return False
    src = SCREEN_READER_PATH.read_text(encoding="utf-8")

    # 建立新的 REGIONS_REL 字串
    lines = ["REGIONS_REL = {\n"]
    region_groups = [
        ("手牌", ["hole_1","hole_2"]),
        ("公共牌", ["board_1","board_2","board_3","board_4","board_5"]),
        ("底池",  ["pot"]),
        ("自己籌碼", ["my_stack"]),
        ("Blind", ["blind"]),
    ]
    for group_name, keys in region_groups:
        lines.append(f"    # ── {group_name} ──\n")
        for key in keys:
            if key in regions:
                v = regions[key]
                lines.append(f'    "{key}":{" "*(10-len(key))}({v[0]:.4f}, {v[1]:.4f}, {v[2]:.4f}, {v[3]:.4f}),\n')
    lines.append("}\n")
    new_block = "".join(lines)

    # 替換原本的 REGIONS_REL block
    new_src = re.sub(
        r'REGIONS_REL\s*=\s*\{[^}]+\}\s*\n',
        new_block,
        src, flags=re.DOTALL
    )
    if new_src == src:
        # 嘗試更寬鬆的替換（多行註解）
        new_src = re.sub(
            r'REGIONS_REL\s*=\s*\{.*?^\}',
            new_block.rstrip(),
            src, flags=re.DOTALL | re.MULTILINE
        )
    SCREEN_READER_PATH.write_text(new_src, encoding="utf-8")
    print(f"[OK] 已更新 {SCREEN_READER_PATH}")
    return True

# ─────────────────────────────────────────────────────────────
# CAPTURE SCREENSHOT
# ─────────────────────────────────────────────────────────────
def take_screenshot() -> Image.Image:
    """嘗試用 screen_reader 模組截圖，否則用 pyautogui/全螢幕"""
    # 優先用 screen_reader 的截圖邏輯
    try:
        sys.path.insert(0, str(Path.cwd()))
        import screen_reader as sr
        wc = sr.get_window_capture()
        img = wc.capture()
        print(f"[OK] 截圖成功: {img.size}")
        return img
    except Exception as e:
        print(f"[WARN] screen_reader 截圖失敗: {e}")

    # fallback: pyautogui
    try:
        import pyautogui
        return pyautogui.screenshot()
    except Exception:
        pass

    # fallback: 用上次的 calibrate.png
    cal = DEBUG_DIR / "_calibrate.png"
    if cal.exists():
        print(f"[INFO] 載入上次校正截圖: {cal}")
        return Image.open(cal)

    raise RuntimeError("無法截圖，請先執行 screen_reader.py 的 F6 校正")

# ─────────────────────────────────────────────────────────────
# GUI APPLICATION
# ─────────────────────────────────────────────────────────────
class CalibrateApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("GameSofa 辨識區塊校正工具")
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(True, True)

        # 狀態
        self.screenshot: Image.Image = None
        self.photo: ImageTk.PhotoImage = None
        self.scale = 1.0          # 截圖顯示縮放比例
        self.img_offset = (0, 0)  # 圖片在 canvas 上的偏移

        self.regions: dict = {}   # name -> [x1,y1,x2,y2] (比例)
        self.orig_regions: dict = {}  # 備份原始值

        # 拖拉狀態
        self.selected = None      # 選中的 region name
        self.drag_mode = None     # 'move' | 'resize_XX'
        self.drag_start = None    # (canvas_x, canvas_y)
        self.drag_box_start = None # 拖拉開始時的框座標

        self._build_ui()
        self._load()

    # ── UI 建構 ───────────────────────────────────────────────
    def _build_ui(self):
        # 上方工具列
        toolbar = Frame(self.root, bg="#16213e", pady=6)
        toolbar.pack(side=TOP, fill=X)

        btn_style = dict(bg="#0f3460", fg="white", relief="flat",
                         padx=12, pady=4, font=("Consolas",10,"bold"),
                         cursor="hand2", activebackground="#e94560",
                         activeforeground="white")

        Button(toolbar, text="📷  重新截圖 (R)",
               command=self._retake, **btn_style).pack(side=LEFT, padx=4)
        Button(toolbar, text="💾  儲存並寫入 screen_reader.py (S)",
               command=self._save, **btn_style).pack(side=LEFT, padx=4)
        Button(toolbar, text="↺  還原選取框 (Del)",
               command=self._reset_selected, **btn_style).pack(side=LEFT, padx=4)
        Button(toolbar, text="↺↺ 全部還原",
               command=self._reset_all, **btn_style).pack(side=LEFT, padx=4)

        self.status_var = StringVar(value="載入中...")
        Label(toolbar, textvariable=self.status_var,
              bg="#16213e", fg="#aaa",
              font=("Consolas",9)).pack(side=RIGHT, padx=12)

        # 左側圖例
        legend = Frame(self.root, bg="#0f3460", width=160)
        legend.pack(side=LEFT, fill=Y)
        Label(legend, text=" 區域列表", bg="#0f3460", fg="white",
              font=("Consolas",10,"bold"), anchor="w").pack(fill=X, pady=(8,0))

        self.listbox = Listbox(
            legend, bg="#1a1a2e", fg="white", selectbackground="#e94560",
            font=("Consolas",10), relief="flat", width=16, activestyle="none"
        )
        self.listbox.pack(fill=BOTH, expand=True, padx=4, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

        # 座標顯示
        Label(legend, text=" 選取框座標", bg="#0f3460", fg="#aaa",
              font=("Consolas",9), anchor="w").pack(fill=X)
        self.coord_var = StringVar(value="-")
        Label(legend, textvariable=self.coord_var,
              bg="#0f3460", fg="#ffcc00", font=("Consolas",8),
              justify=LEFT, anchor="w", wraplength=150).pack(fill=X, padx=4, pady=4)

        # 主 Canvas (含卷軸)
        canvas_frame = Frame(self.root, bg="#1a1a2e")
        canvas_frame.pack(side=LEFT, fill=BOTH, expand=True)

        hbar = Scrollbar(canvas_frame, orient=HORIZONTAL, bg="#0f3460")
        vbar = Scrollbar(canvas_frame, orient=VERTICAL,   bg="#0f3460")
        hbar.pack(side=BOTTOM, fill=X)
        vbar.pack(side=RIGHT,  fill=Y)

        self.canvas = Canvas(
            canvas_frame,
            bg="#0d0d1a", cursor="crosshair",
            xscrollcommand=hbar.set,
            yscrollcommand=vbar.set,
        )
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        hbar.config(command=self.canvas.xview)
        vbar.config(command=self.canvas.yview)

        # 綁定事件
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",        self._on_drag)
        self.canvas.bind("<ButtonRelease-1>",  self._on_release)
        self.canvas.bind("<Motion>",           self._on_motion)
        self.root.bind("<Delete>",    lambda e: self._reset_selected())
        self.root.bind("<BackSpace>", lambda e: self._reset_selected())
        self.root.bind("s",           lambda e: self._save())
        self.root.bind("S",           lambda e: self._save())
        self.root.bind("r",           lambda e: self._retake())
        self.root.bind("R",           lambda e: self._retake())

    # ── 載入資料 ──────────────────────────────────────────────
    def _load(self):
        self.regions = load_regions_from_file()
        self.orig_regions = {k: tuple(v) for k, v in self.regions.items()}
        self._update_listbox()
        self._retake()

    def _retake(self):
        self.status_var.set("截圖中...")
        self.root.update()
        try:
            img = take_screenshot()
        except Exception as e:
            messagebox.showerror("截圖失敗", str(e))
            self.status_var.set("截圖失敗")
            return
        self.screenshot = img
        self.img_w, self.img_h = img.size

        # 計算縮放 (適合視窗)
        cw = self.canvas.winfo_width()  or MAX_DISPLAY_W
        ch = self.canvas.winfo_height() or MAX_DISPLAY_H
        scale_w = min(1.0, cw / self.img_w)
        scale_h = min(1.0, ch / self.img_h)
        self.scale = min(scale_w, scale_h, 1.0)

        disp_w = int(self.img_w * self.scale)
        disp_h = int(self.img_h * self.scale)
        disp_img = img.resize((disp_w, disp_h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(disp_img)
        self.canvas.config(scrollregion=(0, 0, disp_w, disp_h))
        self.status_var.set(f"截圖: {self.img_w}×{self.img_h}  縮放: {self.scale:.2f}")
        self._redraw()

    def _update_listbox(self):
        self.listbox.delete(0, END)
        for name in self.regions:
            self.listbox.insert(END, f"  {name}")
        # 設定顏色
        for i, name in enumerate(self.regions):
            clr = REGION_COLORS.get(name, "#ffffff")
            self.listbox.itemconfig(i, fg=clr)

    # ── 繪製 ──────────────────────────────────────────────────
    def _redraw(self):
        c = self.canvas
        c.delete(ALL)
        # 畫截圖
        if self.photo:
            c.create_image(0, 0, anchor=NW, image=self.photo, tags="bg")

        # 畫每個框
        for name, rel in self.regions.items():
            x1c, y1c, x2c, y2c = self._rel_to_canvas(rel)
            color  = REGION_COLORS.get(name, "#ffffff")
            width  = 3 if name == self.selected else 2
            stipple = "" if name == self.selected else ""

            # 填充（選中時半透明效果）
            if name == self.selected:
                c.create_rectangle(x1c, y1c, x2c, y2c,
                                   outline=color, width=3,
                                   fill="", tags=("region", name))
                # 畫控制點
                self._draw_handles(x1c, y1c, x2c, y2c, color, name)
            else:
                c.create_rectangle(x1c, y1c, x2c, y2c,
                                   outline=color, width=2,
                                   fill="", tags=("region", name))

            # 標籤
            label_x = x1c + 3
            label_y = y1c + 2
            # 背景遮罩
            c.create_text(label_x+1, label_y+1, text=name,
                          anchor=NW, fill="#000", font=("Consolas",8,"bold"),
                          tags=("label", name))
            c.create_text(label_x, label_y, text=name,
                          anchor=NW, fill=color, font=("Consolas",8,"bold"),
                          tags=("label", name))

    def _draw_handles(self, x1, y1, x2, y2, color, name):
        h = HANDLE_SIZE // 2
        corners = [
            ("nw", x1, y1), ("n",  (x1+x2)//2, y1),
            ("ne", x2, y1), ("e",  x2, (y1+y2)//2),
            ("se", x2, y2), ("s",  (x1+x2)//2, y2),
            ("sw", x1, y2), ("w",  x1, (y1+y2)//2),
        ]
        for tag, cx, cy in corners:
            self.canvas.create_rectangle(
                cx-h, cy-h, cx+h, cy+h,
                fill=color, outline="white", width=1,
                tags=("handle", f"handle_{name}_{tag}")
            )

    # ── 座標轉換 ──────────────────────────────────────────────
    def _rel_to_canvas(self, rel):
        rx1, ry1, rx2, ry2 = rel
        s = self.scale
        w, h = self.img_w, self.img_h
        return (rx1*w*s, ry1*h*s, rx2*w*s, ry2*h*s)

    def _canvas_to_rel(self, cx, cy):
        s = self.scale
        w, h = self.img_w, self.img_h
        return (cx / (w*s), cy / (h*s))

    def _clamp_rel(self, v):
        return max(0.0, min(1.0, v))

    # ── 滑鼠事件 ──────────────────────────────────────────────
    def _canvas_xy(self, event):
        """取得相對於 canvas 圖片的座標（考慮捲動）"""
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        return x, y

    def _hit_handle(self, cx, cy):
        """偵測是否點到控制點，回傳 (name, handle_tag) 或 None"""
        h = HANDLE_SIZE
        items = self.canvas.find_overlapping(cx-2, cy-2, cx+2, cy+2)
        for item in items:
            tags = self.canvas.gettags(item)
            for t in tags:
                if t.startswith("handle_") and "_" in t[7:]:
                    # handle_hole_1_nw → name=hole_1, dir=nw
                    parts = t[len("handle_"):].rsplit("_", 1)
                    if len(parts) == 2:
                        return parts[0], parts[1]
        return None

    def _hit_region(self, cx, cy):
        """偵測是否點到某個框（非控制點），回傳 name 或 None"""
        items = self.canvas.find_overlapping(cx-2, cy-2, cx+2, cy+2)
        for item in reversed(items):  # 最上層優先
            tags = self.canvas.gettags(item)
            if "region" in tags:
                for t in tags:
                    if t in self.regions:
                        return t
            if "label" in tags:
                for t in tags:
                    if t in self.regions:
                        return t
        # 如果沒命中，檢查哪個框包含此點
        for name, rel in self.regions.items():
            x1c, y1c, x2c, y2c = self._rel_to_canvas(rel)
            if x1c <= cx <= x2c and y1c <= cy <= y2c:
                return name
        return None

    def _on_press(self, event):
        cx, cy = self._canvas_xy(event)
        self.drag_start = (cx, cy)

        # 先查控制點
        hit = self._hit_handle(cx, cy)
        if hit:
            name, direction = hit
            self.selected = name
            self.drag_mode = f"resize_{direction}"
            self.drag_box_start = list(self.regions[name])
            self._redraw(); self._update_coord_label()
            return

        # 查框內部
        name = self._hit_region(cx, cy)
        if name:
            self.selected = name
            self.drag_mode = "move"
            self.drag_box_start = list(self.regions[name])
            self._redraw(); self._update_coord_label()
        else:
            self.selected = None
            self.drag_mode = None
            self._redraw()

    def _on_drag(self, event):
        if not self.drag_mode or not self.selected: return
        cx, cy = self._canvas_xy(event)
        dx_rel, dy_rel = self._canvas_to_rel(
            cx - self.drag_start[0], cy - self.drag_start[1]
        )
        bx1, by1, bx2, by2 = self.drag_box_start

        if self.drag_mode == "move":
            w = bx2 - bx1; h = by2 - by1
            nx1 = self._clamp_rel(bx1 + dx_rel)
            ny1 = self._clamp_rel(by1 + dy_rel)
            nx2 = self._clamp_rel(nx1 + w)
            ny2 = self._clamp_rel(ny1 + h)
            # 防止超出右/下邊界後位移
            if nx2 > 1.0: nx1 = 1.0 - w; nx2 = 1.0
            if ny2 > 1.0: ny1 = 1.0 - h; ny2 = 1.0
            self.regions[self.selected] = (nx1, ny1, nx2, ny2)

        else:  # resize
            d = self.drag_mode[len("resize_"):]
            nx1, ny1, nx2, ny2 = bx1, by1, bx2, by2
            cx_rel = self._clamp_rel(bx1 + (cx - self.drag_start[0]) / (self.img_w * self.scale) + (bx2-bx1) if 'e' in d else bx1)

            rx = self._clamp_rel(bx1 + (cx - self.drag_start[0]) / (self.img_w * self.scale))
            ry = self._clamp_rel(by1 + (cy - self.drag_start[1]) / (self.img_h * self.scale))
            # 計算拖拉後游標在圖片上的比例座標
            cur_rx, cur_ry = self._canvas_to_rel(cx, cy)
            cur_rx = self._clamp_rel(cur_rx)
            cur_ry = self._clamp_rel(cur_ry)

            MIN = 0.01
            if 'n' in d: ny1 = min(cur_ry, by2 - MIN)
            if 's' in d: ny2 = max(cur_ry, by1 + MIN)
            if 'w' in d: nx1 = min(cur_rx, bx2 - MIN)
            if 'e' in d: nx2 = max(cur_rx, bx1 + MIN)

            self.regions[self.selected] = (nx1, ny1, nx2, ny2)

        self._redraw()
        self._update_coord_label()

    def _on_release(self, event):
        self.drag_mode = None
        self._update_coord_label()

    def _on_motion(self, event):
        cx, cy = self._canvas_xy(event)
        # 更新游標樣式
        hit = self._hit_handle(cx, cy)
        if hit:
            _, d = hit
            cursors = {
                "nw":"size_nw_se", "se":"size_nw_se",
                "ne":"size_ne_sw", "sw":"size_ne_sw",
                "n":"size_ns",     "s":"size_ns",
                "e":"size_we",     "w":"size_we",
            }
            self.canvas.config(cursor=cursors.get(d, "fleur"))
        elif self._hit_region(cx, cy):
            self.canvas.config(cursor="fleur")
        else:
            self.canvas.config(cursor="crosshair")

    # ── 列表選擇 ─────────────────────────────────────────────
    def _on_listbox_select(self, event):
        sel = self.listbox.curselection()
        if not sel: return
        name = list(self.regions.keys())[sel[0]]
        self.selected = name
        self._redraw()
        self._update_coord_label()
        # 捲動到框位置
        if self.screenshot:
            rel = self.regions[name]
            cx = (rel[0]+rel[2])/2 * self.img_w * self.scale
            cy = (rel[1]+rel[3])/2 * self.img_h * self.scale
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            tw = int(self.img_w * self.scale)
            th = int(self.img_h * self.scale)
            self.canvas.xview_moveto(max(0, (cx - cw//2) / tw))
            self.canvas.yview_moveto(max(0, (cy - ch//2) / th))

    def _update_coord_label(self):
        if not self.selected or self.selected not in self.regions:
            self.coord_var.set("-"); return
        v = self.regions[self.selected]
        px_x1 = int(v[0] * self.img_w); px_y1 = int(v[1] * self.img_h)
        px_x2 = int(v[2] * self.img_w); px_y2 = int(v[3] * self.img_h)
        self.coord_var.set(
            f"比例:\n  x1={v[0]:.4f} y1={v[1]:.4f}\n  x2={v[2]:.4f} y2={v[3]:.4f}\n"
            f"像素 ({self.img_w}×{self.img_h}):\n  ({px_x1},{px_y1})-({px_x2},{px_y2})\n"
            f"大小: {px_x2-px_x1}×{px_y2-px_y1}"
        )
        # 同步 listbox 選取
        names = list(self.regions.keys())
        if self.selected in names:
            idx = names.index(self.selected)
            self.listbox.selection_clear(0, END)
            self.listbox.selection_set(idx)
            self.listbox.see(idx)

    # ── 操作按鈕 ──────────────────────────────────────────────
    def _reset_selected(self):
        if self.selected and self.selected in self.orig_regions:
            self.regions[self.selected] = tuple(self.orig_regions[self.selected])
            self._redraw(); self._update_coord_label()
            self.status_var.set(f"已還原: {self.selected}")

    def _reset_all(self):
        if messagebox.askyesno("全部還原", "確定要還原所有區域到原始值？"):
            self.regions = {k: tuple(v) for k, v in self.orig_regions.items()}
            self._redraw(); self._update_coord_label()
            self.status_var.set("已還原全部區域")

    def _save(self):
        if not messagebox.askyesno("儲存", "確定要將新座標寫入 screen_reader.py？"):
            return
        ok = write_regions_to_file(self.regions)
        if ok:
            self.orig_regions = {k: tuple(v) for k, v in self.regions.items()}
            self.status_var.set("✅ 已儲存到 screen_reader.py")
            messagebox.showinfo("儲存成功",
                "座標已更新！\n下次執行 screen_reader.py 會使用新座標。")
        else:
            messagebox.showerror("儲存失敗", "無法寫入 screen_reader.py")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    root = Tk()
    root.geometry(f"{MAX_DISPLAY_W+200}x{MAX_DISPLAY_H+80}")
    app = CalibrateApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
