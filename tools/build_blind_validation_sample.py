"""Parts 7-9: candidate pool, sample-size justification, and the blind doctor sheet.

Design principle: the sample must CHALLENGE the model, not flatter it. Selection is
deterministic and never uses the experimental score to prefer products the model
happens to like - score band is a stratum to be covered, not a filter.

Audit only. Nothing here modifies the scorer, production, or any dataset.
"""
from __future__ import annotations

import csv, hashlib, json, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "outputs" / "roopsee_canonical" / "ingredient_first_validation"
SEED = "roopsee-blind-validation-v1"

audit = list(csv.DictReader((OUT / "full_4037_score_audit.csv").open(newline="", encoding="utf-8-sig")))
indep = {r["canonical_product_id_v2"]: r for r in
         csv.DictReader((OUT / "doctor_independence_analysis.csv").open(newline="", encoding="utf-8-sig"))}
mapping = {}
mp = REPO / "data" / "source" / "scoring_input_ingredient_mapping_v2.csv"
if mp.exists():
    with mp.open(newline="", encoding="utf-8-sig") as h:
        for r in csv.DictReader(h):
            mapping[r["canonical_product_id_v2"]] = r
pop = {}
with (REPO / "data" / "source" / "canonical_scoring_population_v2.csv").open(newline="", encoding="utf-8-sig") as h:
    for r in csv.DictReader(h):
        pop[r["canonical_product_id_v2"]] = r


def order_key(*parts):
    return hashlib.sha256((SEED + "|" + "|".join(map(str, parts))).encode()).hexdigest()


def score_band(v):
    v = float(v)
    if v <= -100: return "blocked"
    return "high (80+)" if v >= 80 else "upper-mid (65-79)" if v >= 65 \
        else "mid (50-64)" if v >= 50 else "low (<50)"


def write(path, rows, fields=None):
    if not rows:
        path.write_text("", encoding="utf-8"); return
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields or list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)


# ---------------- PART 7: candidate pool ----------------
pool = []
for r in audit:
    uid = r["canonical_product_id_v2"]
    ind = indep.get(uid, {})
    cls = ind.get("independence_class", "D. no doctor reference")
    fs = float(r["final_score"])
    strata = []
    if fs >= 80: strata.append("high-score")
    if fs < 50 and fs > -100: strata.append("low-score")
    if 50 <= fs < 65: strata.append("borderline-mid")
    if r["safety_status"] == "hard_block": strata.append("safety-block")
    if r["safety_status"] == "capped": strata.append("safety-capped")
    if r["evidence_strength"] in ("none", "weak"): strata.append("weak-evidence")
    if r["evidence_strength"] == "strong": strata.append("strong-evidence")
    if r["active_role"] == "primary": strata.append("primary-concern-evidence")
    if r["active_role"] == "incidental": strata.append("incidental-only-evidence")
    if r["active_role"] == "none": strata.append("no-concern-evidence")
    if r["confidence"] == "Low": strata.append("low-confidence")
    if r["mapping_confidence"] in ("REVIEW", "MEDIUM"): strata.append("weak-mapping")
    if r["type_role"] == "incidental": strata.append("type-not-a-treatment-vehicle")
    if fs >= 80 and r["evidence_strength"] in ("none", "weak"): strata.append("FAILURE-high-score-weak-evidence")
    if fs < 40 and r["active_role"] == "primary": strata.append("FAILURE-low-score-strong-primary")
    if r["product_type"] == "sunscreen" and "Acne" in r["profile_label"]: strata.append("FAILURE-sunscreen-on-acne")

    pool.append({**{k: r[k] for k in ["profile_id", "profile_label", "canonical_product_id_v2", "gtin",
                                      "product_name", "brand", "product_type", "final_score",
                                      "ingredient_suitability_score", "product_context_score",
                                      "type_role", "active_role", "confidence", "evidence_strength",
                                      "safety_status", "mapping_confidence", "primary_ingredients",
                                      "secondary_ingredients"]},
                 "score_band": score_band(r["final_score"]),
                 "independence_class": cls,
                 "eligible_for_blind_review": "false" if cls.startswith("B.") else "true",
                 "validation_strata": "; ".join(strata) or "baseline",
                 "order_key": order_key(r["profile_id"], uid)})
write(OUT / "doctor_candidate_pool.csv", pool)

eligible = [p for p in pool if p["eligible_for_blind_review"] == "true"]
excluded = len(pool) - len(eligible)

# ---------------- PART 9: sample size, justified from the real strata ----------------
# Primary margins the review must be able to report on.
profiles = sorted({p["profile_id"] for p in eligible})
types = sorted({p["product_type"] for p in eligible})
bands = ["high (80+)", "upper-mid (65-79)", "mid (50-64)", "low (<50)", "blocked"]

cells = defaultdict(list)
for p in eligible:
    cells[(p["profile_id"], p["product_type"], p["score_band"])].append(p)
