"""INCI and ingredient-list parsing.

A comma is only sometimes a delimiter. These tests pin the cases where it is
not, because a wrong split silently invents an ingredient: the fragment "1"
from a split "1,2-Hexanediol" is not rejected as non-ingredient text, so it
becomes a Created fallback match, which in turn demotes the whole product to
Medium confidence in build_final_platform_dataset.py:308-311.
"""

from __future__ import annotations

import pytest


LOCANT_INGREDIENTS = [
    "1,2-Hexanediol",
    "1,3-Butylene Glycol",
    "1,2-Pentanediol",
    "2,3-Butanediol",
    "1,10-Decanediol",
]


class TestSplitFullInciItems:
    @pytest.mark.parametrize("ingredient", LOCANT_INGREDIENTS)
    def test_locant_comma_is_not_a_delimiter(self, auto_scorer, ingredient):
        items = auto_scorer.split_full_inci_items(f"Aqua, {ingredient}, Glycerin")
        assert items == ["Aqua", ingredient, "Glycerin"]

    def test_real_inci_string_with_locant(self, auto_scorer):
        items = auto_scorer.split_full_inci_items(
            "Aqua, 1,2-Hexanediol, Butylene Glycol, Sodium Hyaluronate, Phenoxyethanol"
        )
        assert items == [
            "Aqua",
            "1,2-Hexanediol",
            "Butylene Glycol",
            "Sodium Hyaluronate",
            "Phenoxyethanol",
        ]

    def test_slash_is_never_a_delimiter(self, auto_scorer):
        items = auto_scorer.split_full_inci_items("Caprylic/Capric Triglyceride, Squalane")
        assert items == ["Caprylic/Capric Triglyceride", "Squalane"]

    def test_water_aqua_eau_slash_form_stays_whole(self, auto_scorer):
        items = auto_scorer.split_full_inci_items("Water / Aqua / Eau, Butylene Glycol")
        assert items == ["Water / Aqua / Eau", "Butylene Glycol"]

    def test_comma_inside_parentheses_is_not_a_delimiter(self, auto_scorer):
        items = auto_scorer.split_full_inci_items(
            "Rosa Damascena (Rose, Flower) Extract, Glycerin"
        )
        assert items == ["Rosa Damascena (Rose, Flower) Extract", "Glycerin"]

    def test_parenthetical_synonym_is_preserved(self, auto_scorer):
        items = auto_scorer.split_full_inci_items(
            "Isobutylamido Thiazolyl Resorcinol (Thiamidol), Glycerin"
        )
        assert items[0] == "Isobutylamido Thiazolyl Resorcinol (Thiamidol)"

    def test_and_form_in_parentheses_stays_whole(self, auto_scorer):
        items = auto_scorer.split_full_inci_items("Butylene Glycol (and) Glycerin, Silica")
        assert items == ["Butylene Glycol (and) Glycerin", "Silica"]

    def test_hyphenated_names_survive(self, auto_scorer):
        items = auto_scorer.split_full_inci_items("4-T-Butylcyclohexanol, Bisabolol")
        assert items == ["4-T-Butylcyclohexanol", "Bisabolol"]

    def test_semicolon_still_splits(self, auto_scorer):
        assert auto_scorer.split_full_inci_items("Aqua; Glycerin") == ["Aqua", "Glycerin"]

    def test_empty_text_returns_empty_list(self, auto_scorer):
        assert auto_scorer.split_full_inci_items("") == []

    def test_unbalanced_bracket_does_not_swallow_the_rest(self, auto_scorer):
        items = auto_scorer.split_full_inci_items("Aqua, Extract (unclosed, Glycerin")
        assert "Aqua" in items
        assert len(items) >= 2


class TestSplitIngredients:
    @pytest.mark.parametrize("ingredient", LOCANT_INGREDIENTS)
    def test_locant_comma_is_not_a_delimiter(self, auto_scorer, ingredient):
        assert auto_scorer.split_ingredients(f"Niacinamide, {ingredient}") == [
            "Niacinamide",
            ingredient,
        ]

    def test_percentage_prefixed_list_with_locant(self, auto_scorer):
        assert auto_scorer.split_ingredients("Niacinamide 10%, 1,2-Hexanediol, Zinc PCA") == [
            "Niacinamide",
            "1,2-Hexanediol",
            "Zinc PCA",
        ]

    def test_thousands_separator_is_still_collapsed(self, auto_scorer):
        # "Aloe Complex 55,000ppm" must not become two ingredients.
        parts = auto_scorer.split_ingredients("Aloe Complex 55,000ppm, Glycerin")
        assert len(parts) == 2
        assert parts[1] == "Glycerin"
        assert "Aloe Complex" in parts[0]

    def test_plus_still_splits(self, auto_scorer):
        assert auto_scorer.split_ingredients("AHA + BHA") == ["AHA", "BHA"]

    def test_pipe_still_splits(self, auto_scorer):
        assert auto_scorer.split_ingredients("Niacinamide | Zinc PCA") == [
            "Niacinamide",
            "Zinc PCA",
        ]

    def test_slash_compound_stays_whole(self, auto_scorer):
        assert auto_scorer.split_ingredients("Caprylic/Capric Triglyceride") == [
            "Caprylic/Capric Triglyceride"
        ]

    def test_ingredients_prefix_is_stripped(self, auto_scorer):
        assert auto_scorer.split_ingredients("Ingredients: Niacinamide, Glycerin") == [
            "Niacinamide",
            "Glycerin",
        ]


class TestSplitIngredientParts:
    """The shared delimiter walker both splitters are built on."""

    def test_returns_raw_parts_without_cleaning(self, auto_scorer):
        assert auto_scorer.split_ingredient_parts("a, b") == ["a", " b"]

    def test_plus_only_splits_when_requested(self, auto_scorer):
        assert auto_scorer.split_ingredient_parts("AHA + BHA") == ["AHA + BHA"]
        assert auto_scorer.split_ingredient_parts("AHA + BHA", split_plus=True) == ["AHA ", " BHA"]

    def test_plus_without_surrounding_space_is_not_a_delimiter(self, auto_scorer):
        assert auto_scorer.split_ingredient_parts("Vitamin C+E", split_plus=True) == ["Vitamin C+E"]

    def test_nested_brackets_track_depth(self, auto_scorer):
        assert auto_scorer.split_ingredient_parts("A (B (C, D), E), F") == ["A (B (C, D), E)", " F"]
