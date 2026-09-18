"""Strategy Correlation Guard - Improve signal diversity.

Prevents over-reliance on correlated strategies voting the same way.
Reduces false positives by ensuring independent confirmations.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class StrategyGroup:
    """Group of related/correlated strategies."""
    name: str
    strategies: List[str]
    description: str
    max_vote_weight: float = 0.35  # Max 35% of total weight from this group


# Define strategy groups based on correlation
# Strategies in same group often give same signal
STRATEGY_GROUPS = [
    StrategyGroup(
        name="mean_reversion",
        strategies=["mean_reversion", "bollinger_bounce", "vol_norm_reversion"],
        description="Mean reversion (RSI-band / Bollinger / vol-normalized)",
        max_vote_weight=0.30
    ),
    StrategyGroup(
        name="trend_following",
        strategies=["trend_pullback", "ema_ribbon", "supertrend", "psar", "adx_di"],
        description="Trend following (EMA / Supertrend / PSAR / ADX)",
        max_vote_weight=0.35
    ),
    StrategyGroup(
        name="momentum",
        strategies=["momentum_breakout", "macd_cross", "stoch_rsi"],
        description="Momentum / oscillator crossovers",
        max_vote_weight=0.30
    ),
    StrategyGroup(
        name="divergence",
        strategies=["rsi_divergence"],
        description="Price/RSI divergence",
        max_vote_weight=0.20
    ),
    StrategyGroup(
        name="composite_confluence",
        strategies=[
            "confluence_breakout",
            "multi_confluence",
            "session_swing",
        ],
        description="Composite strategies that already blend several indicators internally",
        max_vote_weight=0.35
    ),
]


class StrategyCorrelationGuard:
    """Manage strategy voting to ensure diversity."""
    
    def __init__(self):
        self.groups = {g.name: g for g in STRATEGY_GROUPS}
        self._build_group_index()
    
    def _build_group_index(self) -> None:
        """Build reverse index: strategy -> group."""
        self.strategy_to_group: Dict[str, str] = {}
        for group in self.groups.values():
            for strategy in group.strategies:
                self.strategy_to_group[strategy] = group.name
    
    def analyze_vote_diversity(self, votes: Dict[str, str]) -> Dict[str, float]:
        """Analyze diversity of strategy votes.
        
        Args:
            votes: Dict of strategy_name -> "CALL"|"PUT"|"neutral"
        
        Returns:
            Dict with diversity metrics
        """
        call_votes = [s for s, v in votes.items() if v == "CALL"]
        put_votes = [s for s, v in votes.items() if v == "PUT"]
        total_votes = len([v for v in votes.values() if v != "neutral"])
        
        # Group breakdown
        group_votes: Dict[str, int] = {}
        for strategy, vote in votes.items():
            if vote != "neutral":
                group_name = self.strategy_to_group.get(strategy, "unknown")
                group_votes[group_name] = group_votes.get(group_name, 0) + 1
        
        # Identify dominant group
        dominant_group = max(group_votes.items(), key=lambda x: x[1])[0] if group_votes else None
        dominant_count = group_votes.get(dominant_group, 0) if dominant_group else 0
        
        return {
            "call_count": len(call_votes),
            "put_count": len(put_votes),
            "total_votes": total_votes,
            "unique_groups": len(group_votes),
            "dominant_group": dominant_group,
            "dominant_group_votes": dominant_count,
            "group_breakdown": group_votes,
        }
    
    def apply_correlation_penalty(
        self,
        votes: Dict[str, str],
        weights: Dict[str, float],
        direction: str
    ) -> Tuple[Dict[str, float], List[str]]:
        """Apply penalty to over-represented groups.
        
        Args:
            votes: Strategy votes
            weights: Current strategy weights (before penalty)
            direction: "CALL" or "PUT" target direction
        
        Returns:
            Adjusted weights and penalty reasons
        """
        reasons = []
        adjusted_weights = weights.copy()
        
        # Get relevant votes and groups
        target_votes = {
            s: v for s, v in votes.items()
            if v == direction
        }
        
        if not target_votes:
            return adjusted_weights, reasons
        
        # Calculate weight per group
        group_weights: Dict[str, float] = {}
        for strategy, vote in target_votes.items():
            if vote == direction:
                weight = weights.get(strategy, 1.0)
                group_name = self.strategy_to_group.get(strategy, "unknown")
                group_weights[group_name] = group_weights.get(group_name, 0) + weight
        
        total_weight = sum(group_weights.values())
        if total_weight <= 0:
            return adjusted_weights, reasons
        
        # Apply penalties for over-represented groups
        for strategy, vote in target_votes.items():
            if vote != direction:
                continue
            
            group_name = self.strategy_to_group.get(strategy, "unknown")
            group = self.groups.get(group_name)
            if not group:
                continue
            
            group_weight = group_weights.get(group_name, 0)
            group_weight_pct = group_weight / total_weight
            
            # Apply penalty if group exceeds max
            if group_weight_pct > group.max_vote_weight:
                penalty = (group_weight_pct - group.max_vote_weight) * 0.5
                original_weight = adjusted_weights.get(strategy, 1.0)
                adjusted_weights[strategy] = max(0.1, original_weight * (1 - penalty))
                
                if abs(original_weight - adjusted_weights[strategy]) > 0.05:
                    reasons.append(
                        f"Correlation penalty: {strategy} ({group_name}) "
                        f"reduced {original_weight:.2f}→{adjusted_weights[strategy]:.2f}"
                    )
        
        return adjusted_weights, reasons
    
    def check_excessive_correlation(
        self,
        votes: Dict[str, str],
        threshold: float = 0.80
    ) -> Tuple[bool, str]:
        """Check if all strategies are voting the same way (low diversity).
        
        Args:
            votes: Strategy votes
            threshold: Correlation threshold (0.0-1.0)
        
        Returns:
            (is_excessive, reason)
        """
        diversity = self.analyze_vote_diversity(votes)
        
        if diversity["total_votes"] < 3:
            # Not enough votes for meaningful correlation check
            return False, ""
        
        # Check if one group dominates
        if diversity["unique_groups"] == 1:
            return True, f"All votes from single group: {diversity['dominant_group']}"
        
        # Check dominant group percentage
        dominant_pct = diversity["dominant_group_votes"] / diversity["total_votes"]
        if dominant_pct > threshold:
            return True, (
                f"Excessive correlation: {diversity['dominant_group']} "
                f"accounts for {dominant_pct*100:.0f}% of votes"
            )
        
        return False, ""
    
    def get_group_report(self, votes: Dict[str, str]) -> str:
        """Get readable report of group voting breakdown."""
        diversity = self.analyze_vote_diversity(votes)
        lines = [
            f"Strategy Groups: {diversity['unique_groups']}",
            f"Total Votes: {diversity['total_votes']} (CALL: {diversity['call_count']}, PUT: {diversity['put_count']})",
        ]
        
        for group_name, count in sorted(diversity["group_breakdown"].items()):
            pct = (count / diversity["total_votes"] * 100) if diversity["total_votes"] > 0 else 0
            lines.append(f"  {group_name}: {count} votes ({pct:.0f}%)")
        
        return "\n".join(lines)
