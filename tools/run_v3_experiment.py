"""Test V3 against V2. Analysis only; nothing merged into production."""
from __future__ import annotations

import csv, json, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
OUT = REPO / "outputs" / "roopsee_canonical" / "ingredient_first_v3"
OUT.mkdir(parents=True, exist_ok=True)

import ingredient_first_experimental as v2mod  # noqa: E402
import ingredient_first_v3_experimental as v3mod  # noqa: E402

PROFILES = json.loads(
    (REPO / "outputs" / "roopsee_canonical" / "ingredient_first_experiment" / "experiment_profiles.json")
    .read_text(encoding="utf-8"))
LABEL = {p["profile_id"]: p["label"] for p in PROFILES}


def write(path, rows, fields=None):
    if not rows:
        path.write_text("", encoding="utf-8"); return
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields or list(rows[0].keys())); w.writeheader(); w.writerows(rows)


def stats(vals):
    v = [x for x in vals if x > -100]
    if not v: return {}
    s = sorted(v); q = lambda f: s[min(len(s)-1, max(0, int(round(f*(len(s)-1)))))]
    def pc(lo, hi): return round(100*sum(1 for x in s if lo <= x <= hi)/len(s), 2)
    return {"n": len(s), "min": s[0], "p10": q(.10), "p25": q(.25),
            "median": round(statistics.median(s), 1), "p75": q(.75), "p90": q(.90),
            "max": s[-1], "mean": round(statistics.mean(s), 2), "stdev": round(statistics.pstdev(s), 2),
            "pct_0_19": pc(0, 19), "pct_20_39": pc(20, 39), "pct_40_59": pc(40, 59),
            "pct_60_79": pc(60, 79), "pct_80_89": pc(80, 89), "pct_90_100": pc(90, 100),
            "pct_ge_80": round(100*sum(1 for x in s if x >= 80)/len(s), 2),
            "pct_ge_90": round(100*sum(1 for x in s if x >= 90)/len(s), 2),
            "pct_lt_50": round(100*sum(1 for x in s if x < 50)/len(s), 2)}


print("V3 experiment")
V2 = v2mod.IngredientFirstScorer(verbose=False)
V3 = v3mod.IngredientFirstV3(verbose=False)
master = V3.master
print(f"  column ceilings: Acne={V3.column_ceiling('Acne')} Oily Score={V3.column_ceiling('Oily Score')} "
      f"Pregnancy Score={V3.column_ceiling('Pregnancy Score')}")

TEST = {"profile_id": "T", "label": "Oily + Acne", "skinType": "Oily", "sensitive": False,
        "age": "Adult", "gender": "female", "concern": "Acne", "specialConditions": ["None"]}


def pick(col, val, n):
    return [k for k, r in master.items() if r["scores"].get(col) == val][:n]


strong = pick("Acne", 90, 4)
neutral = pick("Acne", 40, 20)
negative = pick("Acne", 0, 2)
print(f"  strong={strong[:2]}  neutral={neutral[:2]}  negative={negative[:1]}")


def synth(uid, ptype, prim, sec):
    return {"uid": uid, "gtin": "", "name": uid, "normalizedType": ptype, "confidence": "High",
            "families": [], "primaryIngredients": ", ".join(prim), "secondaryIngredients": ", ".join(sec),
            "matchedPrimaryIngredients": "; ".join(f"{p} -> {p} (Exact or alias)" for p in prim),
            "matchedSecondaryIngredients": "; ".join(f"{p} -> {p} (Exact or alias)" for p in sec),
            "scoreLayers": {}, "support": {}, "nearestDoctorAnchors": []}


def run(scorer, uid, ptype, prim, sec, profile=TEST):
    p = synth(uid, ptype, prim, sec)
    scorer.inci_by_id[uid] = []
    return scorer.score(p, profile)


