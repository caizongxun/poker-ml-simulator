# 突破性研究想法

## 1. Opponent Profile Vector (OPV) — 對手風格向量

**概念**：把每個對手的歷史行為編碼成 16 維向量，作為 ML 模型的額外輸入。

**為什麼突破性**：
- 大多數 AI 撲克研究假設對手是未知或 GTO，OPV 讓模型**動態切換**從 GTO 到 exploitative 策略
- 16 維涵蓋 VPIP, PFR, AF, CBet, WSD 等所有核心統計數據
- 用 Dirichlet 先驗做 Bayesian 更新，少量樣本也能合理估計

**實作位置**：`features/opponent_profile.py`

---

## 2. KL-Constrained PPO（GTO 錨定 RL）

**概念**：在 PPO 的 loss 中加入 KL 懲罰項，懲罰模型偏離 GTO baseline 太多。

**為什麼突破性**：
- 純 exploitative 模型容易被反向利用（被對手發現你的策略後反打）
- 純 GTO 無法對抗魚（loose passive 玩家）
- KL 錨定讓模型在「安全的 GTO 偏差範圍」內做 exploitation
- 用 `kl_coef` 超參數控制 GTO vs exploitative 的 trade-off

**實作位置**：`models/rl_agent.py` → `PokerRLAgent.update()`

---

## 3. Bayesian Range Update（貝葉斯對手範圍動態更新）

**概念**：不用固定的對手手牌範圍，而是每次行動後動態更新對手可能手牌的機率分佈。

**公式**：
```
P(hand | actions) ∝ P(actions | hand) * P(hand)
```

- `P(hand)` 初始化為 position-aware preflop range
- `P(actions | hand)` 根據行動（bet, check, raise 的大小）更新
- 每個 action 都 squeeze 對手的 range

**研究方向**：結合 PokerCFR 解的 action frequency 表來初始化 likelihood

---

## 4. Mixed Strategy Output（混合策略輸出）

**概念**：模型輸出不是單一 action，而是 (fold%, call%, raise_small%, raise_large%, all_in%) 的機率分佈，實際決策時從分佈中 sample。

**為什麼突破性**：
- 傳統 ML 模型用 argmax → 確定性策略 → 可被讀取 → 被剝削
- 混合策略讓你的下注頻率不可預測，接近 GTO 的核心思想
- 溫度參數（temperature）控制隨機性：訓練時高溫度探索，生產時低溫度利用

**實作位置**：`models/decision_model.py` → `DecisionModel.decide()`

---

## 5. Opponent Pool Self-Play（對手池自對弈）

**概念**：不只跟「當前版本」的自己對打，而是維護一個歷史快照池，隨機選擇過去版本做為對手。

**為什麼突破性**：
- 純自對弈容易陷入 cyclic best response（A 打敗 B，B 打敗 C，C 打敗 A，循環）
- Opponent pool 確保模型能對抗各種不同風格，提高策略魯棒性
- 類似 AlphaStar 和 OpenAI Five 的 league training 機制

**實作位置**：`models/rl_agent.py` → `PokerRLAgent.snapshot_to_pool()`

---

## 6. Street-Level Attention（街道注意力機制）

**未來想法**：用 Transformer 對每個 street 的行動序列做 attention，讓模型學到「這個對手在 flop 的 check 後 turn raise 代表什麼」的 temporal pattern。

這比 MLP 更能捕捉「時序行動」的含義，是下一步研究方向。

---

## 7. Position-Aware Reward Shaping（位置感知獎勵塑形）

**想法**：在 RL 訓練中，根據位置給予不同的基準 reward。
- 有位置（BTN, CO）的玩家 baseline reward 調高（因為長期期望值本來就較高）
- 無位置（SB, UTG）的玩家 baseline reward 調低
- 讓模型學到「相同手牌，有位置時更積極」的位置意識
