"""Evaluate every aggregation candidate. Analysis only; nothing is adopted.

Baseline (tools/ingredient_first_experimental.py), production, static/app.js, all
weights and the 180-pair dermatologist sample are untouched.
"""
from __future__ import annotations

import csv, json, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
OUT = REPO / "outputs" / "roopsee_canonical" / "ingredient_first_validation" / "aggregation_experiment"
OUT.mkdir(parents=True, exist_ok=True)

import aggregation_variants_experimental as av  # noqa: E402

PROFILES = json.loads(
    (REPO / "outputs" / "roopsee_canonical" / "ingredient_first_experiment" / "experiment_profiles.json")
    .read_text(encoding="utf-8"))
LABEL = {p["profile_id"]: p["label"] for p in PROFILES}
BANDS = ["0-19", "20-39", "40-59", "60-69", "70-79", "80-89", "90-100"]


def band(v):
    return "90-100" if v >= 90 else "80-89" if v >= 80 else "70-79" if v >= 70 \
        else "60-69" if v >= 60 else "40-59" if v >= 40 else "20-39" if v >= 20 else "0-19"


def stats(vals):
    v = [x for x in vals if x > -100]
    if not v: return {}
    s = sorted(v); q = lambda f: s[min(len(s)-1, max(0, int(round(f*(len(s)-1)))))]
    c = Counter(band(x) for x in s)
    d = {"n": len(s), "min": s[0], "p10": q(.10), "p25": q(.25), "median": round(statistics.median(s),1),
         "p75": q(.75), "p90": q(.90), "p95": q(.95), "p99": q(.99), "max": s[-1],
         "mean": round(statistics.mean(s),2), "stdev": round(statistics.pstdev(s),2)}
    for b in BANDS:
        d[f"pct_{b}"] = round(100*c.get(b,0)/len(s), 1)
    d["pct_ge_70"] = round(100*sum(1 for x in s if x>=70)/len(s), 1)
    d["pct_ge_80"] = round(100*sum(1 for x in s if x>=80)/len(s), 1)
    d["pct_ge_90"] = round(100*sum(1 for x in s if x>=90)/len(s), 1)
    return d


def write(path, rows, fields=None):
    if not rows: path.write_text("", encoding="utf-8"); return
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields or list(rows[0].keys())); w.writeheader(); w.writerows(rows)


print("Aggregation experiment — building variants")
variants = av.build_variants(verbose=False)
names = [v.variant_name for v in variants]
print("  " + ", ".join(names))

ref = variants[0]
ref_by_uid = {p["uid"]: p for p in ref.products}
master = ref.master
TEST = {"profile_id": "T", "label": "Oily + Acne", "skinType": "Oily", "sensitive": False,
        "age": "Adult", "gender": "female", "concern": "Acne", "specialConditions": ["None"]}


def find(col, value, n=30):
    return [k for k, r in master.items() if r["scores"].get(col) == value][:n]


strong = find("Acne", 90, 6)          # "Well suited"
neutral = find("Acne", 40, 30)        # "Tolerated, no particular benefit"
negative = find("Acne", 0, 3)         # "Unsuitable / no benefit"
moderate = [k for k, r in master.items() if 40 < r["scores"].get("Acne", 0) < 90][:3]
print(f"  strong={strong[:2]} moderate={moderate[:1]} neutral={neutral[:2]} negative={negative[:1]}")


def synth(uid, ptype, primary, secondary):
    return {"uid": uid, "gtin": "", "name": uid, "normalizedType": ptype, "confidence": "High",
            "families": [], "primaryIngredients": ", ".join(primary),
            "secondaryIngredients": ", ".join(secondary),
            "matchedPrimaryIngredients": "; ".join(f"{p} -> {p} (Exact or alias)" for p in primary),
            "matchedSecondaryIngredients": "; ".join(f"{p} -> {p} (Exact or alias)" for p in secondary),
            "scoreLayers": {}, "support": {}, "nearestDoctorAnchors": []}


