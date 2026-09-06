"""EXPERIMENTAL aggregation variants. Baseline is FROZEN and not modified.

BASELINE_V1 is tools/ingredient_first_experimental.py, untouched. Each variant here
subclasses it and overrides exactly ONE method - `_ingredient_column_score` - so the
only difference between candidates is how a group of ingredient values is combined.
Everything else (tier weights, concern/skin blend, product context, confidence
ceilings, safety gate) is inherited unchanged, which makes this a controlled experiment.

EVIDENCE SEMANTICS come from the ingredient master's own Scale sheet, not from us:

    -100  "Disqualifying - should gate the product out, NOT AVERAGE IN"
       0  "Unsuitable / no benefit"                  -> negative evidence
      40  "Tolerated, no particular benefit"         -> NEUTRAL, carries no information
      50  "Neutral (age and pregnancy/breastfeeding columns)"
      90  "Well suited"                              -> positive evidence
     100  "Ideal / fully cleared"                    -> positive evidence

The dilution defect follows directly from averaging a scale whose most common value
(56.5% of cells) explicitly means "no information". A neutral should not move a score.
A negative should. The master says as much for -100 in its own words.

Shared rules across every non-baseline variant, so they differ only in how POSITIVES
are combined:

  * -100 anywhere  -> hard block (never averaged in)
  * neutrals       -> excluded from aggregation entirely (no information)
  * negatives      -> retained and allowed to pull the score down, in proportion to
                      how much of the evidence they represent
  * no evidence    -> the column's neutral value

NOTHING HERE IS ADOPTED. These are candidates for evaluation only.
"""
from __future__ import annotations

import math
from typing import Any

import ingredient_first_experimental as base

HARD_BLOCK = -100

# Columns where the master uses 50 rather than 40 as its neutral point.
NEUTRAL_50_COLUMNS = {"<16", "17-25", "+>25", "Pregnancy Score", "Breastfeeling Score"}


def neutral_for(column: str) -> float:
    return 50.0 if column in NEUTRAL_50_COLUMNS else 40.0


def classify(values: list[float], column: str) -> dict[str, list[float]]:
    """Split raw master values into the classes the Scale sheet defines."""
    n = neutral_for(column)
    return {"neutral": n,
            "positive": [v for v in values if v > n],
            "negative": [v for v in values if v < n and v > HARD_BLOCK],
            "neutral_values": [v for v in values if v == n],
            "blocked": [v for v in values if v <= HARD_BLOCK]}


def apply_negatives(positive_signal: float, parts: dict[str, Any], total: int) -> float:
    """Let negative evidence pull the score down; never let neutrals do so.

    Negatives are real information ("unsuitable"), so they are blended in proportion
    to how much of the ingredient evidence they represent. Neutrals are excluded from
    `total` precisely so a longer INCI of inert ingredients cannot dilute anything.
    """
    negatives = parts["negative"]
    if not negatives:
        return positive_signal
    informative = len(parts["positive"]) + len(negatives)
    if informative == 0:
        return positive_signal
    neg_share = len(negatives) / informative
    return positive_signal * (1 - neg_share) + (sum(negatives) / len(negatives)) * neg_share


class VariantMixin:
    """Shared plumbing. Subclasses implement `combine_positives` only."""

    variant_name = "unnamed"
    params: dict[str, Any] = {}

    def combine_positives(self, positives: list[float], column: str) -> float:
        raise NotImplementedError

    def _ingredient_column_score(self, names: list[str], column: str) -> float | None:
        values: list[float] = []
        for name in names:
            row = self.master.get(name)
            if row is None:
                canonical = self.alias_map.get(base.norm_key(name))
                row = self.master.get(canonical) if canonical else None
            if row is None:
                continue
            v = row["scores"].get(column)
            if v is not None:
                values.append(float(v))
        if not values:
            return None

        parts = classify(values, column)
        if parts["blocked"]:
            return HARD_BLOCK  # the master: "gate the product out, not average in"

        neutral = parts["neutral"]
        positives = parts["positive"]
        signal = self.combine_positives(positives, column) if positives else neutral
        return apply_negatives(signal, parts, len(values))


# ----------------------------------------------------------------------
# A. BASELINE - arithmetic mean over everything. Frozen reference.
# ----------------------------------------------------------------------

class BaselineMean(base.IngredientFirstScorer):
    variant_name = "A_baseline_mean"
    params: dict[str, Any] = {}
    # inherits _ingredient_column_score unchanged - this IS BASELINE_V1


# ----------------------------------------------------------------------
# B. MAX - strongest single piece of evidence
# ----------------------------------------------------------------------

class MaxEvidence(VariantMixin, base.IngredientFirstScorer):
    variant_name = "B_max"

    def combine_positives(self, positives, column):
        return max(positives)


# ----------------------------------------------------------------------
# C. TOP-K MEAN - average of the K strongest signals
# ----------------------------------------------------------------------

class TopKMean(VariantMixin, base.IngredientFirstScorer):
    def __init__(self, k: int = 3, **kw):
        self.k = k
        self.variant_name = f"C_topk{k}"
        self.params = {"k": k}
        super().__init__(**kw)

    def combine_positives(self, positives, column):
        top = sorted(positives, reverse=True)[: self.k]
        return sum(top) / len(top)


# ----------------------------------------------------------------------
# D. WEIGHTED TOP-K - rank-decayed, so the strongest dominates but
#    additional actives still add something
# ----------------------------------------------------------------------

class WeightedTopK(VariantMixin, base.IngredientFirstScorer):
    def __init__(self, k: int = 3, decay: float = 0.5, **kw):
        self.k = k
        self.decay = decay
        self.variant_name = f"D_weighted_topk{k}_decay{decay}"
        self.params = {"k": k, "decay": decay}
        super().__init__(**kw)

    def combine_positives(self, positives, column):
        top = sorted(positives, reverse=True)[: self.k]
        weights = [self.decay ** i for i in range(len(top))]
        return sum(v * w for v, w in zip(top, weights)) / sum(weights)


# ----------------------------------------------------------------------
# E. NOISY-OR - independent evidence reinforces without dilution
# ----------------------------------------------------------------------

class NoisyOr(VariantMixin, base.IngredientFirstScorer):
    def __init__(self, ceiling: float = 100.0, **kw):
        self.ceiling = ceiling
        self.variant_name = "E_noisy_or"
        self.params = {"ceiling": ceiling}
        super().__init__(**kw)

    def combine_positives(self, positives, column):
        """Each positive is independent evidence of suitability.

        Normalise each to a strength in [0,1] above neutral, combine with
        1 - prod(1 - p), then map back onto the score scale. A neutral has
        strength 0 and therefore contributes nothing, while two strong actives
        reinforce beyond either alone.
        """
        n = neutral_for(column)
        span = max(1e-9, self.ceiling - n)
        combined = 1.0
        for v in positives:
            p = min(1.0, max(0.0, (v - n) / span))
            combined *= (1.0 - p)
        return n + (1.0 - combined) * span


def build_variants(verbose: bool = False) -> list:
    """Every candidate under evaluation. Order is the reporting order."""
    return [
        BaselineMean(verbose=verbose),
        MaxEvidence(verbose=verbose),
        TopKMean(k=1, verbose=verbose),
        TopKMean(k=2, verbose=verbose),
        TopKMean(k=3, verbose=verbose),
        TopKMean(k=5, verbose=verbose),
        WeightedTopK(k=3, decay=0.5, verbose=verbose),
        WeightedTopK(k=5, decay=0.7, verbose=verbose),
        NoisyOr(verbose=verbose),
    ]
