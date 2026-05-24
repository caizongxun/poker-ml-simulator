# GameSofa Texas Hold'em Screen Reader

## 使用流程

1. 安裝依賴（見 requirements.txt）
2. 開啟 GameSofa 到 Texas Hold'em 牌桌
3. 執行: `python screen_reader.py`
4. 切回瀏覽器，確認已輪到你行動
5. 按 **F9** 自動截圖 → 辨識手牌 + 公共牌
6. 查看 terminal 輸出的辨識結果
7. 將結果輸入到 `texas-holdem-advisor.html` 執行模擬

## 辨識原理

| 功能 | 方法 |
|-----------|-------------------------------|
| 截圖 | pyautogui.screenshot() |
| 裁切區域 | 固定座標（基準 1214x915） |
| 花色辨識 | HSV 顏色分析（紅/黑） |
| 牌面辨識 | Tesseract OCR（左上角數字） |
| 空牌偵測 | 青綠色背景比例判斷 |

## 輸出

- Terminal 顯示辨識結果
- `debug_crops/_annotated.png` — 標記了辨識框的截圖
- `last_read.json` — 結構化輸出

## 座標調整

如截圖尺寸不是 1214x915，程式會自動按比例縮放。
若辨識位置偏移，請修改 `screen_reader.py` 中的 `REGIONS` 字典。

## 快捷鍵

| 鍵 | 功能 |
|-----|-------------|
| F9 | 截圖並辨識 |
| F10 | 離開程式 |
