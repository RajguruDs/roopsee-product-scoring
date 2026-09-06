"""EXPERIMENTAL ingredient-first scorer. NOT production.

Production scoring lives in static/app.js and is untouched by this module.
Nothing here is imported by the production pipeline, the frontend, or any
existing test. This module only reads data; it never writes to data/ or static/.

WHY THIS EXISTS
---------------
The production score is 65% doctor-derived (anchor 55% + typeFamily 5% + type 5%),
so a canonical product with no close doctor analogue is scored largely by averages
of other people's products. Phase 1 also traced the sunscreen/acne flooding to two
compounding faults in the production path:

  Fault 1  profileLayerScore() gives moisturizer and sunscreen `components = [skin]`,
           so the concern column is never read and a poor concern fit is free.
  Fault 2  supportsConcern() is a boolean over ingredient families; ONE qualifying
           ingredient anywhere lifts productRelevanceCap from 74 to 100.

This scorer answers a different question in three explicitly separated layers:

  1. INGREDIENT SUITABILITY  - how suitable are this product's ingredients for this
                               user? Ingredient evidence ONLY. No doctor anchor, no
                               doctor similarity, no product/family prior, no type prior.
  2. PRODUCT CONTEXT         - is this PRODUCT, as a whole, an appropriate answer to
                               the user's goal? Type purpose and ingredient role.
  3. SAFETY                  - a separate gate, never folded into a weighted average.

Phase 2 deliberately performs NO doctor calibration. See doctor_calibration_readiness.md.

THE TWO ARCHITECTURAL FIXES
---------------------------
Fault 1 is fixed by construction: every product type reads the concern column. There
are no exemptions. A sunscreen's weak acne evidence now costs it, exactly as a
cleanser's does.

Fault 2 is fixed by a rule stated once, here: **ingredient evidence moves the score
within a product's role band, but can never promote the role itself.** A niacinamide
sunscreen keeps its positive niacinamide evidence and stays a sunscreen - the presence
of an acne-family ingredient does not make sun protection an acne treatment.

The role bands are not invented. They are read off the existing business rule in
static/app.js:237-256 (productRelevanceCap), which already encodes "a sunscreen treats
photo concerns and merely supports others". The bug was that an ingredient family could
override it; here it cannot.

ASSUMPTIONS, STATED
-------------------
* Concentration is not available as structured data anywhere in the population. Only
  name-borne percentage claims exist. `hero_claim` records those; no concentration is
  inferred where none is written down.
* "Treatment vs supportive role" has no pre-existing rule in the repository. TYPE_ROLE
  below is derived mechanically from productRelevanceCap's own numbers rather than
  authored from clinical opinion, and every threshold is configurable.
* Ingredient tier weights (primary/secondary/incidental) are a new, configurable
  hierarchy. Defaults are stated and their sensitivity is reported, not asserted.
"""

from __future__ import annotations

import csv
import json
import os
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# Configuration - every number here is tunable and reported, none is hidden
# --------------------------------------------------------------------------

CONFIG: dict[str, Any] = {
    # How much each ingredient tier contributes. Primary = what the product claims,
    # secondary = declared supporting actives, incidental = resolved from INCI only.
    "tier_weight_primary": 0.60,
    "tier_weight_secondary": 0.30,
    "tier_weight_incidental": 0.10,
    # When a concern is selected the user has told us their goal, so concern evidence
    # leads and skin compatibility modifies. Production instead averages them, or for
    # sunscreen and moisturizer ignores the concern entirely (Fault 1).
    "weight_concern": 0.65,
    "weight_skin": 0.35,
    # Role bands, read off productRelevanceCap (app.js:237-256). Ceiling on the whole
    # product, by how well the product TYPE serves the concern.
    "ceiling_treatment": 100,
    "ceiling_supportive": 84,
    "ceiling_incidental": 74,
    # Penalty applied inside a band when the concern-relevant actives are not claimed.
    "role_step_secondary_only": 6,
    "role_step_incidental_only": 14,
    "role_step_none": 22,
    # Confidence ceilings, mirroring confidenceCap (app.js:258-262).
    "confidence_ceiling": {"High": 100, "Medium": 96, "Low": 88},
    # Safety thresholds, ported verbatim from safetyAdjustment (app.js:531-585).
    "safety_block_at": -100,
    "safety_cap_below_60": 70,
    "safety_cap_below_85": 84,
    "safety_cap_dryness_zero": 69,
    "missing_ingredient_score": 50,
}

HARD_BLOCK = -100

