# Canonical v2 Population Migration

This documents the migration of the Roopsee scoring pipeline from the legacy
name-keyed product population to the canonical GTIN-keyed population.

It was a **data pipeline migration**. The scoring methodology was deliberately
not redesigned.

---

## 1. What Changed

### Before

```text
useful_skin_bodycare_products.xlsx        14,119 rows: product name, primary, secondary
        |  identity = free-text product name, read positionally from columns 0-2
        v
        join on normalised product name  <--  retailer_products_rows.csv.gz
        |  lossy: 303 of 7,172 shipped products had no brand and no product URL
        v
   scoring engine -> 7,172 High-confidence products -> frontend (16.7 MB)
```

### After

```text
canonical_scoring_population_v2.csv        4,037 rows, one per GTIN
        +  join on canonical_product_id_v2 (1:1, exact)
scoring_input_ingredient_mapping_v2.csv    ingredients pre-resolved to the score master
        |
        v  evidence-based primary/secondary selection
   roopsee_final_scoring_input.csv
        |
        v  SAME scoring engine, SAME doctor anchors
   4,037 scored -> 3,841 eligible -> 1,000 onboarded -> frontend (2.8 MB)
```

### Measured effect

| | Legacy | Canonical |
| --- | --- | --- |
| Products scored | 14,119 | 4,037 |
| High confidence | 7,290 (51.6%) | 3,875 (96.0%) |
| Medium / Low | 4,942 / 1,887 | 0 / 162 |
| Products with created-fallback ingredients | 5,855 | **0** |
| Brand + product URL coverage | 303 missing | **100%** |
| Mean doctor-anchor support | — | 11.05 of a maximum 12 |
| Mean exact ingredient matches | — | 24.15 |
| Frontend payload | 16.7 MB | 2.8 MB |

The ingredient quality improvement is the substantive one: every canonical
ingredient resolves against the score master, so nothing is scored on an invented
fallback ingredient.

---

## 2. What Was Deliberately Not Changed

Verified by `tests/test_scoring_invariants.py` and by re-running the legacy
population, which reproduces its previous payload **byte for byte**.

- `VISIBLE_SCORE_WEIGHTS` — baseline .05 / v2 .30 / anchor .55 / typeFamily .05 / type .05
- `RANK_FUSION_WEIGHTS` — score .20 / baseline .10 / v2 .10 / anchor .55 / typeFamily .05
- Primary/secondary weighting — serum 80/20, every other type 50/50
- All six ingredient match strategies and their confidence values
- Doctor-anchor similarity function, the `>= 4.5` threshold, and the top-12 cap
- `build_v2_vector()` weight profiles and the Excessive Dryness ternarisation
- Confidence tiering, safety caps, hard-block propagation
- Every scoring function in `static/app.js`
- `roopsee_coverage/`, `data/products.csv`, and the ingredient score master

**The doctor/reference layer is untouched.** `data/products.csv` still supplies all
384 reference products, and the canonical products are scored *against* them exactly
as before. The new products did not replace or dilute the anchors.

---

## 3. Key Design Decisions

### The migration seam

`build_rows()` in `tools/build_automated_scores.py` consumes exactly two shapes per
product: a product dict and a retailer-row-shaped `representative` dict. Everything
downstream of ingredient matching is population-agnostic. The migration therefore
swapped the *loader* and left the scoring loop intact.

Shaping the canonical record as a retailer-row lookalike means `product_type_from()`,
`product_category_from()`, `infer_product_type()` and `infer_product_category()` all
keep working unmodified.

### Join on the canonical id, never on GTIN

The mapping file wrote `gtin` as an integer, so its 94 leading-zero values no longer
match the canonical strings. Joining on `gtin` silently loses those 94 products
(3,943 of 4,037). `canonical_product_id_v2` is exact.

For the same reason the loader uses the stdlib `csv` module rather than pandas: `csv`
never coerces types, so leading zeros survive. This is locked by a test.

### Evidence-based primary/secondary

Being matched to the score master does not make an ingredient the Primary. The ladder,
strongest evidence first:

