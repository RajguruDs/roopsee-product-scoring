"""Eligibility gating and onboarding selection."""

from __future__ import annotations

import pytest


SCORE_COLUMNS = [
    "<16",
    "17-25",
    "+>25",
    "Acne",
    "Body Acne",
    "Dryness",
    "Open Pores",
    "Uneven Skin Tone",
    "Dark Spots/Pigmentation",
    "Melasma",
    "Barrier Repair",
    "Comedones",
    "Wrinkles/Fine lines",
    "Redness/Irritation",
    "Dehydration",
    "Dullness",
    "Tanning",
    "Oily Score",
    "Oily+Sensitive Score",
    "Dry Score",
    "Dry+Sensitive Score",
    "Normal Score",
    "Normal+Sensitive Score",
    "Combination Score",
    "Combination+Sensitive Score",
    "Excessive Dryness score",
    "Pregnancy Score",
    "Breastfeeling Score",
    "None",
]

WEIGHTS = {"baseline": 0.05, "v2": 0.30, "anchor": 0.55, "type_family": 0.05, "type": 0.05}


def make_product(uid="GTIN_4005800238963", value=80, **overrides):
    layers = {key: [value] * len(SCORE_COLUMNS) for key in ["baseline", "v2", "anchor", "typeFamily", "type"]}
    product = {
        "uid": uid,
        "gtin": uid.replace("GTIN_", ""),
        "name": "Test Serum",
        "brand": "Test Brand",
        "normalizedType": "serum",
        "category": "Face",
        "confidence": "High",
        "primaryIngredients": "Niacinamide",
        "secondaryIngredients": "Glycerin",
        "families": ["acne"],
        "needsReviewIngredientCount": 0,
        "createdFallbackIngredientCount": 0,
        "reviewFlags": [],
        "support": {"anchor": 12, "exact": 20, "family": 30, "typeFamily": 30, "type": 40},
        "scoreLayers": layers,
    }
    product.update(overrides)
    return product


def evaluate(onboarding, product, **kwargs):
    return onboarding.evaluate_eligibility(product, SCORE_COLUMNS, WEIGHTS, **kwargs)


class TestEligibility:
    def test_a_good_product_is_eligible(self, onboarding):
        status, reason = evaluate(onboarding, make_product())
        assert status == onboarding.STATUS_ELIGIBLE and reason == ""

    def test_missing_name_is_excluded(self, onboarding):
        status, _ = evaluate(onboarding, make_product(name=""))
        assert status == onboarding.STATUS_MISSING_DATA

    def test_missing_brand_is_excluded(self, onboarding):
        status, _ = evaluate(onboarding, make_product(brand=""))
        assert status == onboarding.STATUS_MISSING_DATA

    def test_no_ingredients_is_excluded(self, onboarding):
        status, _ = evaluate(onboarding, make_product(primaryIngredients="", secondaryIngredients=""))
        assert status == onboarding.STATUS_MISSING_DATA

    def test_unsupported_product_type_is_excluded(self, onboarding):
        status, _ = evaluate(onboarding, make_product(normalizedType="supplement"))
        assert status == onboarding.STATUS_PRODUCT_TYPE

    def test_low_confidence_is_excluded(self, onboarding):
        status, _ = evaluate(onboarding, make_product(confidence="Low"))
        assert status == onboarding.STATUS_LOW_CONFIDENCE

    def test_unscoreable_ingredients_need_review(self, onboarding):
        status, _ = evaluate(onboarding, make_product(needsReviewIngredientCount=2))
        assert status == onboarding.STATUS_REVIEW

    def test_no_active_family_needs_review(self, onboarding):
        status, _ = evaluate(onboarding, make_product(families=[]))
        assert status == onboarding.STATUS_REVIEW

    def test_non_topical_is_excluded(self, onboarding):
        status, _ = evaluate(onboarding, make_product(), is_non_topical=lambda _product: True)
        assert status == onboarding.STATUS_NON_TOPICAL

    def test_invalid_gtin_is_excluded(self, onboarding):
        status, _ = evaluate(
            onboarding, make_product(), validate_gtin=lambda _value: (False, "check digit mismatch")
        )
        assert status == onboarding.STATUS_INVALID_GTIN

    def test_duplicate_gtin_is_excluded(self, onboarding):
        seen: set[str] = set()
        first, _ = evaluate(onboarding, make_product(), seen_gtins=seen)
        second, reason = evaluate(onboarding, make_product(), seen_gtins=seen)
        assert first == onboarding.STATUS_ELIGIBLE
        assert second == onboarding.STATUS_INVALID_GTIN and reason == "duplicate gtin"


