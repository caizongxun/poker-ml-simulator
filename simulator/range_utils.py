"""Hand range utilities.

PREFLOP_RANGES: simplified GTO-inspired opening ranges by position.
Range strings use standard poker notation:
  'AA' = pocket aces
  'AKs' = suited AK
  'AKo' = offsuit AK
  'ATs+' = AT suited and better (ATs, AJs, AQs, AKs)
  'QQ+' = QQ, KK, AA
"""
from __future__ import annotations
from typing import List
from engine.card import Card, RANKS, SUITS
import random

# Simplified preflop opening ranges (% of hands)
PREFLOP_RANGES = {
    'UTG':  ['AA','KK','QQ','JJ','TT','99','88','AKs','AQs','AJs','ATs','AKo','AQo'],
    'MP':   ['AA','KK','QQ','JJ','TT','99','88','77','AKs','AQs','AJs','ATs','A9s',
             'KQs','KJs','AKo','AQo','KQo'],
    'CO':   ['AA','KK','QQ','JJ','TT','99','88','77','66','AKs','AQs','AJs','ATs',
             'A9s','A8s','KQs','KJs','KTs','QJs','AKo','AQo','AJo','KQo'],
    'BTN':  ['AA','KK','QQ','JJ','TT','99','88','77','66','55','44','AKs','AQs',
             'AJs','ATs','A9s','A8s','A7s','A6s','A5s','KQs','KJs','KTs','K9s',
             'QJs','QTs','JTs','T9s','98s','AKo','AQo','AJo','ATo','KQo','KJo'],
    'SB':   ['AA','KK','QQ','JJ','TT','99','88','77','AKs','AQs','AJs','ATs','A5s',
             'A4s','A3s','A2s','KQs','KJs','QJs','AKo','AQo'],
    'BB':   []  # BB defends vs raises (context-dependent)
}


class Range:
    def __init__(self, combos: List[List[Card]]):
        self.combos = combos

    @classmethod
    def from_position(cls, position: str) -> 'Range':
        hands = PREFLOP_RANGES.get(position.upper(), [])
        combos = []
        for hand in hands:
            combos.extend(Range._expand_hand(hand))
        return cls(combos)

    @staticmethod
    def _expand_hand(hand_str: str) -> List[List[Card]]:
        """Expand 'AKs', 'QQ', 'TT+', etc. to all Card combos."""
        results = []
        # Handle '+' notation like 'QQ+'
        if hand_str.endswith('+'):
            base = hand_str[:-1]
            r1 = RANKS.index(base[0].upper())
            if len(base) == 2 and base[0] == base[1]:  # pocket pair like 'QQ+'
                for rank in range(r1, 13):
                    results.extend(Range._pocket_pair(rank))
            else:
                results.extend(Range._expand_hand(base))
            return results

        if len(hand_str) == 2 and hand_str[0] == hand_str[1]:  # pocket pair
            return Range._pocket_pair(RANKS.index(hand_str[0].upper()))

        if len(hand_str) == 3:
            r1 = RANKS.index(hand_str[0].upper())
            r2 = RANKS.index(hand_str[1].upper())
            suited = hand_str[2].lower() == 's'
            return Range._two_card_combos(r1, r2, suited)

        return results

    @staticmethod
    def _pocket_pair(rank: int) -> List[List[Card]]:
        from itertools import combinations
        suits = list(range(4))
        return [[Card(rank, s1), Card(rank, s2)] for s1, s2 in combinations(suits, 2)]

    @staticmethod
    def _two_card_combos(r1: int, r2: int, suited: bool) -> List[List[Card]]:
        combos = []
        for s1 in range(4):
            for s2 in range(4):
                if suited and s1 != s2:
                    continue
                if not suited and s1 == s2:
                    continue
                combos.append([Card(r1, s1), Card(r2, s2)])
        return combos

    def sample(self, exclude: List[Card] = None) -> List[Card]:
        """Sample a random combo from the range (excluding known cards)."""
        exclude_set = set(exclude or [])
        valid = [c for c in self.combos if not any(card in exclude_set for card in c)]
        return random.choice(valid) if valid else []
