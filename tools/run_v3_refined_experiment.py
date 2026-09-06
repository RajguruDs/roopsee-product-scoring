"""Validate the V3 tie refinement across all 40,370 pairs. Analysis only."""
from __future__ import annotations

import csv, json, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
OUT = REPO / "outputs" / "roopsee_canonical" / "ingredient_first_v3" / "tie_refinement"
OUT.mkdir(parents=True, exist_ok=True)

import v3_tie_refinement_experimental as ref  # noqa: E402

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
    s = sorted(v); q = lambda f: s[min(len(s)-1, max(0, int(round(f*(len(s)-1)))))]
    def pc(lo, hi): return round(100*sum(1 for x in s if lo <= x <= hi)/len(s), 2)
    return {"n": len(s), "min": round(s[0],1), "p10": round(q(.10),1), "p25": round(q(.25),1),
            "median": round(statistics.median(s),1), "p75": round(q(.75),1), "p90": round(q(.90),1),
            "max": round(s[-1],1), "mean": round(statistics.mean(s),2), "stdev": round(statistics.pstdev(s),2),
            "pct_0_19": pc(0,19), "pct_20_39": pc(20,39), "pct_40_59": pc(40,59),
            "pct_60_79": pc(60,79.999), "pct_80_89": pc(80,89.999), "pct_90": pc(90,90),
            "distinct_values": len(set(s))}


print("V3 tie-refinement validation")
S = ref.V3TieRefinedScorer(verbose=False)
master = S.master

# ---------------- control tests ----------------
TEST = {"profile_id": "T", "label": "Oily + Acne", "skinType": "Oily", "sensitive": False,
        "age": "Adult", "gender": "female", "concern": "Acne", "specialConditions": ["None"]}
pick = lambda col, val, n: [k for k, r in master.items() if r["scores"].get(col) == val][:n]
strong, neutral, negative = pick("Acne", 90, 4), pick("Acne", 40, 20), pick("Acne", 0, 2)


def synth(uid, ptype, prim, sec):
    return {"uid": uid, "gtin": "", "name": uid, "normalizedType": ptype, "confidence": "High",
            "families": [], "primaryIngredients": ", ".join(prim), "secondaryIngredients": ", ".join(sec),
            "matchedPrimaryIngredients": "; ".join(f"{p} -> {p} (Exact or alias)" for p in prim),
            "matchedSecondaryIngredients": "; ".join(f"{p} -> {p} (Exact or alias)" for p in sec),
            "scoreLayers": {}, "support": {}, "nearestDoctorAnchors": []}


def run(uid, ptype, prim, sec, profile=TEST):
    p = synth(uid, ptype, prim, sec)
    S.inci_by_id[uid] = []
    return S.score_refined(p, profile)


CONTROLS = [
    ("1. strong Primary 90 + 8 neutral 40s", "serum", strong[:1], neutral[:8], "close to 90"),
    ("2. strong Primary + strong Secondary + neutrals", "serum", strong[:1], strong[1:2]+neutral[:4], "within 90 ceiling"),
    ("3. strong Primary 90 + genuine negative 0", "serum", strong[:1], negative[:1], "penalty remains"),
    ("4. only neutral 40 ingredients", "serum", neutral[:2], neutral[2:6], "remains neutral"),
    ("5. no relevant ingredients, long INCI", "serum", neutral[:3], neutral[3:18], "no reward for length"),
    ("6a. strong actives in SERUM", "serum", strong[:2], [], "context appropriate"),
    ("6b. strong actives in SUNSCREEN", "sunscreen", strong[:2], [], "context still matters"),
    ("7a. strong Primary, 1 neutral", "serum", strong[:1], neutral[:1], "baseline"),
    ("7b. strong Primary, 15 neutral", "serum", strong[:1], neutral[:15], "MUST equal 7a"),
]
ctrl = []
for lbl, ptype, prim, sec, expect in CONTROLS:
    r = run(f"c_{lbl}", ptype, prim, sec)
    ctrl.append({"test": lbl, "expectation": expect, "product_type": ptype, "n_secondary": len(sec),
                 "original_v3": r.original_v3_score, "refined_v3": r.refined_v3_score,
                 "adjustment": r.score_adjustment, "evidence_quality": r.evidence_quality,
                 "q_depth": r.q_depth, "q_tier": r.q_tier, "q_headroom": r.q_headroom,
                 "neutral_count": r.base.neutral_ingredient_count,
                 "strong_count": r.base.strong_positive_count,
                 "negative_penalty": r.base.negative_penalty})
write(OUT / "refined_control_tests.csv", ctrl)

