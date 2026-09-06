"""Evidence-based primary / secondary ingredient selection.

The rule these tests defend: being matched to the score master does NOT make an
ingredient the product's Primary. Primary is what the product itself claims.
"""

from __future__ import annotations

import pytest


ALIAS_MAP = {
    "niacinamide": "Niacinamide",
    "salicylicacid": "Salicylic Acid",
    "thiamidol": "Isobutylamido Thiazolyl Resorcinol (Thiamidol)",
    "isobutylamidothiazolylresorcinolthiamidol": "Isobutylamido Thiazolyl Resorcinol (Thiamidol)",
    "hyaluronicacid": "Hyaluronic Acid",
    "licochalconea": "Licochalcone A",
    "retinol": "Retinol",
    "glycerin": "Glycerin",
    "aloevera": "Aloe Vera",
    "water": "Water",
}


def select(canonical_population, **row):
    return canonical_population.select_primary_secondary(row, ALIAS_MAP)


class TestExplicitHeroClaim:
    def test_percentage_claim_in_name_becomes_primary(self, canonical_population):
        primary, _secondary, evidence, _flags = select(
            canonical_population,
            product_name="10% Niacinamide Face Serum",
            key_score_master_matches="Niacinamide; Zinc PCA",
            inci_score_master_matches="Glycerin; Niacinamide",
        )
        assert primary == ["Niacinamide"]
        assert evidence == "product_name_hero"

    def test_trailing_percentage_form_also_counts(self, canonical_population):
        primary, _s, evidence, _f = select(
            canonical_population,
            product_name="Face Serum Niacinamide 10%",
            key_score_master_matches="Niacinamide",
            inci_score_master_matches="Niacinamide; Glycerin",
        )
        assert primary == ["Niacinamide"]
        assert evidence == "product_name_hero"

    def test_named_hero_wins_over_another_mapped_ingredient(self, canonical_population):
        """The Eucerin/Thiamidol case: do not pick Licochalcone A just because it maps."""
        primary, secondary, evidence, _f = select(
            canonical_population,
            product_name="Eucerin Pigment Control Sunscreen Fluid SPF50+ with Thiamidol",
            key_score_master_matches="Licochalcone A; Thiamidol",
            inci_score_master_matches="Glycerin; Thiamidol",
        )
        assert primary == ["Thiamidol"]
        assert evidence == "product_name_hero"
        assert "Licochalcone A" in secondary

    def test_multiple_explicit_heroes_are_all_primary(self, canonical_population):
        primary, _s, evidence, _f = select(
            canonical_population,
            product_name="2% Salicylic Acid + 3% Niacinamide Face Serum",
            key_score_master_matches="Salicylic Acid; Niacinamide",
            inci_score_master_matches="Salicylic Acid; Niacinamide; Glycerin",
        )
        assert set(primary) == {"Salicylic Acid", "Niacinamide"}
        assert evidence == "product_name_hero"

    def test_uncorroborated_name_claim_is_not_promoted(self, canonical_population):
        """A name can claim anything; the ingredient data has to back it up."""
        primary, _s, evidence, _f = select(
            canonical_population,
            product_name="Retinol Youth Cream",
            key_score_master_matches="Glycerin",
            inci_score_master_matches="Glycerin",
        )
        assert primary == ["Glycerin"]
        assert evidence == "key_ingredients"


