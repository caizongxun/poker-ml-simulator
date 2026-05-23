"""Quick demo: simulate equity + extract features + make a random decision.

執行：
    python demo.py
"""
from engine.card import Card
from engine.game_state import GameState, PlayerState, Street
from engine.hand_evaluator import HandEvaluator
from simulator.mc_equity import MonteCarloEquity
from features.feature_extractor import FeatureExtractor
from features.opponent_profile import OpponentProfileVector
from models.decision_model import DecisionModel

print("=" * 60)
print("POKER ML SIMULATOR - DEMO")
print("=" * 60)

# Setup: hero has AhKh, board is Qh Jh 2c (3 players)
hero_hole = [Card.from_str('Ah'), Card.from_str('Kh')]
board = [Card.from_str('Qh'), Card.from_str('Jh'), Card.from_str('2c')]
num_players = 3

print(f"\nHero hand : {hero_hole}")
print(f"Board     : {board}")
print(f"Players   : {num_players}")

# Monte Carlo equity
print("\n[1] Monte Carlo Equity (10,000 sims)")
result = MonteCarloEquity.simulate(hero_hole, board, num_players=num_players, n=10000, verbose=True)
for k, v in result.items():
    print(f"   {k:20s}: {v}")

# Outs
print("\n[2] Outs Estimation")
outs = MonteCarloEquity.compute_outs(hero_hole, board)
for k, v in outs.items():
    print(f"   {k:24s}: {v}")

# Feature extraction
print("\n[3] Feature Vector (39 dims)")
players = [
    PlayerState(0, 100, hero_hole, bet=2, total_invested=5),
    PlayerState(1, 80, [], bet=0),
    PlayerState(2, 120, [], bet=0),
]
state = GameState(
    num_players=3, players=players, board=board,
    street=Street.FLOP, pot=15, current_bet=2,
    dealer_pos=2, current_player=0
)
opv_lag = OpponentProfileVector.loose_aggressive().to_array()
extractor = FeatureExtractor(mc_sims=500)
features = extractor.extract(state, opv_lag)
print(f"   Feature shape: {features.shape}")
print(f"   Equity (feat[5]): {features[5]:.4f}")
print(f"   Pot odds (feat[4]): {features[4]:.4f}")
print(f"   Stack depth (feat[6]): {features[6]:.4f}")

# Decision (untrained model for demo)
print("\n[4] ML Decision (untrained model)")
model = DecisionModel()
decision = model.decide(features, temperature=1.0)
print(f"   Action     : {decision['action_name']}")
print(f"   EV estimate: {decision['ev']}")
print("   Strategy distribution:")
for action, prob in decision['probs'].items():
    bar = '#' * int(prob * 40)
    print(f"     {action:15s}: {prob:.4f} [{bar}]")

print("\n" + "=" * 60)
print("Run training/train_supervised.py to train the model!")
print("=" * 60)
