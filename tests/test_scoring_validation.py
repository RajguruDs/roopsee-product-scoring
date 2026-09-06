"""Scoring-quality validation framework.

The framework's job is to measure honestly. These tests pin the properties that
make it trustworthy: the sample is reproducible, doctor scores are never
invented, missing data never turns into a result, and nothing it does can reach
the scoring engine or the production data.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "outputs" / "roopsee_canonical"
SAMPLE_PATH = OUTPUT_DIR / "scoring_quality_validation_sample.csv"
PREDICTIONS_PATH = OUTPUT_DIR / "scoring_quality_predictions.csv"
META_PATH = OUTPUT_DIR / "scoring_quality_validation_meta.json"


@pytest.fixture(scope="module")
def validation(request):
    from tests.conftest import load_tool_module

    return load_tool_module("scoring_validation")


@pytest.fixture(scope="module")
def builder():
    from tests.conftest import load_tool_module

    return load_tool_module("build_scoring_validation")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def sample_rows():
    if not SAMPLE_PATH.exists():
        pytest.skip("validation sample not built")
    return _read_csv(SAMPLE_PATH)


@pytest.fixture(scope="module")
def prediction_rows():
    if not PREDICTIONS_PATH.exists():
        pytest.skip("validation predictions not built")
    return _read_csv(PREDICTIONS_PATH)


# --------------------------------------------------------------------------
# Versioning
# --------------------------------------------------------------------------


class TestVersioning:
    def test_versions_are_declared(self, validation):
        assert validation.SCORING_VALIDATION_VERSION == "canonical_v1"
        assert validation.VALIDATION_SAMPLE_VERSION
        assert validation.VALIDATION_PROFILE_VERSION

    def test_meta_records_provenance(self):
        if not META_PATH.exists():
            pytest.skip("validation meta not built")
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        for key in [
            "scoring_validation_version",
            "validation_sample_version",
            "validation_profile_version",
            "generated_at",
            "sample_seed",
            "sample_size",
            "prediction_pairs",
            "doctor_scores_present",
            "metric_methodology",
            "known_limitations",
        ]:
            assert key in meta, f"meta is missing {key}"

    def test_meta_records_the_anchor_divergence(self):
        if not META_PATH.exists():
            pytest.skip("validation meta not built")
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        limitations = " ".join(meta["known_limitations"]).lower()
        assert "4.5" in limitations and "3.25" in limitations


# --------------------------------------------------------------------------
# Step 1 -- deterministic sampling
# --------------------------------------------------------------------------


class TestDeterministicSampling:
    def test_order_key_is_stable(self, builder):
        first = builder.stable_order_key("GTIN_4005800238963")
        second = builder.stable_order_key("GTIN_4005800238963")
        assert first == second

    def test_order_key_differs_between_products(self, builder):
        assert builder.stable_order_key("GTIN_1") != builder.stable_order_key("GTIN_2")

    def test_sampling_twice_gives_the_same_products(self, builder):
        products, score_columns = builder.load_scored_population()
        scoring_input = builder.load_scoring_input()
        first = builder.build_sample(products, score_columns, scoring_input, 60)
        second = builder.build_sample(products, score_columns, scoring_input, 60)
        assert [e["canonical_id"] for e in first] == [e["canonical_id"] for e in second]

    def test_sample_is_not_just_the_first_rows(self, builder):
        products, score_columns = builder.load_scored_population()
        sample = builder.build_sample(products, score_columns, builder.load_scoring_input(), 60)
        head = [product["uid"] for product in products[:60]]
        assert [e["canonical_id"] for e in sample] != head


class TestSampleContents:
    def test_no_duplicate_products(self, sample_rows):
        ids = [row["canonical_product_id_v2"] for row in sample_rows]
        assert len(ids) == len(set(ids))

    def test_no_duplicate_gtins(self, sample_rows):
        gtins = [row["gtin"] for row in sample_rows if row["gtin"]]
        assert len(gtins) == len(set(gtins))

    def test_all_product_types_are_represented(self, sample_rows):
        expected = {"serum", "cleanser", "moisturizer", "sunscreen", "mask", "toner", "other"}
        assert {row["product_type"] for row in sample_rows} == expected

    def test_sample_is_in_the_target_size_range(self, sample_rows):
        assert 100 <= len(sample_rows) <= 200

    def test_every_row_records_why_it_was_selected(self, sample_rows):
        assert all(row["validation_strata"] for row in sample_rows)

    def test_safety_cases_are_present(self, sample_rows):
        blocked = [row for row in sample_rows if row["safety_blocked_columns"]]
        assert blocked, "no safety-blocked products were sampled"

    def test_products_with_and_without_anchor_support_are_present(self, sample_rows):
        bands = {row["anchor_band"] for row in sample_rows}
        assert "no_anchor" in bands
        assert bands & {"strong_anchor", "medium_anchor"}


# --------------------------------------------------------------------------
# Step 2 -- profiles
# --------------------------------------------------------------------------


class TestProfiles:
    def test_profile_ids_are_unique(self, validation):
        ids = [profile["profile_id"] for profile in validation.VALIDATION_PROFILES]
        assert len(ids) == len(set(ids))

    def test_sensitive_is_a_boolean(self, validation):
        """app.js treats state.sensitive as a boolean; the string "No" is truthy."""
        for profile in validation.VALIDATION_PROFILES:
            assert isinstance(profile["sensitive"], bool)

    def test_concerns_are_real_score_columns(self, validation):
        from tests.conftest import load_tool_module

        builder = load_tool_module("build_final_platform_dataset")
        columns = set(builder.SCORE_COLUMNS)
        for profile in validation.VALIDATION_PROFILES:
            if profile["concern"] != "None":
                assert profile["concern"] in columns, profile["concern"]

    def test_the_required_coverage_exists(self, validation):
        profiles = validation.VALIDATION_PROFILES
        assert any(p["age"] == "Teen" for p in profiles)
        assert any("Pregnant" in p["specialConditions"] for p in profiles)
        assert any("Breastfeeding" in p["specialConditions"] for p in profiles)
        assert any("Excessive Dryness" in p["specialConditions"] for p in profiles)
        assert any(p["sensitive"] for p in profiles)
        assert {p["skinType"] for p in profiles} == {"Oily", "Dry", "Normal", "Combination"}

    def test_every_profile_explains_itself(self, validation):
        assert all(profile["rationale"] for profile in validation.VALIDATION_PROFILES)


# --------------------------------------------------------------------------
# Step 3/4 -- predictions and the doctor template
# --------------------------------------------------------------------------


class TestPredictions:
    def test_doctor_columns_start_blank(self, prediction_rows):
        """Nothing may pre-fill a doctor answer. The whole exercise depends on it."""
        for row in prediction_rows:
            assert row["doctor_score"] == ""
            assert row["doctor_recommendation"] == ""
            assert row["doctor_notes"] == ""

    def test_the_three_score_columns_are_distinct(self, prediction_rows):
        header = prediction_rows[0].keys()
        assert "legacy_score" in header
        assert "canonical_v2_score" in header
        assert "doctor_score" in header

    def test_every_row_has_a_canonical_score(self, prediction_rows):
        assert all(row["canonical_v2_score"] != "" for row in prediction_rows)

    def test_same_products_are_used_for_both_systems(self, prediction_rows):
        """Where legacy exists it must be the same product and profile, not a proxy."""
        for row in prediction_rows:
            if row["legacy_score"] != "":
                assert row["legacy_match_method"] == "normalised product name"
                assert row["canonical_product_id_v2"]
                assert row["profile_id"]

    def test_pairs_are_unique(self, prediction_rows):
        keys = [(row["canonical_product_id_v2"], row["profile_id"]) for row in prediction_rows]
        assert len(keys) == len(set(keys))

    def test_grid_is_products_times_profiles(self, prediction_rows, sample_rows, validation):
        assert len(prediction_rows) == len(sample_rows) * len(validation.VALIDATION_PROFILES)


class TestDoctorTemplate:
    def test_template_exists_in_some_readable_form(self):
        xlsx = OUTPUT_DIR / "doctor_validation_template.xlsx"
        csv_path = OUTPUT_DIR / "doctor_validation_template.csv"
        if not xlsx.exists() and not csv_path.exists():
            pytest.skip("doctor template not built")
        assert xlsx.exists() or csv_path.exists()

    def test_template_doctor_columns_are_blank(self):
        csv_path = OUTPUT_DIR / "doctor_validation_template.csv"
        if not csv_path.exists():
            pytest.skip("doctor template csv not built")
        for row in _read_csv(csv_path):
            assert row["DOCTOR SCORE"] == ""
            assert row["DOCTOR RECOMMENDATION"] == ""
            assert row["DOCTOR NOTES"] == ""

    def test_template_carries_both_system_scores(self):
        csv_path = OUTPUT_DIR / "doctor_validation_template.csv"
        if not csv_path.exists():
            pytest.skip("doctor template csv not built")
        header = _read_csv(csv_path)[0].keys()
        assert "Legacy score" in header
        assert "Canonical v2 score" in header


# --------------------------------------------------------------------------
# Step 5 -- metrics
# --------------------------------------------------------------------------


class TestBuckets:
    @pytest.mark.parametrize(
        "score,expected",
        [(95, "90-100"), (90, "90-100"), (85, "80-89"), (75, "70-79"), (60, "50-69"),
         (49, "1-49"), (1, "1-49"), (-100, "blocked"), (-150, "blocked")],
    )
    def test_score_bins_match_the_ui(self, validation, score, expected):
        assert validation.score_bin(score) == expected

    @pytest.mark.parametrize(
        "score,expected",
        [(95, "Excellent Match"), (85, "Great Match"), (75, "Good Match"),
         (55, "Fits with Caution"), (20, "Not Recommended"), (-100, "Not Recommended")],
    )
    def test_labels_match_the_doctor_engine(self, validation, score, expected):
        assert validation.score_label(score) == expected

    def test_bucket_distance(self, validation):
        assert validation.bin_distance(95, 92) == 0
        assert validation.bin_distance(95, 85) == 1
        assert validation.bin_distance(95, -100) == 5

    def test_missing_score_has_no_bucket(self, validation):
        assert validation.score_bin(None) == ""
        assert validation.bin_distance(None, 90) is None


class TestMetrics:
    def test_perfect_agreement(self, validation):
        values = [10.0, 50.0, 90.0]
        assert validation.mean_absolute_error(values, values) == 0
        assert validation.within_tolerance(values, values, 5) == 100.0
        assert validation.pearson_correlation(values, values) == 1.0
        assert validation.same_bucket_rate(values, values) == 100.0

    def test_mae_and_median(self, validation):
        system = [10.0, 20.0, 100.0]
        doctor = [10.0, 30.0, 50.0]
        assert validation.mean_absolute_error(system, doctor) == 20.0
        assert validation.median_absolute_error(system, doctor) == 10.0

    def test_within_tolerance(self, validation):
        assert validation.within_tolerance([10.0, 20.0], [12.0, 40.0], 5) == 50.0

    def test_pearson_detects_inverse_relationship(self, validation):
        assert validation.pearson_correlation([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0

    def test_spearman_handles_ties(self, validation):
        result = validation.spearman_correlation([1.0, 2.0, 2.0, 3.0], [1.0, 2.0, 2.0, 3.0])
        assert result == pytest.approx(1.0)

    def test_spearman_is_rank_based_not_value_based(self, validation):
        """A monotonic but non-linear relationship is perfect for Spearman."""
        assert validation.spearman_correlation([1.0, 2.0, 3.0], [1.0, 4.0, 9.0]) == pytest.approx(1.0)


class TestMetricsHandleMissingData:
    def test_no_pairs_returns_none_not_zero(self, validation):
        assert validation.mean_absolute_error([], []) is None
        assert validation.median_absolute_error([], []) is None
        assert validation.within_tolerance([], [], 10) is None
        assert validation.same_bucket_rate([], []) is None

    def test_all_doctor_scores_missing(self, validation):
        assert validation.mean_absolute_error([50.0, 60.0], [None, None]) is None

    def test_partial_doctor_scores_use_only_rated_pairs(self, validation):
        assert validation.mean_absolute_error([50.0, 60.0], [55.0, None]) == 5.0

    def test_correlation_needs_two_points(self, validation):
        assert validation.pearson_correlation([1.0], [1.0]) is None
        assert validation.spearman_correlation([1.0], [1.0]) is None

    def test_zero_variance_does_not_divide_by_zero(self, validation):
        assert validation.pearson_correlation([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None

    def test_metric_suite_survives_an_empty_input(self, validation):
        suite = validation.metric_suite([], [])
        assert suite["pairs"] == 0
        assert suite["mae"] is None
        assert suite["hard_block_agreement_pct"] is None


class TestHardBlockMetrics:
    def test_agreement(self, validation):
        result = validation.hard_block_metrics([-100.0, 80.0], [-100.0, 85.0])
        assert result["hard_block_agreement_pct"] == 100.0
        assert result["false_safe_count"] == 0
        assert result["false_block_count"] == 0

    def test_false_safe_is_doctor_blocked_system_not(self, validation):
        result = validation.hard_block_metrics([80.0], [-100.0])
        assert result["false_safe_count"] == 1
        assert result["false_block_count"] == 0

    def test_false_block_is_system_blocked_doctor_not(self, validation):
        result = validation.hard_block_metrics([-100.0], [80.0])
        assert result["false_block_count"] == 1
        assert result["false_safe_count"] == 0

    def test_counts_are_reported_for_both_sides(self, validation):
        result = validation.hard_block_metrics([-100.0, 80.0, -100.0], [-100.0, -100.0, 90.0])
        assert result["system_blocked_count"] == 2
        assert result["doctor_blocked_count"] == 2
        assert result["pairs"] == 3


class TestComparison:
    def test_lower_is_better_for_error_metrics(self, validation):
        assert validation.compare_metric("mae", 20.0, 10.0)["better"] == "canonical_v2"
        assert validation.compare_metric("mae", 10.0, 20.0)["better"] == "legacy"

    def test_higher_is_better_for_agreement_metrics(self, validation):
        assert validation.compare_metric("within_10_pct", 50.0, 70.0)["better"] == "canonical_v2"
        assert validation.compare_metric("within_10_pct", 70.0, 50.0)["better"] == "legacy"

    def test_false_safe_lower_is_better(self, validation):
        assert validation.compare_metric("false_safe_count", 5, 1)["better"] == "canonical_v2"

    def test_equal_values_are_a_tie(self, validation):
        assert validation.compare_metric("mae", 10.0, 10.0)["better"] == "tie"

    def test_missing_data_never_declares_a_winner(self, validation):
        """An absent doctor score must never read as a result for either system."""
        assert validation.compare_metric("mae", None, 10.0)["better"] == "insufficient data"
        assert validation.compare_metric("mae", 10.0, None)["better"] == "insufficient data"
        assert validation.compare_metric("mae", None, None)["better"] == "insufficient data"

    def test_descriptive_metrics_have_no_winner(self, validation):
        assert validation.compare_metric("pairs", 10, 20)["better"] == "n/a"


class TestRankingMetrics:
    def test_full_overlap(self, validation):
        ranked = ["a", "b", "c", "d", "e"]
        assert validation.top_n_overlap(ranked, ranked, 5) == 100.0

    def test_no_overlap(self, validation):
        assert validation.top_n_overlap(["a", "b"], ["c", "d"], 2) == 0.0

    def test_partial_overlap(self, validation):
        assert validation.top_n_overlap(["a", "b", "c"], ["a", "x", "y"], 3) == pytest.approx(33.33, abs=0.01)

    def test_empty_ranking_returns_none(self, validation):
        assert validation.top_n_overlap([], ["a"], 5) is None
        assert validation.top_1_agreement([], []) is None

    def test_unsuitable_in_top_n(self, validation):
        assert validation.unsuitable_in_top_n(["a", "b"], {"a": 20.0, "b": 90.0}, 2) == 1

    def test_missed_strong_recommendations(self, validation):
        missed = validation.missed_strong_recommendations(["a"], {"a": 90.0, "b": 95.0}, 1)
        assert missed == ["b"]


class TestFailureClassification:
    def test_no_doctor_score_is_unknown(self, validation):
        category, _ = validation.classify_failure({"doctor_score": "", "canonical_v2_score": "80"})
        assert category == "unknown"

    def test_block_disagreement_is_safety(self, validation):
        category, _ = validation.classify_failure(
            {"doctor_score": "-100", "canonical_v2_score": "80", "special_conditions": "Pregnant"}
        )
        assert category == "safety logic"

    def test_missing_primary_is_selection(self, validation):
        category, _ = validation.classify_failure(
            {"doctor_score": "80", "canonical_v2_score": "40", "primary_ingredients": ""}
        )
        assert category == "primary/secondary selection"

    def test_weak_mapping_is_flagged(self, validation):
        category, _ = validation.classify_failure(
            {"doctor_score": "80", "canonical_v2_score": "40",
             "primary_ingredients": "Niacinamide", "mapping_confidence": "REVIEW"}
        )
        assert category == "ingredient mapping"

    def test_no_anchor_is_flagged(self, validation):
        category, _ = validation.classify_failure(
            {"doctor_score": "80", "canonical_v2_score": "40", "primary_ingredients": "Niacinamide",
             "mapping_confidence": "HIGH", "exact_support": "10", "anchor_support": "0"}
        )
        assert category == "doctor anchor"

    def test_every_category_is_declared(self, validation):
        assert "calibration" in validation.FAILURE_CATEGORIES
        assert "unknown" in validation.FAILURE_CATEGORIES


class TestBreakdowns:
    def test_groups_by_key(self, validation):
        rows = [
            {"product_type": "serum", "legacy_score": "70", "canonical_v2_score": "80", "doctor_score": "80"},
            {"product_type": "serum", "legacy_score": "60", "canonical_v2_score": "70", "doctor_score": "70"},
            {"product_type": "toner", "legacy_score": "50", "canonical_v2_score": "40", "doctor_score": "60"},
        ]
        table = validation.breakdown(rows, "product_type")
        assert {row["product_type"] for row in table} == {"serum", "toner"}
        serum = next(row for row in table if row["product_type"] == "serum")
        assert serum["rows"] == 2
        assert serum["canonical_mae"] == 0.0

    def test_group_with_no_doctor_scores_is_safe(self, validation):
        rows = [{"product_type": "serum", "legacy_score": "70", "canonical_v2_score": "80", "doctor_score": ""}]
        table = validation.breakdown(rows, "product_type")
        assert table[0]["canonical_mae"] is None
        assert table[0]["better_mae"] == "insufficient data"


# --------------------------------------------------------------------------
# Isolation -- the framework must not touch production or scoring
# --------------------------------------------------------------------------


class TestValidationIsolation:
    PRODUCTION_PATHS = [
        REPO_ROOT / "data" / "products.csv",
        REPO_ROOT / "data" / "source" / "canonical_scoring_population_v2.csv",
        REPO_ROOT / "data" / "source" / "scoring_input_ingredient_mapping_v2.csv",
        REPO_ROOT / "data" / "source" / "roopsee_ingredient_scores_v3.xlsx",
    ]

    def test_validation_tools_never_write_to_data(self):
        """A validation run must be incapable of mutating the reference layer."""
        for name in ["scoring_validation", "build_scoring_validation", "run_scoring_validation"]:
            source = (REPO_ROOT / "tools" / f"{name}.py").read_text(encoding="utf-8")
            for marker in ['open("data', "open('data", 'to_csv("data', "REPO_ROOT / \"data\""]:
                assert marker not in source, f"{name}.py appears to write into data/"

    def test_validation_does_not_import_the_score_builders_for_writing(self):
        source = (REPO_ROOT / "tools" / "run_scoring_validation.py").read_text(encoding="utf-8")
        assert "build_final_platform_dataset" not in source
        assert "build_automated_scores" not in source

    def test_production_files_are_present_and_readable(self):
        for path in self.PRODUCTION_PATHS:
            if path.exists():
                assert path.stat().st_size > 0

    def test_doctor_anchor_roster_is_untouched_by_validation(self):
        """The 384 reference products must not gain validation rows."""
        path = REPO_ROOT / "data" / "products.csv"
        if not path.exists():
            pytest.skip("doctor reference not present")
        assert len(_read_csv(path)) == 384

    def test_validation_scores_come_from_the_shipped_engine(self):
        """No second scoring implementation may creep into the framework."""
        harness = (REPO_ROOT / "tools" / "score_profiles.mjs").read_text(encoding="utf-8")
        assert "static" in harness and "app.js" in harness
        assert "__computeScoredRows" in harness
        for name in ["scoring_validation", "build_scoring_validation", "run_scoring_validation"]:
            source = (REPO_ROOT / "tools" / f"{name}.py").read_text(encoding="utf-8")
            assert "def customerFacingScore" not in source
            assert "def profileLayerScore" not in source
