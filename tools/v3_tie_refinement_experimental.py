"""REVERTED - DISABLED. DO NOT USE.

This broad tie refinement was reverted on 2026-09-06 after audit. Importing the module
still works so the code and its rationale remain readable, but instantiating the scorer
raises unless `allow_reverted=True` is passed explicitly. The current Experimental V3 is
`tools/ingredient_first_v3_experimental.py`, unmodified and revalidated.

WHY IT WAS REVERTED
-------------------
It was built to resolve the crowd of products tied at exactly 90. The audit showed it did
close to the opposite:

  * 26,498 of 28,792 changed pairs (92%) were originally BELOW 90; only 2,294 were at 90
  * sub-90 change rate 81.2% versus 41.0% at 90 - the target population was touched least
  * mean adjustment -1.12 below 90 versus -0.22 at 90, with 12,141 sub-90 pairs at the
    maximum -2.2 penalty and ZERO at 90
  * 9,296 pairs moved downward with no resolution gained at all - 94 tie groups were
    purely translated, every member shifting identically and staying perfectly tied
  * the 5,593-strong 90 group ended with only 4 distinct values inside a 0.9-point band

Root cause: the adjustment was a shortfall from perfect evidence, so weak-evidence
products took the largest penalty. Weak evidence concentrates at LOW scores, so the term
acted as a general mid-range deflator rather than a ceiling resolver. That is backwards
for its stated purpose.

The 90-saturation problem is still open. It was deliberately NOT re-attempted here.

--- original module documentation follows ---

EXPERIMENTAL V3 tie refinement. NOT production.

Production, the frontend, V2, recommendation/filtering and onboarding are all untouched.
This adds one bounded, evidence-derived term on top of the V3 score.

WHY
---
V3 fixed dilution by taking the strongest positive evidence rather than a mean. The cost
was resolution: on a three-level master (0/40/90) a max collapses to the ceiling whenever
any positive exists, so 38,198 of 38,234 live pairs share a score with something else.

THE ARCHITECTURAL POINT
-----------------------
The refinement is a PURE FUNCTION OF ONE PRODUCT'S OWN EVIDENCE. It never looks at other
products, never looks at rank, and never looks at whether a tie exists. Two products with
identical evidence receive an identical adjustment and therefore stay tied; two with
different evidence separate. Ties break as a CONSEQUENCE of evidence differing, which is
the required direction:

    evidence -> score -> ordering        (correct)
    ordering -> score                    (never)

A tie-membership gate was deliberately rejected: making A's score depend on whether B
exists is a population-dependent rule, which is the very pattern the brief forbids. It
would also affect only 36 of 38,234 pairs, since nearly everything is tied already.

WHAT THE REFINEMENT USES - and what it must never use
-----------------------------------------------------
Uses, all already exposed by V3 and all varying meaningfully inside tie groups:

  * strong_positive_count    depth of well-suited evidence   (varies in 65% of tie groups)
  * strongest_positive_tier  primary > secondary > incidental (31%)
  * constraint headroom      how far the non-binding limits sit above the binding one (84%)

NEVER uses `neutral_ingredient_count`, even though it varies in 98.6% of tie groups and
would be the easiest separator available. Using it would reintroduce exactly the
long-INCI dilution V3 was built to remove: a product with eight tolerated ingredients
would score below an identical product with two. Requirement 3, enforced by construction
and by an explicit test.

NEVER uses negative evidence either - it is already fully priced into the base V3 score
through the tier-scaled penalty, so counting it again would double-charge it.

NEVER uses doctor data.

WHY THE CEILING AND FLOOR HOLD
------------------------------
The adjustment is defined as a shortfall from perfect evidence, so it is always <= 0.
A product with the best possible evidence receives 0 and keeps its V3 score; anything
weaker moves slightly down. Nothing can rise, so 90 remains the ceiling by construction
rather than by clamping. The result is clamped to [0, 90] regardless, as a guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import ingredient_first_v3_experimental as v3mod

# The whole refinement is bounded by this. Conservative on purpose: it exists to add
# resolution inside a score band, not to re-rank across bands.
MAX_REFINEMENT = 3.0

REFINE_CONFIG: dict[str, Any] = {
    "max_refinement": MAX_REFINEMENT,
    # How much each evidence signal contributes to the quality term. Sum to 1.0.
    "w_depth": 0.45,
    "w_tier": 0.30,
    "w_headroom": 0.25,
    # Evidence depth saturates here: two distinct well-suited actives is "fully evidenced".
    # Beyond that, more actives do not keep raising the score - consistent with V3's rule
    # that multiplicity raises confidence rather than suitability.
    "depth_saturation": 2,
    # Credit for the tier carrying the strongest positive evidence.
    "tier_quality": {"primary": 1.0, "secondary": 0.6, "incidental": 0.3, "": 0.0, "n/a": 0.0},
    # Headroom above this many points counts as fully clear. 10 = one full score band,
    # chosen as the natural unit of the 0-90 scale rather than tuned to a distribution.
    "headroom_full": 10.0,
    # Decimal places kept. One place gives ~31 distinct sub-levels inside the bound,
    # which is what actually resolves the ties.
    "round_to": 1,
}


@dataclass
class RefinedPrediction:
    """V3 prediction plus the refinement, with both scores kept for comparison."""
    base: Any
    original_v3_score: float = 0.0
    refined_v3_score: float = 0.0
    score_adjustment: float = 0.0
    evidence_quality: float = 0.0
    q_depth: float = 0.0
    q_tier: float = 0.0
    q_headroom: float = 0.0
    tie_resolved: bool = False
    refinement_reason: str = ""


class V3TieRefinedScorer(v3mod.IngredientFirstV3):
    """V3 with the evidence-quality refinement applied. V3 itself is unmodified."""

    variant_name = "V3_refined"

    def __init__(self, refine_config: dict[str, Any] | None = None,
                 allow_reverted: bool = False, **kw):
        if not allow_reverted:
            raise RuntimeError(
                "v3_tie_refinement_experimental is REVERTED and disabled (2026-09-06). "
                "It altered 92% of pairs below 90 while barely resolving the 90 ceiling, "
                "and moved 9,296 pairs with no resolution gained. Use "
                "tools/ingredient_first_v3_experimental.IngredientFirstV3 instead. "
                "Pass allow_reverted=True only to reproduce the archived audit."
            )
        self.refine = {**REFINE_CONFIG, **(refine_config or {})}
        super().__init__(**kw)

    # ------------------------------------------------------------------
    # the refinement
    # ------------------------------------------------------------------

    def evidence_quality(self, pred: v3mod.V3Prediction) -> dict[str, Any]:
        """A [0,1] quality term from this product's own evidence. No other product is consulted."""
        cfg = self.refine

        # 1. DEPTH - how many distinct well-suited actives support the concern.
        #    Saturating, so a product with six actives is not endlessly better than one
        #    with two. This is the signal V3 deliberately kept out of the score itself.
        depth = min(1.0, (pred.strong_positive_count or 0) / float(cfg["depth_saturation"]))

        # 2. TIER - primary evidence outranks secondary, which outranks incidental.
        #    Partly expressed in the base score through attenuation, but that saturates
        #    at the ceiling, so it still discriminates among products scoring 90.
        tier = float(cfg["tier_quality"].get(pred.strongest_positive_tier or "", 0.0))

        # 3. HEADROOM - how far the NON-BINDING constraints sit above the binding one.
        #    A product whose product context and safety cap both sit well above its final
        #    score is more comfortably qualified than one scraping past every limit.
        #
        #    The binding constraint is EXCLUDED. By definition it equals the final score
        #    and contributes zero slack, so including it would structurally penalise the
        #    best products: a product at the 90 ceiling can never have slack on the
        #    constraint that put it there, which made evidence quality of 1.0 - and
        #    therefore the score 90 itself - unreachable. That inverted the intent, so
        #    only the constraints the product genuinely cleared are measured.
        final = float(pred.final_score)
        constraints = [c for c in (pred.ingredient_suitability_score, pred.product_context_score,
                                   pred.safety_cap) if c is not None]
        slack = [float(c) - final for c in constraints if float(c) - final > 1e-9]
        headroom = min(1.0, (sum(slack) / len(slack)) / float(cfg["headroom_full"])) if slack else 0.0

        quality = (cfg["w_depth"] * depth + cfg["w_tier"] * tier + cfg["w_headroom"] * headroom)
        return {"quality": quality, "depth": depth, "tier": tier, "headroom": headroom}

    def refine_score(self, pred: v3mod.V3Prediction) -> RefinedPrediction:
        out = RefinedPrediction(base=pred, original_v3_score=float(pred.final_score))

        # A blocked product is a safety decision, not a suitability score. Never refine it.
        if pred.final_score <= v3mod.HARD_BLOCK or pred.safety_status == "hard_block":
            out.refined_v3_score = float(pred.final_score)
            out.refinement_reason = "hard-blocked; refinement not applied"
            return out

        q = self.evidence_quality(pred)
        out.evidence_quality = round(q["quality"], 4)
        out.q_depth = round(q["depth"], 3)
        out.q_tier = round(q["tier"], 3)
        out.q_headroom = round(q["headroom"], 3)

        # Shortfall from perfect evidence. Always <= 0, so the 90 ceiling holds by
        # construction and no score can ever be inflated.
        adjustment = -(1.0 - q["quality"]) * float(self.refine["max_refinement"])
        refined = max(0.0, min(90.0, out.original_v3_score + adjustment))
        refined = round(refined, int(self.refine["round_to"]))

        out.refined_v3_score = refined
        out.score_adjustment = round(refined - out.original_v3_score, int(self.refine["round_to"]))

        bits = []
        if pred.strong_positive_count:
            bits.append(f"{pred.strong_positive_count} well-suited active(s)")
        if pred.strongest_positive_tier:
            bits.append(f"strongest evidence is {pred.strongest_positive_tier}")
        if q["headroom"] > 0:
            bits.append(f"headroom {q['headroom']:.2f} above binding constraint")
        if not bits:
            bits.append("no positive concern evidence")
        out.refinement_reason = (
            f"evidence quality {q['quality']:.3f} "
            f"(depth {q['depth']:.2f} x{self.refine['w_depth']}, tier {q['tier']:.2f} x{self.refine['w_tier']}, "
            f"headroom {q['headroom']:.2f} x{self.refine['w_headroom']}); " + "; ".join(bits)
        )
        return out

    def score_refined(self, product: dict[str, Any], profile: dict[str, Any]) -> RefinedPrediction:
        return self.refine_score(self.score(product, profile))

    def score_all_refined(self, profile: dict[str, Any]) -> list[RefinedPrediction]:
        return [self.refine_score(p) for p in self.score_all(profile)]


def mark_resolved_ties(refined: list[RefinedPrediction]) -> None:
    """Annotate which pairs were in an exact tie and whether the refinement separated it.

    Reporting only. This runs AFTER every score is final and never feeds back into any
    score - it exists so the report can say how many ties were resolved versus retained.
    """
    groups: dict[float, list[RefinedPrediction]] = {}
    for r in refined:
        if r.original_v3_score <= v3mod.HARD_BLOCK:
            continue
        groups.setdefault(r.original_v3_score, []).append(r)
    for members in groups.values():
        if len(members) < 2:
            continue
        distinct = {m.refined_v3_score for m in members}
        resolved = len(distinct) > 1
        for m in members:
            m.tie_resolved = resolved
