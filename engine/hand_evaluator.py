"""Fast 7-card hand evaluator using lookup tables.

Returns a numeric rank: higher = stronger.
Categories (0-8):
    8 = Straight Flush
    7 = Four of a Kind
    6 = Full House
    5 = Flush
    4 = Straight
    3 = Three of a Kind
    2 = Two Pair
    1 = One Pair
    0 = High Card
"""
from __future__ import annotations
from itertools import combinations
from typing import List, Tuple
from .card import Card


def _rank_5(cards: List[Card]) -> Tuple[int, List[int]]:
    """Evaluate a 5-card hand. Returns (category, tiebreakers)."""
    ranks = sorted([c.rank for c in cards], reverse=True)
    suits = [c.suit for c in cards]
    is_flush = len(set(suits)) == 1
    rank_diffs = [ranks[i] - ranks[i+1] for i in range(4)]
    is_straight = (rank_diffs == [1,1,1,1]) or (ranks == [12,3,2,1,0])  # wheel

    from collections import Counter
    cnt = Counter(ranks)
    freq = sorted(cnt.values(), reverse=True)
    groups = sorted(cnt.keys(), key=lambda r: (cnt[r], r), reverse=True)

    if is_straight and is_flush:
        return (8, ranks if ranks != [12,3,2,1,0] else [3,2,1,0,-1])
    if freq[0] == 4:
        return (7, groups)
    if freq[:2] == [3, 2]:
        return (6, groups)
    if is_flush:
        return (5, ranks)
    if is_straight:
        top = ranks[0] if ranks != [12,3,2,1,0] else 3
        return (4, [top])
    if freq[0] == 3:
        return (3, groups)
    if freq[:2] == [2, 2]:
        return (2, groups)
    if freq[0] == 2:
        return (1, groups)
    return (0, ranks)


class HandEvaluator:
    @staticmethod
    def best_5_from_7(hole: List[Card], board: List[Card]) -> Tuple[int, List[int]]:
        """Find the best 5-card hand from hole + board (up to 7 cards)."""
        all_cards = hole + board
        assert 5 <= len(all_cards) <= 7, f"Need 5-7 cards, got {len(all_cards)}"
        best = None
        for combo in combinations(all_cards, 5):
            score = _rank_5(list(combo))
            if best is None or score > best:
                best = score
        return best  # type: ignore

    @staticmethod
    def hand_rank_int(hole: List[Card], board: List[Card]) -> int:
        """Returns a comparable integer (higher = stronger)."""
        cat, tb = HandEvaluator.best_5_from_7(hole, board)
        # Pack into single int for fast comparison
        score = cat * (14**5)
        for i, v in enumerate(tb[:5]):
            score += max(v, 0) * (14 ** (4 - i))
        return score

    @staticmethod
    def hand_category_name(category: int) -> str:
        names = ['High Card','One Pair','Two Pair','Three of a Kind',
                 'Straight','Flush','Full House','Four of a Kind','Straight Flush']
        return names[category]
