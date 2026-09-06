"""Canonical v2 product population loader.

Replaces the legacy population (useful_skin_bodycare_products.xlsx, keyed by
free-text product name) with the canonical GTIN-keyed population.

Two source files, joined 1:1 on ``canonical_product_id_v2``:

- ``canonical_scoring_population_v2.csv`` — one row per canonical product:
  brand, name, category, price, URL, image, full INCI, key ingredients.
- ``scoring_input_ingredient_mapping_v2.csv`` — the ingredient intelligence
  layer: ingredients already resolved against the Roopsee score master, plus
  ``product_type_mapped`` and mapping confidence. It is NOT a second product
  population; it carries no scores of its own.

This module only *prepares* the scoring input. Every score is still produced by
the existing engine in build_automated_scores.py and build_final_platform_dataset.py.

Run standalone to write the scoring input and print a coverage summary:

    python tools/canonical_population.py
"""

from __future__ import annotations

import csv
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "data" / "source"


def env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


CANONICAL_POPULATION = env_path("ROOPSEE_CANONICAL_POPULATION", SOURCE_DIR / "canonical_scoring_population_v2.csv")
INGREDIENT_MAPPING = env_path("ROOPSEE_INGREDIENT_MAPPING", SOURCE_DIR / "scoring_input_ingredient_mapping_v2.csv")
CANONICAL_OUTPUT_DIR = env_path("ROOPSEE_CANONICAL_OUTPUT_DIR", REPO_ROOT / "outputs" / "roopsee_canonical")
SCORING_INPUT_PATH = env_path("ROOPSEE_SCORING_INPUT", CANONICAL_OUTPUT_DIR / "roopsee_final_scoring_input.csv")

JOIN_KEY = "canonical_product_id_v2"

