"""Scoring methodology invariants.

The canonical migration replaces the product population. It must not change how
a product is scored. These tests pin the values the migration is not allowed to
move, so an accidental edit to the engine fails here rather than silently
shifting every score in the catalogue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestProductTypeWeighting:
    def test_serum_is_eighty_twenty(self, auto_scorer):
        rule = auto_scorer.product_type_rule("Serum")
        assert rule["primary_weight"] == 0.8
        assert rule["secondary_weight"] == 0.2

    @pytest.mark.parametrize(
        "product_type", ["Cleanser", "Toner", "Moisturizer", "Sunscreen", "Mask", "Other", "Anything Else"]
    )
    def test_every_other_type_is_fifty_fifty(self, auto_scorer, product_type):
        rule = auto_scorer.product_type_rule(product_type)
        assert rule["primary_weight"] == 0.5
        assert rule["secondary_weight"] == 0.5

    def test_cleanser_drops_the_concern_for_wrinkles(self, auto_scorer):
        assert auto_scorer.product_type_rule("Cleanser")["concern"] == "Yes except wrinkles"

    def test_moisturizer_and_sunscreen_ignore_concern(self, auto_scorer):
        assert auto_scorer.product_type_rule("Moisturizer")["concern"] is False
        assert auto_scorer.product_type_rule("Sunscreen")["concern"] is False


class TestHardBlockPropagation:
    def test_any_minus_100_blocks_the_group(self, auto_scorer):
        assert auto_scorer.average_with_hard_block([90.0, 95.0, -100.0]) == -100

    def test_a_clean_group_averages(self, auto_scorer):
        assert auto_scorer.average_with_hard_block([80.0, 90.0]) == 85

    def test_an_empty_group_has_no_score(self, auto_scorer):
        assert auto_scorer.average_with_hard_block([]) is None

    def test_minus_100_survives_the_weighted_blend(self, auto_scorer):
        blocked = auto_scorer.weighted_average_with_hard_block([(-100.0, 0.8), (100.0, 0.2)])
        assert blocked == -100


class TestScoreRounding:
    def test_rounds_half_away_from_zero(self, auto_scorer):
        assert auto_scorer.score_value(0.5) == 1
        assert auto_scorer.score_value(1.5) == 2
        assert auto_scorer.score_value(-0.5) == -1
        assert auto_scorer.score_value(-1.5) == -2

    def test_does_not_use_bankers_rounding(self, auto_scorer):
        """Python's round() would give 2 here; the engine must give 3."""
        assert auto_scorer.score_value(2.5) == 3


class TestExcessiveDrynessQuantisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [(0, 0), (50, -100), (30, -100), (51, 0), (84, 0), (85, 100), (100, 100), (-100, -100)],
    )
    def test_three_state_bucketing(self, raw, expected):
        from tests.conftest import load_tool_module  # noqa: PLC0415

        scoring = __import__("roopsee_coverage.scoring", fromlist=["scoring"])
        assert scoring.transformed_excessive_dryness_score(raw) == expected


class TestDoctorEngineRules:
    def test_serum_skips_skin_type(self):
        from roopsee_coverage.scoring import scoring_rule_for_product_type

        rule = scoring_rule_for_product_type("Serum")
        assert rule == {"age": True, "concern": True, "skin": False, "special": True}

    def test_moisturizer_skips_concern(self):
        from roopsee_coverage.scoring import scoring_rule_for_product_type

        rule = scoring_rule_for_product_type("Moisturizer")
        assert rule == {"age": True, "concern": False, "skin": True, "special": True}

    def test_hard_blocker_wins_over_the_average(self):
        from roopsee_coverage.scoring import rounded_average_score

        assert rounded_average_score([{"score": 100}, {"score": -100}]) == -100

    def test_average_rounds_away_from_zero(self):
        from roopsee_coverage.scoring import rounded_average_score

        assert rounded_average_score([{"score": 80}, {"score": 85}]) == 83


class TestDatasetWeightsAreUnchanged:
    def test_visible_score_weights(self):
        from tests.conftest import load_tool_module  # noqa: PLC0415

        builder = load_tool_module("build_final_platform_dataset")
        assert builder.VISIBLE_SCORE_WEIGHTS == {
            "baseline": 0.05,
            "v2": 0.30,
            "anchor": 0.55,
            "type_family": 0.05,
            "type": 0.05,
        }

    def test_rank_fusion_weights(self):
        from tests.conftest import load_tool_module  # noqa: PLC0415

        builder = load_tool_module("build_final_platform_dataset")
        assert builder.RANK_FUSION_WEIGHTS == {
            "score": 0.20,
            "baseline_rank": 0.10,
            "v2_rank": 0.10,
            "anchor_rank": 0.55,
            "type_family_rank": 0.05,
        }

    def test_weights_sum_to_one(self):
        from tests.conftest import load_tool_module  # noqa: PLC0415

        builder = load_tool_module("build_final_platform_dataset")
        assert sum(builder.VISIBLE_SCORE_WEIGHTS.values()) == pytest.approx(1.0)
        assert sum(builder.RANK_FUSION_WEIGHTS.values()) == pytest.approx(1.0)

    def test_anchor_threshold_and_cap_are_unchanged(self):
        """The doctor-anchor layer is 55% of the score; its gating must not drift."""
        source = (REPO_ROOT / "tools" / "build_final_platform_dataset.py").read_text(encoding="utf-8")
        assert "if sim >= 4.5:" in source
        assert "for doctor_index, _doctor_record, sim in anchors[:12]:" in source