def run(v, uid, ptype, prim, sec, profile=TEST):
    p = synth(uid, ptype, prim, sec)
    v.inci_by_id[uid] = []
    return v.score(p, profile)


# ---------------- 4. controlled primary/secondary tests ----------------
cases = [
    ("1. one strong Primary", "serum", strong[:1], []),
    ("2. one strong Primary + 3 neutral", "serum", strong[:1], neutral[:3]),
    ("3. one strong Primary + one strong Secondary", "serum", strong[:1], strong[1:2]),
    ("4. two strong Primary", "serum", strong[:2], []),
    ("5. several Secondary, no Primary", "serum", neutral[:1], strong[:2]),
    ("6. strong Primary + negative ingredient", "serum", strong[:1], negative[:1]),
    ("7. weak Primary + many neutral", "serum", moderate[:1] or neutral[:1], neutral[:8]),
    ("8. only neutral/incidental evidence", "serum", neutral[:1], neutral[1:4]),
]
ctrl = []
for lbl, ptype, prim, sec in cases:
    row = {"case": lbl, "primary": ", ".join(prim), "secondary": ", ".join(sec)}
    b = run(variants[0], f"c_{lbl}", ptype, prim, sec).final_score
    row["baseline"] = b
    for v in variants:
        s = run(v, f"c_{lbl}_{v.variant_name}", ptype, prim, sec)
        row[v.variant_name] = s.final_score
        row[f"delta_{v.variant_name}"] = s.final_score - b
    ctrl.append(row)
write(OUT / "controlled_aggregation_tests.csv", ctrl)

# ---------------- 5. INCI length sensitivity ----------------
inci = []
for n_neutral in [0, 2, 5, 10, 20]:
    row = {"neutral_ingredients_added": n_neutral}
    for v in variants:
        s = run(v, f"len_{n_neutral}_{v.variant_name}", "serum", strong[:1], neutral[:n_neutral])
        row[v.variant_name] = s.final_score
    inci.append(row)
for v in variants:
    base_v = next(r[v.variant_name] for r in inci if r["neutral_ingredients_added"] == 0)
    for r in inci:
        r[f"drift_{v.variant_name}"] = r[v.variant_name] - base_v
write(OUT / "inci_length_sensitivity.csv", inci)

# ---------------- 6. strong-evidence monotonicity ----------------
ev_cases = [
    ("A. no relevant ingredients", neutral[:2], []),
    ("B. one weak/moderate relevant", moderate[:1] or neutral[:1], []),
    ("C. one strong relevant", strong[:1], []),
    ("D. two strong relevant", strong[:2], []),
    ("E. strong Primary + strong Secondary", strong[:1], strong[1:2]),
    ("F. three strong relevant", strong[:3], []),
    ("G. strong Primary + negative", strong[:1], negative[:1]),
]
sev = []
for lbl, prim, sec in ev_cases:
    row = {"case": lbl}
    for v in variants:
        row[v.variant_name] = run(v, f"ev_{lbl}_{v.variant_name}", "serum", prim, sec).final_score
    sev.append(row)
write(OUT / "strong_evidence_sensitivity.csv", sev)

# ---------------- 7. product context separation ----------------
ctx = []
for ptype in ["serum", "cleanser", "mask", "toner", "moisturizer", "sunscreen"]:
    row = {"product_type": ptype}
    for v in variants:
        s = run(v, f"ctx_{ptype}_{v.variant_name}", ptype, strong[:2], [])
        row[f"{v.variant_name}_ingredient"] = s.ingredient_suitability_score
        row[f"{v.variant_name}_context"] = s.product_context_score
        row[f"{v.variant_name}_final"] = s.final_score
    ctx.append(row)
write(OUT / "product_context_tests.csv", ctx)