class TestSafetyIsNarrow:
    def test_product_blocked_for_every_skin_type_is_excluded(self, onboarding):
        status, _ = evaluate(onboarding, make_product(value=-100))
        assert status == onboarding.STATUS_SAFETY

    def test_pregnancy_block_alone_does_not_exclude(self, onboarding):
        """Blocked for one profile is not blocked for everyone -- the browser
        already applies the per-profile block at render time."""
        product = make_product()
        index = SCORE_COLUMNS.index("Pregnancy Score")
        for layer in product["scoreLayers"].values():
            layer[index] = -100
        status, _ = evaluate(onboarding, product)
        assert status == onboarding.STATUS_ELIGIBLE

    def test_teen_block_alone_does_not_exclude(self, onboarding):
        product = make_product()
        index = SCORE_COLUMNS.index("<16")
        for layer in product["scoreLayers"].values():
            layer[index] = -100
        status, _ = evaluate(onboarding, product)
        assert status == onboarding.STATUS_ELIGIBLE


class TestEvidenceQuality:
    def test_quality_reflects_the_weighted_blend(self, onboarding):
        product = make_product(value=80)
        assert onboarding.evidence_quality(product, SCORE_COLUMNS, WEIGHTS) == pytest.approx(80.0)

    def test_hard_blocked_columns_do_not_drag_quality_down(self, onboarding):
        product = make_product(value=80)
        index = SCORE_COLUMNS.index("Pregnancy Score")
        for layer in product["scoreLayers"].values():
            layer[index] = -100
        assert onboarding.evidence_quality(product, SCORE_COLUMNS, WEIGHTS) == pytest.approx(80.0)

    def test_better_layers_rank_higher(self, onboarding):
        good = onboarding.evidence_quality(make_product(value=90), SCORE_COLUMNS, WEIGHTS)
        poor = onboarding.evidence_quality(make_product(value=50), SCORE_COLUMNS, WEIGHTS)
        assert good > poor


class TestOnboardingLimit:
    @pytest.fixture
    def eligible(self, onboarding):
        products = []
        for product_type in ["serum", "cleanser", "moisturizer", "sunscreen", "mask", "toner", "other"]:
            for index in range(300):
                product = make_product(
                    uid=f"GTIN_{product_type}_{index:04d}",
                    value=50 + (index % 40),
                    normalizedType=product_type,
                )
                product["_evidenceQuality"] = onboarding.evidence_quality(product, SCORE_COLUMNS, WEIGHTS)
                products.append(product)
        return products

    def test_limit_1000_yields_exactly_1000(self, onboarding, eligible):
        assert len(onboarding.select_onboarding(eligible, 1000)) == 1000

    def test_limit_2000_yields_exactly_2000(self, onboarding, eligible):
        assert len(onboarding.select_onboarding(eligible, 2000)) == 2000

    def test_limit_none_yields_every_eligible_product(self, onboarding, eligible):
        assert len(onboarding.select_onboarding(eligible, None)) == len(eligible)

    def test_limit_above_supply_yields_everything(self, onboarding, eligible):
        assert len(onboarding.select_onboarding(eligible, 99999)) == len(eligible)

    def test_no_duplicate_uids(self, onboarding, eligible):
        selected = onboarding.select_onboarding(eligible, 1000)
        uids = [product["uid"] for product in selected]
        assert len(uids) == len(set(uids))

    def test_every_product_type_is_represented(self, onboarding, eligible):
        selected = onboarding.select_onboarding(eligible, 1000)
        types = {product["normalizedType"] for product in selected}
        assert types == {"serum", "cleanser", "moisturizer", "sunscreen", "mask", "toner", "other"}

    def test_selection_prefers_stronger_evidence(self, onboarding, eligible):
        selected = onboarding.select_onboarding(eligible, 700)
        chosen = {product["uid"] for product in selected}
        rest = [p for p in eligible if p["uid"] not in chosen]
        assert min(p["_evidenceQuality"] for p in selected) >= max(p["_evidenceQuality"] for p in rest) - 1e-9

    def test_selection_is_reproducible(self, onboarding, eligible):
        first = [p["uid"] for p in onboarding.select_onboarding(eligible, 1000)]
        second = [p["uid"] for p in onboarding.select_onboarding(eligible, 1000)]
        assert first == second


class TestBlockingIsEvaluatedPerLayer:
    """The browser blocks when ANY one layer is -100, so averaging first is wrong."""

    def test_a_single_blocking_layer_counts(self, onboarding):
        product = make_product(value=80)
        index = SCORE_COLUMNS.index("Excessive Dryness score")
        product["scoreLayers"]["v2"][index] = -100
        assert onboarding.column_is_hard_blocked(product, index)
        assert not onboarding.is_broadly_safe(product, SCORE_COLUMNS, WEIGHTS)

    def test_the_blend_would_have_hidden_it(self, onboarding):
        """Four healthy layers average a lone -100 away; that must not happen."""
        product = make_product(value=80)
        index = SCORE_COLUMNS.index("Excessive Dryness score")
        product["scoreLayers"]["v2"][index] = -100
        blended = onboarding.blended_layer_values(product, SCORE_COLUMNS, WEIGHTS)[index]
        assert blended > -100
        assert not onboarding.is_broadly_safe(product, SCORE_COLUMNS, WEIGHTS)

    def test_a_clean_product_is_broadly_safe(self, onboarding):
        assert onboarding.is_broadly_safe(make_product(value=80), SCORE_COLUMNS, WEIGHTS)

    def test_a_skin_type_block_also_disqualifies(self, onboarding):
        product = make_product(value=80)
        product["scoreLayers"]["anchor"][SCORE_COLUMNS.index("Dry Score")] = -100
        assert not onboarding.is_broadly_safe(product, SCORE_COLUMNS, WEIGHTS)