class TestScoreColumnContract:
    """The 29 columns are positional: layers are bare arrays indexed by order."""

    def test_column_count_and_order(self):
        from tests.conftest import load_tool_module  # noqa: PLC0415

        builder = load_tool_module("build_final_platform_dataset")
        assert len(builder.SCORE_COLUMNS) == 29
        assert builder.SCORE_COLUMNS[0] == "<16"
        assert builder.SCORE_COLUMNS[-1] == "None"

    def test_load_bearing_misspelling_is_preserved(self):
        """'Breastfeeling Score' is misspelled in the source workbook and every
        consumer depends on it. Rename it everywhere at once or not at all."""
        from tests.conftest import load_tool_module  # noqa: PLC0415

        builder = load_tool_module("build_final_platform_dataset")
        assert "Breastfeeling Score" in builder.SCORE_COLUMNS
        assert "Excessive Dryness score" in builder.SCORE_COLUMNS
        assert "+>25" in builder.SCORE_COLUMNS


@pytest.fixture(scope="module")
def dataset():
    path = REPO_ROOT / "static" / "data" / "final_scored_products.json"
    if not path.exists():
        pytest.skip("frontend dataset not built")
    return json.loads(path.read_text(encoding="utf-8"))


class TestFrontendContract:
    """Fields static/app.js reads. Dropping one silently degrades scores."""

    REQUIRED_FIELDS = [
        "uid",
        "name",
        "brand",
        "category",
        "productType",
        "normalizedType",
        "mrp",
        "sellingPrice",
        "imageUrl",
        "primaryIngredients",
        "secondaryIngredients",
        "matchedPrimaryIngredients",
        "matchedSecondaryIngredients",
        "families",
        "confidence",
        "reviewFlags",
        "sourceValidationFlags",
        "formulaQualityFlags",
        "support",
        "nearestDoctorAnchors",
        "scoreLayers",
    ]

    def test_top_level_keys(self, dataset):
        assert set(dataset) >= {"metadata", "quizOptions", "scoreColumns", "products"}

    def test_every_product_has_the_required_fields(self, dataset):
        for product in dataset["products"]:
            for key in self.REQUIRED_FIELDS:
                assert key in product, f"{product.get('uid')} is missing {key}"

    def test_score_layers_are_complete_and_aligned(self, dataset):
        width = len(dataset["scoreColumns"])
        for product in dataset["products"]:
            layers = product["scoreLayers"]
            assert set(layers) == {"baseline", "v2", "anchor", "typeFamily", "type"}
            for values in layers.values():
                assert len(values) == width
                assert all(isinstance(value, int) for value in values)

    def test_support_has_every_counter(self, dataset):
        for product in dataset["products"]:
            assert set(product["support"]) == {"anchor", "typeFamily", "type", "exact", "family"}

    def test_weights_use_the_snake_case_keys_the_browser_reads(self, dataset):
        assert set(dataset["metadata"]["visibleScoreWeights"]) == {
            "baseline",
            "v2",
            "anchor",
            "type_family",
            "type",
        }
        assert set(dataset["metadata"]["rankFusionWeights"]) == {
            "score",
            "baseline_rank",
            "v2_rank",
            "anchor_rank",
            "type_family_rank",
        }

    def test_normalized_types_are_in_the_expected_vocabulary(self, dataset):
        expected = {"serum", "cleanser", "moisturizer", "sunscreen", "mask", "toner", "other"}
        assert {product["normalizedType"] for product in dataset["products"]} <= expected

    def test_categories_are_in_the_expected_vocabulary(self, dataset):
        expected = {"Face", "Body", "Face & Body", "Lips", "Eye"}
        assert {product["category"] for product in dataset["products"]} <= expected

    def test_concern_options_match_score_columns(self, dataset):
        """profileLayerScore() uses the concern label directly as a column name."""
        columns = set(dataset["scoreColumns"])
        for concern in dataset["quizOptions"]["faceBodyConcerns"]:
            assert concern in columns, f"concern {concern!r} has no score column"


class TestOnboardedDatasetShape:
    def test_respects_the_onboarding_limit(self, dataset):
        limit = dataset["metadata"].get("onboardLimit")
        if limit is None:
            pytest.skip("no onboarding limit configured")
        assert len(dataset["products"]) <= limit

    def test_product_count_matches_the_payload(self, dataset):
        assert dataset["metadata"]["productCount"] == len(dataset["products"])

    def test_uids_are_unique(self, dataset):
        uids = [product["uid"] for product in dataset["products"]]
        assert len(uids) == len(set(uids))

    def test_gtins_are_unique_when_present(self, dataset):
        gtins = [product.get("gtin") for product in dataset["products"] if product.get("gtin")]
        assert len(gtins) == len(set(gtins))
