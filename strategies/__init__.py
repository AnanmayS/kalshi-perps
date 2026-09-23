from .base import Bar, Strategy
from .funding_carry import FundingCarry
from .mean_reversion import MeanReversion
from .momentum import BuyAndHold, Momentum

STRATEGIES = {Momentum.name: Momentum, BuyAndHold.name: BuyAndHold,
              MeanReversion.name: MeanReversion, FundingCarry.name: FundingCarry}

__all__ = ["Bar", "Strategy", "Momentum", "BuyAndHold", "MeanReversion", "FundingCarry", "STRATEGIES"]