class TestSafetyReserve:
    """Evidence-only ranking selects actives-led products, which are exactly the
    ones blocked for pregnancy, teens and a compromised barrier."""

    def build_group(self, onboarding, safe_count, blocked_count):
        products = []
        index = SCORE_COLUMNS.index("Excessive Dryness score")
        # Blocked products are given the HIGHER evidence, so an evidence-only
        # selection would take all of them and leave nothing safe.
        for i in range(blocked_count):
            product = make_product(uid=f"GTIN_blocked_{i:04d}", value=95, normalizedType="toner")
            product["scoreLayers"]["v2"][index] = -100
            products.append(product)
        for i in range(safe_count):
            products.append(make_product(uid=f"GTIN_safe_{i:04d}", value=60, normalizedType="toner"))
        for product in products:
            product["_evidenceQuality"] = onboarding.evidence_quality(product, SCORE_COLUMNS, WEIGHTS)
            product["_broadlySafe"] = onboarding.is_broadly_safe(product, SCORE_COLUMNS, WEIGHTS)
        return products

    def test_reserve_admits_safe_products_despite_lower_evidence(self, onboarding):
        products = self.build_group(onboarding, safe_count=50, blocked_count=50)
        selected = onboarding.select_onboarding(products, 20)
        safe = [p for p in selected if p["_broadlySafe"]]
        assert safe, "safety reserve admitted no broadly-safe product"
        assert len(safe) >= int(20 * onboarding.SAFETY_RESERVE_FRACTION) - 1

    def test_evidence_still_leads_the_selection(self, onboarding):
        products = self.build_group(onboarding, safe_count=50, blocked_count=50)
        selected = onboarding.select_onboarding(products, 20)
        strong = [p for p in selected if not p["_broadlySafe"]]
        assert len(strong) > len(selected) - len(strong) or len(strong) >= 10

    def test_reserve_never_changes_the_total(self, onboarding):
        products = self.build_group(onboarding, safe_count=50, blocked_count=50)
        assert len(onboarding.select_onboarding(products, 20)) == 20

    def test_reserve_falls_back_when_nothing_is_safe(self, onboarding):
        """A catalogue with no safe product in a type must still fill the quota."""
        products = self.build_group(onboarding, safe_count=0, blocked_count=40)
        assert len(onboarding.select_onboarding(products, 20)) == 20

    def test_reserve_is_reproducible(self, onboarding):
        products = self.build_group(onboarding, safe_count=50, blocked_count=50)
        first = [p["uid"] for p in onboarding.select_onboarding(products, 20)]
        second = [p["uid"] for p in onboarding.select_onboarding(products, 20)]
        assert first == second


class TestQuotaAllocation:
    def test_quotas_sum_to_the_limit(self, onboarding):
        quotas = onboarding.allocate_quotas({"serum": 500, "cleanser": 300, "toner": 50}, 400)
        assert sum(quotas.values()) == 400

    def test_quotas_are_proportional(self, onboarding):
        quotas = onboarding.allocate_quotas({"serum": 800, "cleanser": 200}, 500)
        assert quotas["serum"] > quotas["cleanser"]

    def test_a_thin_routine_type_is_not_squeezed_out(self, onboarding):
        quotas = onboarding.allocate_quotas({"serum": 10000, "toner": 3}, 100)
        assert quotas["toner"] >= 1

    def test_never_allocates_more_than_available(self, onboarding):
        quotas = onboarding.allocate_quotas({"serum": 5, "cleanser": 1000}, 500)
        assert quotas["serum"] <= 5

    def test_limit_above_supply_returns_everything(self, onboarding):
        available = {"serum": 5, "cleanser": 7}
        assert onboarding.allocate_quotas(available, 100) == available


class TestEnvironmentLimit:
    @pytest.mark.parametrize(
        "raw,expected",
        [("1000", 1000), ("2000", 2000), ("3000", 3000), ("none", None), ("all", None), ("0", None), ("", None)],
    )
    def test_limit_parsing(self, onboarding, monkeypatch, raw, expected):
        monkeypatch.setenv("ROOPSEE_TEST_LIMIT", raw)
        assert onboarding.env_int("ROOPSEE_TEST_LIMIT", 1000) == expected

    def test_default_is_used_when_unset(self, onboarding, monkeypatch):
        monkeypatch.delenv("ROOPSEE_TEST_LIMIT", raising=False)
        assert onboarding.env_int("ROOPSEE_TEST_LIMIT", 1000) == 1000

    def test_garbage_is_rejected_loudly(self, onboarding, monkeypatch):
        monkeypatch.setenv("ROOPSEE_TEST_LIMIT", "lots")
        with pytest.raises(SystemExit):
            onboarding.env_int("ROOPSEE_TEST_LIMIT", 1000)
