"""Scoring-quality validation framework.

Measures how well a scoring system agrees with independent doctor judgement, and
compares the legacy system against the canonical v2 system on the same products,
the same profiles and the same doctor answers.

This module MEASURES. It never scores and never tunes. Scores come from the
shipped engine via tools/score_profiles.mjs, which executes static/app.js itself.

Three data layers are kept strictly separate:

  production   canonical_scoring_population_v2.csv, the ingredient master
  reference    data/products.csv -- the 384 doctor anchors the engine scores against
  validation   the sample, the predictions, and the doctor answers in this framework

Validation doctor scores are never fed back as anchors. If they were, the engine
would be scored against its own inputs and every metric here would be circular.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]

# Bump when the sample, the profiles, or the metric methodology change in a way
# that makes a new run non-comparable with an old one.
SCORING_VALIDATION_VERSION = "canonical_v1"
VALIDATION_SAMPLE_VERSION = "sample_v1"
VALIDATION_PROFILE_VERSION = "profiles_v1"

HARD_BLOCK = -100


# --------------------------------------------------------------------------
# Score buckets -- mirrors of what the product already uses. Not new buckets.
# --------------------------------------------------------------------------

# static/app.js:85-92 (SCORE_BINS) -- what the UI actually shows.
SCORE_BINS: list[tuple[str, Any]] = [
    ("90-100", lambda score: score >= 90),
    ("80-89", lambda score: 80 <= score < 90),
    ("70-79", lambda score: 70 <= score < 80),
    ("50-69", lambda score: 50 <= score < 70),
    ("1-49", lambda score: HARD_BLOCK < score < 50),
    ("blocked", lambda score: score <= HARD_BLOCK),
]

BIN_ORDER = ["blocked", "1-49", "50-69", "70-79", "80-89", "90-100"]

# roopsee_coverage/constants.py:118-124 (SCORE_LABELS) -- the doctor-facing wording.
SCORE_LABELS: list[tuple[int, str]] = [
    (90, "Excellent Match"),
    (80, "Great Match"),
    (70, "Good Match"),
    (50, "Fits with Caution"),
    (-999, "Not Recommended"),
]


def score_bin(score: float | None) -> str:
    if score is None:
        return ""
    for key, test in SCORE_BINS:
        if test(score):
            return key
    return "1-49"


def score_label(score: float | None) -> str:
    if score is None:
        return ""
    if score <= HARD_BLOCK:
        return "Not Recommended"
    for threshold, label in SCORE_LABELS:
        if score >= threshold:
            return label
    return "Not Recommended"


def bin_distance(left: float | None, right: float | None) -> int | None:
    """How many buckets apart two scores are, on the ordered bucket scale."""
    if left is None or right is None:
        return None
    return abs(BIN_ORDER.index(score_bin(left)) - BIN_ORDER.index(score_bin(right)))


def is_hard_blocked(score: float | None) -> bool | None:
    return None if score is None else score <= HARD_BLOCK


# --------------------------------------------------------------------------
# Validation profiles (Step 2)
# --------------------------------------------------------------------------
#
# Field names and values match static/app.js `state` exactly (app.js:94-107), so
# they can be handed to the shipped scorer untouched. `sensitive` is a BOOLEAN
# there, not "Yes"/"No" -- a string would be truthy and silently make every
# profile sensitive.
#
# Concerns must be verbatim score-column names: profileLayerScore() uses the
# concern label directly as a column key (app.js:226, :591).

VALIDATION_PROFILES: list[dict[str, Any]] = [
    {
        "profile_id": "P01_oily_acne",
        "label": "Oily skin, acne",
        "skinType": "Oily",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Acne",
        "specialConditions": ["None"],
        "rationale": "Highest-volume acne case; the core commercial profile.",
    },
    {
        "profile_id": "P02_oily_open_pores",
        "label": "Oily skin, open pores",
        "skinType": "Oily",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Open Pores",
        "specialConditions": ["None"],
        "rationale": "Exercises the acne/exfoliant/clay family logic without acne itself.",
    },
    {
        "profile_id": "P03_dry_dehydration",
        "label": "Dry skin, dehydration",
        "skinType": "Dry",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Dehydration",
        "specialConditions": ["None"],
        "rationale": "Hydration family; moisturizer-led scoring.",
    },
    {
        "profile_id": "P04_dry_excessive_dryness",
        "label": "Dry skin, barrier repair, excessive dryness",
        "skinType": "Dry",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Barrier Repair",
        "specialConditions": ["Excessive Dryness"],
        "rationale": "Known failure area: the excessive-dryness gate and the dryness rescue path.",
    },
    {
        "profile_id": "P05_sensitive_redness",
        "label": "Sensitive skin, redness and irritation",
        "skinType": "Normal",
        "sensitive": True,
        "age": "Adult",
        "gender": "female",
        "concern": "Redness/Irritation",
        "specialConditions": ["None"],
        "rationale": "Sensitivity picks the +Sensitive skin column and sets profile risk.",
    },
    {
        "profile_id": "P06_combination_pigmentation",
        "label": "Combination skin, dark spots and pigmentation",
        "skinType": "Combination",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Dark Spots/Pigmentation",
        "specialConditions": ["None"],
        "rationale": "Brightening family and the sunscreen photo-concern relevance path.",
    },
    {
        "profile_id": "P07_normal_uneven_tone",
        "label": "Normal skin, uneven skin tone",
        "skinType": "Normal",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Uneven Skin Tone",
        "specialConditions": ["None"],
        "rationale": "Baseline low-risk profile; a control against the risk profiles.",
    },
    {
        "profile_id": "P08_normal_wrinkles",
        "label": "Normal skin, wrinkles and fine lines",
        "skinType": "Normal",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Wrinkles/Fine lines",
        "specialConditions": ["None"],
        "rationale": "Retinoid/anti-aging families and the cleanser wrinkles exception.",
    },
    {
        "profile_id": "P09_teen_acne",
        "label": "Teen, oily skin, acne",
        "skinType": "Oily",
        "sensitive": False,
        "age": "Teen",
        "gender": "female",
        "concern": "Acne",
        "specialConditions": ["None"],
        "rationale": "Known failure area: the under-16 safety gate.",
    },
    {
        "profile_id": "P10_pregnancy_acne",
        "label": "Pregnant, oily skin, acne",
        "skinType": "Oily",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Acne",
        "specialConditions": ["Pregnant"],
        "rationale": "Known failure area: pregnancy false-safe and false-block cases.",
    },
    {
        "profile_id": "P11_breastfeeding_pigmentation",
        "label": "Breastfeeding, pigmentation",
        "skinType": "Normal",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Dark Spots/Pigmentation",
        "specialConditions": ["Breastfeeding"],
        "rationale": "Lactation safety against a brightening/retinoid-adjacent concern.",
    },
    {
        "profile_id": "P12_sensitive_dry_barrier",
        "label": "Sensitive dry skin, barrier repair",
        "skinType": "Dry",
        "sensitive": True,
        "age": "Adult",
        "gender": "female",
        "concern": "Barrier Repair",
        "specialConditions": ["None"],
        "rationale": "Compromised barrier without the special-condition flag; separates the two paths.",
    },
    {
        "profile_id": "P13_combination_dullness",
        "label": "Combination skin, dullness",
        "skinType": "Combination",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "Dullness",
        "specialConditions": ["None"],
        "rationale": "Partial-relevance path for sunscreen and the brightening family.",
    },
    {
        "profile_id": "P14_oily_no_concern",
        "label": "Oily skin, no specific concern",
        "skinType": "Oily",
        "sensitive": False,
        "age": "Adult",
        "gender": "female",
        "concern": "None",
        "specialConditions": ["None"],
        "rationale": "Exercises the None fallback column and the serum concern fallback of 60.",
    },
]

# Profiles that exist specifically to test a safety gate.
SAFETY_PROFILE_IDS = {
    "P04_dry_excessive_dryness",
    "P09_teen_acne",
    "P10_pregnancy_acne",
    "P11_breastfeeding_pigmentation",
}


def profile_by_id(profile_id: str) -> dict[str, Any] | None:
    for profile in VALIDATION_PROFILES:
        if profile["profile_id"] == profile_id:
            return profile
    return None


# --------------------------------------------------------------------------
# Metric primitives -- pure Python, no numpy/scipy (not repo dependencies).
# Every one returns None rather than raising when there is nothing to measure.
# --------------------------------------------------------------------------


def _paired(system: Sequence[float | None], doctor: Sequence[float | None]) -> list[tuple[float, float]]:
    """Keep only pairs where both sides have a value."""
    return [
        (float(left), float(right))
        for left, right in zip(system, doctor)
        if left is not None and right is not None
    ]


def mean_absolute_error(system: Sequence[float | None], doctor: Sequence[float | None]) -> float | None:
    pairs = _paired(system, doctor)
    if not pairs:
        return None
    return round(sum(abs(a - b) for a, b in pairs) / len(pairs), 3)


def median_absolute_error(system: Sequence[float | None], doctor: Sequence[float | None]) -> float | None:
    pairs = _paired(system, doctor)
    if not pairs:
        return None
    return round(statistics.median([abs(a - b) for a, b in pairs]), 3)


def within_tolerance(
    system: Sequence[float | None], doctor: Sequence[float | None], tolerance: float
) -> float | None:
    """Percentage of pairs within +/- tolerance points."""
    pairs = _paired(system, doctor)
    if not pairs:
        return None
    hits = sum(1 for a, b in pairs if abs(a - b) <= tolerance)
    return round(100.0 * hits / len(pairs), 2)


def pearson_correlation(system: Sequence[float | None], doctor: Sequence[float | None]) -> float | None:
    pairs = _paired(system, doctor)
    if len(pairs) < 2:
        return None
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    numerator = sum(a * b for a, b in zip(dx, dy))
    denominator = math.sqrt(sum(a * a for a in dx)) * math.sqrt(sum(b * b for b in dy))
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Ranks with ties averaged, which is what Spearman requires."""
    indexed = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position
        while end + 1 < len(indexed) and values[indexed[end + 1]] == values[indexed[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for offset in range(position, end + 1):
            ranks[indexed[offset]] = average
        position = end + 1
    return ranks


def spearman_correlation(system: Sequence[float | None], doctor: Sequence[float | None]) -> float | None:
    pairs = _paired(system, doctor)
    if len(pairs) < 2:
        return None
    xs = _average_ranks([a for a, _ in pairs])
    ys = _average_ranks([b for _, b in pairs])
    return pearson_correlation(xs, ys)


def same_bucket_rate(system: Sequence[float | None], doctor: Sequence[float | None]) -> float | None:
    pairs = _paired(system, doctor)
    if not pairs:
        return None
    hits = sum(1 for a, b in pairs if score_bin(a) == score_bin(b))
    return round(100.0 * hits / len(pairs), 2)


def bucket_distance_rate(
    system: Sequence[float | None], doctor: Sequence[float | None], distance: int, at_least: bool = False
) -> float | None:
    pairs = _paired(system, doctor)
    if not pairs:
        return None
    hits = 0
    for a, b in pairs:
        gap = bin_distance(a, b) or 0
        if (gap >= distance) if at_least else (gap == distance):
            hits += 1
    return round(100.0 * hits / len(pairs), 2)


def hard_block_metrics(
    system: Sequence[float | None], doctor: Sequence[float | None]
) -> dict[str, float | int | None]:
    """Agreement on the block decision, plus the two asymmetric failure kinds.

    false_safe  -- the doctor blocked it, the system did not. The dangerous one.
    false_block -- the system blocked it, the doctor did not. Costs coverage.
    """
    pairs = _paired(system, doctor)
    if not pairs:
        return {
            "hard_block_agreement_pct": None,
            "false_safe_count": None,
            "false_safe_pct": None,
            "false_block_count": None,
            "false_block_pct": None,
            "doctor_blocked_count": None,
            "system_blocked_count": None,
            "pairs": 0,
        }
    agree = false_safe = false_block = doctor_blocked = system_blocked = 0
    for system_score, doctor_score in pairs:
        system_block = system_score <= HARD_BLOCK
        doctor_block = doctor_score <= HARD_BLOCK
        doctor_blocked += int(doctor_block)
        system_blocked += int(system_block)
        if system_block == doctor_block:
            agree += 1
        elif doctor_block and not system_block:
            false_safe += 1
        else:
            false_block += 1
    total = len(pairs)
    return {
        "hard_block_agreement_pct": round(100.0 * agree / total, 2),
        "false_safe_count": false_safe,
        "false_safe_pct": round(100.0 * false_safe / total, 2),
        "false_block_count": false_block,
        "false_block_pct": round(100.0 * false_block / total, 2),
        "doctor_blocked_count": doctor_blocked,
        "system_blocked_count": system_blocked,
        "pairs": total,
    }


def metric_suite(system: Sequence[float | None], doctor: Sequence[float | None]) -> dict[str, Any]:
    """Every Step 5 metric for one system against the doctor answers."""
    suite: dict[str, Any] = {
        "pairs": len(_paired(system, doctor)),
        "mae": mean_absolute_error(system, doctor),
        "median_ae": median_absolute_error(system, doctor),
        "within_5_pct": within_tolerance(system, doctor, 5),
        "within_10_pct": within_tolerance(system, doctor, 10),
        "within_20_pct": within_tolerance(system, doctor, 20),
        "pearson": pearson_correlation(system, doctor),
        "spearman": spearman_correlation(system, doctor),
        "same_bucket_pct": same_bucket_rate(system, doctor),
        "one_bucket_pct": bucket_distance_rate(system, doctor, 1),
        "large_bucket_disagreement_pct": bucket_distance_rate(system, doctor, 2, at_least=True),
    }
    suite.update(hard_block_metrics(system, doctor))
    return suite


# Metrics where a LOWER value is the better result.
LOWER_IS_BETTER = {
    "mae",
    "median_ae",
    "large_bucket_disagreement_pct",
    "false_safe_count",
    "false_safe_pct",
    "false_block_count",
    "false_block_pct",
}

# Metrics that are descriptive context rather than a quality judgement.
NEUTRAL_METRICS = {"pairs", "doctor_blocked_count", "system_blocked_count"}


def compare_metric(name: str, legacy: Any, canonical: Any) -> dict[str, Any]:
    """Legacy vs canonical for one metric, with an explicit winner.

    Returns 'insufficient data' rather than a winner when either side is missing,
    so an absent doctor score can never be read as a result.
    """
    if legacy is None or canonical is None:
        return {"metric": name, "legacy": legacy, "canonical_v2": canonical, "delta": None, "better": "insufficient data"}
    delta = round(canonical - legacy, 4)
    if name in NEUTRAL_METRICS:
        return {"metric": name, "legacy": legacy, "canonical_v2": canonical, "delta": delta, "better": "n/a"}
    if delta == 0:
        better = "tie"
    elif name in LOWER_IS_BETTER:
        better = "canonical_v2" if delta < 0 else "legacy"
    else:
        better = "canonical_v2" if delta > 0 else "legacy"
    return {"metric": name, "legacy": legacy, "canonical_v2": canonical, "delta": delta, "better": better}


def compare_suites(legacy: dict[str, Any], canonical: dict[str, Any]) -> list[dict[str, Any]]:
    order = [
        "pairs",
        "mae",
        "median_ae",
        "within_5_pct",
        "within_10_pct",
        "within_20_pct",
        "pearson",
        "spearman",
        "same_bucket_pct",
        "one_bucket_pct",
        "large_bucket_disagreement_pct",
        "hard_block_agreement_pct",
        "false_safe_count",
        "false_safe_pct",
        "false_block_count",
        "false_block_pct",
    ]
    return [compare_metric(name, legacy.get(name), canonical.get(name)) for name in order]


# --------------------------------------------------------------------------
# Ranking metrics (Step 7)
# --------------------------------------------------------------------------


def top_n_overlap(system_ranked: Sequence[str], doctor_ranked: Sequence[str], n: int) -> float | None:
    """Share of the doctor's top n that the system also puts in its top n."""
    if not system_ranked or not doctor_ranked:
        return None
    doctor_top = list(doctor_ranked[:n])
    if not doctor_top:
        return None
    system_top = set(system_ranked[:n])
    hits = sum(1 for uid in doctor_top if uid in system_top)
    return round(100.0 * hits / len(doctor_top), 2)


def top_1_agreement(system_ranked: Sequence[str], doctor_ranked: Sequence[str]) -> bool | None:
    if not system_ranked or not doctor_ranked:
        return None
    return system_ranked[0] == doctor_ranked[0]


def unsuitable_in_top_n(
    system_ranked: Sequence[str], doctor_scores: dict[str, float], n: int, threshold: float = 50
) -> int | None:
    """Products the system ranks highly that the doctor scored as a poor fit."""
    if not system_ranked:
        return None
    counted = 0
    for uid in system_ranked[:n]:
        score = doctor_scores.get(uid)
        if score is not None and score < threshold:
            counted += 1
    return counted


def missed_strong_recommendations(
    system_ranked: Sequence[str], doctor_scores: dict[str, float], n: int, threshold: float = 80
) -> list[str]:
    """Products the doctor rated strongly that the system left outside its top n."""
    system_top = set(system_ranked[:n])
    return sorted(
        uid for uid, score in doctor_scores.items() if score >= threshold and uid not in system_top
    )


# --------------------------------------------------------------------------
# Failure classification (Step 8)
# --------------------------------------------------------------------------

FAILURE_CATEGORIES = [
    "ingredient mapping",
    "primary/secondary selection",
    "ingredient score",
    "product type",
    "doctor anchor",
    "similarity",
    "concern logic",
    "skin type logic",
    "safety logic",
    "confidence/cap",
    "calibration",
    "ranking",
    "data quality",
    "unknown",
]


def classify_failure(row: dict[str, Any]) -> tuple[str, str]:
    """Suggest a cause for one large error. A hypothesis, never a diagnosis.

    Ordered most-specific first. Returns (category, note).
    """

    def number(key: str) -> float | None:
        value = row.get(key)
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    doctor = number("doctor_score")
    canonical = number("canonical_v2_score")
    anchor_support = number("anchor_support") or 0
    exact_support = number("exact_support") or 0
    mapping_confidence = str(row.get("mapping_confidence") or "").upper()
    confidence = str(row.get("confidence") or "")
    special = str(row.get("special_conditions") or "")
    age = str(row.get("age") or "")
    has_special = special not in ("", "None") or age == "Teen"

    if doctor is None or canonical is None:
        return "unknown", "no doctor score to compare against"

    doctor_blocked = doctor <= HARD_BLOCK
    system_blocked = canonical <= HARD_BLOCK

    if doctor_blocked != system_blocked:
        if has_special:
            return "safety logic", "block decision disagrees on a profile with a safety gate"
        return "safety logic", "block decision disagrees with no safety gate active"

    if not row.get("primary_ingredients"):
        return "primary/secondary selection", "no primary ingredient was selected for this product"

    if mapping_confidence in {"REVIEW", "MEDIUM"}:
        return "ingredient mapping", f"ingredient mapping confidence is {mapping_confidence}"

    if exact_support == 0:
        return "ingredient mapping", "no exact ingredient matched the score master"

    if anchor_support == 0:
        return "doctor anchor", "no doctor anchor was close enough to borrow from"
    if anchor_support < 3:
        return "doctor anchor", f"thin doctor-anchor support ({int(anchor_support)} of a maximum 12)"

    if confidence in {"Low", "Medium"}:
        return "confidence/cap", f"{confidence} confidence caps the achievable score"

    if has_special:
        return "safety logic", "large error on a profile with a safety gate active"

    if doctor is not None and canonical is not None and abs(doctor - canonical) >= 20:
        return "calibration", "large numeric gap with clean ingredients, anchors and confidence"

    return "unknown", "no single dominant signal"


# --------------------------------------------------------------------------
# Breakdowns (Step 6)
# --------------------------------------------------------------------------


def breakdown(
    rows: Iterable[dict[str, Any]],
    key: str,
    legacy_field: str = "legacy_score",
    canonical_field: str = "canonical_v2_score",
    doctor_field: str = "doctor_score",
) -> list[dict[str, Any]]:
    """Per-group metrics for both systems. Aggregates hide exactly this."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key) or "(none)"), []).append(row)

    def numbers(group: list[dict[str, Any]], field: str) -> list[float | None]:
        output: list[float | None] = []
        for row in group:
            value = row.get(field)
            if value in (None, ""):
                output.append(None)
                continue
            try:
                output.append(float(value))
            except (TypeError, ValueError):
                output.append(None)
        return output

    results: list[dict[str, Any]] = []
    for group_key in sorted(groups):
        group = groups[group_key]
        doctor = numbers(group, doctor_field)
        legacy = metric_suite(numbers(group, legacy_field), doctor)
        canonical = metric_suite(numbers(group, canonical_field), doctor)
        entry: dict[str, Any] = {key: group_key, "rows": len(group)}
        for name in [
            "pairs",
            "mae",
            "median_ae",
            "within_10_pct",
            "same_bucket_pct",
            "pearson",
            "spearman",
            "hard_block_agreement_pct",
            "false_safe_count",
            "false_block_count",
        ]:
            entry[f"legacy_{name}"] = legacy.get(name)
            entry[f"canonical_{name}"] = canonical.get(name)
        entry["better_mae"] = compare_metric("mae", legacy.get("mae"), canonical.get("mae"))["better"]
        results.append(entry)
    return results