# Whether to re-validate the chosen ingredients against the raw INCI text.
# Off by default, and deliberately so: hero ingredients are routinely marketed
# under a name the INCI does not use ("Licochalcone A" appears in the INCI as
# "Glycyrrhiza Inflata Root Extract"), so the presence check strips real actives.
# The mapping layer already resolved ingredients from this same INCI, which
# makes a second textual pass both redundant and lossy. Enable to compare.
VALIDATE_AGAINST_INCI = os.environ.get("ROOPSEE_CANONICAL_VALIDATE_INCI", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

# Primary is capped so the serum 80/20 primary/secondary split stays meaningful,
# and secondary is capped so a 40-item INCI cannot drown the hero ingredients in
# the 50/50 product types. Both limits are on the scoring input, not on scoring.
MAX_PRIMARY_INGREDIENTS = 4
MAX_SECONDARY_INGREDIENTS = 12

# Shortest ingredient name we will lift out of a product title. Below this,
# name matching produces noise ("Oil", "Gel") rather than evidence.
MIN_NAME_EVIDENCE_LENGTH = 4

# Formulation ingredients that must never be promoted on their own evidence:
# solvents, preservatives, chelators, pH adjusters, and generic thickeners or
# emulsifiers. Deliberately EXCLUDED from this list, because Roopsee scores them
# as real actives: fragrance/parfum (drives fragrance_risk), glycerin and
# butylene glycol (humectants), cetearyl alcohol and squalane (emollients).
GENERIC_EXCIPIENTS = {
    "water",
    "aqua",
    "eau",
    "purified water",
    "aqua water",
    "water aqua eau",
    "deionized water",
    "distilled water",
    "phenoxyethanol",
    "ethylhexylglycerin",
    "sodium benzoate",
    "potassium sorbate",
    "chlorphenesin",
    "benzyl alcohol",
    "dehydroacetic acid",
    "sodium dehydroacetate",
    "disodium edta",
    "tetrasodium edta",
    "trisodium edta",
    "edta",
    "sodium hydroxide",
    "potassium hydroxide",
    "triethanolamine",
    "xanthan gum",
    "carbomer",
    "acrylates copolymer",
    "sodium polyacrylate",
    "cellulose gum",
    "hydroxyethylcellulose",
    "hydroxypropyl methylcellulose",
    "silica",
    "mica",
    "ci 77891",
    "titanium dioxide ci 77891",
}

PERCENT_BEFORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*([A-Za-z][A-Za-z0-9\s\-/']{2,60})")
PERCENT_AFTER_RE = re.compile(r"([A-Za-z][A-Za-z0-9\s\-/']{2,60}?)\s*(\d+(?:\.\d+)?)\s*%")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def norm_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()


def norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm_label(value))


def without_percent(value: str) -> str:
    return clean_text(re.sub(r"\b\d+(?:\.\d+)?\s*%\s*", "", clean_text(value)))


def ingredient_key(value: Any) -> str:
    """Identity key for de-duplication.

    Strength claims are not identity: "Niacinamide" and "Niacinamide 10%" are
    the same ingredient and must not appear as both primary and secondary.
    """
    return norm_key(without_percent(clean_text(value)))


def is_generic_excipient(label: str) -> bool:
    return norm_label(label) in GENERIC_EXCIPIENTS


# --------------------------------------------------------------------------
# GTIN
# --------------------------------------------------------------------------

VALID_GTIN_LENGTHS = {8, 12, 13, 14}


def gtin_check_digit(digits: str) -> int:
    """GS1 mod-10 check digit for the payload (all digits except the last)."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        weight = 3 if index % 2 == 0 else 1
        total += int(char) * weight
    return (10 - (total % 10)) % 10


def validate_gtin(value: Any) -> tuple[bool, str]:
    """Return (is_valid, reason). Reason is empty when valid.

    GTINs are validated but never dropped from the scored population — an
    invalid GTIN only gates onboarding, so the product stays auditable.
    """
    text = clean_text(value)
    if not text:
        return False, "missing"
    if not text.isdigit():
        return False, "non-numeric"
    if len(text) not in VALID_GTIN_LENGTHS:
        return False, f"length {len(text)} not in 8/12/13/14"
    if gtin_check_digit(text[:-1]) != int(text[-1]):
        return False, "check digit mismatch"
    return True, ""


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV with every value as a string.

    csv.DictReader never coerces types, which is what keeps GTIN leading zeros
    intact. Do not swap this for pandas: a naive read turns the 94 GTINs that
    begin with 0 into shorter integers.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def split_mapping_list(value: Any) -> list[str]:
    """Mapping columns pack matched ingredients into a ';'-delimited string."""
    return [clean_text(part) for part in clean_text(value).split(";") if clean_text(part)]


def parse_optional_number(value: Any) -> str:
    text = clean_text(value)
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def parse_bool_text(value: Any) -> str:
    """Normalise to the 'true'/'false' strings the retailer schema uses."""
    text = clean_text(value).lower()
    if text in {"true", "1", "yes"}:
        return "true"
    if text in {"false", "0", "no"}:
        return "false"
    return ""


# --------------------------------------------------------------------------
# Evidence-based primary / secondary selection
# --------------------------------------------------------------------------


def hero_ingredients_from_name(name: str, alias_map: dict[str, str]) -> list[tuple[str, str]]:
    """Ingredients the product itself claims as its hero, in the product name.

    Strongest evidence available: a brand naming "10% Niacinamide" or
    "Thiamidol" in the title is asserting the active. Percentage-qualified
    claims are read first, then any score-master ingredient (or curated alias,
    including parenthetical synonyms) appearing verbatim in the name.

    A product may legitimately claim several heroes -- "2% Salicylic Acid + 3%
    Niacinamide" returns both -- so this never forces a single winner.

    Returns (surface_form, canonical) pairs. Both are kept because the score
    master's canonical spelling and the mapping file's label for the same
    ingredient do not always agree, and either may be the one that corroborates.
    """
    if not alias_map:
        return []

    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(pair: tuple[str, str]) -> None:
        surface, canonical = pair
        if not canonical or is_generic_excipient(canonical) or is_generic_excipient(surface):
            return
        key = ingredient_key(canonical)
        if key and key not in seen:
            seen.add(key)
            found.append((surface, canonical))

    def longest_alias_in(text: str) -> tuple[str, str]:
        """Longest leading word-run of `text` that resolves to a canonical name."""
        words = clean_text(text).split()
        for size in range(min(len(words), 6), 0, -1):
            candidate = " ".join(words[:size])
            if len(candidate) < MIN_NAME_EVIDENCE_LENGTH:
                continue
            canonical = alias_map.get(norm_key(candidate))
            if canonical:
                return candidate, canonical
        return "", ""

    # 1. Percentage-qualified claims, both orders ("10% Niacinamide", "Niacinamide 10%").
    for match in PERCENT_BEFORE_RE.finditer(name):
        add(longest_alias_in(match.group(2)))
    for match in PERCENT_AFTER_RE.finditer(name):
        trailing = " ".join(clean_text(match.group(1)).split()[-6:])
        words = trailing.split()
        for start in range(len(words)):
            pair = longest_alias_in(" ".join(words[start:]))
            if pair[1]:
                add(pair)
                break

    # 2. Any canonical ingredient named outright in the title.
    words = clean_text(name).split()
    index = 0
    while index < len(words):
        matched: tuple[str, str] = ("", "")
        for size in range(min(6, len(words) - index), 0, -1):
            candidate = " ".join(words[index : index + size])
            if len(candidate) < MIN_NAME_EVIDENCE_LENGTH:
                continue
            canonical = alias_map.get(norm_key(candidate))
            if canonical:
                matched = (candidate, canonical)
                index += size
                break
        if matched[1]:
            add(matched)
        else:
            index += 1

    return found


def select_primary_secondary(
    row: dict[str, str],
    alias_map: dict[str, str] | None = None,
) -> tuple[list[str], list[str], str, list[str]]:
    """Choose primary and secondary ingredients from evidence, not INCI order.

    Primary evidence ladder, strongest first; the first tier that yields
    anything wins:

      1. product name / explicit hero claim
      2. key_ingredients resolved against the score master
      3. resolved score-master ingredients (covers alias-only resolutions)
      4. full INCI matches, minus generic excipients  -> flags REVIEW_REQUIRED

    Returns (primary, secondary, evidence_tier, review_flags).
    """
    review_flags: list[str] = []
    alias_map = alias_map or {}

    key_matches = [item for item in split_mapping_list(row.get("key_score_master_matches"))]
    inci_matches = [item for item in split_mapping_list(row.get("inci_score_master_matches"))]
    resolved = [item for item in split_mapping_list(row.get("resolved_score_master_ingredients"))]
    name = clean_text(row.get("product_name"))

    def dedupe(labels: list[str]) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for label in labels:
            key = ingredient_key(label)
            if key and key not in seen:
                seen.add(key)
                output.append(label)
        return output

    # Tier 1 -- the product's own hero claim.
    name_heroes = hero_ingredients_from_name(name, alias_map)
    # A name can claim anything, so a hero only counts when the ingredient data
    # corroborates it. Match on either the surface form found in the title or
    # the score master's canonical spelling, since the mapping file's label for
    # the same ingredient does not always use the same wording. When it does
    # corroborate, prefer the mapping's label: that is what the matcher in
    # build_automated_scores.py will resolve exactly.
    supported: dict[str, str] = {}
    for item in key_matches + resolved + inci_matches:
        supported.setdefault(ingredient_key(item), item)

    corroborated: list[str] = []
    for surface, canonical in name_heroes:
        if not supported:
            corroborated.append(canonical)
            continue
        label = supported.get(ingredient_key(canonical)) or supported.get(ingredient_key(surface))
        if label:
            corroborated.append(label)

    primary: list[str] = []
    evidence = ""
    if corroborated:
        primary = dedupe(corroborated)
        evidence = "product_name_hero"
    elif key_matches:
        primary = dedupe([item for item in key_matches if not is_generic_excipient(item)])
        evidence = "key_ingredients"
    if not primary and resolved:
        primary = dedupe([item for item in resolved if not is_generic_excipient(item)])
        evidence = "resolved_score_master"
    if not primary and inci_matches:
        primary = dedupe([item for item in inci_matches if not is_generic_excipient(item)])
        evidence = "full_inci_fallback"
        review_flags.append("primary ingredients inferred from full INCI only")
    if not primary:
        evidence = "none"
        review_flags.append("no score-master ingredient could be resolved")

    primary = primary[:MAX_PRIMARY_INGREDIENTS]
    primary_keys = {ingredient_key(item) for item in primary}

    # Secondary -- meaningful supporting ingredients only.
    candidates = dedupe(key_matches + resolved + inci_matches)
    secondary: list[str] = []
    dropped_excipients: list[str] = []
    for label in candidates:
        if ingredient_key(label) in primary_keys:
            continue
        if is_generic_excipient(label):
            dropped_excipients.append(label)
            continue
        secondary.append(label)

    if dropped_excipients:
        review_flags.append(
            "generic excipients not scored as ingredients: " + ", ".join(sorted(set(dropped_excipients))[:6])
        )

    return primary, secondary[:MAX_SECONDARY_INGREDIENTS], evidence, review_flags


# --------------------------------------------------------------------------
# The population
# --------------------------------------------------------------------------


@dataclass
class CanonicalProduct:
    canonical_id: str
    gtin: str
    gtin_valid: bool
    gtin_issue: str
    brand: str
    product_name: str
    category: str
    product_type: str
    variant: str
    sku: str
    site: str
    source_sites: str
    parent_product_id: str
    product_id: str
    mrp: str
    selling_price: str
    discount_pct: str
    rating: str
    rating_count: str
    in_stock: str
    product_url: str
    image_url: str
    ingredients: str
    key_ingredients: str
    how_to_use: str
    description: str
    primary_ingredients: list[str]
    secondary_ingredients: list[str]
    primary_evidence: str
    mapping_status: str
    mapping_resolution: str
    mapping_confidence: str
    key_match_count: str
    inci_match_count: str
    resolved_count: str
    source_row: int
    review_flags: list[str] = field(default_factory=list)


def title_case_product_type(value: str) -> str:
    """Match the Title-case product_type the payload has always carried."""
    text = clean_text(value)
    return text[:1].upper() + text[1:] if text else ""


def load_canonical_products(alias_map: dict[str, str] | None = None) -> list[CanonicalProduct]:
    """Join the canonical population to its ingredient mapping and pick ingredients.

    The join is on canonical_product_id_v2 and never on gtin: the mapping file
    wrote gtin as an integer, so its 94 leading-zero values no longer match the
    canonical strings.
    """
    population_rows = read_csv_rows(CANONICAL_POPULATION)
    mapping_rows = read_csv_rows(INGREDIENT_MAPPING)
    mapping_by_id = {clean_text(row.get(JOIN_KEY)): row for row in mapping_rows}

    products: list[CanonicalProduct] = []
    seen_ids: set[str] = set()
    for source_row, row in enumerate(population_rows, start=2):
        canonical_id = clean_text(row.get(JOIN_KEY))
        if not canonical_id or canonical_id in seen_ids:
            continue
        seen_ids.add(canonical_id)

        mapping = mapping_by_id.get(canonical_id, {})
        merged = {**row, **{k: v for k, v in mapping.items() if k not in row or not clean_text(row.get(k))}}
        primary, secondary, evidence, review_flags = select_primary_secondary(merged, alias_map)

        gtin = clean_text(row.get("gtin"))
        gtin_valid, gtin_issue = validate_gtin(gtin)
        if not mapping:
            review_flags.append("no ingredient mapping row for this canonical product")

        product_type = title_case_product_type(mapping.get("product_type_mapped", ""))

        products.append(
            CanonicalProduct(
                canonical_id=canonical_id,
                gtin=gtin,
                gtin_valid=gtin_valid,
                gtin_issue=gtin_issue,
                brand=clean_text(row.get("brand")),
                product_name=clean_text(row.get("product_name")),
                category=clean_text(row.get("category")),
                product_type=product_type,
                variant=clean_text(row.get("variant")),
                sku=clean_text(row.get("sku")),
                site=clean_text(row.get("site")),
                source_sites=clean_text(row.get("source_sites")),
                parent_product_id=clean_text(row.get("parent_product_id")),
                product_id=clean_text(row.get("product_id")),
                mrp=parse_optional_number(row.get("mrp")),
                selling_price=parse_optional_number(row.get("selling_price")),
                discount_pct=parse_optional_number(row.get("discount_pct")),
                rating=parse_optional_number(row.get("rating")),
                rating_count=parse_optional_number(row.get("rating_count")),
                in_stock=parse_bool_text(row.get("in_stock")),
                product_url=clean_text(row.get("product_url")),
                image_url=clean_text(row.get("image_url")),
                ingredients=clean_text(row.get("ingredients")),
                key_ingredients=clean_text(row.get("key_ingredients")),
                how_to_use=clean_text(row.get("how_to_use")),
                description=clean_text(row.get("description")),
                primary_ingredients=primary,
                secondary_ingredients=secondary,
                primary_evidence=evidence,
                mapping_status=clean_text(mapping.get("ingredient_mapping_status")),
                mapping_resolution=clean_text(mapping.get("mapping_resolution")),
                mapping_confidence=clean_text(mapping.get("mapping_confidence")),
                key_match_count=clean_text(mapping.get("key_score_master_count")),
                inci_match_count=clean_text(mapping.get("inci_score_master_count")),
                resolved_count=clean_text(mapping.get("resolved_count")),
                source_row=source_row,
                review_flags=review_flags,
            )
        )
    return products


def as_population_rows(
    products: list[CanonicalProduct],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield (product, representative) in the shapes build_rows() already consumes.

    The second dict is shaped like a retailer row so that product_type_from(),
    product_category_from(), source_validated_ingredients() and
    formula_quality_flags() keep working without modification -- the canonical
    ``category`` text feeds their existing `categories` lookups.
    """
    for product in products:
        product_dict = {
            "source_row": product.source_row,
            "product_name": product.product_name,
            "product_name_key": norm_key(product.product_name),
            "product_type": product.product_type,
            "canonical_id": product.canonical_id,
            "gtin": product.gtin,
            "primary_ingredients_text": ", ".join(product.primary_ingredients),
            "secondary_ingredients_text": ", ".join(product.secondary_ingredients),
            "primary_ingredients": list(product.primary_ingredients),
            "secondary_ingredients": list(product.secondary_ingredients),
        }
        representative = {
            "id": product.canonical_id,
            "site": product.site,
            "parent_product_id": product.parent_product_id,
            "product_id": product.product_id,
            "sku": product.sku,
            "brand": product.brand,
            "variant": product.variant,
            "categories": product.category,
            "source_categories": product.category,
            "product_attributes": "",
            "mrp": product.mrp,
            "selling_price": product.selling_price,
            "discount_pct": product.discount_pct,
            "rating": product.rating,
            "rating_count": product.rating_count,
            "review_count": "",
            "in_stock": product.in_stock,
            "product_url": product.product_url,
            "image_url": product.image_url,
            "ingredients": product.ingredients,
            "key_ingredients": product.key_ingredients,
            "how_to_use": product.how_to_use,
            # See VALIDATE_AGAINST_INCI: the canonical `ingredients` column is a
            # genuine full INCI, but treating it as authoritative for ingredient
            # presence removes correctly-identified actives, so it stays opt-in.
            "is_full_inci": bool(product.ingredients) and VALIDATE_AGAINST_INCI,
        }
        yield product_dict, representative


SCORING_INPUT_HEADERS = [
    "canonical_product_id_v2",
    "gtin",
    "gtin_valid",
    "gtin_issue",
    "brand",
    "product_name",
    "category",
    "product_type",
    "variant",
    "sku",
    "site",
    "source_sites",
    "primary_ingredients",
    "secondary_ingredients",
    "primary_evidence",
    "ingredients",
    "key_ingredients",
    "how_to_use",
    "mrp",
    "selling_price",
    "rating",
    "rating_count",
    "in_stock",
    "product_url",
    "image_url",
    "ingredient_mapping_status",
    "mapping_resolution",
    "mapping_confidence",
    "key_score_master_count",
    "inci_score_master_count",
    "resolved_count",
    "review_flags",
]


def write_scoring_input(products: list[CanonicalProduct], path: Path = SCORING_INPUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SCORING_INPUT_HEADERS)
        for product in products:
            writer.writerow(
                [
                    product.canonical_id,
                    product.gtin,
                    "true" if product.gtin_valid else "false",
                    product.gtin_issue,
                    product.brand,
                    product.product_name,
                    product.category,
                    product.product_type,
                    product.variant,
                    product.sku,
                    product.site,
                    product.source_sites,
                    ", ".join(product.primary_ingredients),
                    ", ".join(product.secondary_ingredients),
                    product.primary_evidence,
                    product.ingredients,
                    product.key_ingredients,
                    product.how_to_use,
                    product.mrp,
                    product.selling_price,
                    product.rating,
                    product.rating_count,
                    product.in_stock,
                    product.product_url,
                    product.image_url,
                    product.mapping_status,
                    product.mapping_resolution,
                    product.mapping_confidence,
                    product.key_match_count,
                    product.inci_match_count,
                    product.resolved_count,
                    "; ".join(product.review_flags),
                ]
            )
    return path


def coverage_summary(products: list[CanonicalProduct]) -> dict[str, Any]:
    from collections import Counter

    gtins = [product.gtin for product in products]
    return {
        "canonical_products": len(products),
        "distinct_gtins": len(set(gtins)),
        "duplicate_gtins": len(gtins) - len(set(gtins)),
        "invalid_gtins": sum(1 for product in products if not product.gtin_valid),
        "gtin_issues": dict(Counter(product.gtin_issue for product in products if product.gtin_issue)),
        "gtin_length_distribution": dict(Counter(len(product.gtin) for product in products)),
        "leading_zero_gtins": sum(1 for product in products if product.gtin.startswith("0")),
        "product_types": dict(Counter(product.product_type for product in products).most_common()),
        "primary_evidence": dict(Counter(product.primary_evidence for product in products).most_common()),
        "mapping_confidence": dict(Counter(product.mapping_confidence for product in products).most_common()),
        "mapping_status": dict(Counter(product.mapping_status for product in products).most_common()),
        "with_primary": sum(1 for product in products if product.primary_ingredients),
        "with_secondary": sum(1 for product in products if product.secondary_ingredients),
        "without_any_ingredient": sum(
            1 for product in products if not product.primary_ingredients and not product.secondary_ingredients
        ),
        "with_full_inci": sum(1 for product in products if product.ingredients),
        "with_brand": sum(1 for product in products if product.brand),
        "with_product_url": sum(1 for product in products if product.product_url),
        "mean_primary_count": round(
            sum(len(product.primary_ingredients) for product in products) / max(1, len(products)), 2
        ),
        "mean_secondary_count": round(
            sum(len(product.secondary_ingredients) for product in products) / max(1, len(products)), 2
        ),
    }


def main() -> int:
    import json

    if not CANONICAL_POPULATION.exists():
        print(f"Canonical population not found: {CANONICAL_POPULATION}", file=sys.stderr)
        return 1

    alias_map: dict[str, str] = {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import build_automated_scores as auto_scorer  # noqa: PLC0415

        _canonical_rows, alias_map, _labels, _originals = auto_scorer.read_ingredient_scores()
    except Exception as error:  # pragma: no cover - alias map is an enhancement, not a requirement
        print(f"Warning: could not load the ingredient score master ({error}).", file=sys.stderr)
        print("Falling back to mapping-only evidence for primary selection.", file=sys.stderr)

    products = load_canonical_products(alias_map)
    path = write_scoring_input(products)
    summary = coverage_summary(products)
    print(json.dumps(summary, indent=2))
    print(f"\nWrote scoring input: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
