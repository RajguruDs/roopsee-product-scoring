# Roopsee Product Coverage Service

This repository contains the Roopsee product scoring playground used for catalog coverage review. It lets a reviewer choose a quiz profile and instantly see scored products, explanations, product details, and routine picks.

The project is intentionally simple for handover:

- Source sheets live in the repo.
- Python scripts regenerate the scored dataset.
- The frontend reads one generated JSON file.
- No database is required for this testing platform.

## Current Dataset

The tool now runs on the **canonical v2 product population**: 4,037 products, one per GTIN.

How the numbers reach the UI:

- **4,037 canonical products** are the scoring population. Every one of them is scored.
- **3,875 products** pass the high-confidence scoring gate (96%).
- **3,841 products** are eligible after quality, safety and topical checks.
- **1,000 products** are onboarded to the frontend, the default `ONBOARD_LIMIT`.

The remaining 2,841 eligible products stay in the full scored population and can be
released by raising `ONBOARD_LIMIT` — no re-scoring required.

The topical filter exists because the source catalog can include oral supplements, capsules, tablets, ingestible powders, gummies, baby powder, and other non-topical rows. The UI should recommend products that are intended to be applied on skin, not every raw catalog item.

The previous population (`useful_skin_bodycare_products.xlsx`, 14,119 name-keyed rows
producing 7,172 visible products) is retained and still runnable. See
`docs/MIGRATION_CANONICAL_V2.md` for what changed and why.

## What The Tool Does

For a selected user profile, the browser calculates and displays:

- Products sorted by score and ranking strength.
- Customer-facing product score from 0 to 100.
- Score range bins for quick coverage checking.
- Product detail modal with ingredients, evidence layers, confidence, and doctor-reference anchors.
- AM and PM routine suggestions using the best matching product types.

## Quick Start

Install dependencies if needed:

```bash
pip install -r requirements.txt
```

Run the local static app:

```bash
python3 app.py
```

Open:

```text
http://127.0.0.1:8020
```

For hosted deployments, the Flask entrypoint is:

```text
api/index.py
```

### Experimental V3 Scoring (localhost only)

The same server can run the Experimental V3 engine behind a query flag, for local
testing only. Nothing about the default URL changes.

```bash
python app.py
```

| URL | Engine | Population |
|---|---|---|
| `http://127.0.0.1:8020/` | production scorer in `static/app.js` | 1,000 onboarded |
| `http://127.0.0.1:8020/?scorer=v3` | Experimental V3 (`tools/ingredient_first_v3_experimental.py`) | 4,037 canonical |

With the flag, `static/app.js` loads `/api/v3/dataset` and POSTs the quiz profile to
`/api/v3/score`; Python returns one V3 score per product and the existing UI renders
them. Without it, not a single code path changes — verified by scoring 2,000
product-profile pairs before and after the integration with zero differences.

Notes:

- **No doctor calibration.** V3 scores come only from ingredient evidence, product
  context and safety. The old customer-facing uplift ladder is not applied, so the
  ceiling is **90**, not 95/100.
- **No ranking.** V3 has no rank-fusion implementation, so the old 55%-doctor-anchor
  ranking is deliberately *not* reused. Ordering is V3 score descending, then product
  name; `rankingScore` is returned as `null`. Many products tie at 90, so the top of
  the list is currently an alphabetical slice of a large tied group.
- This lives in `app.py` only. The deployment entrypoint `api/index.py` has no V3 code.

## Pipeline Architecture

```text
canonical_scoring_population_v2.csv          ~4,037 canonical products, one per GTIN
        +
scoring_input_ingredient_mapping_v2.csv      ingredient intelligence, joined 1:1
        |
        v  evidence-based primary/secondary selection
roopsee_final_scoring_input.csv              the scoring input
        |
        v  EXISTING ROOPSEE SCORING ENGINE (unchanged)
        |     - ingredient score master: roopsee_ingredient_scores_v3.xlsx
        |     - doctor anchors:          data/products.csv (384 reference products)
        v
FULL SCORED POPULATION                       ~4,037, all five evidence layers
        |
        v  quality / safety / eligibility gates
ELIGIBLE POPULATION
        |
        v  ONBOARD_LIMIT (default 1000), type-balanced by evidence
FRONTEND DATASET                             static/data/final_scored_products.json
```

Two numbers matter and they are not the same:

- **~4,037 is the scoring population.** Every canonical product is scored.
- **1,000 is the initial onboarding population.** That is what the frontend ships.