print("\n=== CONTROL TESTS ===")
print(f"{'test':50s} {'orig':>6s} {'refined':>8s} {'adj':>6s} {'neutrals':>9s}")
for c in ctrl:
    print(f"  {c['test']:48s} {c['original_v3']:6.0f} {c['refined_v3']:8.1f} {c['adjustment']:+6.1f} {c['neutral_count']:9d}")
same = abs(next(c for c in ctrl if c["test"].startswith("7a"))["refined_v3"] -
           next(c for c in ctrl if c["test"].startswith("7b"))["refined_v3"]) < 1e-9
print(f"\n  TEST 7 (long-INCI dilution check): 7a == 7b -> {same}")

# ---------------- full run ----------------
print("\nFull run: 4,037 x 10")
orig, refined_all, rows = [], [], []
failures = 0
per_profile = defaultdict(list)
for profile in PROFILES:
    pid = profile["profile_id"]
    try:
        preds = S.score_all_refined(profile)
    except Exception as exc:  # pragma: no cover
        failures += 1; print(f"  FAILURE {pid}: {exc}"); continue
    ref.mark_resolved_ties(preds)
    per_profile[pid] = preds
    for r in preds:
        b = r.base
        orig.append(r.original_v3_score); refined_all.append(r.refined_v3_score)
        rows.append({
            "profile_id": pid, "profile_label": LABEL[pid], "uid": b.uid, "gtin": b.gtin,
            "product_name": b.name, "product_type": b.product_type, "confidence": b.confidence,
            "original_v3_score": r.original_v3_score, "refined_v3_score": r.refined_v3_score,
            "score_adjustment": r.score_adjustment, "tie_resolved": r.tie_resolved,
            "evidence_quality": r.evidence_quality, "q_depth": r.q_depth, "q_tier": r.q_tier,
            "q_headroom": r.q_headroom,
            "strong_positive_count": b.strong_positive_count,
            "strongest_positive_tier": b.strongest_positive_tier,
            "strongest_positive_ingredient": b.strongest_positive_ingredient,
            "strongest_negative_ingredient": b.strongest_negative_ingredient,
            "negative_penalty": b.negative_penalty,
            "neutral_ingredient_count": b.neutral_ingredient_count,
            "concern_evidence": b.concern_evidence, "skin_fit": b.skin_fit,
            "ingredient_suitability_score": b.ingredient_suitability_score,
            "product_context_score": b.product_context_score, "type_role": b.type_role,
            "safety_status": b.safety_status, "safety_cap": b.safety_cap,
            "evidence_strength": b.evidence_strength,
            "refinement_reason": r.refinement_reason})
write(OUT / "v3_refined_predictions.csv", rows)

so, sr = stats(orig), stats(refined_all)
write(OUT / "v3_original_vs_refined_distribution.csv",
      [{"version": "V3 original", **so}, {"version": "V3 refined", **sr}])

live = [(o, r) for o, r in zip(orig, refined_all) if o > -100]
adj = [r - o for o, r in live]
changed = [a for a in adj if abs(a) > 1e-9]
change_summary = {
    "live_pairs": len(live), "changed": len(changed),
    "pct_changed": round(100*len(changed)/len(live), 2),
    "increased": sum(1 for a in adj if a > 1e-9), "decreased": sum(1 for a in adj if a < -1e-9),
    "unchanged": sum(1 for a in adj if abs(a) <= 1e-9),
    "mean_abs_adjustment": round(statistics.mean([abs(a) for a in adj]), 3),
    "max_positive": round(max(adj), 2), "max_negative": round(min(adj), 2),
    "median_adjustment": round(statistics.median(adj), 2)}

# ---------------- tie analysis ----------------
def tie_groups(vals_by_profile, key):
    groups = 0; pairs = 0; sizes = Counter()
    for pid, preds in vals_by_profile.items():
        g = defaultdict(list)
        for r in preds:
            if r.original_v3_score <= -100: continue
            g[round(getattr(r, key), 6)].append(r)
        for v in g.values():
            if len(v) > 1:
                groups += 1; pairs += len(v); sizes[len(v)] += 1
    return groups, pairs, sizes


bg, bp, bs = tie_groups(per_profile, "original_v3_score")
ag, ap, asz = tie_groups(per_profile, "refined_v3_score")
resolved = sum(1 for preds in per_profile.values() for r in preds if r.tie_resolved)
retained = bp - ap
tie_summary = {"before_groups": bg, "before_pairs_in_ties": bp,
               "before_size_distribution": dict(sorted(bs.items(), reverse=True)[:8]),
               "after_groups": ag, "after_pairs_in_ties": ap,
               "pairs_freed_from_ties": bp - ap,
               "pairs_in_groups_the_refinement_separated": resolved,
               "pairs_still_tied_equivalent_evidence": ap}