1. **Product name / explicit hero claim** — percentage-qualified actives and any
   score-master ingredient or curated alias named in the title, including parenthetical
   synonyms. Multiple heroes are allowed.
2. **`key_score_master_matches`** from the mapping file
3. **`resolved_score_master_ingredients`** — covers alias-only resolutions
4. **Full INCI matches** — flags the product `REVIEW_REQUIRED`

Worked example — `Eucerin Pigment Control Sunscreen ... Thiamidol`, whose mapping
offers both Licochalcone A and Thiamidol: Primary is **Thiamidol**, because the product
name claims it. Licochalcone A becomes Secondary.

A name claim only counts when the ingredient data corroborates it, matched on either
the surface form in the title or the score master's canonical spelling.

Secondary excludes a curated `GENERIC_EXCIPIENTS` list — solvents, preservatives,
chelators, pH adjusters, generic thickeners and emulsifiers. It deliberately **keeps**
fragrance/parfum, glycerin, cetearyl alcohol and squalane, which carry real Roopsee
scores. Dropped excipients are recorded in the audit; this is what stopped "Purified
Water" being scored as an ingredient.

### INCI parsing fix

`split_full_inci_items()` and `split_ingredients()` split on every comma, which broke
chemical locants: `1,2-Hexanediol` became `["1", "2-Hexanediol"]`. The `"1"` fragment
is not rejected as non-ingredient text, so it became a *Created fallback* ingredient —
precisely the signal that demotes a product to Medium confidence and out of the
shipped dataset.

Both now use a shared depth-aware splitter that treats a comma between two digits, and
any comma inside brackets, as part of the name. Slashes were never delimiters, so
`Caprylic/Capric Triglyceride` was always safe.

Blast radius on the legacy population, measured: **1 product of 14,119**.

### Onboarding is separate from scoring

Every canonical product is scored. Eligibility and `ONBOARD_LIMIT` then decide what
ships. Changing the limit re-runs selection only.

Onboarding rank deliberately does **not** use the customer-facing score: that score is
per-profile and lives in `static/app.js`. Reimplementing it in Python would fork the
scoring engine into two copies that could drift. Rank uses a profile-independent proxy
— the visible-score weights applied to the five layers, averaged over the concern and
skin columns — then anchor and exact support, then the canonical id as a tie-break.

### Safety reserve in the selection

Ranking purely by evidence strength selects actives-led products, and those are exactly
the ones hard-blocked for pregnancy, teens and a compromised barrier. Left unchecked,
the onboarding set left whole product types with nothing to offer those profiles.

35% of each product type's quota is therefore reserved for products that are not
hard-blocked on any special-condition or skin-type column. Measured effect on the
Dry+Sensitive / Barrier Repair / Excessive Dryness profile: blocked products fell from
407 to 345, and pregnancy-profile blocks fell from 57 to 35, with no loss on ordinary
profiles.

Blocking is evaluated **per layer**, not on the blend, because that is what the browser
does — `customerFacingScore()` blocks when any one of the five features is `-100`
(`static/app.js:461`). Averaging first hides a single blocking layer behind four healthy
ones.

---

## 4. Eligibility Statuses

| Status | Meaning |
| --- | --- |
| `ELIGIBLE` | Can be onboarded. |
| `EXCLUDED_INVALID_GTIN` | Missing, malformed, check-digit-failing, or duplicate GTIN. |
| `EXCLUDED_MISSING_DATA` | No product name, no brand, or no usable ingredient information. |
| `EXCLUDED_LOW_CONFIDENCE` | Confidence tier below `ROOPSEE_ONBOARD_CONFIDENCE`. |
| `EXCLUDED_SAFETY` | Hard-blocked for **every** skin type. |
| `EXCLUDED_PRODUCT_TYPE` | Product type outside the supported seven. |
| `EXCLUDED_NON_TOPICAL` | Caught by the existing non-topical filter. |
| `REVIEW_REQUIRED` | Ingredients could not be scored, or no recognised active family. |

`EXCLUDED_SAFETY` is deliberately narrow. A product blocked only for pregnancy,
breastfeeding or under-16 stays eligible — the browser already applies that block per
profile, and excluding it outright would remove it from everyone.

