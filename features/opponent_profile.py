"""Opponent Profile Vector (OPV) - 突破性設計

把每個對手的歷史行為編碼成 16 維向量，動態更新。
讓模型學會 exploitative 對抗不同風格對手，同時保留 GTO baseline。

16 個指標：
    [0]  VPIP: Voluntarily Put money In Pot (越高 = 越鬆)
    [1]  PFR:  Pre-Flop Raise %
    [2]  AF:   Aggression Factor (bet+raise / call)
    [3]  3Bet%: 3-Bet preflop frequency
    [4]  FoldTo3Bet%: 對 3-Bet 棄牌率
    [5]  CBet%: Continuation bet frequency
    [6]  FoldToCBet%: 對 c-bet 棄牌率
    [7]  WSD:  Went to Showdown % (越高 = 越不願棄牌)
    [8]  WTSD: Won When Saw Showdown %
    [9]  AFq:  Aggression Frequency
    [10] SteelAttempt%: 嘗試偷盲率
    [11] FoldToSteal%: 對 steal 棄牌率
    [12] CheckRaise%: Check-raise 頻率 (trap 傾向)
    [13] RiverBet%:  河牌下注頻率
    [14] Bluff%:     估計詐唬率 (高 AF + 低 WSD)
    [15] Reliability: 樣本量信心度 (0-1)
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class OpponentProfileVector:
    # Raw counters
    hands_seen: int = 0
    vpip_count: int = 0
    pfr_count: int = 0
    raise_count: int = 0
    call_count: int = 0
    bet_count: int = 0
    threebet_opp: int = 0
    threebet_count: int = 0
    fold_to_3b_opp: int = 0
    fold_to_3b_count: int = 0
    cbet_opp: int = 0
    cbet_count: int = 0
    fold_to_cbet_opp: int = 0
    fold_to_cbet_count: int = 0
    showdown_count: int = 0
    won_showdown: int = 0
    steal_opp: int = 0
    steal_count: int = 0
    fold_to_steal_opp: int = 0
    fold_to_steal_count: int = 0
    check_raise_opp: int = 0
    check_raise_count: int = 0
    river_opp: int = 0
    river_bet_count: int = 0

    def _safe_div(self, a: int, b: int) -> float:
        return a / b if b > 0 else 0.5  # uninformative prior = 0.5

    def to_array(self) -> np.ndarray:
        vpip = self._safe_div(self.vpip_count, self.hands_seen)
        pfr = self._safe_div(self.pfr_count, self.hands_seen)
        af_denom = self.call_count
        af_num = self.raise_count + self.bet_count
        af = min(af_num / max(af_denom, 1), 5.0) / 5.0  # normalize 0-5 -> 0-1
        threebet = self._safe_div(self.threebet_count, self.threebet_opp)
        fold_3b = self._safe_div(self.fold_to_3b_count, self.fold_to_3b_opp)
        cbet = self._safe_div(self.cbet_count, self.cbet_opp)
        fold_cbet = self._safe_div(self.fold_to_cbet_count, self.fold_to_cbet_opp)
        wsd = self._safe_div(self.showdown_count, self.hands_seen)
        wtsd = self._safe_div(self.won_showdown, self.showdown_count)
        afq = min((self.raise_count + self.bet_count) / max(self.hands_seen, 1), 1.0)
        steal = self._safe_div(self.steal_count, self.steal_opp)
        fold_steal = self._safe_div(self.fold_to_steal_count, self.fold_to_steal_opp)
        cr = self._safe_div(self.check_raise_count, self.check_raise_opp)
        river_bet = self._safe_div(self.river_bet_count, self.river_opp)
        # Bluff estimate: high aggression + low WSD = likely bluffer
        bluff_est = min(af * (1 - wsd), 1.0)
        reliability = min(self.hands_seen / 100, 1.0)  # 100 hands = full confidence

        return np.array([
            vpip, pfr, af, threebet, fold_3b, cbet, fold_cbet,
            wsd, wtsd, afq, steal, fold_steal, cr, river_bet,
            bluff_est, reliability
        ], dtype=np.float32)

    @classmethod
    def default(cls) -> 'OpponentProfileVector':
        """Unknown opponent: use balanced GTO-like priors."""
        opv = cls()
        opv.hands_seen = 0  # reliability=0, all values = 0.5 (balanced)
        return opv

    @classmethod
    def loose_aggressive(cls) -> 'OpponentProfileVector':
        """LAG player profile."""
        opv = cls(hands_seen=200, vpip_count=120, pfr_count=90,
                  raise_count=80, call_count=40, bet_count=70,
                  cbet_opp=100, cbet_count=80, fold_to_cbet_opp=50,
                  fold_to_cbet_count=15, showdown_count=40, won_showdown=22)
        return opv

    @classmethod
    def tight_passive(cls) -> 'OpponentProfileVector':
        """Nit/rock profile."""
        opv = cls(hands_seen=200, vpip_count=30, pfr_count=15,
                  raise_count=10, call_count=60, bet_count=10,
                  cbet_opp=100, cbet_count=40, fold_to_cbet_opp=50,
                  fold_to_cbet_count=35, showdown_count=20, won_showdown=14)
        return opv
