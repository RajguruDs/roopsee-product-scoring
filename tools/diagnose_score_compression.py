"""Diagnose the 40-59 score compression in the Phase 2 experimental scorer.

DIAGNOSIS ONLY. Production, static/app.js, the Phase 2 scorer, its weights and every
dataset are untouched. This script reads and writes only into
outputs/roopsee_canonical/ingredient_first_validation/score_compression_audit/.
"""
from __future__ import annotations

import csv, json, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
OUT = REPO / "outputs" / "roopsee_canonical" / "ingredient_first_validation" / "score_compression_audit"
OUT.mkdir(parents=True, exist_ok=True)

import ingredient_first_experimental as ife  # noqa: E402

PROFILES = json.loads(
    (REPO / "outputs" / "roopsee_canonical" / "ingredient_first_experiment" / "experiment_profiles.json")
    .read_text(encoding="utf-8"))
LABEL = {p["profile_id"]: p["label"] for p in PROFILES}
BANDS = ["0-19", "20-39", "40-59", "60-69", "70-79", "80-89", "90-100"]


def band(v):
    if v is None: return "n/a"
    return "90-100" if v >= 90 else "80-89" if v >= 80 else "70-79" if v >= 70 \
        else "60-69" if v >= 60 else "40-59" if v >= 40 else "20-39" if v >= 20 else "0-19"


def stats(vals):
    v = [x for x in vals if x is not None and x > -100]
    if not v: return {}
    s = sorted(v)
    q = lambda f: s[min(len(s) - 1, max(0, int(round(f * (len(s) - 1)))))]
    d = {"n": len(s), "min": round(s[0], 1), "p10": round(q(.10), 1), "p25": round(q(.25), 1),
         "median": round(statistics.median(s), 1), "p75": round(q(.75), 1), "p90": round(q(.90), 1),
         "p95": round(q(.95), 1), "p99": round(q(.99), 1), "max": round(s[-1], 1),
         "mean": round(statistics.mean(s), 2), "stdev": round(statistics.pstdev(s), 2)}
    c = Counter(band(x) for x in s)
    for b in BANDS:
        d[f"band_{b}"] = c.get(b, 0)
        d[f"pct_{b}"] = round(100 * c.get(b, 0) / len(s), 1)
    return d


def write(path, rows, fields=None):
    if not rows:
        path.write_text("", encoding="utf-8"); return
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields or list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)


print("Score compression diagnosis")
scorer = ife.IngredientFirstScorer()
cfg = scorer.cfg
by_uid = {p["uid"]: p for p in scorer.products}

# ---------- 0. the ingredient master itself ----------
allcells = [v for r in scorer.master.values() for v in r["scores"].values()]
master_dist = Counter(allcells)
master_summary = {"cells": len(allcells),
                  "distinct_values": len(master_dist),
                  "top_values": master_dist.most_common(10),
                  "pct_equal_40": round(100 * master_dist[40] / len(allcells), 2),
                  "pct_equal_90": round(100 * master_dist[90] / len(allcells), 2),
                  "pct_in_41_89": round(100 * sum(n for v, n in master_dist.items() if 41 <= v <= 89) / len(allcells), 2)}
percol = {}
for col in ["Acne", "Open Pores", "Dehydration", "Dark Spots/Pigmentation", "Redness/Irritation",
            "Oily Score", "Dry Score", "Normal+Sensitive Score"]:
    vals = [r["scores"].get(col) for r in scorer.master.values() if r["scores"].get(col) is not None]
    c = Counter(vals)
    percol[col] = {"n": len(vals), "mean": round(statistics.mean(vals), 1),
                   "median": statistics.median(vals), "top": c.most_common(5),
                   "pct_40": round(100 * c[40] / len(vals), 1), "pct_90": round(100 * c[90] / len(vals), 1)}

# ---------- 1 + 2: instrumented run over every pair ----------
STAGES = ["concern_fit", "skin_fit", "primary_contribution", "secondary_contribution",
          "incidental_contribution", "ingredient_suitability", "product_context",
          "pre_safety", "confidence_ceiling", "final_score"]
collected = {s: [] for s in STAGES}
trace_rows = []

