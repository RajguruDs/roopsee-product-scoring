# Scoring Quality Validation Framework

Answers one question, and only with evidence:

> Does the canonical v2 scoring system produce product suitability scores and
> rankings that agree better with independent doctor judgement than the legacy
> system did?

The legacy system is **not** the target. Independent doctor judgement is the
quality reference. It is entirely possible for this framework to conclude that
the legacy system was better, or that neither is good enough.

**The scoring engine is frozen for this phase.** Nothing in this framework
changes a weight, a threshold, a safety rule or a scoring function. It measures.

---

## 1. Data separation

Three layers, kept strictly apart:

| Layer | Files | Rule |
| --- | --- | --- |
| Production | `canonical_scoring_population_v2.csv`, `scoring_input_ingredient_mapping_v2.csv`, `roopsee_ingredient_scores_v3.xlsx` | Read-only to this framework |
| Reference / anchors | `data/products.csv` — the 384 doctor-scored products | Read-only. Never gains validation rows |
| Validation | the sample, the predictions, the doctor answers | Lives only under `outputs/` |

**Validation doctor scores are never used as scoring anchors.** If they were,
the engine would be measured against its own inputs and every number here would
be circular. `tests/test_scoring_validation.py::TestValidationIsolation`
enforces this: it asserts the framework contains no path that writes into
`data/`, that `data/products.csv` still holds exactly 384 rows, and that no
second scoring implementation exists anywhere in the framework.

---

## 2. How scores are produced

Scores come from **the shipped engine**, not a reimplementation.

`tools/score_profiles.mjs` loads `static/app.js` in a Node VM with a minimal DOM
stub, sets `state` to a validation profile exactly as the quiz would, and calls
the page's own `computeScoredRows()`.

This matters. The customer-facing score is computed in the browser
(`customerFacingScore()`, `static/app.js:459-529`). A Python reimplementation
would be a second copy that drifts, and every validation number would then
describe code that never ships.

One consequence worth knowing: `state.sensitive` is a **boolean** in app.js
(`:95`). Passing the string `"No"` is truthy and silently makes every profile
sensitive. The profile definitions use real booleans and a test pins it.

---

## 3. Running it

```bash
# One-time: build the legacy full scored population for comparison
ROOPSEE_POPULATION_SOURCE=legacy ROOPSEE_AUTO_OUTPUT_DIR=outputs/legacy_validation \
  python tools/build_automated_scores.py
ROOPSEE_AUTO_PAYLOAD=outputs/legacy_validation/automated_scoring_payload.json \
ROOPSEE_POPULATION_SOURCE=legacy ROOPSEE_CANONICAL_OUTPUT_DIR=outputs/legacy_validation \
ROOPSEE_ONBOARD_LIMIT=none ROOPSEE_ONBOARD_CONFIDENCE=High,Medium,Low \
ROOPSEE_FINAL_DATASET=outputs/legacy_validation/final_scored_products.json \
  python tools/build_final_platform_dataset.py

# Build the sample, predictions and the doctor template
python tools/build_scoring_validation.py

# Before doctor review: writes a report that says it is awaiting review
python tools/run_scoring_validation.py

# After doctor review
python tools/run_scoring_validation.py --doctor-file outputs/roopsee_canonical/doctor_validation_completed.xlsx
```

The legacy comparison deliberately uses the legacy **full scored population**
(all 14,119) rather than the shipped legacy dataset (7,172, High-confidence
only). The shipped one overlaps just 32% of the canonical catalogue by name; the
full population overlaps ~63%, which roughly doubles the comparable sample.

---

## 4. The sample (Step 1)

`outputs/roopsee_canonical/scoring_quality_validation_sample.csv` — 149 products
drawn deterministically from the 4,037 canonical products.

Selection is seeded (`SAMPLE_SEED`) and ordered by `sha256(seed + canonical_id)`,
never by file order. A plain head-of-file sample would be dominated by whichever
brand and retailer the CSV happens to list first. Re-running produces the same
149 products; changing the seed or the strata requires bumping
`VALIDATION_SAMPLE_VERSION`.

Stratified across the dimensions that actually drive a score:

- all 7 product types, proportional to the catalogue
- doctor-anchor band: strong (>=8) / medium (3-7) / thin (1-2) / none
- ingredient-mapping confidence: HIGH / MEDIUM / REVIEW
- primary-ingredient evidence tier, including the INCI-fallback and
  product-name-hero paths
- confidence tier, evidence-score band, eligibility status

Two groups are **force-included** before proportional filling, because they are
rare and they are the historical failure areas:

- products hard-blocked on each safety column (pregnancy, breastfeeding, `<16`,
  excessive dryness), spread across product types
- products with no anchor support, weak ingredient mapping, or an INCI-only
  primary

Every row carries `validation_strata` recording why it was chosen.

---

## 5. The profiles (Step 2)

`outputs/roopsee_canonical/scoring_quality_profiles.json` — 14 profiles, defined
in `tools/scoring_validation.py::VALIDATION_PROFILES`.

Field names and values match `state` in app.js exactly, so they are handed to
the shipped scorer untouched. Concerns are verbatim score-column names, because
`profileLayerScore()` uses the concern label directly as a column key.

Coverage: all four skin types, sensitivity, teen, pregnancy, breastfeeding,
excessive dryness, acne, open pores, dehydration, barrier repair, redness,
pigmentation, uneven tone, wrinkles, dullness, and a no-concern control.
Four are explicitly safety profiles (`SAFETY_PROFILE_IDS`).

