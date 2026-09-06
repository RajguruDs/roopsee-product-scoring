"""Canonical v2 population loading, GTIN handling, and the scoring input."""

from __future__ import annotations

import pytest


class TestGtinValidation:
    @pytest.mark.parametrize(
        "gtin",
        [
            "4005800238963",  # Eucerin, 13-digit
            "8906147701775",  # Dot & Key, 13-digit
            "020714157760",  # 12-digit with a leading zero
        ],
    )
    def test_real_catalogue_gtins_are_valid(self, canonical_population, gtin):
        valid, reason = canonical_population.validate_gtin(gtin)
        assert valid, f"{gtin} rejected: {reason}"

    def test_missing_gtin_is_invalid(self, canonical_population):
        assert canonical_population.validate_gtin("") == (False, "missing")

    def test_non_numeric_gtin_is_invalid(self, canonical_population):
        valid, reason = canonical_population.validate_gtin("ABC123")
        assert not valid and reason == "non-numeric"

    def test_wrong_length_is_invalid(self, canonical_population):
        valid, reason = canonical_population.validate_gtin("12345")
        assert not valid and "length" in reason

    def test_bad_check_digit_is_invalid(self, canonical_population):
        valid, reason = canonical_population.validate_gtin("4005800238964")
        assert not valid and reason == "check digit mismatch"

    def test_check_digit_matches_gs1_worked_example(self, canonical_population):
        # GS1 documents 629104150021 -> check digit 3.
        assert canonical_population.gtin_check_digit("629104150021") == 3


class TestGtinIsAString:
    def test_leading_zeros_survive_reading(self, canonical_population, source_dir, has_canonical_sources):
        if not has_canonical_sources:
            pytest.skip("canonical v2 sources not present")
        rows = canonical_population.read_csv_rows(source_dir / "canonical_scoring_population_v2.csv")
        leading_zero = [row["gtin"] for row in rows if row["gtin"].startswith("0")]
        assert leading_zero, "expected GTINs with leading zeros in the canonical population"
        assert all(isinstance(value, str) for value in leading_zero)
        # The canonical id embeds the raw GTIN, so the two must agree.
        by_gtin = {row["gtin"]: row["canonical_product_id_v2"] for row in rows}
        for gtin, canonical_id in by_gtin.items():
            assert canonical_id == f"GTIN_{gtin}"

    def test_every_gtin_is_digits_only(self, canonical_population, source_dir, has_canonical_sources):
        if not has_canonical_sources:
            pytest.skip("canonical v2 sources not present")
        rows = canonical_population.read_csv_rows(source_dir / "canonical_scoring_population_v2.csv")
        assert all(row["gtin"].isdigit() for row in rows)


@pytest.fixture(scope="module")
def products(canonical_population, has_canonical_sources):
    if not has_canonical_sources:
        pytest.skip("canonical v2 sources not present")
    return canonical_population.load_canonical_products()


@pytest.fixture(scope="module")
def first_pair(canonical_population, products):
    return next(iter(canonical_population.as_population_rows(products)))


class TestPopulationLoading:
    def test_population_is_the_full_canonical_set(self, products):
        assert len(products) > 4000

    def test_one_product_per_gtin(self, products):
        gtins = [product.gtin for product in products]
        assert len(gtins) == len(set(gtins))

    def test_canonical_ids_are_unique(self, products):
        ids = [product.canonical_id for product in products]
        assert len(ids) == len(set(ids))

    def test_every_product_has_identity_fields(self, products):
        for product in products:
            assert product.canonical_id
            assert product.gtin
            assert product.product_name

    def test_product_type_uses_the_downstream_vocabulary(self, products):
        expected = {"Serum", "Cleanser", "Moisturizer", "Sunscreen", "Mask", "Toner", "Other"}
        assert {product.product_type for product in products} <= expected

    def test_commerce_metadata_is_complete(self, products):
        """The legacy name-join left 303 products with no brand or URL."""
        assert all(product.brand for product in products)
        assert all(product.product_url for product in products)

    def test_mapping_is_joined_for_every_product(self, products):
        unmapped = [p for p in products if "no ingredient mapping row" in "; ".join(p.review_flags)]
        assert not unmapped


class TestPopulationRowShapes:
    """The loader must emit exactly what build_rows() already consumes."""

    def test_product_dict_has_the_fields_build_rows_reads(self, first_pair):
        product, _representative = first_pair
        for key in [
            "source_row",
            "product_name",
            "product_name_key",
            "primary_ingredients",
            "secondary_ingredients",
        ]:
            assert key in product
        assert isinstance(product["primary_ingredients"], list)
        assert isinstance(product["secondary_ingredients"], list)

    def test_representative_is_shaped_like_a_retailer_row(self, first_pair):
        _product, representative = first_pair
        for key in [
            "site",
            "brand",
            "variant",
            "mrp",
            "selling_price",
            "in_stock",
            "product_url",
            "image_url",
            "ingredients",
            "categories",
            "source_categories",
        ]:
            assert key in representative

    def test_in_stock_uses_retailer_string_form(self, first_pair):
        _product, representative = first_pair
        assert representative["in_stock"] in {"true", "false", ""}

    def test_inci_presence_validation_is_opt_in(self, first_pair):
        """Re-validating heroes against raw INCI strips real actives, so it is off.

        "Licochalcone A" appears in an INCI as "Glycyrrhiza Inflata Root
        Extract"; a textual presence check drops it. Enable deliberately with
        ROOPSEE_CANONICAL_VALIDATE_INCI=1 to compare.
        """
        _product, representative = first_pair
        assert representative["is_full_inci"] is False


class TestScoringInput:
    def test_write_and_read_round_trip(self, canonical_population, tmp_path, has_canonical_sources):
        if not has_canonical_sources:
            pytest.skip("canonical v2 sources not present")
        products = canonical_population.load_canonical_products()
        path = canonical_population.write_scoring_input(products, tmp_path / "scoring_input.csv")
        rows = canonical_population.read_csv_rows(path)
        assert len(rows) == len(products)
        assert list(rows[0].keys()) == canonical_population.SCORING_INPUT_HEADERS
        for required in ["gtin", "brand", "product_name", "product_type", "primary_ingredients"]:
            assert required in rows[0]

    def test_scoring_input_preserves_gtin_as_text(self, canonical_population, tmp_path, has_canonical_sources):
        if not has_canonical_sources:
            pytest.skip("canonical v2 sources not present")
        products = canonical_population.load_canonical_products()
        path = canonical_population.write_scoring_input(products, tmp_path / "scoring_input.csv")
        rows = canonical_population.read_csv_rows(path)
        written = {row["gtin"] for row in rows}
        assert {product.gtin for product in products} == written


class TestDeterminism:
    def test_two_loads_produce_identical_output(self, canonical_population, has_canonical_sources):
        if not has_canonical_sources:
            pytest.skip("canonical v2 sources not present")
        first = canonical_population.load_canonical_products()
        second = canonical_population.load_canonical_products()
        assert [p.canonical_id for p in first] == [p.canonical_id for p in second]
        assert [p.primary_ingredients for p in first] == [p.primary_ingredients for p in second]
        assert [p.secondary_ingredients for p in first] == [p.secondary_ingredients for p in second]