class TestEvidenceLadder:
    def test_falls_back_to_key_ingredients(self, canonical_population):
        primary, _s, evidence, _f = select(
            canonical_population,
            product_name="Gentle Daily Cream",
            key_score_master_matches="Hyaluronic Acid; Glycerin",
            inci_score_master_matches="Glycerin",
        )
        assert primary == ["Hyaluronic Acid", "Glycerin"]
        assert evidence == "key_ingredients"

    def test_falls_back_to_resolved_matches(self, canonical_population):
        primary, _s, evidence, _f = select(
            canonical_population,
            product_name="Gentle Daily Cream",
            resolved_score_master_ingredients="Aloe Vera",
            inci_score_master_matches="Glycerin",
        )
        assert primary == ["Aloe Vera"]
        assert evidence == "resolved_score_master"

    def test_inci_fallback_is_flagged_for_review(self, canonical_population):
        primary, _s, evidence, flags = select(
            canonical_population,
            product_name="Gentle Daily Cream",
            inci_score_master_matches="Glycerin; Hyaluronic Acid",
        )
        assert primary == ["Glycerin", "Hyaluronic Acid"]
        assert evidence == "full_inci_fallback"
        assert any("full INCI only" in flag for flag in flags)

    def test_no_resolvable_ingredient_is_flagged(self, canonical_population):
        primary, secondary, evidence, flags = select(
            canonical_population, product_name="Mystery Cream"
        )
        assert primary == [] and secondary == []
        assert evidence == "none"
        assert any("no score-master ingredient" in flag for flag in flags)

    def test_primary_is_capped(self, canonical_population):
        primary, _s, _e, _f = select(
            canonical_population,
            product_name="Everything Cream",
            key_score_master_matches="A; B; C; D; E; F; G",
        )
        assert len(primary) <= canonical_population.MAX_PRIMARY_INGREDIENTS


class TestGenericIngredientsAreNotPromoted:
    @pytest.mark.parametrize(
        "excipient",
        ["Water", "Aqua", "Purified Water", "Phenoxyethanol", "Disodium EDTA", "Xanthan Gum", "Sodium Hydroxide"],
    )
    def test_excipients_are_excluded(self, canonical_population, excipient):
        primary, secondary, _e, _f = select(
            canonical_population,
            product_name="Hydrating Gel",
            key_score_master_matches=f"{excipient}; Aloe Vera",
            inci_score_master_matches=excipient,
        )
        assert excipient not in primary
        assert excipient not in secondary
        assert "Aloe Vera" in primary

    def test_dropping_excipients_is_recorded(self, canonical_population):
        _p, _s, _e, flags = select(
            canonical_population,
            product_name="Hydrating Gel",
            key_score_master_matches="Purified Water; Aloe Vera",
        )
        assert any("generic excipients not scored" in flag for flag in flags)

    @pytest.mark.parametrize("kept", ["Glycerin", "Fragrance", "Parfum", "Cetearyl Alcohol", "Squalane"])
    def test_scoring_relevant_ingredients_are_kept(self, canonical_population, kept):
        """These look generic but carry real Roopsee scores, so they must survive."""
        assert not canonical_population.is_generic_excipient(kept)


class TestSecondarySelection:
    def test_secondary_excludes_the_primaries(self, canonical_population):
        primary, secondary, _e, _f = select(
            canonical_population,
            product_name="10% Niacinamide Serum",
            key_score_master_matches="Niacinamide",
            inci_score_master_matches="Niacinamide; Glycerin; Hyaluronic Acid",
        )
        assert primary == ["Niacinamide"]
        assert "Niacinamide" not in secondary

    def test_strength_variant_is_not_a_separate_ingredient(self, canonical_population):
        """'Niacinamide' and 'Niacinamide 10%' are the same ingredient."""
        primary, secondary, _e, _f = select(
            canonical_population,
            product_name="10% Niacinamide Serum",
            key_score_master_matches="Niacinamide",
            inci_score_master_matches="Niacinamide 10%; Glycerin",
        )
        assert primary == ["Niacinamide"]
        assert not any("niacinamide" in item.lower() for item in secondary)

    def test_secondary_is_capped(self, canonical_population):
        many = "; ".join(f"Ingredient {index}" for index in range(40))
        _p, secondary, _e, _f = select(
            canonical_population,
            product_name="Complex Cream",
            key_score_master_matches="Aloe Vera",
            inci_score_master_matches=many,
        )
        assert len(secondary) <= canonical_population.MAX_SECONDARY_INGREDIENTS


class TestIngredientKey:
    def test_percentage_is_not_part_of_identity(self, canonical_population):
        assert canonical_population.ingredient_key("Niacinamide 10%") == canonical_population.ingredient_key(
            "Niacinamide"
        )

    def test_punctuation_and_case_are_normalised(self, canonical_population):
        assert canonical_population.ingredient_key("BHT") == canonical_population.ingredient_key("Bht")
        assert canonical_population.ingredient_key("4-T-Butylcyclohexanol") == canonical_population.ingredient_key(
            "4-t-Butylcyclohexanol"
        )
