"""Run the Phase 2 experimental scorer and produce every Phase 2 output.

Analysis only. Reads production data, writes exclusively into
outputs/roopsee_canonical/ingredient_first_experiment/. Never touches
static/app.js, production weights, datasets, or any existing test.
"""
from __future__ import annotations

import csv, json, os, re, statistics, subprocess, sys, tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
OUT = REPO_ROOT / "outputs" / "roopsee_canonical" / "ingredient_first_experiment"
OUT.mkdir(parents=True, exist_ok=True)

import ingredient_first_experimental as ife  # noqa: E402

# Ten profiles, all values valid against quizOptions. `sensitive` is a boolean.
PROFILES = [
    {"profile_id": "P1", "label": "Oily + Acne", "skinType": "Oily", "sensitive": False,
     "age": "Adult", "gender": "female", "concern": "Acne", "specialConditions": ["None"]},
    {"profile_id": "P2", "label": "Oily + Open Pores", "skinType": "Oily", "sensitive": False,
     "age": "Adult", "gender": "female", "concern": "Open Pores", "specialConditions": ["None"]},
    {"profile_id": "P3", "label": "Dry + Dehydration", "skinType": "Dry", "sensitive": False,
     "age": "Adult", "gender": "female", "concern": "Dehydration", "specialConditions": ["None"]},
    {"profile_id": "P4", "label": "Dry + Excessive Dryness", "skinType": "Dry", "sensitive": False,
     "age": "Adult", "gender": "female", "concern": "Dryness", "specialConditions": ["Excessive Dryness"]},
    {"profile_id": "P5", "label": "Sensitive + Redness", "skinType": "Normal", "sensitive": True,
     "age": "Adult", "gender": "female", "concern": "Redness/Irritation", "specialConditions": ["None"]},
    {"profile_id": "P6", "label": "Combination + Pigmentation", "skinType": "Combination", "sensitive": False,
     "age": "Adult", "gender": "female", "concern": "Dark Spots/Pigmentation", "specialConditions": ["None"]},
    {"profile_id": "P7", "label": "Oily + Pigmentation", "skinType": "Oily", "sensitive": False,
     "age": "Adult", "gender": "female", "concern": "Dark Spots/Pigmentation", "specialConditions": ["None"]},
    {"profile_id": "P8", "label": "Dry + Acne", "skinType": "Dry", "sensitive": False,
     "age": "Adult", "gender": "female", "concern": "Acne", "specialConditions": ["None"]},
    {"profile_id": "P9", "label": "Teen + Acne", "skinType": "Oily", "sensitive": False,
     "age": "Teen", "gender": "female", "concern": "Acne", "specialConditions": ["None"]},
    {"profile_id": "P10", "label": "Pregnancy + Pigmentation", "skinType": "Normal", "sensitive": False,
     "age": "Adult", "gender": "female", "concern": "Dark Spots/Pigmentation", "specialConditions": ["Pregnant"]},
]

TOP_N = 20


def write(path: Path, rows: list[dict], fields=None):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields or list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


print("Phase 2 experimental run")
scorer = ife.IngredientFirstScorer()
(OUT / "experiment_profiles.json").write_text(json.dumps(PROFILES, indent=2), encoding="utf-8")

# ---------------- experimental scores ----------------
preds: dict[str, list] = {}
for profile in PROFILES:
    preds[profile["profile_id"]] = scorer.score_all(profile)
    print(f"  {profile['profile_id']:4s} {profile['label']:28s} scored {len(preds[profile['profile_id']])}")

PRED_FIELDS = ["profile_id", "profile_label", "uid", "gtin", "product_name", "product_type", "confidence",
               "ingredient_suitability_score", "concern_fit", "skin_fit", "product_context_score",
               "type_role", "active_role", "pre_safety_score", "safety_status", "safety_cap",
               "final_score", "evidence_strength", "hero_claim", "incidental_count",
               "primary_ingredients", "secondary_ingredients", "reason_codes"]