for profile in PROFILES:
    pid = profile["profile_id"]
    concern = profile["concern"] if profile["concern"] != "None" else None
    skin_col = ife.SKIN_COLUMN[(profile["skinType"], bool(profile["sensitive"]))]
    for prod in scorer.products:
        r = scorer.score(prod, profile)
        tiers = scorer.tiers_for(prod)
        # per-tier contribution on the CONCERN column, the decisive one
        tier_vals = {}
        for t in ("primary", "secondary", "incidental"):
            tier_vals[t] = scorer._ingredient_column_score(tiers[t], concern) if concern else None
        conf_ceiling = cfg["confidence_ceiling"].get(prod.get("confidence", "Low"), 88)

        collected["concern_fit"].append(r.concern_fit)
        collected["skin_fit"].append(r.skin_fit)
        collected["primary_contribution"].append(tier_vals["primary"])
        collected["secondary_contribution"].append(tier_vals["secondary"])
        collected["incidental_contribution"].append(tier_vals["incidental"])
        collected["ingredient_suitability"].append(r.ingredient_suitability_score)
        collected["product_context"].append(r.product_context_score)
        collected["pre_safety"].append(r.pre_safety_score)
        collected["confidence_ceiling"].append(conf_ceiling)
        collected["final_score"].append(r.final_score)

        if len(trace_rows) < 400:
            trace_rows.append({
                "profile_id": pid, "product_name": r.name, "product_type": r.product_type,
                "concern_column": concern or "", "skin_column": skin_col,
                "primary_ingredients": r.primary_ingredients,
                "primary_n": len(tiers["primary"]), "secondary_n": len(tiers["secondary"]),
                "incidental_n": len(tiers["incidental"]),
                "primary_contribution_on_concern": tier_vals["primary"],
                "secondary_contribution_on_concern": tier_vals["secondary"],
                "incidental_contribution_on_concern": tier_vals["incidental"],
                "concern_fit": r.concern_fit, "skin_fit": r.skin_fit,
                "ingredient_suitability": r.ingredient_suitability_score,
                "product_context": r.product_context_score, "type_role": r.type_role,
                "active_role": r.active_role, "pre_safety": r.pre_safety_score,
                "confidence_ceiling": conf_ceiling, "safety_cap": r.safety_cap,
                "final_score": r.final_score})

dist_rows = [{"stage": s, **stats(collected[s])} for s in STAGES]
write(OUT / "intermediate_score_distributions.csv", dist_rows)
write(OUT / "formula_trace_sample.csv", trace_rows)

print("\n=== DISTRIBUTION AT EVERY STAGE (40,370 pairs) ===")
print(f"{'stage':26s} {'mean':>7s} {'med':>6s} {'p25':>6s} {'p75':>6s} {'max':>6s} {'sd':>6s} {'%40-59':>7s} {'%80+':>6s}")
for d in dist_rows:
    if not d.get("n"): continue
    p80 = d.get("pct_80-89", 0) + d.get("pct_90-100", 0)
    print(f"{d['stage']:26s} {d['mean']:7.1f} {d['median']:6.1f} {d['p25']:6.1f} {d['p75']:6.1f} "
          f"{d['max']:6.1f} {d['stdev']:6.1f} {d.get('pct_40-59',0):6.1f}% {p80:5.1f}%")

# ---------- 4. controlled tests on real master ingredients ----------
def find(col, value, n=4, exclude=()):
    out = []
    for name, row in scorer.master.items():
        if name in exclude: continue
        if row["scores"].get(col) == value:
            out.append(name)
        if len(out) >= n: break
    return out


TEST_PROFILE = {"profile_id": "T", "label": "Oily + Acne", "skinType": "Oily", "sensitive": False,
                "age": "Adult", "gender": "female", "concern": "Acne", "specialConditions": ["None"]}
strong = find("Acne", 90, 3)
neutral = find("Acne", 40, 6)
negative = find("Acne", 0, 2) or find("Acne", -100, 1)


def synth(name, ptype, primary, secondary, incidental=()):
    return {"uid": f"SYNTH_{name}", "gtin": "", "name": name, "normalizedType": ptype,
            "confidence": "High", "families": [], "primaryIngredients": ", ".join(primary),
            "secondaryIngredients": ", ".join(secondary),
            "matchedPrimaryIngredients": "; ".join(f"{p} -> {p} (Exact or alias)" for p in primary),
            "matchedSecondaryIngredients": "; ".join(f"{p} -> {p} (Exact or alias)" for p in secondary),
            "scoreLayers": {}, "support": {}, "nearestDoctorAnchors": []}


