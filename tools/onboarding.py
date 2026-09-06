"""Eligibility and onboarding selection.

Scoring and onboarding are separate concerns. Every canonical product is scored;
onboarding then decides which of the scored products the frontend ships first.
Changing ONBOARD_LIMIT re-runs selection only -- it never re-runs the scoring
engine, and the full scored population stays available either way.

Set the limit with ROOPSEE_ONBOARD_LIMIT:

    ROOPSEE_ONBOARD_LIMIT=1000   (default)
    ROOPSEE_ONBOARD_LIMIT=2000
    ROOPSEE_ONBOARD_LIMIT=none   ship every eligible product
"""

from __future__ import annotations

import csv
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]


def env_int(name: str, default: int | None) -> int | None:
    """Read an integer limit. 'none', 'all', '' and 0 all mean no limit."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in {"", "none", "all", "unlimited", "0"}:
        return None
    try:
        value = int(text)
    except ValueError as error:
        raise SystemExit(f"{name} must be an integer or 'none', got {raw!r}") from error
    return value if value > 0 else None


ONBOARD_LIMIT = env_int("ROOPSEE_ONBOARD_LIMIT", 1000)

# Confidence tiers admitted to onboarding. The dry run over the canonical
# population produced 3,875 High and no Medium, so High alone fills the limit
# several times over; widen this only with a deliberate quality decision.
ONBOARDING_CONFIDENCE_LEVELS = {
    level.strip()
    for level in os.environ.get("ROOPSEE_ONBOARD_CONFIDENCE", "High").split(",")
    if level.strip()
}

SUPPORTED_PRODUCT_TYPES = {"serum", "cleanser", "moisturizer", "sunscreen", "mask", "toner", "other"}

# Types that must be represented for AM/PM routine building to work at all.
ROUTINE_CRITICAL_TYPES = ["cleanser", "moisturizer", "sunscreen", "serum", "mask", "toner"]

STATUS_ELIGIBLE = "ELIGIBLE"
STATUS_INVALID_GTIN = "EXCLUDED_INVALID_GTIN"
STATUS_MISSING_DATA = "EXCLUDED_MISSING_DATA"
STATUS_LOW_CONFIDENCE = "EXCLUDED_LOW_CONFIDENCE"
STATUS_SAFETY = "EXCLUDED_SAFETY"
STATUS_PRODUCT_TYPE = "EXCLUDED_PRODUCT_TYPE"
STATUS_NON_TOPICAL = "EXCLUDED_NON_TOPICAL"
STATUS_REVIEW = "REVIEW_REQUIRED"

HARD_BLOCK = -100

SKIN_COLUMNS = [
    "Oily Score",
    "Oily+Sensitive Score",
    "Dry Score",
    "Dry+Sensitive Score",
    "Normal Score",
    "Normal+Sensitive Score",
    "Combination Score",
    "Combination+Sensitive Score",
]

# Columns that hard-block a product for a whole class of people rather than for
# a concern: the special conditions and the teen gate.
SAFETY_COLUMNS = ["Excessive Dryness score", "Pregnancy Score", "Breastfeeling Score", "<16"]

# Share of each product type's quota reserved for products that are not
# hard-blocked on any safety column.
#
# Ranking purely by evidence strength selects actives-led products, and those
# are exactly the ones blocked for pregnancy, teens and a compromised barrier.
# Without this reserve the onboarding set leaves whole product types with no
# option at all for those profiles -- measured on this catalogue, an
# evidence-only top 61 toners was 61/61 blocked for excessive dryness.
SAFETY_RESERVE_FRACTION = 0.35

CONCERN_COLUMNS = [
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
]


# --------------------------------------------------------------------------
# Evidence quality
# --------------------------------------------------------------------------


def blended_layer_values(
    product: dict[str, Any],
    score_columns: list[str],
    weights: dict[str, float],
) -> list[float]:
    """Apply the visible score weights to the five layers, column by column.

    This is the same fixed weighted sum the browser uses to turn layers into an
    evidence score, with no profile applied. It is arithmetic already specified
    by VISIBLE_SCORE_WEIGHTS, not a reimplementation of the customer-facing
    score -- that one is per-profile and lives in static/app.js.
    """
    layers = product.get("scoreLayers") or {}
    key_map = {
        "baseline": "baseline",
        "v2": "v2",
        "anchor": "anchor",
        "type_family": "typeFamily",
        "type": "type",
    }
    output: list[float] = []
    for index in range(len(score_columns)):
        total = 0.0
        for weight_key, layer_key in key_map.items():
            values = layers.get(layer_key) or []
            value = float(values[index]) if index < len(values) else 50.0
            total += float(weights.get(weight_key, 0.0)) * value
        output.append(total)
    return output


def evidence_quality(
    product: dict[str, Any],
    score_columns: list[str],
    weights: dict[str, float],
) -> float:
    """A profile-independent quality proxy used only to rank onboarding.

    Averaged over the concern and skin-type columns, ignoring hard blocks so a
    product that is unsuitable for one profile is not penalised overall.
    """
    blended = blended_layer_values(product, score_columns, weights)
    index_of = {column: index for index, column in enumerate(score_columns)}
    values = [
        blended[index_of[column]]
        for column in CONCERN_COLUMNS + SKIN_COLUMNS
        if column in index_of and blended[index_of[column]] > HARD_BLOCK
    ]
    return round(sum(values) / len(values), 4) if values else 0.0


def column_is_hard_blocked(product: dict[str, Any], index: int) -> bool:
    """True when ANY layer hard-blocks this column.

    Blocking is evaluated per layer, not on the blend, because that is what the
    browser does: profileLayerScore() returns -100 for a layer whose applicable
    column is -100, and customerFacingScore() blocks the product when any one of
    the five features is -100 (static/app.js:461). Averaging first would hide a
    single blocking layer behind four healthy ones.
    """
    for values in (product.get("scoreLayers") or {}).values():
        if index < len(values) and float(values[index]) <= HARD_BLOCK:
            return True
    return False


def is_broadly_safe(
    product: dict[str, Any],
    score_columns: list[str],
    weights: dict[str, float],
) -> bool:
    """True when the product is usable by the widest range of profiles.

    Requires no hard block on any special-condition column AND none on any skin
    type. Reserving capacity for these guarantees that pregnancy, breastfeeding,
    teen and excessive-dryness profiles have real options in every product type,
    without admitting products that are unusable for ordinary profiles.
    """
    index_of = {column: index for index, column in enumerate(score_columns)}
    return not any(
        column_is_hard_blocked(product, index_of[column])
        for column in SAFETY_COLUMNS + SKIN_COLUMNS
        if column in index_of
    )


def is_universally_blocked(
    product: dict[str, Any],
    score_columns: list[str],
    weights: dict[str, float],
) -> bool:
    """True when no profile could ever be recommended this product.

    Deliberately narrow. A product blocked only for pregnancy, breastfeeding or
    under-16 stays eligible -- the browser already blocks it per profile. This
    catches the "Do not use" case, where every skin type is hard-blocked.
    """
    index_of = {column: index for index, column in enumerate(score_columns)}
    indexes = [index_of[column] for column in SKIN_COLUMNS if column in index_of]
    return bool(indexes) and all(column_is_hard_blocked(product, index) for index in indexes)


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


def evaluate_eligibility(
    product: dict[str, Any],
    score_columns: list[str],
    weights: dict[str, float],
    *,
    seen_gtins: set[str] | None = None,
    is_non_topical: Callable[[dict[str, Any]], bool] | None = None,
    validate_gtin: Callable[[Any], tuple[bool, str]] | None = None,
    require_gtin: bool = True,
) -> tuple[str, str]:
    """Return (status, reason). Only ELIGIBLE products can be onboarded.

    Excluded products are never dropped from the full scored population; the
    status and reason travel with them so every exclusion stays auditable.
    """
    gtin = str(product.get("gtin") or "").strip()

    if not str(product.get("name") or "").strip():
        return STATUS_MISSING_DATA, "missing product name"
    if not str(product.get("brand") or "").strip():
        return STATUS_MISSING_DATA, "missing brand"

    # The legacy population predates GTINs entirely, so a missing GTIN is only
    # a defect for a population that is supposed to carry one. Without this the
    # legacy path excludes every product as EXCLUDED_INVALID_GTIN.
    if require_gtin or gtin:
        if not gtin:
            return STATUS_INVALID_GTIN, "gtin missing"
        if validate_gtin is not None:
            valid, reason = validate_gtin(gtin)
            if not valid:
                return STATUS_INVALID_GTIN, f"gtin {reason}"
        if seen_gtins is not None:
            if gtin in seen_gtins:
                return STATUS_INVALID_GTIN, "duplicate gtin"
            seen_gtins.add(gtin)

    if product.get("normalizedType") not in SUPPORTED_PRODUCT_TYPES:
        return STATUS_PRODUCT_TYPE, f"unsupported product type {product.get('normalizedType')!r}"

    if is_non_topical is not None and is_non_topical(product):
        return STATUS_NON_TOPICAL, "not a topical skincare product"

    if not str(product.get("primaryIngredients") or "").strip() and not str(
        product.get("secondaryIngredients") or ""
    ).strip():
        return STATUS_MISSING_DATA, "no usable ingredient information"

    if int(product.get("needsReviewIngredientCount") or 0) > 0:
        return STATUS_REVIEW, "ingredients could not be scored and need review"

    if not product.get("families"):
        return STATUS_REVIEW, "no recognised active ingredient family"

    if is_universally_blocked(product, score_columns, weights):
        return STATUS_SAFETY, "hard-blocked for every skin type"

    if product.get("confidence") not in ONBOARDING_CONFIDENCE_LEVELS:
        return STATUS_LOW_CONFIDENCE, f"confidence {product.get('confidence')}"

    return STATUS_ELIGIBLE, ""


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def onboarding_sort_key(product: dict[str, Any]) -> tuple[Any, ...]:
    """Deterministic quality ordering within a product type.

    Evidence strength first, then the uid so the result is reproducible for any
    two products that tie on every measured signal.
    """
    support = product.get("support") or {}
    return (
        -float(product.get("_evidenceQuality") or 0.0),
        -int(support.get("anchor") or 0),
        -int(support.get("exact") or 0),
        -int(support.get("family") or 0),
        str(product.get("uid") or ""),
    )


def allocate_quotas(available: dict[str, int], limit: int) -> dict[str, int]:
    """Split `limit` across product types in proportion to what is available.

    Largest-remainder apportionment, then any quota a thin type cannot fill is
    redistributed to the types that still have products left. Keeps every
    routine slot populated instead of letting one type take the whole budget.
    """
    total = sum(available.values())
    if total == 0:
        return {product_type: 0 for product_type in available}
    if limit >= total:
        return dict(available)

    exact = {product_type: limit * count / total for product_type, count in available.items()}
    quotas = {product_type: int(value) for product_type, value in exact.items()}

    # Give every type with anything available at least one slot, so a small but
    # routine-critical type (toner, sunscreen) is never squeezed out entirely.
    for product_type, count in available.items():
        if count > 0 and quotas[product_type] == 0 and product_type in ROUTINE_CRITICAL_TYPES:
            quotas[product_type] = 1

    def remaining_capacity() -> list[str]:
        return sorted(
            (product_type for product_type in available if quotas[product_type] < available[product_type]),
            key=lambda product_type: (-(exact[product_type] - int(exact[product_type])), product_type),
        )

    while sum(quotas.values()) < limit:
        candidates = remaining_capacity()
        if not candidates:
            break
        for product_type in candidates:
            if sum(quotas.values()) >= limit:
                break
            quotas[product_type] += 1

    while sum(quotas.values()) > limit:
        candidates = sorted(
            (product_type for product_type in quotas if quotas[product_type] > 1),
            key=lambda product_type: (exact[product_type] - int(exact[product_type]), product_type),
        )
        if not candidates:
            break
        quotas[candidates[0]] -= 1

    return quotas


def select_onboarding(
    eligible: list[dict[str, Any]],
    limit: int | None = ONBOARD_LIMIT,
) -> list[dict[str, Any]]:
    """Pick the onboarding population: type-balanced, ranked by evidence.

    Reproducible: no randomness, and every ordering ends in a uid tie-break.
    """
    if limit is None or limit >= len(eligible):
        return sorted(eligible, key=onboarding_sort_key)

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in eligible:
        by_type[str(product.get("normalizedType"))].append(product)
    for group in by_type.values():
        group.sort(key=onboarding_sort_key)

    quotas = allocate_quotas({key: len(group) for key, group in by_type.items()}, limit)

    selected: list[dict[str, Any]] = []
    for product_type in sorted(by_type):
        group = by_type[product_type]
        quota = quotas.get(product_type, 0)
        if quota <= 0:
            continue

        # Fill most of the quota on evidence alone, then reserve the rest for
        # products that are safe for every special-condition group, so no
        # product type ends up with nothing to offer a teen, a pregnant user or
        # a compromised barrier. Both passes stay in evidence order.
        reserve = int(quota * SAFETY_RESERVE_FRACTION)
        chosen: list[dict[str, Any]] = list(group[: quota - reserve])
        taken = {id(product) for product in chosen}
        for product in group:
            if len(chosen) >= quota:
                break
            if id(product) not in taken and product.get("_broadlySafe"):
                chosen.append(product)
                taken.add(id(product))
        for product in group:
            if len(chosen) >= quota:
                break
            if id(product) not in taken:
                chosen.append(product)
                taken.add(id(product))
        selected.extend(chosen)

    # Apportionment can land under the limit when a type runs out; top up with
    # the best remaining products regardless of type.
    if len(selected) < limit:
        chosen = {id(product) for product in selected}
        leftovers = sorted(
            (product for product in eligible if id(product) not in chosen),
            key=onboarding_sort_key,
        )
        selected.extend(leftovers[: limit - len(selected)])

    return sorted(selected, key=onboarding_sort_key)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

STATUS_CSV_HEADERS = [
    "uid",
    "gtin",
    "product_name",
    "brand",
    "product_type",
    "category",
    "confidence",
    "status",
    "reason",
    "onboarded",
    "evidence_quality",
    "anchor_support",
    "exact_support",
    "review_flags",
]


def write_status_csv(
    products: Iterable[dict[str, Any]],
    statuses: dict[str, tuple[str, str]],
    onboarded_uids: set[str],
    path: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(STATUS_CSV_HEADERS)
        for product in products:
            uid = str(product.get("uid") or "")
            status, reason = statuses.get(uid, (STATUS_ELIGIBLE, ""))
            support = product.get("support") or {}
            writer.writerow(
                [
                    uid,
                    product.get("gtin", ""),
                    product.get("name", ""),
                    product.get("brand", ""),
                    product.get("normalizedType", ""),
                    product.get("category", ""),
                    product.get("confidence", ""),
                    status,
                    reason,
                    "true" if uid in onboarded_uids else "false",
                    product.get("_evidenceQuality", ""),
                    support.get("anchor", ""),
                    support.get("exact", ""),
                    "; ".join(product.get("reviewFlags") or []),
                ]
            )
    return path


def quality_distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return round(ordered[index], 2)

    return {
        "min": round(ordered[0], 2),
        "p25": percentile(0.25),
        "median": percentile(0.5),
        "p75": percentile(0.75),
        "max": round(ordered[-1], 2),
        "mean": round(sum(ordered) / len(ordered), 2),
    }


def build_audit(
    all_products: list[dict[str, Any]],
    statuses: dict[str, tuple[str, str]],
    onboarded: list[dict[str, Any]],
    *,
    canonical_count: int,
    limit: int | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status_counter = Counter(status for status, _reason in statuses.values())
    reason_counter = Counter(
        f"{status}: {reason}" for status, reason in statuses.values() if status != STATUS_ELIGIBLE
    )
    eligible = [p for p in all_products if statuses.get(str(p.get("uid")), (STATUS_ELIGIBLE, ""))[0] == STATUS_ELIGIBLE]
    gtins = [str(product.get("gtin") or "") for product in all_products]

    return {
        "counts": {
            "canonical": canonical_count,
            "scored": len(all_products),
            "eligible": len(eligible),
            "onboarded": len(onboarded),
            "excluded": len(all_products) - len(eligible),
            "onboardLimit": limit,
        },
        "statuses": dict(status_counter.most_common()),
        "exclusionReasons": dict(reason_counter.most_common()),
        "gtin": {
            "present": sum(1 for value in gtins if value),
            "distinct": len(set(value for value in gtins if value)),
            "duplicates": len([value for value in gtins if value]) - len(set(value for value in gtins if value)),
            "lengthDistribution": dict(Counter(len(value) for value in gtins if value)),
            "leadingZero": sum(1 for value in gtins if value.startswith("0")),
        },
        "confidence": {
            "scored": dict(Counter(product.get("confidence") for product in all_products).most_common()),
            "onboarded": dict(Counter(product.get("confidence") for product in onboarded).most_common()),
        },
        "productTypes": {
            "scored": dict(Counter(product.get("normalizedType") for product in all_products).most_common()),
            "eligible": dict(Counter(product.get("normalizedType") for product in eligible).most_common()),
            "onboarded": dict(Counter(product.get("normalizedType") for product in onboarded).most_common()),
        },
        "categories": {
            "onboarded": dict(Counter(product.get("category") for product in onboarded).most_common()),
        },
        "ingredients": {
            "withPrimary": sum(1 for product in all_products if str(product.get("primaryIngredients") or "").strip()),
            "withSecondary": sum(
                1 for product in all_products if str(product.get("secondaryIngredients") or "").strip()
            ),
            "withNeither": sum(
                1
                for product in all_products
                if not str(product.get("primaryIngredients") or "").strip()
                and not str(product.get("secondaryIngredients") or "").strip()
            ),
            "createdFallbackTotal": sum(
                int(product.get("createdFallbackIngredientCount") or 0) for product in all_products
            ),
            "needsReviewTotal": sum(int(product.get("needsReviewIngredientCount") or 0) for product in all_products),
            "matchStatus": dict(
                Counter(product.get("ingredientMatchStatus") for product in all_products).most_common()
            ),
        },
        "support": {
            "meanAnchor": round(
                sum(int((p.get("support") or {}).get("anchor") or 0) for p in all_products) / max(1, len(all_products)),
                2,
            ),
            "meanExact": round(
                sum(int((p.get("support") or {}).get("exact") or 0) for p in all_products) / max(1, len(all_products)),
                2,
            ),
            "zeroAnchor": sum(1 for p in all_products if int((p.get("support") or {}).get("anchor") or 0) == 0),
            "thinAnchor": sum(1 for p in all_products if int((p.get("support") or {}).get("anchor") or 0) < 3),
        },
        "evidenceQuality": {
            "scored": quality_distribution([float(p.get("_evidenceQuality") or 0) for p in all_products]),
            "onboarded": quality_distribution([float(p.get("_evidenceQuality") or 0) for p in onboarded]),
        },
        "reviewFlags": dict(Counter(flag for p in all_products for flag in (p.get("reviewFlags") or [])).most_common()),
        **(extra or {}),
    }


def audit_markdown(audit: dict[str, Any]) -> str:
    counts = audit["counts"]
    lines = [
        "# Canonical migration audit",
        "",
        "## Population",
        "",
        "| Stage | Count |",
        "| --- | --- |",
        f"| Canonical products | {counts['canonical']} |",
        f"| Scored | {counts['scored']} |",
        f"| Eligible | {counts['eligible']} |",
        f"| Onboarded | {counts['onboarded']} |",
        f"| Excluded | {counts['excluded']} |",
        f"| Onboard limit | {counts['onboardLimit'] if counts['onboardLimit'] is not None else 'none'} |",
        "",
        "## Eligibility",
        "",
        "| Status | Count |",
        "| --- | --- |",
    ]
    for status, count in audit["statuses"].items():
        lines.append(f"| {status} | {count} |")

    if audit["exclusionReasons"]:
        lines += ["", "### Exclusion reasons", "", "| Reason | Count |", "| --- | --- |"]
        for reason, count in audit["exclusionReasons"].items():
            lines.append(f"| {reason} | {count} |")

    lines += ["", "## Product types", "", "| Type | Scored | Eligible | Onboarded |", "| --- | --- | --- | --- |"]
    types = audit["productTypes"]
    for product_type in sorted(types["scored"]):
        lines.append(
            f"| {product_type} | {types['scored'].get(product_type, 0)} "
            f"| {types['eligible'].get(product_type, 0)} | {types['onboarded'].get(product_type, 0)} |"
        )

    gtin = audit["gtin"]
    lines += [
        "",
        "## GTIN validation",
        "",
        f"- Present: {gtin['present']}",
        f"- Distinct: {gtin['distinct']}",
        f"- Duplicates: {gtin['duplicates']}",
        f"- Leading zero preserved: {gtin['leadingZero']}",
        f"- Length distribution: {gtin['lengthDistribution']}",
        "",
        "## Ingredient mapping",
        "",
        f"- With primary: {audit['ingredients']['withPrimary']}",
        f"- With secondary: {audit['ingredients']['withSecondary']}",
        f"- With neither: {audit['ingredients']['withNeither']}",
        f"- Created fallback ingredients: {audit['ingredients']['createdFallbackTotal']}",
        f"- Needs-review ingredients: {audit['ingredients']['needsReviewTotal']}",
        "",
        "## Doctor-anchor support",
        "",
        f"- Mean anchors: {audit['support']['meanAnchor']}",
        f"- Mean exact ingredient matches: {audit['support']['meanExact']}",
        f"- Products with no anchor: {audit['support']['zeroAnchor']}",
        f"- Products with fewer than 3 anchors: {audit['support']['thinAnchor']}",
        "",
        "## Evidence quality",
        "",
        f"- Scored population: {audit['evidenceQuality']['scored']}",
        f"- Onboarded population: {audit['evidenceQuality']['onboarded']}",
        "",
    ]
    return "\n".join(lines)
