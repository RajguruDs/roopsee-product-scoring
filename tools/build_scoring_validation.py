"""Build the scoring-quality validation artefacts (Steps 1-4).

Produces, in outputs/roopsee_canonical/:

  scoring_quality_validation_sample.csv   the fixed, stratified product sample
  scoring_quality_profiles.json           the validation profiles, as scored
  scoring_quality_predictions.csv         product x profile, both systems, doctor blank
  doctor_validation_template.xlsx         the review workbook for the doctor
  scoring_quality_validation_meta.json    version and provenance

Nothing here scores anything. Scores are produced by the shipped engine through
tools/score_profiles.mjs. Nothing here writes to data/ -- the production and
reference layers are read-only to this framework.

    python tools/build_scoring_validation.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring_validation as sv  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_OUTPUT_DIR = Path(
    os.environ.get("ROOPSEE_CANONICAL_OUTPUT_DIR", REPO_ROOT / "outputs" / "roopsee_canonical")
)

SCORED_POPULATION = CANONICAL_OUTPUT_DIR / "scored_population_full.json"
SCORING_INPUT = CANONICAL_OUTPUT_DIR / "roopsee_final_scoring_input.csv"
# The legacy FULL scored population, not the shipped legacy dataset. The shipped
# one was High-confidence-only, which overlaps just 32% of the canonical
# catalogue by name; the full population overlaps ~63% and gives the old-vs-new
# comparison a usable number of shared products. Build it with:
#
#   ROOPSEE_POPULATION_SOURCE=legacy ROOPSEE_AUTO_OUTPUT_DIR=outputs/legacy_validation \
#     python tools/build_automated_scores.py
#   ROOPSEE_AUTO_PAYLOAD=outputs/legacy_validation/automated_scoring_payload.json \
#   ROOPSEE_POPULATION_SOURCE=legacy ROOPSEE_CANONICAL_OUTPUT_DIR=outputs/legacy_validation \
#   ROOPSEE_ONBOARD_LIMIT=none ROOPSEE_ONBOARD_CONFIDENCE=High,Medium,Low \
#   ROOPSEE_FINAL_DATASET=outputs/legacy_validation/final_scored_products.json \
#     python tools/build_final_platform_dataset.py
LEGACY_DATASET = Path(
    os.environ.get(
        "ROOPSEE_LEGACY_DATASET", REPO_ROOT / "outputs" / "legacy_validation" / "scored_population_full.json"
    )
)

SAMPLE_PATH = CANONICAL_OUTPUT_DIR / "scoring_quality_validation_sample.csv"
PROFILES_PATH = CANONICAL_OUTPUT_DIR / "scoring_quality_profiles.json"
PREDICTIONS_PATH = CANONICAL_OUTPUT_DIR / "scoring_quality_predictions.csv"
DOCTOR_TEMPLATE_PATH = CANONICAL_OUTPUT_DIR / "doctor_validation_template.xlsx"
DOCTOR_TEMPLATE_CSV = CANONICAL_OUTPUT_DIR / "doctor_validation_template.csv"
META_PATH = CANONICAL_OUTPUT_DIR / "scoring_quality_validation_meta.json"

# Fixed seed. Changing it changes the sample, which invalidates comparison with
# any previous run -- bump VALIDATION_SAMPLE_VERSION if you ever do.
SAMPLE_SEED = "roopsee-scoring-validation-canonical_v1"

DEFAULT_SAMPLE_SIZE = 150
DEFAULT_PROFILES_PER_PRODUCT = 3

SAFETY_COLUMNS = ["Pregnancy Score", "Breastfeeling Score", "<16", "Excessive Dryness score"]


def stable_order_key(canonical_id: str) -> str:
    """Deterministic, seed-dependent ordering that does not follow file order.

    A plain sort would bias the sample toward whatever the CSV happens to list
    first (one brand, one retailer). Hashing spreads it while staying exactly
    reproducible for a given seed.
    """
    return hashlib.sha256(f"{SAMPLE_SEED}:{canonical_id}".encode("utf-8")).hexdigest()


def norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def anchor_band(support: int) -> str:
    if support >= 8:
        return "strong_anchor"
    if support >= 3:
        return "medium_anchor"
    if support >= 1:
        return "thin_anchor"
    return "no_anchor"


def score_band(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 65:
        return "high_evidence"
    if value >= 55:
        return "mid_evidence"
    return "low_evidence"


def load_scored_population() -> tuple[list[dict[str, Any]], list[str]]:
    if not SCORED_POPULATION.exists():
        raise SystemExit(
            f"Missing {SCORED_POPULATION}.\nRun: python tools/build_final_platform_dataset.py"
        )
    data = json.loads(SCORED_POPULATION.read_text(encoding="utf-8"))
    return data["products"], data["scoreColumns"]


def load_scoring_input() -> dict[str, dict[str, str]]:
    if not SCORING_INPUT.exists():
        return {}
    with SCORING_INPUT.open(newline="", encoding="utf-8-sig") as handle:
        return {row["canonical_product_id_v2"]: row for row in csv.DictReader(handle)}


def blocked_columns(product: dict[str, Any], score_columns: list[str]) -> set[str]:
    """Safety columns where at least one layer hard-blocks.

    Per layer, not on the blend: customerFacingScore() blocks when any single
    feature is -100 (static/app.js:461), so averaging first would hide it.
    """
    index_of = {column: index for index, column in enumerate(score_columns)}
    found: set[str] = set()
    for column in SAFETY_COLUMNS:
        index = index_of.get(column)
        if index is None:
            continue
        for values in (product.get("scoreLayers") or {}).values():
            if index < len(values) and float(values[index]) <= sv.HARD_BLOCK:
                found.add(column)
                break
    return found


def build_sample(
    products: list[dict[str, Any]],
    score_columns: list[str],
    scoring_input: dict[str, dict[str, str]],
    target_size: int,
) -> list[dict[str, Any]]:
    """Deterministic stratified sample across the dimensions that drive scoring.

    Strata are (product type x doctor-anchor band x ingredient-mapping
    confidence). Safety cases are force-included first, because they are rare,
    they are the historical failure area, and proportional sampling alone would
    under-represent them.
    """
    enriched: list[dict[str, Any]] = []
    for product in products:
        canonical_id = product["uid"]
        source = scoring_input.get(canonical_id, {})
        support = product.get("support") or {}
        blocked = blocked_columns(product, score_columns)
        enriched.append(
            {
                "product": product,
                "canonical_id": canonical_id,
                "order_key": stable_order_key(canonical_id),
                "product_type": product.get("normalizedType", "other"),
                "confidence": product.get("confidence", ""),
                "mapping_confidence": source.get("mapping_confidence", ""),
                "primary_evidence": source.get("primary_evidence", ""),
                "anchor_band": anchor_band(int(support.get("anchor") or 0)),
                "score_band": score_band(product.get("evidenceQuality")),
                "families": product.get("families") or [],
                "blocked_columns": sorted(blocked),
                "eligibility": product.get("eligibilityStatus", ""),
            }
        )

    chosen: dict[str, dict[str, Any]] = {}
    reasons: dict[str, list[str]] = defaultdict(list)

    def take(entry: dict[str, Any], reason: str) -> None:
        canonical_id = entry["canonical_id"]
        if canonical_id not in chosen:
            chosen[canonical_id] = entry
        if reason not in reasons[canonical_id]:
            reasons[canonical_id].append(reason)

    # 1. Guarantee safety cases: products hard-blocked on each safety column,
    #    spread across product types so one type cannot monopolise them.
    per_safety_column = max(3, target_size // 20)
    for column in SAFETY_COLUMNS:
        candidates = sorted(
            (entry for entry in enriched if column in entry["blocked_columns"]),
            key=lambda entry: (entry["product_type"], entry["order_key"]),
        )
        seen_types: Counter[str] = Counter()
        for entry in candidates:
            if len([r for r in reasons.values() if f"safety:{column}" in r]) >= per_safety_column:
                break
            if seen_types[entry["product_type"]] >= 2:
                continue
            seen_types[entry["product_type"]] += 1
            take(entry, f"safety:{column}")

    # 2. Guarantee the awkward cases: no anchor support, and weak ingredient
    #    mapping. These are where the engine is least certain.
    for label, predicate in [
        ("no_anchor_support", lambda e: e["anchor_band"] == "no_anchor"),
        ("mapping_review", lambda e: e["mapping_confidence"].upper() == "REVIEW"),
        ("mapping_medium", lambda e: e["mapping_confidence"].upper() == "MEDIUM"),
        ("inci_fallback_primary", lambda e: e["primary_evidence"] == "full_inci_fallback"),
        ("name_hero_primary", lambda e: e["primary_evidence"] == "product_name_hero"),
    ]:
        candidates = sorted((e for e in enriched if predicate(e)), key=lambda e: e["order_key"])
        for entry in candidates[: max(3, target_size // 25)]:
            take(entry, label)

    # 3. Proportional fill by product type, round-robin across the
    #    anchor x mapping-confidence cells inside each type.
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in enriched:
        by_type[entry["product_type"]].append(entry)

    total = len(enriched)
    for product_type in sorted(by_type):
        group = by_type[product_type]
        quota = max(4, round(target_size * len(group) / total))
        cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for entry in group:
            cells[(entry["anchor_band"], entry["mapping_confidence"] or "UNKNOWN")].append(entry)
        for cell in cells.values():
            cell.sort(key=lambda entry: entry["order_key"])

        taken_here = sum(1 for entry in chosen.values() if entry["product_type"] == product_type)
        cell_keys = sorted(cells)
        position = 0
        while taken_here < quota and cell_keys:
            progressed = False
            for cell_key in list(cell_keys):
                if taken_here >= quota:
                    break
                cell = cells[cell_key]
                if position >= len(cell):
                    continue
                entry = cell[position]
                progressed = True
                if entry["canonical_id"] not in chosen:
                    take(entry, f"stratum:{product_type}/{cell_key[0]}/{cell_key[1]}")
                    taken_here += 1
            if not progressed:
                break
            position += 1

    sample = []
    for canonical_id, entry in chosen.items():
        entry = dict(entry)
        entry["selection_reasons"] = reasons[canonical_id]
        sample.append(entry)

    # Stable final ordering, independent of the order strata happened to fill.
    sample.sort(key=lambda entry: entry["order_key"])
    return sample


SAMPLE_HEADERS = [
    "canonical_product_id_v2",
    "gtin",
    "product_name",
    "brand",
    "product_type",
    "category",
    "confidence",
    "mapping_confidence",
    "primary_evidence",
    "primary_ingredients",
    "secondary_ingredients",
    "families",
    "anchor_support",
    "exact_support",
    "anchor_band",
    "evidence_quality",
    "eligibility_status",
    "safety_blocked_columns",
    "mrp",
    "selling_price",
    "product_url",
    "validation_strata",
]


def write_sample(sample: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SAMPLE_HEADERS)
        for entry in sample:
            product = entry["product"]
            support = product.get("support") or {}
            writer.writerow(
                [
                    entry["canonical_id"],
                    product.get("gtin", ""),
                    product.get("name", ""),
                    product.get("brand", ""),
                    entry["product_type"],
                    product.get("category", ""),
                    entry["confidence"],
                    entry["mapping_confidence"],
                    entry["primary_evidence"],
                    product.get("primaryIngredients", ""),
                    product.get("secondaryIngredients", ""),
                    ", ".join(entry["families"]),
                    support.get("anchor", ""),
                    support.get("exact", ""),
                    entry["anchor_band"],
                    product.get("evidenceQuality", ""),
                    entry["eligibility"],
                    "; ".join(entry["blocked_columns"]),
                    product.get("mrp", ""),
                    product.get("sellingPrice", ""),
                    product.get("productUrl", ""),
                    "; ".join(entry["selection_reasons"]),
                ]
            )


def run_scorer(dataset_path: Path, profiles_path: Path, out_path: Path, system: str, uids_path: Path | None) -> dict:
    """Score through the shipped engine (static/app.js) via Node."""
    if shutil.which("node") is None:
        raise SystemExit(
            "node is required: the validation scores products with the shipped static/app.js\n"
            "rather than a second Python reimplementation of the scoring engine."
        )
    command = [
        "node",
        str(REPO_ROOT / "tools" / "score_profiles.mjs"),
        "--dataset", str(dataset_path),
        "--profiles", str(profiles_path),
        "--out", str(out_path),
        "--system", system,
    ]
    if uids_path is not None:
        command += ["--uids", str(uids_path)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"score_profiles.mjs failed for {system}:\n{result.stderr}")
    print(f"  {result.stdout.strip()}")
    return json.loads(out_path.read_text(encoding="utf-8"))


PREDICTION_HEADERS = [
    "validation_version",
    "canonical_product_id_v2",
    "gtin",
    "product_name",
    "brand",
    "product_type",
    "category",
    "profile_id",
    "profile_label",
    "skin_type",
    "sensitive",
    "age",
    "concern",
    "special_conditions",
    "legacy_score",
    "legacy_bin",
    "legacy_hard_blocked",
    "legacy_match_method",
    "canonical_v2_score",
    "canonical_v2_bin",
    "canonical_v2_label",
    "canonical_v2_evidence_score",
    "canonical_v2_hard_blocked",
    "canonical_v2_rank_overall",
    "doctor_score",
    "doctor_recommendation",
    "doctor_notes",
    "confidence",
    "mapping_confidence",
    "primary_evidence",
    "primary_ingredients",
    "secondary_ingredients",
    "families",
    "anchor_support",
    "exact_support",
    "family_support",
    "layer_baseline",
    "layer_v2",
    "layer_anchor",
    "layer_type_family",
    "layer_type",
    "direct_fit",
    "safety_cap",
    "safety_notes",
    "validation_strata",
]


def build_predictions(
    sample: list[dict[str, Any]],
    canonical_rows: list[dict[str, Any]],
    legacy_rows: list[dict[str, Any]],
    legacy_link: dict[str, str],
) -> list[dict[str, Any]]:
    canonical_by_key = {(row["uid"], row["profile_id"]): row for row in canonical_rows}
    legacy_by_key = {(row["uid"], row["profile_id"]): row for row in legacy_rows}
    sample_by_id = {entry["canonical_id"]: entry for entry in sample}

    predictions: list[dict[str, Any]] = []
    for entry in sample:
        product = entry["product"]
        canonical_id = entry["canonical_id"]
        legacy_uid = legacy_link.get(canonical_id)
        for profile in sv.VALIDATION_PROFILES:
            canonical = canonical_by_key.get((canonical_id, profile["profile_id"]))
            if canonical is None:
                continue
            legacy = legacy_by_key.get((legacy_uid, profile["profile_id"])) if legacy_uid else None
            legacy_score = legacy["score"] if legacy else None
            predictions.append(
                {
                    "validation_version": sv.SCORING_VALIDATION_VERSION,
                    "canonical_product_id_v2": canonical_id,
                    "gtin": product.get("gtin", ""),
                    "product_name": product.get("name", ""),
                    "brand": product.get("brand", ""),
                    "product_type": entry["product_type"],
                    "category": product.get("category", ""),
                    "profile_id": profile["profile_id"],
                    "profile_label": profile["label"],
                    "skin_type": profile["skinType"],
                    "sensitive": "Yes" if profile["sensitive"] else "No",
                    "age": profile["age"],
                    "concern": profile["concern"],
                    "special_conditions": ", ".join(profile["specialConditions"]),
                    "legacy_score": legacy_score if legacy_score is not None else "",
                    "legacy_bin": sv.score_bin(legacy_score) if legacy_score is not None else "",
                    "legacy_hard_blocked": ("true" if legacy_score <= sv.HARD_BLOCK else "false")
                    if legacy_score is not None
                    else "",
                    "legacy_match_method": "normalised product name" if legacy_uid else "no legacy counterpart",
                    "canonical_v2_score": canonical["score"],
                    "canonical_v2_bin": sv.score_bin(canonical["score"]),
                    "canonical_v2_label": sv.score_label(canonical["score"]),
                    "canonical_v2_evidence_score": canonical["evidence_score"],
                    "canonical_v2_hard_blocked": "true" if canonical["hard_blocked"] else "false",
                    "canonical_v2_rank_overall": canonical["rank_overall"],
                    # Doctor fields are intentionally blank. Never auto-filled.
                    "doctor_score": "",
                    "doctor_recommendation": "",
                    "doctor_notes": "",
                    "confidence": entry["confidence"],
                    "mapping_confidence": entry["mapping_confidence"],
                    "primary_evidence": entry["primary_evidence"],
                    "primary_ingredients": product.get("primaryIngredients", ""),
                    "secondary_ingredients": product.get("secondaryIngredients", ""),
                    "families": ", ".join(entry["families"]),
                    "anchor_support": canonical.get("anchor_support", ""),
                    "exact_support": canonical.get("exact_support", ""),
                    "family_support": canonical.get("family_support", ""),
                    "layer_baseline": canonical["layer_baseline"],
                    "layer_v2": canonical["layer_v2"],
                    "layer_anchor": canonical["layer_anchor"],
                    "layer_type_family": canonical["layer_type_family"],
                    "layer_type": canonical["layer_type"],
                    "direct_fit": "true" if canonical["direct_fit"] else "false",
                    "safety_cap": canonical.get("safety_cap", ""),
                    "safety_notes": canonical.get("safety_notes", ""),
                    "validation_strata": "; ".join(entry["selection_reasons"]),
                }
            )
    return predictions


def select_review_rows(predictions: list[dict[str, Any]], per_product: int) -> list[dict[str, Any]]:
    """Choose the product x profile pairs actually worth a doctor's time.

    The full grid is ~2,000 rows, which no reviewer will complete honestly.
    Per product, prioritise: any safety profile it is exposed to, then the
    profile where the two systems disagree most (most informative for the
    comparison), then the strongest canonical score (most likely to be shown to
    a real customer). Deterministic throughout.
    """
    by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        by_product[row["canonical_product_id_v2"]].append(row)

    selected: list[dict[str, Any]] = []
    for canonical_id in sorted(by_product):
        rows = by_product[canonical_id]

        def disagreement(row: dict[str, Any]) -> float:
            if row["legacy_score"] == "":
                return -1.0
            return abs(float(row["legacy_score"]) - float(row["canonical_v2_score"]))

        picked: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(row: dict[str, Any], reason: str) -> None:
            if row["profile_id"] in seen or len(picked) >= per_product:
                return
            seen.add(row["profile_id"])
            row = dict(row)
            row["review_reason"] = reason
            picked.append(row)

        safety_rows = sorted(
            (r for r in rows if r["profile_id"] in sv.SAFETY_PROFILE_IDS),
            key=lambda r: (-disagreement(r), r["profile_id"]),
        )
        if safety_rows:
            add(safety_rows[0], "safety profile")

        for row in sorted(rows, key=lambda r: (-disagreement(r), r["profile_id"])):
            if disagreement(row) <= 0:
                break
            add(row, "largest legacy vs canonical disagreement")
            break

        for row in sorted(rows, key=lambda r: (-float(r["canonical_v2_score"]), r["profile_id"])):
            add(row, "highest canonical score for this product")
            if len(picked) >= per_product:
                break

        selected.extend(picked)
    return selected


DOCTOR_COLUMNS = [
    ("canonical_product_id_v2", "Product ID", 24),
    ("gtin", "GTIN", 16),
    ("product_name", "Product name", 52),
    ("brand", "Brand", 18),
    ("product_type", "Type", 13),
    ("primary_ingredients", "Primary ingredients", 34),
    ("secondary_ingredients", "Secondary ingredients", 40),
    ("skin_type", "Skin type", 13),
    ("sensitive", "Sensitive", 10),
    ("age", "Age", 8),
    ("concern", "Concern", 22),
    ("special_conditions", "Special condition", 18),
    ("legacy_score", "Legacy score", 12),
    ("canonical_v2_score", "Canonical v2 score", 17),
    ("doctor_score", "DOCTOR SCORE", 14),
    ("doctor_recommendation", "DOCTOR RECOMMENDATION", 22),
    ("doctor_notes", "DOCTOR NOTES", 46),
    ("review_reason", "Why this row", 32),
    ("profile_id", "Profile ID", 28),
]

INSTRUCTIONS = [
    ("Roopsee scoring validation — doctor review", True),
    ("", False),
    (f"Validation version: {sv.SCORING_VALIDATION_VERSION}", False),
    (f"Sample version: {sv.VALIDATION_SAMPLE_VERSION}    Profile version: {sv.VALIDATION_PROFILE_VERSION}", False),
    ("", False),
    ("What you are being asked to do", True),
    ("", False),
    ("Each row is one product judged for one specific user profile.", False),
    ("Score how suitable that product is for that exact profile, using your own clinical judgement.", False),
    ("", False),
    ("Please judge the product independently.", True),
    ("The two system scores are shown only so we can measure our engine against you.", False),
    ("Do NOT try to match them, and do not let them anchor your answer. If you disagree", False),
    ("with both, that disagreement is the single most valuable thing in this exercise.", False),
    ("", False),
    ("The score scale", True),
    ("", False),
    ("90-100   Excellent Match — you would actively recommend it for this profile", False),
    ("80-89    Great Match — a strong, safe choice", False),
    ("70-79    Good Match — usable, but not the best option", False),
    ("50-69    Fits with Caution — acceptable only with caveats", False),
    ("1-49     Not Recommended — poor fit for this profile", False),
    ("-100     Not suggested at all — unsafe or contraindicated for this profile", False),
    ("", False),
    ("Use -100 only for a genuine hard block: a product that must not be recommended", False),
    ("to this profile at all, for example a retinoid in pregnancy. A merely poor fit", False),
    ("belongs in 1-49, not -100. This distinction is what we measure safety against.", False),
    ("", False),
    ("DOCTOR RECOMMENDATION column", True),
    ("", False),
    ("One of: Recommend / Recommend with caution / Do not recommend / Unsafe for this profile", False),
    ("", False),
    ("DOCTOR NOTES column", True),
    ("", False),
    ("Free text. Most useful when you disagree with the system: a short reason why", False),
    ("(ingredient, concentration, formulation, safety, product format) helps us find", False),
    ("the cause. Leave blank when you have nothing to add.", False),
    ("", False),
    ("Interpreting suitability", True),
    ("", False),
    ("Judge the product as a whole for this profile: the actives and their likely", False),
    ("strength, the product format, whether the product type is a sensible answer to", False),
    ("the stated concern, and safety for any stated special condition.", False),
    ("", False),
    ("Ingredient lists shown here are the ones our system scored. If they look wrong", False),
    ("or incomplete for the product, please say so in the notes — that is itself a finding.", False),
    ("", False),
    ("Please leave the three DOCTOR columns as the only ones you edit.", True),
]


def write_doctor_template(rows: list[dict[str, Any]], xlsx_path: Path, csv_path: Path) -> str:
    """Write the doctor workbook. Falls back to CSV if openpyxl is unavailable."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([header for _key, header, _width in DOCTOR_COLUMNS])
        for row in rows:
            writer.writerow([row.get(key, "") for key, _header, _width in DOCTOR_COLUMNS])

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError:
        return "csv"

    workbook = Workbook()
    guide = workbook.active
    guide.title = "Instructions"
    guide.column_dimensions["A"].width = 96
    for index, (text, is_heading) in enumerate(INSTRUCTIONS, start=1):
        cell = guide.cell(row=index, column=1, value=text)
        cell.font = Font(bold=is_heading, size=12 if is_heading and index == 1 else 11)
        cell.alignment = Alignment(vertical="top")

    sheet = workbook.create_sheet("Validation")
    doctor_fill = PatternFill("solid", fgColor="FFF2CC")
    header_fill = PatternFill("solid", fgColor="DDEEE3")
    doctor_keys = {"doctor_score", "doctor_recommendation", "doctor_notes"}

    for column_index, (_key, header, width) in enumerate(DOCTOR_COLUMNS, start=1):
        cell = sheet.cell(row=1, column=column_index, value=header)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(column_index)].width = width

    for row_index, row in enumerate(rows, start=2):
        for column_index, (key, _header, _width) in enumerate(DOCTOR_COLUMNS, start=1):
            cell = sheet.cell(row=row_index, column=column_index, value=row.get(key, ""))
            cell.alignment = Alignment(vertical="top", wrap_text=key in {"product_name", "primary_ingredients", "secondary_ingredients"})
            if key in doctor_keys:
                cell.fill = doctor_fill

    score_column = get_column_letter([k for k, _h, _w in DOCTOR_COLUMNS].index("doctor_score") + 1)
    recommendation_column = get_column_letter(
        [k for k, _h, _w in DOCTOR_COLUMNS].index("doctor_recommendation") + 1
    )
    last_row = len(rows) + 1

    score_rule = DataValidation(
        type="decimal", operator="between", formula1="-100", formula2="100", allow_blank=True,
        error="Enter a score from 0 to 100, or -100 for a hard block.", errorTitle="Invalid score",
    )
    sheet.add_data_validation(score_rule)
    score_rule.add(f"{score_column}2:{score_column}{last_row}")

    recommendation_rule = DataValidation(
        type="list",
        formula1='"Recommend,Recommend with caution,Do not recommend,Unsafe for this profile"',
        allow_blank=True,
    )
    sheet.add_data_validation(recommendation_rule)
    recommendation_rule.add(f"{recommendation_column}2:{recommendation_column}{last_row}")

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(DOCTOR_COLUMNS))}{last_row}"

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(xlsx_path)
    return "xlsx"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the scoring-quality validation artefacts.")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--profiles-per-product", type=int, default=DEFAULT_PROFILES_PER_PRODUCT)
    args = parser.parse_args()

    warnings: list[str] = []
    print(f"Scoring validation {sv.SCORING_VALIDATION_VERSION}\n")

    products, score_columns = load_scored_population()
    scoring_input = load_scoring_input()
    if not scoring_input:
        warnings.append(f"{SCORING_INPUT.name} not found; mapping confidence will be blank in the sample.")
    print(f"Scored population: {len(products)} products")

    # Step 1
    sample = build_sample(products, score_columns, scoring_input, args.sample_size)
    write_sample(sample, SAMPLE_PATH)
    print(f"Sample: {len(sample)} products -> {SAMPLE_PATH.name}")

    # Step 2
    PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_PATH.write_text(json.dumps(sv.VALIDATION_PROFILES, indent=2), encoding="utf-8")
    print(f"Profiles: {len(sv.VALIDATION_PROFILES)} -> {PROFILES_PATH.name}")

    # Step 3 -- score both systems through the shipped engine.
    sample_ids = [entry["canonical_id"] for entry in sample]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        uids_file = tmp_path / "uids.json"
        uids_file.write_text(json.dumps(sample_ids), encoding="utf-8")

        # The canonical run needs an app.js-shaped dataset. The full scored
        # population carries extra fields, which the engine simply ignores.
        canonical_dataset = tmp_path / "canonical_dataset.json"
        canonical_dataset.write_text(
            json.dumps(
                {
                    "metadata": {
                        "visibleScoreWeights": {"baseline": 0.05, "v2": 0.30, "anchor": 0.55, "type_family": 0.05, "type": 0.05},
                        "rankFusionWeights": {"score": 0.20, "baseline_rank": 0.10, "v2_rank": 0.10, "anchor_rank": 0.55, "type_family_rank": 0.05},
                    },
                    "scoreColumns": score_columns,
                    "products": products,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        print("\nScoring through the shipped engine (static/app.js):")
        canonical_result = run_scorer(
            canonical_dataset, PROFILES_PATH, tmp_path / "canonical_scores.json", "canonical_v2", uids_file
        )

        legacy_rows: list[dict[str, Any]] = []
        legacy_link: dict[str, str] = {}
        if LEGACY_DATASET.exists():
            legacy_data = json.loads(LEGACY_DATASET.read_text(encoding="utf-8"))
            legacy_by_name: dict[str, str] = {}
            for product in legacy_data["products"]:
                legacy_by_name.setdefault(norm_key(product.get("name")), product["uid"])
            for entry in sample:
                match = legacy_by_name.get(norm_key(entry["product"].get("name")))
                if match:
                    legacy_link[entry["canonical_id"]] = match
            legacy_uids_file = tmp_path / "legacy_uids.json"
            legacy_uids_file.write_text(json.dumps(sorted(set(legacy_link.values()))), encoding="utf-8")
            legacy_result = run_scorer(
                LEGACY_DATASET, PROFILES_PATH, tmp_path / "legacy_scores.json", "legacy", legacy_uids_file
            )
            legacy_rows = legacy_result["rows"]
        else:
            warnings.append(
                f"Legacy dataset not found at {LEGACY_DATASET}. legacy_score will be blank everywhere, "
                "so no old-vs-new comparison is possible until it is restored."
            )

        canonical_rows = canonical_result["rows"]

    matched = len(legacy_link)
    coverage = round(100.0 * matched / max(1, len(sample)), 1)
    print(f"\nLegacy counterparts matched: {matched}/{len(sample)} sampled products ({coverage}%)")
    if matched < len(sample):
        warnings.append(
            f"{len(sample) - matched} of {len(sample)} sampled products have no legacy counterpart "
            f"({coverage}% coverage). The legacy population was keyed by product name and is a different "
            "catalogue, so old-vs-new comparison is only possible on the matched subset."
        )

    predictions = build_predictions(sample, canonical_rows, legacy_rows, legacy_link)
    PREDICTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PREDICTIONS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PREDICTION_HEADERS)
        writer.writeheader()
        writer.writerows(predictions)
    print(f"Predictions: {len(predictions)} product x profile rows -> {PREDICTIONS_PATH.name}")

    # Step 4
    review_rows = select_review_rows(predictions, args.profiles_per_product)
    template_format = write_doctor_template(review_rows, DOCTOR_TEMPLATE_PATH, DOCTOR_TEMPLATE_CSV)
    print(f"Doctor template: {len(review_rows)} rows -> {DOCTOR_TEMPLATE_PATH.name} ({template_format})")
    if template_format == "csv":
        warnings.append("openpyxl unavailable; the doctor template was written as CSV only.")

    with_legacy = sum(1 for row in predictions if row["legacy_score"] != "")
    meta = {
        "scoring_validation_version": sv.SCORING_VALIDATION_VERSION,
        "validation_sample_version": sv.VALIDATION_SAMPLE_VERSION,
        "validation_profile_version": sv.VALIDATION_PROFILE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_seed": SAMPLE_SEED,
        "sample_size": len(sample),
        "profile_count": len(sv.VALIDATION_PROFILES),
        "prediction_pairs": len(predictions),
        "prediction_pairs_with_legacy": with_legacy,
        "doctor_review_rows": len(review_rows),
        "profiles_per_product_in_template": args.profiles_per_product,
        "doctor_scores_present": False,
        "scored_population_source": str(SCORED_POPULATION.relative_to(REPO_ROOT).as_posix()),
        "legacy_dataset_source": str(LEGACY_DATASET) if LEGACY_DATASET.exists() else None,
        "legacy_match_method": "normalised product name",
        "legacy_matched_products": matched,
        "legacy_coverage_pct": coverage,
        "scoring_engine": "static/app.js executed via tools/score_profiles.mjs (no reimplementation)",
        "metric_methodology": {
            "buckets": "static/app.js SCORE_BINS",
            "labels": "roopsee_coverage/constants.py SCORE_LABELS",
            "hard_block": "score <= -100",
            "correlations": "Pearson and Spearman, pure Python, ties averaged",
        },
        "sample_product_types": dict(Counter(entry["product_type"] for entry in sample).most_common()),
        "sample_confidence": dict(Counter(entry["confidence"] for entry in sample).most_common()),
        "sample_mapping_confidence": dict(Counter(entry["mapping_confidence"] for entry in sample).most_common()),
        "sample_anchor_band": dict(Counter(entry["anchor_band"] for entry in sample).most_common()),
        "known_limitations": [
            "Shipped anchor logic uses top-12 at similarity >= 4.5; the leave-one-out validation in "
            "tools/validate_v2_automated_logic.py uses top-5 at >= 3.25. They were left divergent on "
            "purpose; this framework measures the SHIPPED configuration only.",
            "Legacy comparison is limited to products present in both catalogues, matched by "
            "normalised product name.",
            "Doctor scores are absent until the template is filled in; every metric is undefined until then.",
        ],
        "warnings": warnings,
    }
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print("\nSample product types:")
    for product_type, count in meta["sample_product_types"].items():
        print(f"  {product_type:14s} {count}")

    print(f"\nDoctor review required: {len(review_rows)} product x profile evaluations")
    print(f"(full prediction grid is {len(predictions)} pairs; the template is the reviewable subset)")

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  - {warning}")

    print(f"\nMeta -> {META_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