label = {p["profile_id"]: p["label"] for p in PROFILES}
all_rows = []
for pid, rs in preds.items():
    for r in rs:
        all_rows.append({
            "profile_id": pid, "profile_label": label[pid], "uid": r.uid, "gtin": r.gtin,
            "product_name": r.name, "product_type": r.product_type, "confidence": r.confidence,
            "ingredient_suitability_score": r.ingredient_suitability_score, "concern_fit": r.concern_fit,
            "skin_fit": r.skin_fit, "product_context_score": r.product_context_score,
            "type_role": r.type_role, "active_role": r.active_role, "pre_safety_score": r.pre_safety_score,
            "safety_status": r.safety_status, "safety_cap": r.safety_cap, "final_score": r.final_score,
            "evidence_strength": r.evidence_strength, "hero_claim": r.hero_claim,
            "incidental_count": r.incidental_count, "primary_ingredients": r.primary_ingredients,
            "secondary_ingredients": r.secondary_ingredients, "reason_codes": " | ".join(r.reason_codes)})
write(OUT / "experimental_predictions.csv", all_rows, PRED_FIELDS)

# layer-specific extracts
write(OUT / "ingredient_scores.csv",
      [{k: r[k] for k in ["profile_id", "uid", "product_name", "product_type",
                          "ingredient_suitability_score", "concern_fit", "skin_fit",
                          "evidence_strength", "incidental_count", "primary_ingredients"]} for r in all_rows])
write(OUT / "product_context_scores.csv",
      [{k: r[k] for k in ["profile_id", "uid", "product_name", "product_type",
                          "product_context_score", "type_role", "active_role", "hero_claim"]} for r in all_rows])

# ---------------- top 20 ----------------
top_rows = []
for pid, rs in preds.items():
    live = [r for r in rs if r.final_score > -100]
    for rank, r in enumerate(sorted(live, key=lambda x: (-x.final_score, x.name))[:TOP_N], 1):
        top_rows.append({
            "profile_id": pid, "profile_label": label[pid], "rank": rank, "product_name": r.name,
            "product_type": r.product_type, "final_score": r.final_score,
            "ingredient_suitability_score": r.ingredient_suitability_score,
            "concern_fit": r.concern_fit, "skin_fit": r.skin_fit,
            "product_context_score": r.product_context_score, "type_role": r.type_role,
            "active_role": r.active_role, "confidence": r.confidence, "safety_status": r.safety_status,
            "evidence_strength": r.evidence_strength, "hero_claim": r.hero_claim,
            "primary_ingredients": r.primary_ingredients, "secondary_ingredients": r.secondary_ingredients,
            "reason_codes": " | ".join(r.reason_codes)})
write(OUT / "profile_top20.csv", top_rows)

# ---------------- distributions ----------------
def buckets(vals):
    b = Counter()
    for v in vals:
        b["blocked" if v <= -100 else "90-100" if v >= 90 else "80-89" if v >= 80
          else "70-79" if v >= 70 else "50-69" if v >= 50 else "1-49"] += 1
    return b


dist_rows = []
for pid, rs in preds.items():
    v = [r.final_score for r in rs]
    live = [x for x in v if x > -100]
    b = buckets(v)
    dist_rows.append({"profile_id": pid, "profile_label": label[pid], "products": len(v),
                      "blocked": b["blocked"], "mean": round(statistics.mean(live), 1),
                      "median": round(statistics.median(live), 1), "min": min(live), "max": max(live),
                      "stdev": round(statistics.pstdev(live), 1),
                      "pct_90_100": round(100 * b["90-100"] / len(v), 1),
                      "pct_80_89": round(100 * b["80-89"] / len(v), 1),
                      "pct_70_79": round(100 * b["70-79"] / len(v), 1),
                      "pct_50_69": round(100 * b["50-69"] / len(v), 1),
                      "pct_1_49": round(100 * b["1-49"] / len(v), 1),
                      "top20_types": "; ".join(f"{k}:{n}" for k, n in Counter(
                          r.product_type for r in sorted([x for x in rs if x.final_score > -100],
                                                         key=lambda x: -x.final_score)[:TOP_N]).most_common())})
write(OUT / "score_distributions.csv", dist_rows)

