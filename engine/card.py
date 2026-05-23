"""Card and Deck primitives."""
from __future__ import annotations
import random
from dataclasses import dataclass
from typing import List, Optional

RANKS = '23456789TJQKA'
SUITS = 'cdhs'

RANK_MAP = {r: i for i, r in enumerate(RANKS)}  # '2'->0, 'A'->12
SUIT_MAP = {s: i for i, s in enumerate(SUITS)}


@dataclass(frozen=True, order=True)
class Card:
    rank: int   # 0-12
    suit: int   # 0-3

    @classmethod
    def from_str(cls, s: str) -> 'Card':
        """Parse '2c', 'Ah', 'Ts', etc."""
        s = s.strip()
        rank_char = s[0].upper()
        suit_char = s[1].lower()
        if rank_char not in RANK_MAP:
            raise ValueError(f"Unknown rank: {rank_char}")
        if suit_char not in SUIT_MAP:
            raise ValueError(f"Unknown suit: {suit_char}")
        return cls(RANK_MAP[rank_char], SUIT_MAP[suit_char])

    def __str__(self) -> str:
        return RANKS[self.rank] + SUITS[self.suit]

    def __repr__(self) -> str:
        return f"Card('{self}')" 

    @property
    def rank_char(self) -> str:
        return RANKS[self.rank]

    @property
    def suit_char(self) -> str:
        return SUITS[self.suit]


class Deck:
    def __init__(self, exclude: Optional[List[Card]] = None):
        excluded = set(exclude or [])
        self.cards: List[Card] = [
            Card(r, s)
            for r in range(13)
            for s in range(4)
            if Card(r, s) not in excluded
        ]
        random.shuffle(self.cards)

    def deal(self, n: int = 1) -> List[Card]:
        if n > len(self.cards):
            raise ValueError("Not enough cards left in deck")
        dealt, self.cards = self.cards[:n], self.cards[n:]
        return dealt

    def __len__(self) -> int:
        return len(self.cards)
