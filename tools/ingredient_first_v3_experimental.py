"""EXPERIMENTAL V3 — evidence-aware ingredient aggregation. NOT production.

Production (`static/app.js`, the 5/30/55/5/5 weights, the doctor-anchor layer) and
EXPERIMENTAL V2 (`tools/ingredient_first_experimental.py`) are both left intact so the
two can be compared. V3 subclasses V2 and replaces exactly one thing: how a set of
ingredient values becomes one column score.

WHY V3 EXISTS
-------------
V2 combined ingredient evidence with arithmetic means in two places, and both diluted:

  Mean 1 (within tier)  mean over ingredient values
  Mean 2 (across tiers) Σ(tier × w)/Σw over primary/secondary/incidental

Because 82% of concern cells in the master are exactly 40 — "Tolerated, no particular
benefit" — averaging drags any real active toward 40. Measured: one salicylic-acid
primary alone scored 90; the same primary plus eight neutral ingredients scored 77.
The product did not get worse, its ingredient list got longer.

V3 removes BOTH means. There is no averaging of ingredient evidence anywhere.

THE SCALE, AS THE MASTER DEFINES IT (verified against the raw workbook)
----------------------------------------------------------------------
    -100  "Disqualifying — gate the product out, NOT AVERAGE IN"
       0  "Unsuitable / no benefit"                 -> negative evidence
      40  "Tolerated, no particular benefit"        -> NEUTRAL, no information
      50  neutral for age and pregnancy/breastfeeding columns
      90  "Well suited"          <- the MAXIMUM for concern and skin columns
     100  "Ideal / fully cleared" <- only ever used in age and special columns

All 44,436 cells are hand-filled; none is blank. `0` and `40` are used distinctly —
salicylic acid is 0 for Dryness (actively unsuitable) but hyaluronic acid is 40 for Acne
(inert). So 40 genuinely means "no information about this concern", and 0 genuinely
means "counts against".

THE V3 ALGORITHM, IN ONE PARAGRAPH
----------------------------------
For one column, look at each tier separately. Within a tier, take the STRONGEST positive
value — not the mean — because a product's suitability for a concern is established by
the best relevant ingredient it contains, not by the average of everything in the tube.
Attenuate that by the tier's credit (primary full, secondary less, incidental least), so
the hierarchy is preserved. Take the best tier-attenuated value across all tiers. Then
subtract a penalty for genuine negative evidence, scaled by the same tier credit.
Neutrals are never counted in either direction. The result is clamped to the column's
own maximum, so a concern score can never exceed 90 — the master's own top value.

CONSEQUENCES, BY DESIGN
-----------------------
* one 90 primary                        -> 90        strong evidence establishes suitability
* 90 primary + any number of 40s        -> 90        neutrals carry no information
* 90 primary + 90 secondary             -> 90        capped at the master's own ceiling
* three 90s                             -> 90        multiplicity raises CONFIDENCE, not score
* 90 primary + a 0 ingredient           -> penalised negative evidence still counts
* only 40s                              -> 40        neutral in, neutral out
* nothing relevant                      -> 40        a long INCI earns nothing by itself

Multiplicity of strong actives is reported through `strong_positive_count` and
`evidence_strength`, deliberately NOT through the score, because the master records only
whether an ingredient is well suited, never how strongly, and never scores a concern
above 90.

NO DOCTOR DATA is used anywhere in V3. Doctor products remain reserved for later blind
validation and calibration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import ingredient_first_experimental as v2
import ingredient_master_validation as masterval

HARD_BLOCK = -100

# Columns where the master's neutral is 50 rather than 40. Verified: age and special
# columns contain no 40s at all.
NEUTRAL_50_COLUMNS = {"<16", "17-25", "+>25", "Pregnancy Score", "Breastfeeling Score"}

V3_CONFIG: dict[str, Any] = {
    # How much of a tier's positive evidence counts. Primary is what the product claims
    # and leads; incidental was merely found in the INCI. This is the Primary > Secondary
    # > Incidental hierarchy, expressed without averaging.
    "tier_credit": {"primary": 1.00, "secondary": 0.75, "incidental": 0.45},
    # How hard genuine negative evidence bites, per tier credit. 1.0 means a negative in
    # the primary tier costs the full distance from neutral down to that value.
    "negative_weight": 1.00,
    # Concern evidence leads when the user has named a concern; skin compatibility
    # modifies. Inherited from V2 unchanged so the comparison isolates aggregation.
    "weight_concern": 0.65,
    "weight_skin": 0.35,
    # A product is "well evidenced" for a concern at this many distinct strong actives.
    # Affects reported confidence only - never the score.
    "strong_evidence_count": 2,
}


def neutral_for(column: str) -> float:
    return 50.0 if column in NEUTRAL_50_COLUMNS else 40.0


@dataclass
class V3Prediction:
    uid: str
    gtin: str
    name: str
    product_type: str
    confidence: str
    profile_id: str
    # ingredient layer
    concern_evidence: float | None = None
    skin_fit: float | None = None
    ingredient_suitability_score: float | None = None
    primary_concern_evidence: float | None = None
    secondary_concern_evidence: float | None = None
    incidental_concern_evidence: float | None = None
    strongest_positive_ingredient: str = ""
    strongest_positive_value: float | None = None
    strongest_positive_tier: str = ""
    strongest_negative_ingredient: str = ""
    strongest_negative_value: float | None = None
    strongest_negative_tier: str = ""
    negative_penalty: float = 0.0
    neutral_ingredient_count: int = 0
    strong_positive_count: int = 0
    # product context layer
    product_context_score: float | None = None
    type_role: str = ""
    active_role: str = ""
    # safety layer
    pre_safety_score: float | None = None
    safety_status: str = "ok"
    safety_cap: float | None = None
    # output
    final_score: float = 0
    evidence_strength: str = ""
    hero_claim: str = ""
    reason_codes: list[str] = field(default_factory=list)


class IngredientFirstV3(v2.IngredientFirstScorer):
    """V3 scorer. Inherits V2's profile resolution, product context and safety gate;
    replaces the ingredient aggregation entirely."""

    variant_name = "V3_evidence_aware"

    def __init__(self, config: dict[str, Any] | None = None,
                 strict_master: bool = True, **kw):
        self.v3 = {**V3_CONFIG, **(config or {})}
        self._column_max: dict[str, float] = {}
        super().__init__(**kw)
        self._compute_column_maxima()
        self._compute_ingredient_breadth()

        # Data hygiene only. No score is read, written or reinterpreted here; this simply
        # refuses to run on a master that violates its own declared value contract rather
        # than scoring silently against bad data.
        self.master_validation = masterval.validate_loaded_master(self.master)
        self.placeholders_removed: dict[str, list[str]] = {}
        if strict_master and not self.master_validation["ok"]:
            first = self.master_validation["violations"][:5]
            raise masterval.MasterValidationError(
                f"ingredient master failed validation: "
                f"{len(self.master_validation['violations'])} cell(s) outside the contract "
                f"{self.master_validation['contract']}; first: {first}"
            )

    def tiers_for(self, product: dict[str, Any]) -> dict[str, list[str]]:  # type: ignore[override]
        """V2 tiering, with non-ingredient placeholder text removed.

        Labels such as "not used" are filler, not ingredients. They never resolved against
        the master, so they never moved a score - but they did inflate the tier lengths
        that `evidence_strength` is derived from, making a product appear to claim an
        active it does not have. Tier membership is otherwise unchanged.
        """
        tiers, removed = masterval.strip_placeholders(super().tiers_for(product))
        if removed:
            self.placeholders_removed[product["uid"]] = removed
        return tiers

    def _compute_column_maxima(self) -> None:
        """The ceiling for each column is the master's OWN maximum for it.

        Derived from the data, not assumed: concern and skin columns top out at 90
        ("well suited"), age and special columns at 100 ("ideal / fully cleared"). A V3
        score can therefore never exceed a value the dermatologist actually uses for
        that column - which is what ruled out noisy-OR, whose reinforcement emitted 98
        and 100 for concerns where the scale stops at 90.
        """
        for row in self.master.values():
            for col, val in row["scores"].items():
                if val is None or val <= HARD_BLOCK:
                    continue
                if val > self._column_max.get(col, float("-inf")):
                    self._column_max[col] = float(val)

    def column_ceiling(self, column: str) -> float:
        return self._column_max.get(column, 90.0)

    # ------------------------------------------------------------------
    # which active gets NAMED as the lead - reporting only, never the score
    # ------------------------------------------------------------------

    def _compute_ingredient_breadth(self) -> None:
        """How many columns of each family an ingredient is 'well suited' for.

        Derived from the master itself, exactly like `_compute_column_maxima`. An
        ingredient at the ceiling for 11 of the 14 concern columns says little about any
        one of them; an ingredient at the ceiling for 2 says a great deal. This is used
        ONLY to choose which of several equally-scored actives is named as the lead.
        """
        families = {
            "concern": list(getattr(self.auto, "CONCERN_COLUMNS", [])),
            "skin": list(getattr(self.auto, "SKIN_COLUMNS", [])),
        }
        self._column_family = {c: fam for fam, cols in families.items() for c in cols}
        self._breadth: dict[str, dict[str, int]] = {}
        for name, row in self.master.items():
            counts = {}
            for fam, cols in families.items():
                counts[fam] = sum(
                    1 for c in cols
                    if row["scores"].get(c) is not None
                    and float(row["scores"][c]) >= self.column_ceiling(c)
                )
            self._breadth[name] = counts

    def lead_rank(self, name: str, column: str) -> tuple[int, str]:
        """Sort key for choosing the lead among actives that scored IDENTICALLY.

        Fewer columns at the ceiling first (more specific to this concern), then the name,
        so the choice is fully deterministic and never depends on ingredient-list order.
        """
        fam = getattr(self, "_column_family", {}).get(column)
        # A tier entry may be an ALIAS rather than a canonical key, so resolve it first.
        # Without this an aliased name misses the index and would look maximally specific.
        row = self._resolve(name)
        canonical = row.get("canonical_ingredient", name) if row else name
        breadth = getattr(self, "_breadth", {}).get(canonical)
        if breadth is None or not fam:
            # Genuinely unknown: rank LAST so a resolved active always wins the tie.
            return (10**6, name)
        return (breadth.get(fam, 10**6), name)

    # ------------------------------------------------------------------
    # the aggregation - this is the whole of V3
    # ------------------------------------------------------------------

    def _resolve(self, name: str) -> dict[str, Any] | None:
        row = self.master.get(name)
        if row is None:
            canonical = self.alias_map.get(v2.norm_key(name))
            row = self.master.get(canonical) if canonical else None
        return row

    def column_evidence(self, tiers: dict[str, list[str]], column: str) -> dict[str, Any]:
        """Evidence-aware aggregation for one column. No averaging anywhere.

        Returns a detail dict so every score is explainable.
        """
        neutral = neutral_for(column)
        ceiling = self.column_ceiling(column)
        credit = self.v3["tier_credit"]

        detail: dict[str, Any] = {
            "blocked": False, "neutral": neutral, "ceiling": ceiling,
            "per_tier": {}, "neutral_count": 0, "strong_positive_count": 0,
            "best_positive": None, "best_positive_ingredient": "", "best_positive_tier": "",
            "worst_negative": None, "worst_negative_ingredient": "", "worst_negative_tier": "",
            "positive_signal": neutral, "negative_penalty": 0.0, "value": neutral,
        }

        best_attenuated = None
        worst_penalty = 0.0

        for tier in ("primary", "secondary", "incidental"):
            names = tiers.get(tier) or []
            if not names:
                continue
            tier_credit = float(credit.get(tier, 0.0))
            tier_best = None
            tier_best_name = ""
            tier_worst = None
            tier_worst_name = ""

            for name in names:
                row = self._resolve(name)
                if row is None:
                    continue
                val = row["scores"].get(column)
                if val is None:
                    continue
                val = float(val)

                # -100 gates the product out; the master says so in its own words.
                if val <= HARD_BLOCK:
                    detail["blocked"] = True
                    return detail

                if val == neutral:
                    # "Tolerated, no particular benefit" - no information, so it is
                    # counted for reporting but never allowed to move the score.
                    detail["neutral_count"] += 1
                elif val > neutral:
                    # Strictly greater wins on evidence. On an exact TIE the most specific
                    # active is named instead of whichever happened to be listed first -
                    # `tier_best` (the value that drives the score) is identical either way.
                    if tier_best is None or val > tier_best or (
                        val == tier_best
                        and self.lead_rank(name, column) < self.lead_rank(tier_best_name, column)
                    ):
                        tier_best, tier_best_name = val, name
                    if val >= ceiling:
                        detail["strong_positive_count"] += 1
                else:
                    # Genuine negative evidence: "unsuitable / no benefit".
                    if tier_worst is None or val < tier_worst:
                        tier_worst, tier_worst_name = val, name

            tier_detail: dict[str, Any] = {"credit": tier_credit, "count": len(names)}

            if tier_best is not None:
                # Strongest positive in this tier, attenuated by the tier's credit.
                # Attenuation is applied to the DISTANCE ABOVE NEUTRAL, so a secondary
                # 90 lands partway between neutral and 90 rather than being scaled
                # toward zero.
                attenuated = neutral + (tier_best - neutral) * tier_credit
                tier_detail["best_positive"] = tier_best
                tier_detail["best_ingredient"] = tier_best_name
                tier_detail["attenuated"] = round(attenuated, 2)
                if best_attenuated is None or attenuated > best_attenuated:
                    best_attenuated = attenuated
                    detail["best_positive"] = tier_best
                    detail["best_positive_ingredient"] = tier_best_name
                    detail["best_positive_tier"] = tier

            if tier_worst is not None:
                # Negative evidence penalises, scaled by the same tier credit, so an
                # unsuitable primary costs more than an unsuitable incidental.
                penalty = (neutral - tier_worst) * tier_credit * float(self.v3["negative_weight"])
                tier_detail["worst_negative"] = tier_worst
                tier_detail["worst_ingredient"] = tier_worst_name
                tier_detail["penalty"] = round(penalty, 2)
                if penalty > worst_penalty:
                    worst_penalty = penalty
                    detail["worst_negative"] = tier_worst
                    detail["worst_negative_ingredient"] = tier_worst_name
                    detail["worst_negative_tier"] = tier

            detail["per_tier"][tier] = tier_detail

        positive_signal = best_attenuated if best_attenuated is not None else neutral
        # Never exceed the master's own maximum for this column.
        positive_signal = min(positive_signal, ceiling)
        value = max(0.0, positive_signal - worst_penalty)

        detail["positive_signal"] = round(positive_signal, 2)
        detail["negative_penalty"] = round(worst_penalty, 2)
        detail["value"] = round(value, 2)
        return detail

    def _tiered_score(self, tiers: dict[str, list[str]], column: str) -> tuple[float | None, bool]:
        """Override of V2's cross-tier weighted mean.

        V2 averaged the tiers, so a neutral secondary tier pulled a strong primary down
        by roughly 17 points. V3 takes the best tier-attenuated evidence instead, which
        removes that second dilution mechanism as well as the first.
        """
        d = self.column_evidence(tiers, column)
        if d["blocked"]:
            return HARD_BLOCK, True
        any_resolved = any(t.get("count") for t in d["per_tier"].values())
        return (d["value"] if any_resolved else None), False

    # ------------------------------------------------------------------
    # scoring, with full explainability
    # ------------------------------------------------------------------

    def score(self, product: dict[str, Any], profile: dict[str, Any]) -> V3Prediction:  # type: ignore[override]
        concern = profile.get("concern") if profile.get("concern") != "None" else None
        skin_col = v2.SKIN_COLUMN[(profile["skinType"], bool(profile["sensitive"]))]
        tiers = self.tiers_for(product)

        pred = V3Prediction(
            uid=product["uid"], gtin=product.get("gtin", ""), name=product.get("name", ""),
            product_type=product.get("normalizedType", ""), confidence=product.get("confidence", ""),
            profile_id=profile["profile_id"])
        claim = v2.PERCENT_CLAIM.search(product.get("name", ""))
        pred.hero_claim = v2.clean(claim.group(0)) if claim else ""

        # LAYER 1 - ingredient suitability, ingredient evidence only
        skin_d = self.column_evidence(tiers, skin_col)
        concern_d = self.column_evidence(tiers, concern) if concern else None

        if skin_d["blocked"] or (concern_d and concern_d["blocked"]):
            pred.safety_status = "hard_block"
            pred.final_score = HARD_BLOCK
            pred.ingredient_suitability_score = HARD_BLOCK
            pred.reason_codes.append("blocked: an ingredient is disqualifying for this profile")
            return pred

        pred.skin_fit = skin_d["value"]
        if concern_d:
            pred.concern_evidence = concern_d["value"]
            pred.primary_concern_evidence = concern_d["per_tier"].get("primary", {}).get("attenuated")
            pred.secondary_concern_evidence = concern_d["per_tier"].get("secondary", {}).get("attenuated")
            pred.incidental_concern_evidence = concern_d["per_tier"].get("incidental", {}).get("attenuated")
            pred.strongest_positive_ingredient = concern_d["best_positive_ingredient"]
            pred.strongest_positive_value = concern_d["best_positive"]
            pred.strongest_positive_tier = concern_d["best_positive_tier"]
            pred.strongest_negative_ingredient = concern_d["worst_negative_ingredient"]
            pred.strongest_negative_value = concern_d["worst_negative"]
            pred.strongest_negative_tier = concern_d["worst_negative_tier"]
            pred.negative_penalty = concern_d["negative_penalty"]
            pred.neutral_ingredient_count = concern_d["neutral_count"]
            pred.strong_positive_count = concern_d["strong_positive_count"]

            ing = (self.v3["weight_concern"] * concern_d["value"]
                   + self.v3["weight_skin"] * skin_d["value"])
            if concern_d["best_positive_ingredient"]:
                pred.reason_codes.append(
                    f"strongest {concern} evidence: {concern_d['best_positive_ingredient']} "
                    f"= {concern_d['best_positive']:g} ({concern_d['best_positive_tier']})")
            else:
                pred.reason_codes.append(f"no ingredient with positive {concern} evidence")
            if concern_d["neutral_count"]:
                pred.reason_codes.append(
                    f"{concern_d['neutral_count']} tolerated ingredient(s) ignored - no information")
            if concern_d["negative_penalty"]:
                pred.reason_codes.append(
                    f"negative evidence: {concern_d['worst_negative_ingredient']} "
                    f"= {concern_d['worst_negative']:g}, penalty {concern_d['negative_penalty']:g}")
        else:
            ing = skin_d["value"]
            pred.neutral_ingredient_count = skin_d["neutral_count"]
            pred.reason_codes.append("no concern selected; ingredient evidence is skin fit")

        pred.ingredient_suitability_score = round(ing, 1)

        # LAYER 2 - product context, inherited from V2 unchanged
        context, type_role, active_role, ctx_reasons = self._product_context(product, concern, tiers)
        pred.product_context_score = round(context, 1)
        pred.type_role = type_role
        pred.active_role = active_role
        pred.reason_codes.extend(ctx_reasons)

        pre = min(ing, context)
        conf_ceiling = self.cfg["confidence_ceiling"].get(product.get("confidence", "Low"), 88)
        pre = min(pre, conf_ceiling)
        if conf_ceiling < 100:
            pred.reason_codes.append(f"{product.get('confidence')} confidence ceiling {conf_ceiling}")
        pred.pre_safety_score = round(pre, 1)

        # LAYER 3 - safety gate, inherited from V2 unchanged
        status, cap, safety_reasons = self._safety(product, profile, tiers)
        pred.safety_status = status
        pred.safety_cap = cap
        pred.reason_codes.extend(safety_reasons)
        if status == "hard_block":
            pred.final_score = HARD_BLOCK
            return pred

        pred.final_score = int(round(max(0.0, min(pre, cap))))

        # Multiplicity of strong actives raises reported CONFIDENCE, never the score.
        n_claimed = len(tiers["primary"]) + len(tiers["secondary"])
        if pred.strong_positive_count >= self.v3["strong_evidence_count"]:
            pred.evidence_strength = "strong (multiple well-suited actives)"
        elif pred.strong_positive_count == 1:
            pred.evidence_strength = "strong (one well-suited active)"
        elif n_claimed >= 2:
            pred.evidence_strength = "moderate"
        elif n_claimed == 1:
            pred.evidence_strength = "weak"
        else:
            pred.evidence_strength = "none"
        return pred

    def score_all(self, profile: dict[str, Any]) -> list[V3Prediction]:  # type: ignore[override]
        return [self.score(p, profile) for p in self.products]