The full scored population is always written to `outputs/roopsee_canonical/` regardless
of the onboarding limit, so raising the limit never requires re-scoring.

## Regenerate The Dataset

The checked-in source files are already the default inputs, so a normal rebuild is:

```bash
python3 tools/build_automated_scores.py
python3 tools/validate_v2_automated_logic.py
python3 tools/build_final_platform_dataset.py
```

Outputs:

| Path | Contents |
| --- | --- |
| `static/data/final_scored_products.json` | The onboarding population the frontend loads (<= `ONBOARD_LIMIT`). |
| `outputs/roopsee_canonical/roopsee_final_scoring_input.csv` | The scoring input built from the canonical population. |
| `outputs/roopsee_canonical/scored_population_full.json` | Every scored product with all five layers and its eligibility status. |
| `outputs/roopsee_canonical/onboarding_status.csv` | One row per product: status, reason, and whether it was onboarded. |
| `outputs/roopsee_canonical/migration_audit.json` / `.md` | Counts, exclusion reasons, GTIN validation, coverage, distributions. |

`tools/canonical_population.py` can also be run on its own to rebuild just the scoring
input and print a coverage summary:

```bash
python3 tools/canonical_population.py
```

### Changing The Onboarding Limit

```bash
ROOPSEE_ONBOARD_LIMIT=2000 python3 tools/build_final_platform_dataset.py
ROOPSEE_ONBOARD_LIMIT=none python3 tools/build_final_platform_dataset.py   # ship everything eligible
```

The limit only re-runs selection. It never re-runs the scoring engine, and it never
changes a score.

### Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ROOPSEE_ONBOARD_LIMIT` | `1000` | Size of the onboarding population. `none`/`all`/`0` means no limit. |
| `ROOPSEE_ONBOARD_CONFIDENCE` | `High` | Comma-separated confidence tiers admitted to onboarding. |
| `ROOPSEE_POPULATION_SOURCE` | `canonical` | `canonical` or `legacy`. See "Legacy Population" below. |
| `ROOPSEE_CANONICAL_POPULATION` | `data/source/canonical_scoring_population_v2.csv` | Canonical product population. |
| `ROOPSEE_INGREDIENT_MAPPING` | `data/source/scoring_input_ingredient_mapping_v2.csv` | Ingredient mapping layer. |
| `ROOPSEE_CANONICAL_OUTPUT_DIR` | `outputs/roopsee_canonical` | Where the master artefacts are written. |
| `ROOPSEE_CANONICAL_VALIDATE_INCI` | off | Re-validate chosen ingredients against raw INCI text. See the note below. |
| `ROOPSEE_INGREDIENT_SCORES` | `data/source/roopsee_ingredient_scores_v3.xlsx` | Ingredient scoring master. |
| `ROOPSEE_USEFUL_PRODUCTS` | `data/source/useful_skin_bodycare_products.xlsx` | Legacy population sheet. |
| `ROOPSEE_RETAILER_PRODUCTS` | `data/source/retailer_products_rows.csv.gz` | Legacy retailer export. |
| `ROOPSEE_AUTO_OUTPUT_DIR` | `outputs/roopsee_automated_scoring` | Intermediate automated scoring files. |
| `ROOPSEE_AUTO_PAYLOAD` | `<auto output dir>/automated_scoring_payload.json` | Intermediate payload path. |
| `ROOPSEE_FINAL_DATASET` | `static/data/final_scored_products.json` | Final frontend JSON path. |
| `ROOPSEE_GENERATED_AT` | today's date | Pin the build date for reproducible builds. |

`ROOPSEE_CANONICAL_VALIDATE_INCI` is off by default on purpose. Hero ingredients are
routinely marketed under a name the INCI does not use — "Licochalcone A" appears in an
INCI as "Glycyrrhiza Inflata Root Extract" — so a textual presence check removes real
actives. The ingredient mapping layer already resolved ingredients from that same INCI.

### Legacy Population

The original name-keyed population is still runnable for comparison and rollback:

```bash
ROOPSEE_POPULATION_SOURCE=legacy ROOPSEE_AUTO_OUTPUT_DIR=outputs/legacy python3 tools/build_automated_scores.py
```