cases = [
    ("A. one very strong primary", "serum", strong[:1], []),
    ("B. two very strong primaries", "serum", strong[:2], []),
    ("C. strong primary + strong secondary", "serum", strong[:1], strong[1:2]),
    ("D. only weak/incidental relevance", "serum", neutral[:1], neutral[1:3]),
    ("E. all neutral ingredients", "serum", neutral[:2], neutral[2:5]),
    ("F. negative ingredient present", "serum", strong[:1], negative[:1]),
    ("G. strong ingredients, WRONG type (sunscreen)", "sunscreen", strong[:2], []),
    ("H. strong ingredients, RIGHT type (serum)", "serum", strong[:2], []),
    ("I. strong primary diluted by 8 neutral secondaries", "serum", strong[:1], neutral[:6] + neutral[:2]),
]
ctrl = []
for lbl, ptype, prim, sec in cases:
    p = synth(lbl, ptype, prim, sec)
    scorer.inci_by_id[p["uid"]] = []
    r = scorer.score(p, TEST_PROFILE)
    ctrl.append({"case": lbl, "product_type": ptype, "primary": ", ".join(prim), "secondary": ", ".join(sec),
                 "primary_count": len(prim), "secondary_count": len(sec),
                 "concern_fit": r.concern_fit, "skin_fit": r.skin_fit,
                 "ingredient_suitability": r.ingredient_suitability_score,
                 "product_context": r.product_context_score, "type_role": r.type_role,
                 "active_role": r.active_role, "pre_safety": r.pre_safety_score, "final_score": r.final_score})
write(OUT / "controlled_test_results.csv", ctrl)

print("\n=== CONTROLLED TESTS (real master ingredients, Oily+Acne) ===")
print(f"  strong acne actives used : {strong[:2]}")
print(f"  neutral fillers used     : {neutral[:2]}")
print(f"{'case':50s} {'concFit':>8s} {'ingSuit':>8s} {'ctx':>5s} {'final':>6s}")
for c in ctrl:
    print(f"  {c['case']:48s} {str(c['concern_fit']):>8s} {str(c['ingredient_suitability']):>8s} "
          f"{str(c['product_context']):>5s} {c['final_score']:6d}")

# ---------- 5. sensitivity ----------
base_prim, base_sec = strong[:1], neutral[:2]
sens = []


def run(lbl, ptype, prim, sec, profile=TEST_PROFILE):
    p = synth(lbl, ptype, prim, sec)
    scorer.inci_by_id[p["uid"]] = []
    return scorer.score(p, profile)


base = run("base", "serum", base_prim, base_sec)
variants = [
    ("baseline: 1 strong primary + 2 neutral secondary", "serum", base_prim, base_sec, TEST_PROFILE),
    ("+ add a second strong primary", "serum", strong[:2], base_sec, TEST_PROFILE),
    ("- remove the strong primary", "serum", neutral[:1], base_sec, TEST_PROFILE),
    ("+ add a strong secondary", "serum", base_prim, base_sec + strong[1:2], TEST_PROFILE),
    ("+ add one neutral secondary", "serum", base_prim, base_sec + neutral[2:3], TEST_PROFILE),
    ("+ add four neutral secondaries", "serum", base_prim, base_sec + neutral[2:6], TEST_PROFILE),
    ("type -> sunscreen (ingredients unchanged)", "sunscreen", base_prim, base_sec, TEST_PROFILE),
    ("type -> moisturizer (ingredients unchanged)", "moisturizer", base_prim, base_sec, TEST_PROFILE),
    ("concern -> Pigmentation (product unchanged)", "serum", base_prim, base_sec,
     {**TEST_PROFILE, "concern": "Dark Spots/Pigmentation"}),
    ("skin -> Dry (product unchanged)", "serum", base_prim, base_sec, {**TEST_PROFILE, "skinType": "Dry"}),
    ("skin -> Normal + sensitive", "serum", base_prim, base_sec, {**TEST_PROFILE, "skinType": "Normal", "sensitive": True}),
]
for lbl, ptype, prim, sec, prof in variants:
    r = run(lbl, ptype, prim, sec, prof)
    sens.append({"variant": lbl, "product_type": ptype, "concern": prof["concern"],
                 "skin": prof["skinType"] + ("+sensitive" if prof["sensitive"] else ""),
                 "concern_fit": r.concern_fit, "ingredient_suitability": r.ingredient_suitability_score,
                 "product_context": r.product_context_score, "final_score": r.final_score,
                 "delta_vs_baseline": r.final_score - base.final_score})
write(OUT / "sensitivity_analysis.csv", sens)

print("\n=== SENSITIVITY (delta vs baseline) ===")
for s in sens:
    print(f"  {s['variant']:48s} final={s['final_score']:4d}  delta={s['delta_vs_baseline']:+4d}")

# ---------- 6/7. high and low score investigation ----------
audit = list(csv.DictReader(
    (OUT.parent / "full_4037_score_audit.csv").open(newline="", encoding="utf-8-sig")))