print("\n=== DISTRIBUTION: original vs refined ===")
print(f"{'':14s} {'mean':>6s} {'med':>6s} {'p10':>6s} {'p25':>6s} {'p75':>6s} {'p90':>6s} {'max':>6s} "
      f"{'distinct':>9s} {'%40-59':>7s} {'%60-79':>7s} {'%80-89':>7s} {'%90':>6s}")
for lbl, s in (("V3 original", so), ("V3 refined", sr)):
    print(f"{lbl:14s} {s['mean']:6.1f} {s['median']:6.1f} {s['p10']:6.1f} {s['p25']:6.1f} {s['p75']:6.1f} "
          f"{s['p90']:6.1f} {s['max']:6.1f} {s['distinct_values']:9d} {s['pct_40_59']:6.1f}% "
          f"{s['pct_60_79']:6.1f}% {s['pct_80_89']:6.1f}% {s['pct_90']:5.1f}%")
print("\n=== SCORE CHANGES ===")
for k, v in change_summary.items(): print(f"  {k:24s} {v}")
print("\n=== TIES ===")
for k, v in tie_summary.items(): print(f"  {k:42s} {v}")

# ---------------- examples ----------------
ex_res, ex_kept = [], []
for pid, preds in per_profile.items():
    g = defaultdict(list)
    for r in preds:
        if r.original_v3_score <= -100: continue
        g[r.original_v3_score].append(r)
    for score, members in g.items():
        if len(members) < 2: continue
        distinct = sorted({m.refined_v3_score for m in members})
        if len(distinct) > 1 and len(ex_res) < 60:
            members.sort(key=lambda m: -m.refined_v3_score)
            for m in (members[0], members[-1]):
                ex_res.append({"profile": LABEL[pid], "product_name": m.base.name,
                               "product_type": m.base.product_type,
                               "original_v3_score": m.original_v3_score,
                               "refined_v3_score": m.refined_v3_score,
                               "adjustment": m.score_adjustment,
                               "strong_positive_count": m.base.strong_positive_count,
                               "strongest_positive_tier": m.base.strongest_positive_tier,
                               "strongest_positive_ingredient": m.base.strongest_positive_ingredient,
                               "neutral_count": m.base.neutral_ingredient_count,
                               "product_context_score": m.base.product_context_score,
                               "evidence_quality": m.evidence_quality,
                               "reason": m.refinement_reason})
        elif len(distinct) == 1 and len(ex_kept) < 30:
            for m in members[:2]:
                ex_kept.append({"profile": LABEL[pid], "product_name": m.base.name,
                                "product_type": m.base.product_type,
                                "original_v3_score": m.original_v3_score,
                                "refined_v3_score": m.refined_v3_score,
                                "strong_positive_count": m.base.strong_positive_count,
                                "strongest_positive_tier": m.base.strongest_positive_tier,
                                "neutral_count": m.base.neutral_ingredient_count,
                                "product_context_score": m.base.product_context_score,
                                "evidence_quality": m.evidence_quality,
                                "why_still_tied": "identical evidence quality"})
write(OUT / "tie_resolution_examples.csv", ex_res)
write(OUT / "tie_retained_examples.csv", ex_kept)

# ---------------- safety checks ----------------
live_ref = [r for r in refined_all if r > -100]
safety = {
    "max_score": max(live_ref), "min_score": min(live_ref),
    "no_score_above_90": max(live_ref) <= 90.0,
    "no_score_below_0": min(live_ref) >= 0.0,
    "blocked_unchanged": sum(1 for x in orig if x <= -100) == sum(1 for x in refined_all if x <= -100),
    "blocked_count": sum(1 for x in refined_all if x <= -100),
    "neutral_count_used_in_refinement": "neutral_ingredient_count" in
        (REPO / "tools" / "v3_tie_refinement_experimental.py").read_text(encoding="utf-8").split("NEVER uses")[0],
    "doctor_data_referenced": any(t in (REPO / "tools" / "v3_tie_refinement_experimental.py").read_text(encoding="utf-8")
                                  for t in ("products.csv", "doctor_score", "nearestDoctorAnchors")),
    "test7_long_inci_equal": same,
}
json.dump({"distribution": {"original": so, "refined": sr}, "changes": change_summary,
           "ties": tie_summary, "controls": ctrl, "safety": safety, "config": S.refine},
          (OUT / "refinement_metrics.json").open("w", encoding="utf-8"), indent=2, default=str)

print("\n=== SAFETY CHECKS ===")
for k, v in safety.items(): print(f"  {k:38s} {v}")
print(f"\nexamples: {len(ex_res)} resolved, {len(ex_kept)} retained")
print(f"written to {OUT}")