It reproduces the pre-migration payload exactly, which is how the migration was verified
not to have changed the scoring engine. See `docs/MIGRATION_CANONICAL_V2.md`.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/
```

The suite covers GTIN handling, ingredient mapping, INCI parsing, evidence-based
primary/secondary selection, onboarding limits and selection, and the scoring
invariants the migration is not allowed to move.

## Scoring Quality Validation

A separate, frozen-engine framework that measures how well the scoring system
agrees with independent doctor judgement, and compares the legacy system against
the canonical v2 system on the same products, profiles and doctor answers.

```bash
python tools/build_scoring_validation.py     # sample, predictions, doctor template
python tools/run_scoring_validation.py       # metrics and reports
```

It measures only. It never changes a weight, a threshold, a safety rule or a
scoring function, and it never writes to `data/`. Scores are produced by the
shipped `static/app.js` itself rather than a Python reimplementation.

Doctor scores start blank and are never auto-filled. Until the doctor review is
complete the report says so and no conclusion about either system is drawn.

See `docs/SCORING_QUALITY_VALIDATION.md`.

## Scoring Summary

The visible score is a rounded 0-100 product score for the selected profile. The system blends:

- Ingredient-sheet suitability score.
- Automated ingredient and formula score.
- Nearest doctor-reference product anchors.
- Same ingredient-family priors.
- Same product-type priors.
- Safety caps for sensitive conditions.
- Confidence caps when ingredient matching or source evidence is weaker.

The visible score is not the only sorting signal. The product list also uses a hidden rank-fusion score so products with stronger doctor-anchor support, cleaner ingredient matching, and better category fit can rank above products with similar visible scores.

For the full scoring explanation, see:

```text
docs/SCORING_METHODOLOGY.md
```

## Public Product Logic

These are the product-behavior rules that can be discussed externally:

- Skin type options are Oily, Dry, Normal, and Combination.
- Sensitivity is selected separately and maps to sensitive skin score columns.
- Age is shown as Teen or Adult.
- One skin concern is selected at a time.
- Male profiles cannot select Pregnancy or Breastfeeding.
- Serums are mainly scored by concern fit, with safety checks still applied.
- Cleansers are scored by skin type plus concern.
- Cleansers for wrinkles or anti-aging use skin type plus safety conditions rather than concern score.
- Moisturizers and sunscreens are mainly scored by skin type fit.
- Masks use concern plus skin-type fit.
- Pregnancy, breastfeeding, teen, sensitive, and excessive dryness profiles are handled cautiously through caps and modifiers.

## File-By-File Handover Map

| File | Why it exists |
| --- | --- |
| `.gitignore` | Keeps local caches, environment files, generated outputs, and temporary files out of Git while allowing required source sheets to stay tracked. |
| `.railwayignore` | Tells Railway which local files should not be uploaded during deployment. |
| `Procfile` | Start command for platforms that use Procfile-style deployment. |
| `README.md` | First-read handover guide for setup, regeneration, dataset meaning, and file purpose. |
| `api/index.py` | Flask deployment entrypoint. It serves the static frontend and health endpoint for hosted environments such as Render or Railway. |
| `app.py` | Small local server for opening the static testing UI without needing a database or full backend stack. |
| `data/Product details and score logic.xlsx` | Doctor-reviewed reference workbook used for calibration, validation, and comparison against known product scoring behavior. |
| `data/products.csv` | Product reference CSV retained for compatibility with the earlier doctor-score engine and validation workflows. |
| `data/source/canonical_scoring_population_v2.csv` | **Primary product population.** ~4,037 canonical products, one per GTIN, with brand, price, URL, image, full INCI, and key ingredients. |
| `data/source/scoring_input_ingredient_mapping_v2.csv` | **Ingredient intelligence layer**, joined 1:1 to the canonical population on `canonical_product_id_v2`. Carries ingredients already resolved against the score master plus mapped product type and mapping confidence. Not a second product population. |
| `data/source/useful_skin_bodycare_products.xlsx` | **Legacy** source sheet (14,119 name-keyed rows). Superseded by the canonical population; retained for comparison and rollback via `ROOPSEE_POPULATION_SOURCE=legacy`. |
| `data/source/retailer_products_rows.csv.gz` | Compressed retailer catalog export containing product metadata such as brand, price, URL, image, stock, and full ingredient text when available. |
| `data/source/roopsee_ingredient_scores_v3.xlsx` | Ingredient scoring master used to convert ingredient evidence into scores across concerns, skin types, ages, and special conditions. |
| `docs/SCORING_METHODOLOGY.md` | Detailed explanation of the scoring theory, formulas, gates, confidence logic, validation, and limitations. |
| `docs/MIGRATION_CANONICAL_V2.md` | What the canonical v2 migration changed, what it deliberately did not change, and what is now legacy. |
| `docs/SCORING_QUALITY_VALIDATION.md` | The scoring-quality validation framework: sample, profiles, metrics, limitations. |
| `requirements-dev.txt` | Test dependencies. |
| `tests/` | Pytest suite covering GTIN handling, ingredient mapping, INCI parsing, primary/secondary selection, onboarding, and scoring invariants. |
| `docs/TOOLS_PIPELINE_MINDMAP.md` | Visual handover map showing the three tool files, their inputs, outputs, and how they connect. |
| `render.yaml` | Render deployment configuration. |
| `requirements.txt` | Python dependencies needed to run the app and rebuild datasets. |
| `roopsee_coverage/__init__.py` | Marks `roopsee_coverage` as a Python package. |
| `roopsee_coverage/constants.py` | Shared constants and default paths for the older doctor-sheet service. |
| `roopsee_coverage/engine.py` | Older deterministic recommendation engine built around doctor sheet scores; kept for reference compatibility and service testing. |
| `roopsee_coverage/loaders.py` | Reads doctor workbook and product CSV inputs for the package engine. |
| `roopsee_coverage/models.py` | Data models used by the package engine for profiles, products, scores, and routines. |
| `roopsee_coverage/profile_rules.py` | Profile-level helper rules such as pregnancy/gender validity and condition handling for the package engine. |
| `roopsee_coverage/profiles.py` | Generates supported profile combinations and quiz option structures for the package engine. |
| `roopsee_coverage/scoring.py` | Doctor-sheet-style profile scoring logic for the package engine. |
| `roopsee_coverage/server.py` | API server wrapper around the package engine for older service-style endpoints. |
| `roopsee_coverage/utils.py` | Shared text cleaning, normalization, and safe parsing helpers. |
| `static/index.html` | Browser UI shell for the testing platform. |
| `static/styles.css` | Visual styling for profile controls, product cards, score circles, bins, routine cards, and product detail modal. |
| `static/app.js` | Main frontend logic. It loads the generated JSON, applies profile scoring, sorts products, renders filters, opens product details, and builds routines. |
| `static/data/final_scored_products.json` | Generated final dataset consumed by the frontend. This is the file that makes the UI fast because it avoids parsing Excel files in the browser. |
| `tools/canonical_population.py` | Loads the canonical population, joins the ingredient mapping, validates GTINs, and selects primary/secondary ingredients from evidence. Writes `roopsee_final_scoring_input.csv`. |
| `tools/onboarding.py` | Eligibility gates, onboarding selection (`ONBOARD_LIMIT`, type-balanced by evidence), and the migration audit. |
| `tools/scoring_validation.py` | Validation framework core: version, profiles, score buckets, metrics, breakdowns, failure classification. Measures only. |
| `tools/build_scoring_validation.py` | Builds the validation sample, the prediction grid, and the doctor review template. |
| `tools/run_scoring_validation.py` | Computes the metrics and writes the old-vs-new reports once doctor scores exist. |
| `tools/score_profiles.mjs` | Scores a dataset against validation profiles by executing the shipped `static/app.js`, so validation never uses a second scoring implementation. |
| `tools/build_automated_scores.py` | Converts the product population into an intermediate automated scoring payload by normalizing products, matching ingredients, and creating score vectors. |
| `tools/validate_v2_automated_logic.py` | Validates automated scoring against doctor-reviewed products and produces comparison evidence. |
| `tools/build_final_platform_dataset.py` | Builds the final UI dataset by combining automated scores, doctor anchors, product-type priors, family priors, confidence gates, and topical filtering. |
| `vercel.json` | Vercel deployment configuration if the project is hosted there instead of Render/Railway. |

## Files Usually Edited

Most changes should happen in one of these places:

- Update scoring theory or explanations in `docs/SCORING_METHODOLOGY.md`.
- Update source data by replacing files in `data/source/`.
- Update scoring generation in `tools/build_automated_scores.py` or `tools/build_final_platform_dataset.py`.
- Update frontend behavior in `static/app.js`.
- Update frontend look and feel in `static/styles.css`.

Do not manually edit `static/data/final_scored_products.json` for normal work. Regenerate it from the source sheets so the output remains reproducible.

## Important Caveat

This is an automated scoring and presentation tool for catalog testing and review. It is built to be auditable and conservative, but it is not a substitute for clinical or regulatory review. New low-confidence products and sensitive-condition edge cases should still be manually checked before production launch.