hi_rows, lo_rows = [], []
for r in audit:
    fs = float(r["final_score"])
    if fs >= 70:
        grp = "90+" if fs >= 90 else "80-89" if fs >= 80 else "70-79"
        hi_rows.append({"group": grp, **{k: r[k] for k in
                        ["profile_id", "product_name", "product_type", "final_score",
                         "ingredient_suitability_score", "concern_fit", "skin_fit",
                         "product_context_score", "type_role", "active_role", "confidence",
                         "evidence_strength", "safety_status", "matched_ingredient_count",
                         "primary_ingredients"]}})
    if -100 < fs < 50 and r["active_role"] == "primary":
        lo_rows.append({k: r[k] for k in
                        ["profile_id", "profile_label", "product_name", "product_type", "final_score",
                         "ingredient_suitability_score", "concern_fit", "skin_fit",
                         "product_context_score", "type_role", "active_role", "confidence",
                         "evidence_strength", "matched_ingredient_count", "primary_ingredients",
                         "secondary_ingredients", "reason_codes"]})
write(OUT / "high_score_analysis.csv", hi_rows)
write(OUT / "low_score_strong_evidence.csv", lo_rows)

hi_summary = {}
for g in ("70-79", "80-89", "90+"):
    sub = [r for r in hi_rows if r["group"] == g]
    if not sub: continue
    hi_summary[g] = {"count": len(sub),
                     "types": dict(Counter(r["product_type"] for r in sub).most_common()),
                     "evidence": dict(Counter(r["evidence_strength"] for r in sub).most_common()),
                     "active_role": dict(Counter(r["active_role"] for r in sub).most_common()),
                     "mean_matched_ingredients": round(statistics.mean(
                         [int(r["matched_ingredient_count"]) for r in sub]), 2),
                     "confidence": dict(Counter(r["confidence"] for r in sub).most_common())}

# ---------- 8/9. by type and profile ----------
pt = defaultdict(list); pf = defaultdict(list)
for r in audit:
    fs = float(r["final_score"])
    if fs <= -100: continue
    pt[r["product_type"]].append(fs)
    pf[r["profile_id"]].append(fs)
write(OUT / "product_type_score_analysis.csv",
      [{"product_type": k, **stats(v)} for k, v in sorted(pt.items(), key=lambda kv: -len(kv[1]))])
write(OUT / "profile_score_analysis.csv",
      [{"profile_id": k, "profile_label": LABEL[k], **stats(v)} for k, v in sorted(pf.items())])

# ---------- 11. doctor distribution, descriptive only ----------
doc = list(csv.DictReader((REPO / "data" / "products.csv").open(newline="", encoding="utf-8-sig")))
COLS = ["Acne", "Open Pores", "Dryness", "Dehydration", "Dark Spots/Pigmentation", "Redness/Irritation"]
dvals = []
for r in doc:
    for c in COLS:
        try:
            v = float(r.get(c, ""))
            if v > -100: dvals.append(v)
        except (TypeError, ValueError):
            pass
doctor_dist = stats(dvals)

json.dump({"master": master_summary, "master_per_column": percol,
           "stage_distributions": {d["stage"]: d for d in dist_rows},
           "high_score_summary": hi_summary,
           "low_score_strong_primary_count": len(lo_rows),
           "doctor_reference_distribution_descriptive_only": doctor_dist},
          (OUT / "compression_metrics.json").open("w", encoding="utf-8"), indent=2, default=str)

print("\n=== INGREDIENT MASTER (the raw material) ===")
print(f"  cells {master_summary['cells']}, distinct values {master_summary['distinct_values']}")
print(f"  == 40 : {master_summary['pct_equal_40']}%   == 90 : {master_summary['pct_equal_90']}%   "
      f"strictly between 41-89 : {master_summary['pct_in_41_89']}%")
print("\n=== HIGH-SCORE GROUPS ===")
for g, d in hi_summary.items():
    print(f"  {g:6s} n={d['count']:5d} types={list(d['types'].items())[:3]} evidence={d['evidence']} "
          f"meanMatched={d['mean_matched_ingredients']}")
print(f"\nlow score (<50) despite PRIMARY concern evidence: {len(lo_rows)}")
print("\n=== DOCTOR REFERENCE DISTRIBUTION (descriptive only, NOT validation) ===")
print(f"  mean {doctor_dist['mean']} median {doctor_dist['median']} "
      f"%40-59 {doctor_dist.get('pct_40-59')}% %80+ {doctor_dist.get('pct_80-89',0)+doctor_dist.get('pct_90-100',0)}%")
print(f"\nwritten to {OUT}")
