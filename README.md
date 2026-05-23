# 🃏 Poker ML Simulator

> 德州撲克 ML 決策引擎：蒙地卡羅模擬 + GTO 強化學習 + 對手風格向量建模

## 核心架構

```
poker-ml-simulator/
├── engine/          # 撲克核心引擎（牌局、評估、歷史）
├── simulator/       # 蒙地卡羅 equity 模擬器
├── models/          # ML 模型（監督 + RL + 對手建模）
├── features/        # 特徵工程（手牌強度、位置、歷史）
├── training/        # 訓練腳本
├── eval/            # 評估 & 回測
└── notebooks/       # 分析筆記本
```

## 亮點特性

### 1. 蒙地卡羅 Equity 引擎
- 多玩家數量（2-9人）快速模擬
- 支援範圍對範圍（Range vs Range）計算
- Outs 自動偵測 + 隱含賠率估算

### 2. GTO-inspired 策略（突破性想法）
- **對手風格向量（OPV, Opponent Profile Vector）**：把每個對手編碼成 16 維向量（VPIP, PFR, AF, 3Bet%, CBet%, FoldToCBet%, WSD... 等），作為模型輸入 → 讓模型學「exploitative vs GTO switching」
- **Bayesian 範圍更新**：每次行動後動態更新對手的手牌範圍分佈（用 Dirichlet 先驗），不再用固定範圍
- **混合策略輸出**：模型輸出不是單一 action，而是 (fold%, call%, raise_size 分佈) → 混合策略防止被剝削

### 3. Counterfactual Regret Minimization (CFR) 融合 RL
- 離線用簡化 CFR 算法生成近似 GTO 策略作為 pre-training 資料
- 再用 PPO/DQN 在自我對弈中 fine-tune，對抗非 GTO 對手

## 快速開始

```bash
git clone https://github.com/caizongxun/poker-ml-simulator
cd poker-ml-simulator
pip install -r requirements.txt

# 執行蒙地卡羅模擬測試
python -m simulator.mc_equity --hand "AhKh" --board "Qh Jh 2c" --players 3 --simulations 50000

# 訓練基礎決策模型
python training/train_supervised.py --epochs 50

# 執行 RL 自我對弈
python training/train_rl.py --episodes 100000
```

## 安裝

```bash
pip install -r requirements.txt
```

## 研究想法：突破性設計

見 `docs/breakthrough_ideas.md`

## License

MIT
