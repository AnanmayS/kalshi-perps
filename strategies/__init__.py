from .base import Bar, Strategy
from .momentum import BuyAndHold, Momentum

STRATEGIES = {Momentum.name: Momentum, BuyAndHold.name: BuyAndHold}

__all__ = ["Bar", "Strategy", "Momentum", "BuyAndHold", "STRATEGIES"]