# ---------------- current vs experimental ----------------
print("\nScoring the CURRENT production engine on the same profiles (static/app.js, unmodified)")
cur_by = {}
with tempfile.TemporaryDirectory() as tmp:
    pj = Path(tmp) / "profiles.json"
    pj.write_text(json.dumps(PROFILES), encoding="utf-8")
    outp = Path(tmp) / "cur.json"
    r = subprocess.run(["node", str(REPO_ROOT / "tools" / "score_profiles.mjs"),
                        "--dataset", str(REPO_ROOT / "static" / "data" / "final_scored_products.json"),
                        "--profiles", str(pj), "--out", str(outp), "--system", "current"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(r.stderr)
    print("  " + r.stdout.strip())
    for row in json.loads(outp.read_text(encoding="utf-8"))["rows"]:
        cur_by[(row["profile_id"], row["uid"])] = row

exp_by = {(pid, r.uid): r for pid, rs in preds.items() for r in rs}
cmp_rows = []
for (pid, uid), crow in cur_by.items():
    e = exp_by.get((pid, uid))
    if e is None:
        continue
    cmp_rows.append({"profile_id": pid, "profile_label": label[pid], "product_name": e.name,
                     "product_type": e.product_type, "confidence": e.confidence,
                     "current_final_score": crow["score"], "current_evidence_score": crow["evidence_score"],
                     "current_hard_blocked": crow["hard_blocked"],
                     "experimental_ingredient_score": e.ingredient_suitability_score,
                     "experimental_context_score": e.product_context_score,
                     "experimental_final_score": e.final_score,
                     "experimental_type_role": e.type_role, "experimental_active_role": e.active_role,
                     "experimental_safety_status": e.safety_status,
                     "difference": (e.final_score - crow["score"]) if crow["score"] > -100 and e.final_score > -100 else "",
                     "primary_ingredients": e.primary_ingredients,
                     "secondary_ingredients": e.secondary_ingredients,
                     "reason_codes": " | ".join(e.reason_codes)})
write(OUT / "current_vs_experimental.csv", cmp_rows)

# ---------------- failure analysis ----------------
fail = []


def add(profile_id, check, observed, verdict, cause, detail):
    fail.append({"profile_id": profile_id, "check": check, "observed": observed,
                 "verdict": verdict, "root_cause": cause, "detail": detail})


def top_types(pid, source):
    if source == "exp":
        live = [r for r in preds[pid] if r.final_score > -100]
        return Counter(r.product_type for r in sorted(live, key=lambda x: -x.final_score)[:TOP_N])
    rows = [v for (p, _u), v in cur_by.items() if p == pid]
    rows = sorted([r for r in rows if r["score"] > -100], key=lambda r: -r["score"])[:TOP_N]
    return Counter(r["normalized_type"] for r in rows)


for pid, cname in [("P1", "A. sunscreen flooding on acne"), ("P2", "B. sunscreen flooding on open pores")]:
    ce, cc = top_types(pid, "exp"), top_types(pid, "cur")
    add(pid, cname, f"current {cc.get('sunscreen',0)}/20 sunscreens -> experimental {ce.get('sunscreen',0)}/20",
        "RESOLVED" if ce.get("sunscreen", 0) <= 2 else "NOT RESOLVED", "product-context problem",
        "production reads only the skin column for sunscreen and lets one acne-family ingredient lift the "
        "relevance cap 74->100; the experimental scorer reads the concern column for every type and never "
        "promotes the role from ingredient evidence")

for pid in ("P6", "P7"):
    ce = top_types(pid, "exp")
    add(pid, "legitimate sunscreen retention on photo concerns",
        f"experimental {ce.get('sunscreen',0)}/20 sunscreens",
        "PRESERVED" if ce.get("sunscreen", 0) >= 1 else "LOST", "n/a" if ce.get("sunscreen", 0) >= 1 else "product-context problem",
        "sunscreen is TREATMENT for photo concerns under the same rule, so it should still rank")

ce = top_types("P1", "exp")
add("P1", "C. cleansers dominating acne", f"experimental cleansers {ce.get('cleanser',0)}/20",
    "EXPECTED" if ce.get("cleanser", 0) <= 15 else "OVER-CONCENTRATED", "product-type problem",
    "cleanser is treatment for acne concerns under the existing rule; heavy presence is expected, total "
    "dominance would indicate the concern column is doing too little work")

ce = top_types("P3", "exp")
add("P3", "D. moisturizers dominating dehydration", f"experimental moisturizers {ce.get('moisturizer',0)}/20",
    "EXPECTED" if ce.get("moisturizer", 0) <= 16 else "OVER-CONCENTRATED", "product-type problem",
    "moisturizer is treatment for hydration concerns")

blocked_p4 = sum(1 for r in preds["P4"] if r.final_score <= -100)
cur_p4 = sum(1 for (p, _u), v in cur_by.items() if p == "P4" and v["score"] <= -100)
add("P4", "E. false blocks for excessive dryness",
    f"current {cur_p4}/1000 blocked; experimental {blocked_p4}/4037 blocked "
    f"({100*blocked_p4/len(preds['P4']):.1f}% vs {100*cur_p4/1000:.1f}%)",
    "REPORTED", "safety problem / data limitation",
    "both systems block on the same Excessive Dryness column; without doctor labels for these products "
    "neither over- nor under-blocking can be confirmed")

for pid, cond, cname in [("P10", "Pregnant", "F. pregnancy safety"), ("P9", "Teen", "H. teen safety")]:
    blocked = sum(1 for r in preds[pid] if r.safety_status == "hard_block")
    capped = sum(1 for r in preds[pid] if r.safety_status == "capped")
    add(pid, cname, f"experimental {blocked} blocked, {capped} capped", "ACTIVE", "n/a",
        "safety is a separate gate applied after scoring and is never averaged away")

inc = [r for r in preds["P1"] if r.active_role == "incidental" and r.final_score >= 80]
add("P1", "I/J. incidental ingredient overpowering product purpose",
    f"{len(inc)} products scoring >=80 on incidental-only relevance",
    "CONTROLLED" if len(inc) < 30 else "PRESENT", "scoring aggregation problem",
    "the role step penalises incidental-only relevance and the ceiling is never raised by it")

for pid in [p["profile_id"] for p in PROFILES]:
    v = [r.final_score for r in preds[pid] if r.final_score > -100]
    hi = 100 * sum(1 for x in v if x >= 90) / len(v)
    cv = [r["score"] for (p, _u), r in cur_by.items() if p == pid and r["score"] > -100]
    chi = 100 * sum(1 for x in cv if x >= 90) / len(cv) if cv else 0
    add(pid, "K. compression near 90-100", f"current {chi:.1f}% vs experimental {hi:.1f}% at 90+",
        "IMPROVED" if hi < chi else "WORSE", "scoring aggregation problem",
        "production applies uplift ladders that lift well-supported fits into the nineties; the "
        "experimental scorer has no uplift stage")

weak = [r for r in preds["P1"] if r.evidence_strength in ("none", "weak") and r.final_score >= 80]
add("P1", "L. weak ingredient evidence scoring high", f"{len(weak)} products",
    "CONTROLLED" if len(weak) < 50 else "PRESENT", "data limitation",
    "products with fewer than two claimed ingredients that still score >=80; a confidence ceiling applies "
    "but there is no explicit evidence-volume penalty")

write(OUT / "failure_analysis.csv", fail)

json.dump({"config": scorer.cfg, "profiles": PROFILES, "products_scored": len(scorer.products),
           "distributions": dist_rows,
           "note": "Phase 2 performs no doctor calibration; see doctor_calibration_readiness.md"},
          (OUT / "calibration_metrics.json").open("w", encoding="utf-8"), indent=2, default=str)

print("\n=== TOP-20 PRODUCT TYPES: current vs experimental ===")
print(f"{'profile':5s} {'concern':26s} {'current':32s} {'experimental':32s}")
for p in PROFILES:
    pid = p["profile_id"]
    cc = "; ".join(f"{k}:{n}" for k, n in top_types(pid, "cur").most_common(4))
    ce = "; ".join(f"{k}:{n}" for k, n in top_types(pid, "exp").most_common(4))
    print(f"{pid:5s} {p['concern'][:26]:26s} {cc[:32]:32s} {ce[:32]:32s}")
print(f"\nwritten to {OUT}")