nonempty = len(cells)

# Sizing: the binding margin is product type x score band, since per-type behaviour is
# what the audit flagged. 7 types x 4 live bands = 28 cells; >=6 per cell gives a
# standard error near 20/sqrt(6) ~ 8 points per cell and ~20/sqrt(24) ~ 4 points per
# type margin, which is enough to rank types but not to certify any single one.
TARGET = 180
sizing = {
    "population_pairs": len(audit),
    "eligible_pairs": len(eligible),
    "excluded_anchor_leaked_pairs": excluded,
    "nonempty_strata_cells": nonempty,
    "profiles": len(profiles),
    "product_types": len(types),
    "score_bands": len(bands),
    "recommended_sample": TARGET,
    "per_profile": round(TARGET / len(profiles), 1),
    "per_product_type": round(TARGET / len(types), 1),
    "per_score_band": round(TARGET / 4, 1),
}

# ---------------- selection: cover every margin, never prefer high scores ----------------
selected: list[dict] = []
taken: set[tuple[str, str]] = set()


def take(cand, reason):
    key = (cand["profile_id"], cand["canonical_product_id_v2"])
    if key in taken or len(selected) >= TARGET:
        return False
    taken.add(key)
    row = dict(cand)
    row["selection_reason"] = reason
    selected.append(row)
    return True


# 1. Known failure modes first - these are the cases most likely to disprove the model.
for label in ["FAILURE-high-score-weak-evidence", "FAILURE-low-score-strong-primary",
              "FAILURE-sunscreen-on-acne", "incidental-only-evidence", "safety-block", "weak-mapping"]:
    cands = sorted([p for p in eligible if label in p["validation_strata"]], key=lambda p: p["order_key"])
    for c in cands[:8]:
        take(c, f"failure-mode: {label}")

# 2. Guarantee every profile x score-band cell that exists.
for pid in profiles:
    for b in bands:
        cands = sorted([p for p in eligible if p["profile_id"] == pid and p["score_band"] == b],
                       key=lambda p: p["order_key"])
        for c in cands[:1]:
            take(c, f"coverage: {pid} x {b}")

# 3. Guarantee every product type x score-band cell.
for t in types:
    for b in bands:
        cands = sorted([p for p in eligible if p["product_type"] == t and p["score_band"] == b],
                       key=lambda p: p["order_key"])
        for c in cands[:1]:
            take(c, f"coverage: {t} x {b}")

# 4. Fill the remainder proportionally across profile x band, round-robin, so the final
#    sample is not dominated by whichever stratum happened to be listed first.
rr = []
for b in bands:
    for pid in profiles:
        rr.append((pid, b))
i = 0
while len(selected) < TARGET and i < 400:
    progressed = False
    for pid, b in rr:
        if len(selected) >= TARGET:
            break
        cands = sorted([p for p in eligible if p["profile_id"] == pid and p["score_band"] == b],
                       key=lambda p: p["order_key"])
        for c in cands:
            if (c["profile_id"], c["canonical_product_id_v2"]) not in taken:
                take(c, f"balance fill: {pid} x {b}")
                progressed = True
                break
    if not progressed:
        break
    i += 1

selected.sort(key=lambda r: r["order_key"])

# VERSION A - internal, everything visible
write(OUT / "final_blind_validation_sample.csv", selected)

# VERSION B - blind sheet. Only what a dermatologist should see.
BLIND = []
for idx, r in enumerate(selected, 1):
    src = pop.get(r["canonical_product_id_v2"], {})
    m = mapping.get(r["canonical_product_id_v2"], {})
    BLIND.append({
        "review_id": f"R{idx:03d}",
        "user_profile": r["profile_label"],
        "product_name": r["product_name"],
        "brand": r["brand"],
        "product_type": r["product_type"],
        "key_ingredients": (src.get("key_ingredients") or "")[:400],
        "full_inci": (src.get("ingredients") or "")[:1500],
        "doctor_suitability_score": "",
        "doctor_recommendation": "",
        "doctor_comments": "",
    })
write(OUT / "doctor_blind_review_sheet.csv", BLIND)