Excluded products remain in `scored_population_full.json` with their status and reason.

Current run: 3,841 eligible, 146 `REVIEW_REQUIRED`, 34 `EXCLUDED_NON_TOPICAL`,
16 `EXCLUDED_MISSING_DATA`.

---

## 5. Known Findings (not defects introduced by this migration)

1. **No toner is suitable for an Excessive Dryness profile.** All 236 eligible toners
   are hard-blocked, because the `v2` layer's documented ternarisation maps any
   predicted value at or below 50 to `-100`, and every canonical toner is actives-led
   enough to land there. The doctor data does not support this strongly — only 11 of 25
   doctor-scored toners are `-100` for excessive dryness. This is pre-existing scoring
   behaviour, was already weak on the legacy population (75 of 510 toners survived), and
   was **not** changed here. Toner is not a routine slot, so routines are unaffected.

2. **`ingredient_summary` truncates at 3,000 characters** (`build_automated_scores.py`),
   cutting mid-token. `parse_matched_ingredients()` then counts the mangled tail as a
   weak match, which forces Medium confidence. Not changed — it shifts confidence tiers.

3. **Two divergent similarity functions.** The shipped anchor layer uses top-12 at
   `>= 4.5`; the leave-one-out validation uses top-5 at `>= 3.25`. The validation
   therefore does not measure the anchor layer that actually ships.

4. **`metadata.validation` is permanently null.** Nothing in the repo produces
   `final_rank_fusion_algorithm_validation_summary.json`, so the UI validation card
   never populates.

5. **The frontend renders only the top 120 products** (`static/app.js:778, :780`,
   duplicated literal, no pagination). With 1,000 onboarded, most are unreachable per
   profile — though the top 120 for a given profile is arguably the useful set.

6. **`data/Product details and score logic.xlsx` is stale** relative to
   `data/products.csv`: 159 products versus 384, with only 112 overlapping UIDs. It is
   not part of the build pipeline. Use it for the logic sheets, not the product roster.

---

## 6. Legacy And Deprecated

Nothing was deleted.

| Item | Status |
| --- | --- |
| `data/source/useful_skin_bodycare_products.xlsx` | Legacy population. Reachable with `ROOPSEE_POPULATION_SOURCE=legacy`. |
| `data/source/retailer_products_rows.csv.gz` | Only used by the legacy path; the canonical population carries its own commerce metadata. |
| `roopsee_coverage/` | Was already a separate, unwired serving path. Unchanged by this migration. |
| `KEEP_CONFIDENCE_LEVELS` | Superseded by `ROOPSEE_ONBOARD_CONFIDENCE`. Retained so the legacy path behaves as before. |

### Reproducing the pre-migration dataset

```bash
ROOPSEE_POPULATION_SOURCE=legacy ROOPSEE_AUTO_OUTPUT_DIR=outputs/legacy \
  python3 tools/build_automated_scores.py
ROOPSEE_AUTO_PAYLOAD=outputs/legacy/automated_scoring_payload.json \
ROOPSEE_ONBOARD_LIMIT=none ROOPSEE_FINAL_DATASET=outputs/legacy/final_scored_products.json \
  python3 tools/build_final_platform_dataset.py
```

---

## 7. Verification Performed

- **176 tests pass** (`python3 -m pytest tests/`).
- **Legacy regression:** the legacy population reproduces its pre-migration payload
  byte for byte, proving the scoring engine was not altered.
- **Rebuild fidelity:** re-running the previous pipeline reproduced the shipped dataset
  to within +/-1 point on 17 of 7,172 products (0.24%), all traceable to float rounding
  at exact `.5` boundaries. Confidence counts matched exactly.
- **End-to-end browser scoring:** the real `static/app.js` scoring functions were
  executed against the migrated dataset across 8 quiz profiles covering sensitive, teen,
  pregnancy, breastfeeding and excessive-dryness cases. Every product scored, no score
  exceeded 100, sorting held, all five routine-relevant product types were covered, and
  all 8 routine slots filled across both price tiers.