The same profiles are used for both systems.

---

## 6. Predictions and the doctor template (Steps 3-4)

`scoring_quality_predictions.csv` — the full grid, 149 x 14 = **2,086 pairs**,
with `legacy_score`, `canonical_v2_score` and `doctor_score` as three distinct
columns. `doctor_score` is blank and is never auto-filled.

`doctor_validation_template.xlsx` — the reviewable subset, **447 rows**
(3 profiles per product), with an Instructions sheet, a dropdown for the
recommendation, and validation on the score cell.

The template is a subset on purpose. A 2,086-row workbook does not get completed
honestly. Per product it prioritises: any safety profile the product is exposed
to, then the profile where the two systems disagree most (most informative for
the comparison), then the highest canonical score (most likely to reach a real
customer). Deterministic; `--profiles-per-product` changes the size.

The instructions tell the doctor explicitly **not** to anchor on the system
scores, and that disagreement is the most valuable output.

---

## 7. Metrics (Steps 5-6)

Buckets and labels are the product's own, not new ones:
`SCORE_BINS` from `static/app.js:85-92`, and `SCORE_LABELS` from
`roopsee_coverage/constants.py:118-124`.

Per system: MAE, median absolute error, within +/-5 / +/-10 / +/-20, Pearson,
Spearman (ties averaged), same-bucket %, one-bucket %, large-bucket
disagreement %, hard-block agreement, false-safe and false-block counts.

Every metric reports **Legacy | Canonical V2 | Delta | Better**. Metrics where
lower is better (MAE, false-safe, false-block, large-bucket disagreement) are
declared explicitly rather than assumed.

Both systems are scored on the **same** pairs — only those where both a legacy
score and a canonical score exist — so legacy is never judged on a different set
of products.

Breakdowns are written per product type, concern, skin type, confidence tier,
mapping confidence, score bucket, special condition, hard-block status and
profile. Aggregate metrics hide exactly the failure this phase exists to catch:
an overall improvement that conceals a pregnancy or excessive-dryness regression.

---

## 8. Ranking, failures, known failure modes (Steps 7-9)

`scoring_quality_ranking.csv` — per profile: top-1 agreement, top-5 and top-10
overlap, rank Spearman, unsuitable products in the top 5, and doctor-strong
products missed from the top 10, for both systems.

`scoring_quality_failures.csv` — the 50 largest canonical-vs-doctor errors with
layers, support counts, ingredients and a **suspected** cause from
`FAILURE_CATEGORIES`. These are hypotheses for investigation, never diagnoses,
and nothing is changed on their basis.

The report explicitly counts all twelve historical failure modes: pregnancy
false-safe and false-block, teen false-safe and under-scoring, excessive-dryness
false-block and under-scoring, hard-block propagation disagreement,
low-confidence artificial caps, and both directions of doctor/system divergence.

---

## 9. Versioning (Step 11)

```
SCORING_VALIDATION_VERSION = "canonical_v1"
VALIDATION_SAMPLE_VERSION  = "sample_v1"
VALIDATION_PROFILE_VERSION = "profiles_v1"
```

`scoring_quality_validation_meta.json` records the versions, the sample seed,
the generation timestamp, sample size, pair counts, whether doctor scores are
present, the metric methodology, the sample composition, and the known
limitations.

---

## 10. Known limitations

These are properties of the data and the repository, not defects in the
framework, and the report restates them every run.

1. **Divergent anchor configuration.** The shipped anchor layer uses the top 12
   doctor anchors at similarity `>= 4.5`; the leave-one-out validation in
   `tools/validate_v2_automated_logic.py` uses the top 5 at `>= 3.25`. Both were
   left exactly as they were. This framework measures the **shipped**
   configuration, so its numbers are not comparable with the leave-one-out
   report's numbers.

2. **Legacy coverage is partial.** 92 of 149 sampled products (61.7%) exist in
   the legacy catalogue. The legacy population was keyed by free-text product
   name, so the catalogues are matched by normalised name and the old-vs-new
   comparison is only valid on that subset. The remaining 57 products have a
   canonical score and can be measured against the doctor, but have no legacy
   counterpart to compare against.

3. **Ranking scope.** Ranking metrics are computed within the ~149-product
   validation sample, not the full 4,037-product catalogue. That is not the
   shelf a customer sees. `canonical_v2_rank_overall` carries the true
   catalogue-wide rank for context.

4. **Doctor coverage is a subset.** 447 of 2,086 pairs are put in front of the
   doctor, so per-group breakdowns rest on small counts. Any group with few
   pairs is indicative only.

5. **Single reviewer.** With one doctor there is no inter-rater agreement
   estimate, so doctor judgement is treated as ground truth without an error bar
   of its own. Two reviewers on an overlapping subset would fix this.

6. **The sample over-weights hard cases by design.** Safety blocks, missing
   anchors and weak mapping are deliberately over-represented so they can be
   measured at all. Aggregate metrics from this sample are therefore *not* an
   estimate of catalogue-wide accuracy, and should not be quoted as one.

---

## 11. What happens next

1. The doctor completes `doctor_validation_template.xlsx` — the three
   highlighted columns only.
2. `python tools/run_scoring_validation.py --doctor-file <completed file>`
3. Read the per-group breakdowns and the false-safe counts **before** the
   overall table.
4. Only then decide whether any scoring change is justified — as a separate,
   deliberate piece of work.