# ---------------- control tests ----------------
CONTROLS = [
    ("Test 1: strong Primary 90 + 8 neutral 40s", "serum", strong[:1], neutral[:8], "concern evidence stays ~90"),
    ("Test 2: strong Primary 90 + strong Secondary 90 + neutrals", "serum", strong[:1], strong[1:2] + neutral[:4],
     "stays ~90, must NOT reach 98/100"),
    ("Test 3: strong Primary 90 + genuine negative 0", "serum", strong[:1], negative[:1], "meaningful penalty"),
    ("Test 4: only neutral 40 ingredients", "serum", neutral[:2], neutral[2:6], "stays ~neutral"),
    ("Test 5: no relevant ingredients (long INCI)", "serum", neutral[:3], neutral[3:18], "no reward for length"),
    ("Test 6a: strong acne actives in a SERUM", "serum", strong[:2], [], "appropriate vehicle"),
    ("Test 6b: strong acne actives in a SUNSCREEN", "sunscreen", strong[:2], [], "context must limit it"),
    ("Test 7a: strong Primary, SHORT inci", "serum", strong[:1], neutral[:1], "baseline"),
    ("Test 7b: strong Primary, LONG inci", "serum", strong[:1], neutral[:15], "must match 7a"),
]
ctrl = []
for lbl, ptype, prim, sec, expect in CONTROLS:
    a = run(V2, f"v2_{lbl}", ptype, prim, sec)
    b = run(V3, f"v3_{lbl}", ptype, prim, sec)
    ctrl.append({"test": lbl, "expectation": expect, "product_type": ptype,
                 "primary": ", ".join(prim), "n_secondary": len(sec),
                 "v2_concern_fit": a.concern_fit, "v3_concern_evidence": b.concern_evidence,
                 "v2_ingredient": a.ingredient_suitability_score, "v3_ingredient": b.ingredient_suitability_score,
                 "v3_context": b.product_context_score, "v3_type_role": b.type_role,
                 "v2_final": a.final_score, "v3_final": b.final_score,
                 "v3_strongest_positive": b.strongest_positive_ingredient,
                 "v3_strongest_negative": b.strongest_negative_ingredient,
                 "v3_negative_penalty": b.negative_penalty,
                 "v3_neutral_count": b.neutral_ingredient_count,
                 "v3_strong_count": b.strong_positive_count,
                 "v3_evidence_strength": b.evidence_strength})
write(OUT / "v3_control_tests.csv", ctrl)

print("\n=== CONTROL TESTS ===")
print(f"{'test':52s} {'V2concern':>10s} {'V3concern':>10s} {'V2final':>8s} {'V3final':>8s}")
for c in ctrl:
    print(f"  {c['test']:50s} {str(c['v2_concern_fit']):>10s} {str(c['v3_concern_evidence']):>10s} "
          f"{c['v2_final']:>8d} {c['v3_final']:>8d}")

# ---------------- full 40,370-pair run ----------------
print("\nFull run: 4,037 products x 10 profiles, V2 and V3")
v2_scores, v3_scores, pairs = [], [], []
failures = 0
v3_rows = []
for profile in PROFILES:
    pid = profile["profile_id"]
    try:
        a_list = V2.score_all(profile)
        b_list = V3.score_all(profile)
    except Exception as exc:  # pragma: no cover
        failures += 1
        print(f"  FAILURE on {pid}: {exc}")
        continue
    for a, b in zip(a_list, b_list):
        v2_scores.append(a.final_score); v3_scores.append(b.final_score)
        pairs.append((pid, a, b))
        v3_rows.append({
            "profile_id": pid, "profile_label": LABEL[pid], "uid": b.uid, "gtin": b.gtin,
            "product_name": b.name, "product_type": b.product_type, "confidence": b.confidence,
            "primary_concern_evidence": b.primary_concern_evidence,
            "secondary_concern_evidence": b.secondary_concern_evidence,
            "incidental_concern_evidence": b.incidental_concern_evidence,
            "strongest_positive_ingredient": b.strongest_positive_ingredient,
            "strongest_positive_value": b.strongest_positive_value,
            "strongest_positive_tier": b.strongest_positive_tier,
            "strongest_negative_ingredient": b.strongest_negative_ingredient,
            "strongest_negative_value": b.strongest_negative_value,
            "negative_penalty": b.negative_penalty,
            "neutral_ingredient_count": b.neutral_ingredient_count,
            "strong_positive_count": b.strong_positive_count,
            "concern_evidence": b.concern_evidence, "skin_fit": b.skin_fit,
            "ingredient_suitability_score": b.ingredient_suitability_score,
            "product_context_score": b.product_context_score, "type_role": b.type_role,
            "active_role": b.active_role, "pre_safety_score": b.pre_safety_score,
            "safety_status": b.safety_status, "safety_cap": b.safety_cap,
            "final_score_v3": b.final_score, "final_score_v2": a.final_score,
            "delta_v3_minus_v2": b.final_score - a.final_score if (a.final_score > -100 and b.final_score > -100) else "",
            "evidence_strength": b.evidence_strength, "hero_claim": b.hero_claim,
            "reason_codes": " | ".join(b.reason_codes)})
write(OUT / "v3_full_predictions.csv", v3_rows)

s2, s3 = stats(v2_scores), stats(v3_scores)
write(OUT / "v2_vs_v3_distribution.csv",
      [{"version": "V2", **s2, "blocked": sum(1 for x in v2_scores if x <= -100)},
       {"version": "V3", **s3, "blocked": sum(1 for x in v3_scores if x <= -100)}])

