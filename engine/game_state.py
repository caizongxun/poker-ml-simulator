"""Game state representation for Texas Hold'em."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional
from .card import Card


class Street(IntEnum):
    PREFLOP = 0
    FLOP = 1
    TURN = 2
    RIVER = 3


class Action(IntEnum):
    FOLD = 0
    CHECK = 1
    CALL = 2
    RAISE = 3
    ALL_IN = 4


@dataclass
class PlayerState:
    player_id: int
    stack: float
    hole_cards: List[Card] = field(default_factory=list)
    bet: float = 0.0
    is_active: bool = True
    is_all_in: bool = False
    total_invested: float = 0.0


@dataclass
class GameState:
    """Snapshot of a Texas Hold'em hand."""
    num_players: int
    players: List[PlayerState]
    board: List[Card] = field(default_factory=list)
    street: Street = Street.PREFLOP
    pot: float = 0.0
    current_bet: float = 0.0
    dealer_pos: int = 0
    current_player: int = 0
    hand_history: List[dict] = field(default_factory=list)
    big_blind: float = 1.0
    small_blind: float = 0.5

    @property
    def active_players(self) -> List[PlayerState]:
        return [p for p in self.players if p.is_active]

    @property
    def pot_odds(self) -> float:
        """Call amount / (pot + call amount)."""
        call_amount = self.current_bet - self.players[self.current_player].bet
        if call_amount <= 0:
            return 0.0
        return call_amount / (self.pot + call_amount)

    @property
    def stack_depth(self) -> float:
        """Effective stack in big blinds."""
        hero = self.players[self.current_player]
        return hero.stack / self.big_blind

    def to_feature_dict(self) -> dict:
        """Export key features for ML input."""
        hero = self.players[self.current_player]
        return {
            'street': int(self.street),
            'pot_size': self.pot,
            'current_bet': self.current_bet,
            'hero_stack': hero.stack,
            'pot_odds': self.pot_odds,
            'stack_depth': self.stack_depth,
            'num_active': len(self.active_players),
            'position': (self.current_player - self.dealer_pos) % self.num_players,
            'total_invested': hero.total_invested,
            'bet_ratio': self.current_bet / self.pot if self.pot > 0 else 0,
        }