# blind XLSX
fmt = "csv"
try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    g = wb.active; g.title = "Instructions"
    g.column_dimensions["A"].width = 100
    INSTR = [
        ("Roopsee blind suitability review", True), ("", False),
        ("Each row is one product judged for one specific user profile.", False),
        ("Score how suitable that product is for that exact profile, using your own judgement.", False),
        ("", False),
        ("This review is deliberately blind.", True),
        ("You are not shown any system score, ranking or confidence. That is intentional:", False),
        ("your answers are the reference we measure the system against, so they must not be", False),
        ("influenced by it. There is no expected answer.", False),
        ("", False),
        ("Score scale", True), ("", False),
        ("90-100   Excellent match - you would actively recommend it for this profile", False),
        ("80-89    Great match - a strong, safe choice", False),
        ("70-79    Good match - usable, not the best option", False),
        ("50-69    Fits with caution - acceptable only with caveats", False),
        ("1-49     Not recommended - poor fit", False),
        ("-100     Not suggested at all - unsafe or contraindicated for this profile", False),
        ("", False),
        ("Use -100 only for a genuine hard block, for example a retinoid in pregnancy.", False),
        ("A merely poor fit belongs in 1-49. This distinction is how we measure safety.", False),
        ("", False),
        ("Recommendation", True), ("", False),
        ("Recommend / Recommend with caution / Do not recommend / Unsafe for this profile", False),
        ("", False),
        ("Comments", True), ("", False),
        ("Most useful where the ingredient list looks wrong or incomplete for the product,", False),
        ("or where the product type is a poor vehicle for the stated concern.", False),
        ("", False),
        ("Please edit only the three highlighted columns.", True),
    ]
    for i, (t, b) in enumerate(INSTR, 1):
        c = g.cell(row=i, column=1, value=t)
        c.font = Font(bold=b, size=12 if b and i == 1 else 11)
        c.alignment = Alignment(vertical="top")

    sh = wb.create_sheet("Review")
    cols = [("review_id", "ID", 9), ("user_profile", "User profile", 30), ("product_name", "Product", 46),
            ("brand", "Brand", 18), ("product_type", "Type", 13), ("key_ingredients", "Key ingredients", 40),
            ("full_inci", "Full INCI", 60), ("doctor_suitability_score", "DOCTOR SCORE", 14),
            ("doctor_recommendation", "DOCTOR RECOMMENDATION", 24), ("doctor_comments", "DOCTOR COMMENTS", 40)]
    hf = PatternFill("solid", fgColor="DDEEE3"); df_ = PatternFill("solid", fgColor="FFF2CC")
    for j, (k, h, w) in enumerate(cols, 1):
        c = sh.cell(row=1, column=j, value=h); c.font = Font(bold=True); c.fill = hf
        c.alignment = Alignment(vertical="center", wrap_text=True)
        sh.column_dimensions[get_column_letter(j)].width = w
    for i, r in enumerate(BLIND, 2):
        for j, (k, _h, _w) in enumerate(cols, 1):
            c = sh.cell(row=i, column=j, value=r.get(k, ""))
            c.alignment = Alignment(vertical="top", wrap_text=k in ("product_name", "key_ingredients", "full_inci"))
            if k.startswith("doctor_"): c.fill = df_
    last = len(BLIND) + 1
    sc = get_column_letter([c[0] for c in cols].index("doctor_suitability_score") + 1)
    rc = get_column_letter([c[0] for c in cols].index("doctor_recommendation") + 1)
    v1 = DataValidation(type="decimal", operator="between", formula1="-100", formula2="100", allow_blank=True,
                        error="Enter 0-100, or -100 for a hard block.", errorTitle="Invalid score")
    sh.add_data_validation(v1); v1.add(f"{sc}2:{sc}{last}")
    v2 = DataValidation(type="list", allow_blank=True,
                        formula1='"Recommend,Recommend with caution,Do not recommend,Unsafe for this profile"')
    sh.add_data_validation(v2); v2.add(f"{rc}2:{rc}{last}")
    sh.freeze_panes = "A2"; sh.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{last}"
    wb.save(OUT / "doctor_blind_review_template.xlsx")
    fmt = "xlsx"
except ImportError:
    pass

sizing["actual_sample"] = len(selected)
sizing["blind_sheet_format"] = fmt
sizing["sample_by_profile"] = dict(Counter(r["profile_id"] for r in selected).most_common())
sizing["sample_by_type"] = dict(Counter(r["product_type"] for r in selected).most_common())
sizing["sample_by_band"] = dict(Counter(r["score_band"] for r in selected).most_common())
sizing["sample_by_independence"] = dict(Counter(r["independence_class"] for r in selected).most_common())
sizing["sample_by_evidence"] = dict(Counter(r["evidence_strength"] for r in selected).most_common())
sizing["failure_mode_rows"] = sum(1 for r in selected if "FAILURE" in r["validation_strata"])
json.dump(sizing, (OUT / "sampling_metrics.json").open("w", encoding="utf-8"), indent=2)

print(f"candidate pool      : {len(pool)} product-profile pairs")
print(f"  eligible          : {len(eligible)}")
print(f"  excluded (leaked) : {excluded}")
print(f"  non-empty strata  : {nonempty}")
print(f"\nfinal blind sample  : {len(selected)} pairs ({fmt} template written)")
for k in ("sample_by_profile", "sample_by_type", "sample_by_band", "sample_by_independence", "sample_by_evidence"):
    print(f"  {k:26s} {sizing[k]}")
print(f"  failure-mode rows          {sizing['failure_mode_rows']}")