# Concern -> ingredient families that treat it. Reused verbatim from app.js:3-18.
CONCERN_FAMILIES = {
    "Acne": ["acne", "exfoliant", "clay"],
    "Body Acne": ["acne", "exfoliant", "clay"],
    "Dryness": ["hydration", "barrier", "soothing", "emollient"],
    "Open Pores": ["acne", "exfoliant", "clay"],
    "Uneven Skin Tone": ["brightening", "exfoliant", "sunscreen", "retinoid"],
    "Dark Spots/Pigmentation": ["brightening", "exfoliant", "sunscreen", "retinoid"],
    "Melasma": ["brightening", "sunscreen"],
    "Barrier Repair": ["barrier", "hydration", "soothing", "emollient"],
    "Comedones": ["acne", "exfoliant", "clay"],
    "Wrinkles/Fine lines": ["retinoid", "anti_aging", "exfoliant", "sunscreen"],
    "Redness/Irritation": ["soothing", "barrier", "hydration"],
    "Dehydration": ["hydration", "barrier", "soothing"],
    "Dullness": ["brightening", "hydration", "exfoliant", "sunscreen"],
    "Tanning": ["sunscreen", "brightening"],
}

PHOTO_CONCERNS = {"Tanning", "Dark Spots/Pigmentation", "Melasma", "Uneven Skin Tone"}
ACNE_CONCERNS = {"Acne", "Body Acne", "Open Pores", "Comedones"}
HYDRATION_CONCERNS = {"Dryness", "Dehydration", "Barrier Repair", "Redness/Irritation", "Dullness"}

# TYPE_ROLE - what a product TYPE is for, per concern class.
#
# Derived mechanically from productRelevanceCap (app.js:237-256): where the existing
# rule grants a type its full 100 for a concern it is treating it; where it grants a
# reduced ceiling it is supporting; where it grants the lowest it is incidental.
# This is the same editorial judgement the product already ships, expressed as a role
# instead of as a number an ingredient family can override.
TYPE_ROLE: dict[str, Any] = {
    "serum": lambda concern: "treatment",
    "cleanser": lambda concern: (
        "supportive" if concern == "Wrinkles/Fine lines"
        else "treatment" if concern in ACNE_CONCERNS
        else "supportive"
    ),
    "moisturizer": lambda concern: "treatment" if concern in HYDRATION_CONCERNS else "supportive",
    "sunscreen": lambda concern: (
        "treatment" if concern in PHOTO_CONCERNS
        else "supportive" if concern in {"Dullness", "Barrier Repair"}
        else "incidental"
    ),
    "mask": lambda concern: "treatment",
    "toner": lambda concern: "supportive",
    "other": lambda concern: "supportive",
}

SKIN_COLUMN = {
    ("Oily", False): "Oily Score", ("Oily", True): "Oily+Sensitive Score",
    ("Dry", False): "Dry Score", ("Dry", True): "Dry+Sensitive Score",
    ("Normal", False): "Normal Score", ("Normal", True): "Normal+Sensitive Score",
    ("Combination", False): "Combination Score", ("Combination", True): "Combination+Sensitive Score",
}
SPECIAL_COLUMN = {
    "Excessive Dryness": "Excessive Dryness score",
    "Pregnant": "Pregnancy Score",
    "Breastfeeding": "Breastfeeling Score",
}