# ---------------- 8. full 40,370-pair re-run ----------------
print("\nFull re-run: 4,037 products x 10 profiles per candidate")
allscores = {}
for v in variants:
    vals, per_profile, per_type, rows_keep = [], defaultdict(list), defaultdict(list), []
    for profile in PROFILES:
        for r in v.score_all(profile):
            vals.append(r.final_score)
            per_profile[profile["profile_id"]].append(r.final_score)
            per_type[r.product_type].append(r.final_score)
            rows_keep.append((profile["profile_id"], r))
    allscores[v.variant_name] = {"vals": vals, "per_profile": per_profile,
                                 "per_type": per_type, "rows": rows_keep}
    print(f"  {v.variant_name:28s} mean={statistics.mean([x for x in vals if x>-100]):5.1f} "
          f"blocked={sum(1 for x in vals if x<=-100)}")

dist_rows = []
for name in names:
    d = stats(allscores[name]["vals"])
    dist_rows.append({"variant": name, "blocked": sum(1 for x in allscores[name]["vals"] if x <= -100), **d})
write(OUT / "full_40370_distribution.csv", dist_rows)

# per-type and per-profile
pt_rows, pf_rows = [], []
for name in names:
    for t, v_ in sorted(allscores[name]["per_type"].items()):
        pt_rows.append({"variant": name, "product_type": t, **stats(v_)})
    for p, v_ in sorted(allscores[name]["per_profile"].items()):
        pf_rows.append({"variant": name, "profile_id": p, "profile_label": LABEL[p], **stats(v_)})
write(OUT / "aggregation_method_results.csv", pt_rows)
write(OUT / "profile_score_by_variant.csv", pf_rows)

# ---------------- 9. failure modes per candidate ----------------
fails = []
for name in names:
    rows = allscores[name]["rows"]
    strong_low = sum(1 for _p, r in rows if -100 < r.final_score < 50 and r.active_role == "primary")
    weak_high = sum(1 for _p, r in rows if r.final_score >= 80 and r.evidence_strength in ("none", "weak"))
    hi = [r for _p, r in rows if r.final_score >= 90]
    lo = [r for _p, r in rows if 70 <= r.final_score < 80]
    mean_ing_hi = round(statistics.mean([len(ref.tiers_for(ref_by_uid[r.uid])["primary"]) +
                                         len(ref.tiers_for(ref_by_uid[r.uid])["secondary"])
                                         for r in hi]), 2) if hi else None
    mean_ing_lo = round(statistics.mean([len(ref.tiers_for(ref_by_uid[r.uid])["primary"]) +
                                         len(ref.tiers_for(ref_by_uid[r.uid])["secondary"])
                                         for r in lo]), 2) if lo else None
    inci_drift = next(r[f"drift_{name}"] for r in inci if r["neutral_ingredients_added"] == 20)
    negatives_matter = next(r[name] for r in sev if r["case"].startswith("G")) < \
                       next(r[name] for r in sev if r["case"].startswith("C"))
    reinforce = next(r[name] for r in sev if r["case"].startswith("D")) > \
                next(r[name] for r in sev if r["case"].startswith("C"))
    fails.append({"variant": name,
                  "strong_primary_but_score_lt50": strong_low,
                  "weak_evidence_but_score_ge80": weak_high,
                  "inci_drift_20_neutral_added": inci_drift,
                  "neutral_dilution_present": "YES" if inci_drift < -1 else "no",
                  "negative_evidence_still_lowers": "YES" if negatives_matter else "NO",
                  "multiple_actives_reinforce": "YES" if reinforce else "no",
                  "mean_ingredients_in_90plus": mean_ing_hi,
                  "mean_ingredients_in_70to79": mean_ing_lo,
                  "short_inci_advantage": ("YES" if (mean_ing_hi is not None and mean_ing_lo is not None
                                                     and mean_ing_hi < mean_ing_lo - 1) else "no"),
                  "blocked_pairs": sum(1 for x in allscores[name]["vals"] if x <= -100)})
write(OUT / "candidate_failure_analysis.csv", fails)