deltas = [b.final_score - a.final_score for _p, a, b in pairs if a.final_score > -100 and b.final_score > -100]
delta_summary = {"pairs_compared": len(deltas), "mean_delta": round(statistics.mean(deltas), 2),
                 "median_delta": round(statistics.median(deltas), 1),
                 "delta_ge_plus10": sum(1 for d in deltas if d >= 10),
                 "delta_le_minus10": sum(1 for d in deltas if d <= -10),
                 "unchanged": sum(1 for d in deltas if d == 0)}

print("\n=== EXECUTION ===")
print(f"  total pairs       : {len(v3_scores)}")
print(f"  failures          : {failures}")
print(f"  V2 blocked        : {sum(1 for x in v2_scores if x <= -100)}")
print(f"  V3 blocked        : {sum(1 for x in v3_scores if x <= -100)}")
print("\n=== V2 vs V3 DISTRIBUTION ===")
print(f"{'':8s} {'mean':>6s} {'med':>6s} {'p10':>5s} {'p25':>5s} {'p75':>5s} {'p90':>5s} {'max':>5s} "
      f"{'%40-59':>7s} {'%>=80':>7s} {'%>=90':>7s} {'%<50':>7s}")
for lbl, s in (("V2", s2), ("V3", s3)):
    print(f"{lbl:8s} {s['mean']:6.1f} {s['median']:6.1f} {s['p10']:5.0f} {s['p25']:5.0f} {s['p75']:5.0f} "
          f"{s['p90']:5.0f} {s['max']:5.0f} {s['pct_40_59']:6.1f}% {s['pct_ge_80']:6.1f}% "
          f"{s['pct_ge_90']:6.1f}% {s['pct_lt_50']:6.1f}%")
print(f"\n  delta V3-V2: mean {delta_summary['mean_delta']:+.2f}  median {delta_summary['median_delta']:+.1f}  "
      f">=+10: {delta_summary['delta_ge_plus10']}  <=-10: {delta_summary['delta_le_minus10']}")

# ---------------- top 20 per profile ----------------
top_rows, rank_summary = [], []
byp = defaultdict(list)
for pid, _a, b in pairs:
    if b.final_score > -100:
        byp[pid].append(b)
for pid in sorted(byp, key=lambda p: (len(p), p)):
    top = sorted(byp[pid], key=lambda r: (-r.final_score, r.name))[:20]
    tc = Counter(r.product_type for r in top)
    for rank, r in enumerate(top, 1):
        top_rows.append({"profile_id": pid, "profile_label": LABEL[pid], "rank": rank,
                         "product_name": r.name, "product_type": r.product_type,
                         "final_score": r.final_score, "concern_evidence": r.concern_evidence,
                         "skin_fit": r.skin_fit, "product_context_score": r.product_context_score,
                         "type_role": r.type_role, "active_role": r.active_role,
                         "strongest_positive_ingredient": r.strongest_positive_ingredient,
                         "strongest_positive_value": r.strongest_positive_value,
                         "neutral_ingredient_count": r.neutral_ingredient_count,
                         "strong_positive_count": r.strong_positive_count,
                         "evidence_strength": r.evidence_strength, "confidence": r.confidence,
                         "safety_status": r.safety_status})
    rank_summary.append({"profile_id": pid, "profile_label": LABEL[pid],
                         "top20_types": "; ".join(f"{k}:{n}" for k, n in tc.most_common()),
                         "sunscreens": tc.get("sunscreen", 0),
                         "treatment_types": sum(tc.get(t, 0) for t in ("serum", "cleanser", "mask", "toner")),
                         "with_positive_evidence": sum(1 for r in top if r.strongest_positive_ingredient),
                         "no_positive_evidence": sum(1 for r in top if not r.strongest_positive_ingredient),
                         "mean_score": round(statistics.mean([r.final_score for r in top]), 1),
                         "mean_neutral_count": round(statistics.mean([r.neutral_ingredient_count for r in top]), 1)})
write(OUT / "v3_profile_top20.csv", top_rows)
write(OUT / "v3_profile_ranking_summary.csv", rank_summary)

json.dump({"v2": s2, "v3": s3, "delta": delta_summary, "controls": ctrl,
           "column_ceilings": {c: V3.column_ceiling(c) for c in ["Acne", "Oily Score", "Pregnancy Score", "<16"]},
           "config": V3.v3},
          (OUT / "v3_metrics.json").open("w", encoding="utf-8"), indent=2, default=str)

print("\n=== TOP-20 BY PROFILE (V3) ===")
print(f"{'prof':5s} {'concern':26s} {'sun':>4s} {'treat':>6s} {'withEvid':>9s} {'noEvid':>7s} {'mean':>6s}")
for r in rank_summary:
    print(f"{r['profile_id']:5s} {r['profile_label'][:26]:26s} {r['sunscreens']:4d} {r['treatment_types']:6d} "
          f"{r['with_positive_evidence']:9d} {r['no_positive_evidence']:7d} {r['mean_score']:6.1f}")
print(f"\nwritten to {OUT}")