PERCENT_CLAIM = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*([A-Za-z][A-Za-z0-9\s\-/']{2,40})")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


MATCH_RE = re.compile(r"^\s*(.*?)\s*->\s*(.*?)\s*\((.*?)\)\s*$")


def parse_matched(text: str) -> list[str]:
    """Canonical ingredient names out of the 'X -> Y (Method)' strings."""
    out: list[str] = []
    for part in clean(text).split(";"):
        m = MATCH_RE.match(part.strip())
        if m and clean(m.group(2)):
            out.append(clean(m.group(2)))
    return out


# --------------------------------------------------------------------------
# Result object - nothing is collapsed into a single number
# --------------------------------------------------------------------------


@dataclass
class Prediction:
    uid: str
    gtin: str
    name: str
    product_type: str
    confidence: str
    profile_id: str
    ingredient_suitability_score: float | None = None
    concern_fit: float | None = None
    skin_fit: float | None = None
    product_context_score: float | None = None
    type_role: str = ""
    active_role: str = ""
    pre_safety_score: float | None = None
    safety_status: str = "ok"
    safety_cap: float | None = None
    final_score: float = 0
    evidence_strength: str = ""
    primary_ingredients: str = ""
    secondary_ingredients: str = ""
    incidental_count: int = 0
    hero_claim: str = ""
    reason_codes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------


class IngredientFirstScorer:
    def __init__(self, config: dict[str, Any] | None = None, verbose: bool = True):
        self.cfg = {**CONFIG, **(config or {})}
        self.verbose = verbose
        self._family_cache: dict[str, set[str]] = {}
        self._load()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def _load(self) -> None:
        import importlib.util
        import sys

        def load(name: str, path: Path):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod

        auto = load("roopsee_auto_for_experiment", REPO_ROOT / "tools" / "build_automated_scores.py")
        self.auto = auto
        # classify_families and FAMILY_KEYWORDS live in validate_v2_automated_logic.py,
        # not in build_automated_scores. Reused verbatim, not reimplemented.
        self.v2 = load("roopsee_v2_for_experiment", REPO_ROOT / "tools" / "validate_v2_automated_logic.py")

        # The ingredient score master: 1,587 canonical ingredients x 28 columns.
        canonical_rows, alias_map, _labels, _orig = auto.read_ingredient_scores()
        self.master = canonical_rows
        self.alias_map = alias_map
        self._log(f"  ingredient master: {len(canonical_rows)} canonical ingredients")

        # Canonical products, already carrying matched primary/secondary ingredients.
        pop_path = Path(os.environ.get(
            "ROOPSEE_CANONICAL_OUTPUT_DIR", REPO_ROOT / "outputs" / "roopsee_canonical")) / "scored_population_full.json"
        data = json.loads(pop_path.read_text(encoding="utf-8"))
        self.products = data["products"]
        self.score_columns = data["scoreColumns"]
        self._log(f"  canonical population: {len(self.products)} products")

        # Incidental evidence: ingredients resolved from full INCI that were NOT
        # selected as primary or secondary.
        mapping_path = REPO_ROOT / "data" / "source" / "scoring_input_ingredient_mapping_v2.csv"
        self.inci_by_id: dict[str, list[str]] = {}
        if mapping_path.exists():
            with mapping_path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    items = [clean(x) for x in clean(row.get("inci_score_master_matches")).split(";") if clean(x)]
                    self.inci_by_id[clean(row.get("canonical_product_id_v2"))] = items
        self._log(f"  INCI evidence available for {len(self.inci_by_id)} products")

    # ---------------- ingredient layer ----------------

    def _ingredient_column_score(self, names: list[str], column: str) -> float | None:
        """Mean master score for a group of ingredients on one column.

        Hard blocks propagate: if any ingredient in the group is -100 for this
        column, the group is -100. Same semantics as average_with_hard_block()
        in build_automated_scores.py:521-527.
        """
        values: list[float] = []
        for name in names:
            row = self.master.get(name)
            if row is None:
                canonical = self.alias_map.get(norm_key(name))
                row = self.master.get(canonical) if canonical else None
            if row is None:
                continue
            v = row["scores"].get(column)
            if v is not None:
                values.append(float(v))
        if not values:
            return None
        if any(v <= HARD_BLOCK for v in values):
            return HARD_BLOCK
        return sum(values) / len(values)

    def _tiered_score(self, tiers: dict[str, list[str]], column: str) -> tuple[float | None, bool]:
        """Combine primary / secondary / incidental evidence on one column.

        Not a flat average: primary evidence is what the product claims and leads,
        incidental INCI evidence contributes least. Returns (score, hard_blocked).
        """
        parts: list[tuple[float, float]] = []
        blocked = False
        for tier, weight_key in (("primary", "tier_weight_primary"),
                                 ("secondary", "tier_weight_secondary"),
                                 ("incidental", "tier_weight_incidental")):
            names = tiers.get(tier) or []
            if not names:
                continue
            value = self._ingredient_column_score(names, column)
            if value is None:
                continue
            if value <= HARD_BLOCK:
                # A hard blocker anywhere blocks the product, whatever tier it sits in.
                blocked = True
                continue
            parts.append((value, float(self.cfg[weight_key])))
        if blocked:
            return HARD_BLOCK, True
        if not parts:
            return None, False
        total_w = sum(w for _v, w in parts)
        return (sum(v * w for v, w in parts) / total_w if total_w else None), False

    def _families(self, ingredient: str) -> set[str]:
        """Ingredient families, via the repository's own classify_families."""
        cached = self._family_cache.get(ingredient)
        if cached is None:
            cached = self.v2.classify_families(ingredient)
            self._family_cache[ingredient] = cached
        return cached

    # ---------------- product context layer ----------------

    def _active_role(self, tiers: dict[str, list[str]], concern: str | None) -> str:
        """Which tier carries the concern-relevant actives.

        This is the role of the RELEVANT ingredient, not of the product. It answers
        'does the product claim this benefit, or does it merely contain something
        related?' - the distinction production lacks entirely.
        """
        if not concern or concern == "None":
            return "n/a"
        wanted = set(CONCERN_FAMILIES.get(concern, []))
        if not wanted:
            return "n/a"
        for tier in ("primary", "secondary", "incidental"):
            for name in tiers.get(tier) or []:
                if wanted & self._families(name):
                    return tier
        return "none"

    def _product_context(self, product: dict[str, Any], concern: str | None,
                         tiers: dict[str, list[str]]) -> tuple[float, str, str, list[str]]:
        """A ceiling on the whole product, from type purpose and active role."""
        reasons: list[str] = []
        ptype = product.get("normalizedType") or "other"
        if not concern or concern == "None":
            return 100.0, "n/a", "n/a", ["no concern selected; product context not applied"]

        role_fn = TYPE_ROLE.get(ptype, TYPE_ROLE["other"])
        type_role = role_fn(concern)
        ceiling = float(self.cfg[f"ceiling_{type_role}"])
        reasons.append(f"type role: a {ptype} is {type_role} for {concern} (ceiling {ceiling:g})")

        active_role = self._active_role(tiers, concern)
        if active_role == "primary":
            step = 0
            reasons.append("relevant actives are claimed as primary")
        elif active_role == "secondary":
            step = self.cfg["role_step_secondary_only"]
            reasons.append("relevant actives appear only as declared supporting ingredients")
        elif active_role == "incidental":
            step = self.cfg["role_step_incidental_only"]
            reasons.append("relevant actives appear only incidentally in the INCI")
        else:
            step = self.cfg["role_step_none"]
            reasons.append("no ingredient family relevant to this concern")

        # THE FAULT-2 FIX, stated in one place: ingredient evidence moves the score
        # WITHIN the band. It never raises the ceiling. A niacinamide sunscreen keeps
        # its niacinamide evidence and stays a sunscreen.
        context = max(0.0, ceiling - step)
        if type_role == "incidental" and active_role == "primary":
            reasons.append(
                f"NOTE: relevant active is claimed, but a {ptype} is still not a treatment "
                f"vehicle for {concern}; role is not promoted")
        return context, type_role, active_role, reasons

    # ---------------- safety layer ----------------

    def _safety(self, product: dict[str, Any], profile: dict[str, Any],
                tiers: dict[str, list[str]]) -> tuple[str, float, list[str]]:
        """Separate gate. Thresholds ported verbatim from safetyAdjustment (app.js:531-585)."""
        reasons: list[str] = []
        cap = 100.0
        ptype = product.get("normalizedType") or "other"

        def column_value(col: str) -> float | None:
            v, blocked = self._tiered_score(tiers, col)
            return HARD_BLOCK if blocked else v

        if profile.get("age") == "Teen":
            v = column_value("<16")
            if v is not None:
                if v <= HARD_BLOCK:
                    return "hard_block", HARD_BLOCK, ["blocked: unsuitable under 16"]
                if v < 60:
                    cap = min(cap, self.cfg["safety_cap_below_60"])
                    reasons.append("teen caution: weak under-16 evidence")
                elif v < 85:
                    cap = min(cap, self.cfg["safety_cap_below_85"])
                    reasons.append("teen caution")

        for condition in profile.get("specialConditions", []):
            if condition == "None":
                continue
            col = SPECIAL_COLUMN.get(condition)
            if not col:
                continue
            # Serums skip Excessive Dryness, exactly as production does (app.js:555-557).
            if condition == "Excessive Dryness" and ptype == "serum":
                reasons.append("serum: excessive dryness not scored, per existing rule")
                continue
            v = column_value(col)
            if v is None:
                continue
            if v <= HARD_BLOCK:
                return "hard_block", HARD_BLOCK, [f"blocked: unsuitable for {condition}"]
            if condition == "Excessive Dryness" and v == 0:
                cap = min(cap, self.cfg["safety_cap_dryness_zero"])
                reasons.append("limited fit for excessive dryness")
            elif v < 60:
                cap = min(cap, self.cfg["safety_cap_below_60"])
                reasons.append(f"caution for {condition}")
            elif v < 85:
                cap = min(cap, self.cfg["safety_cap_below_85"])
                reasons.append(f"mild caution for {condition}")

        return ("capped" if cap < 100 else "ok"), cap, reasons

    # ---------------- orchestration ----------------

    def tiers_for(self, product: dict[str, Any]) -> dict[str, list[str]]:
        primary = parse_matched(product.get("matchedPrimaryIngredients", ""))
        secondary = parse_matched(product.get("matchedSecondaryIngredients", ""))
        claimed = {norm_key(x) for x in primary + secondary}
        incidental = [x for x in self.inci_by_id.get(product["uid"], []) if norm_key(x) not in claimed]
        return {"primary": primary, "secondary": secondary, "incidental": incidental}

    def score(self, product: dict[str, Any], profile: dict[str, Any]) -> Prediction:
        concern = profile.get("concern") if profile.get("concern") != "None" else None
        skin_col = SKIN_COLUMN[(profile["skinType"], bool(profile["sensitive"]))]
        tiers = self.tiers_for(product)

        pred = Prediction(
            uid=product["uid"], gtin=product.get("gtin", ""), name=product.get("name", ""),
            product_type=product.get("normalizedType", ""), confidence=product.get("confidence", ""),
            profile_id=profile["profile_id"],
            primary_ingredients=product.get("primaryIngredients", ""),
            secondary_ingredients=product.get("secondaryIngredients", "")[:160],
            incidental_count=len(tiers["incidental"]),
        )
        claim = PERCENT_CLAIM.search(product.get("name", ""))
        pred.hero_claim = clean(claim.group(0)) if claim else ""

        # LAYER 1 - ingredient suitability. Ingredient evidence only.
        skin_v, skin_blocked = self._tiered_score(tiers, skin_col)
        pred.skin_fit = None if skin_v is None else round(skin_v, 1)
        concern_v, concern_blocked = (None, False)
        if concern:
            concern_v, concern_blocked = self._tiered_score(tiers, concern)
            pred.concern_fit = None if concern_v is None else round(concern_v, 1)

        if skin_blocked or concern_blocked:
            pred.safety_status = "hard_block"
            pred.final_score = HARD_BLOCK
            pred.reason_codes.append("blocked: an ingredient is a hard blocker for this profile")
            pred.ingredient_suitability_score = HARD_BLOCK
            return pred

        missing = self.cfg["missing_ingredient_score"]
        skin_score = missing if skin_v is None else skin_v
        if concern:
            concern_score = missing if concern_v is None else concern_v
            # Fault-1 fix: the concern column is read for EVERY product type, and
            # because the user named the concern, it leads.
            ing = (self.cfg["weight_concern"] * concern_score + self.cfg["weight_skin"] * skin_score)
            pred.reason_codes.append(
                f"ingredient evidence: {concern} {concern_score:.0f} x{self.cfg['weight_concern']:g}, "
                f"skin {skin_score:.0f} x{self.cfg['weight_skin']:g}")
        else:
            ing = skin_score
            pred.reason_codes.append(f"no concern selected; ingredient evidence is skin fit {skin_score:.0f}")
        pred.ingredient_suitability_score = round(ing, 1)

        # LAYER 2 - product context.
        context, type_role, active_role, ctx_reasons = self._product_context(product, concern, tiers)
        pred.product_context_score = round(context, 1)
        pred.type_role = type_role
        pred.active_role = active_role
        pred.reason_codes.extend(ctx_reasons)

        # Combine as a ceiling, the same idiom production uses (lowest cap wins).
        pre = min(ing, context)
        conf_ceiling = self.cfg["confidence_ceiling"].get(product.get("confidence", "Low"), 88)
        pre = min(pre, conf_ceiling)
        if conf_ceiling < 100:
            pred.reason_codes.append(f"{product.get('confidence')} confidence ceiling {conf_ceiling}")
        pred.pre_safety_score = round(pre, 1)

        # LAYER 3 - safety gate, applied last and never averaged away.
        status, cap, safety_reasons = self._safety(product, profile, tiers)
        pred.safety_status = status
        pred.safety_cap = cap
        pred.reason_codes.extend(safety_reasons)
        if status == "hard_block":
            pred.final_score = HARD_BLOCK
            return pred

        pred.final_score = int(round(min(pre, cap)))

        n_claimed = len(tiers["primary"]) + len(tiers["secondary"])
        pred.evidence_strength = ("strong" if n_claimed >= 4 else "moderate" if n_claimed >= 2
                                  else "weak" if n_claimed >= 1 else "none")
        return pred

    def score_all(self, profile: dict[str, Any]) -> list[Prediction]:
        return [self.score(p, profile) for p in self.products]