# ---------------- 10. ranking comparison ----------------
rank_rows = []
for name in names:
    byp = defaultdict(list)
    for pid, r in allscores[name]["rows"]:
        if r.final_score > -100:
            byp[pid].append(r)
    for pid in sorted(byp, key=lambda x: (len(x), x)):
        top = sorted(byp[pid], key=lambda r: (-r.final_score, r.name))[:20]
        tc = Counter(r.product_type for r in top)
        base_top = None
        if name != names[0]:
            bb = defaultdict(list)
            for p2, r2 in allscores[names[0]]["rows"]:
                if r2.final_score > -100: bb[p2].append(r2)
            base_top = {r.uid for r in sorted(bb[pid], key=lambda r: (-r.final_score, r.name))[:20]}
        rank_rows.append({
            "variant": name, "profile_id": pid, "profile_label": LABEL[pid],
            "top20_types": "; ".join(f"{k}:{n}" for k, n in tc.most_common()),
            "sunscreens_in_top20": tc.get("sunscreen", 0),
            "treatment_types_in_top20": sum(tc.get(t, 0) for t in ("serum", "cleanser", "mask", "toner")),
            "mean_top20_score": round(statistics.mean([r.final_score for r in top]), 1),
            "primary_evidence_in_top20": sum(1 for r in top if r.active_role == "primary"),
            "weak_evidence_in_top20": sum(1 for r in top if r.evidence_strength in ("none", "weak")),
            "overlap_with_baseline_top20": (len({r.uid for r in top} & base_top) if base_top is not None else 20)})
write(OUT / "profile_ranking_comparison.csv", rank_rows)

json.dump({"variants": [{"name": v.variant_name, "params": v.params} for v in variants],
           "distributions": dist_rows, "failure_analysis": fails,
           "controlled": ctrl, "inci_length": inci, "strong_evidence": sev},
          (OUT / "aggregation_metrics.json").open("w", encoding="utf-8"), indent=2, default=str)

print("\n=== INCI-LENGTH SENSITIVITY (1 strong primary + N neutral) ===")
hdr = f"{'N neutral':>10s} " + " ".join(f"{n[:14]:>14s}" for n in names)
print(hdr)
for r in inci:
    print(f"{r['neutral_ingredients_added']:>10d} " + " ".join(f"{r[n]:>14d}" for n in names))
print("  drift at N=20: " + ", ".join(f"{n[:12]}={r[f'drift_{n}']:+d}" for n in names
                                      for r in [inci[-1]]))

print("\n=== STRONG-EVIDENCE MONOTONICITY ===")
print(f"{'case':38s} " + " ".join(f"{n[:12]:>12s}" for n in names))
for r in sev:
    print(f"{r['case']:38s} " + " ".join(f"{r[n]:>12d}" for n in names))

print("\n=== FULL DISTRIBUTION ===")
print(f"{'variant':28s} {'mean':>6s} {'med':>5s} {'sd':>5s} {'%40-59':>7s} {'%>=70':>6s} {'%>=80':>6s} {'%>=90':>6s}")
for d in dist_rows:
    print(f"{d['variant']:28s} {d['mean']:6.1f} {d['median']:5.1f} {d['stdev']:5.1f} "
          f"{d['pct_40-59']:6.1f}% {d['pct_ge_70']:5.1f}% {d['pct_ge_80']:5.1f}% {d['pct_ge_90']:5.1f}%")

print("\n=== CANDIDATE FAILURE MODES ===")
print(f"{'variant':28s} {'dilution':>9s} {'negKept':>8s} {'reinforce':>10s} {'shortINCIadv':>13s} {'strongLow':>10s} {'weakHigh':>9s}")
for f in fails:
    print(f"{f['variant']:28s} {f['neutral_dilution_present']:>9s} {f['negative_evidence_still_lowers']:>8s} "
          f"{f['multiple_actives_reinforce']:>10s} {f['short_inci_advantage']:>13s} "
          f"{f['strong_primary_but_score_lt50']:>10d} {f['weak_evidence_but_score_ge80']:>9d}")
print(f"\nwritten to {OUT}")
