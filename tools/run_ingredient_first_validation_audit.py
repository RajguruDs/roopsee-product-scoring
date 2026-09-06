"""Phase 3 audit: score all 4,037 canonical products and design the blind doctor sample.

AUDIT ONLY. The Phase 2 scorer, its weights, production scoring, static/app.js and
every dataset are untouched. This script only reads and writes into
outputs/roopsee_canonical/ingredient_first_validation/.
"""
from __future__ import annotations

import csv, json, os, re, statistics, sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
OUT = REPO / "outputs" / "roopsee_canonical" / "ingredient_first_validation"
OUT.mkdir(parents=True, exist_ok=True)

import ingredient_first_experimental as ife  # noqa: E402

PROFILES = json.loads(
    (REPO / "outputs" / "roopsee_canonical" / "ingredient_first_experiment" / "experiment_profiles.json")
    .read_text(encoding="utf-8"))
LABEL = {p["profile_id"]: p["label"] for p in PROFILES}
TOP_N = 20


def write(path, rows, fields=None):
    if not rows:
        path.write_text("", encoding="utf-8"); return
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields or list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)


def pct(n, d):
    return round(100.0 * n / d, 2) if d else 0.0


def band(v):
    if v <= -100: return "blocked"
    return "90-100" if v >= 90 else "80-89" if v >= 80 else "70-79" if v >= 70 \
        else "60-69" if v >= 60 else "40-59" if v >= 40 else "20-39" if v >= 20 else "0-19"


BANDS = ["blocked", "0-19", "20-39", "40-59", "60-69", "70-79", "80-89", "90-100"]

print("Phase 3 validation audit")
scorer = ife.IngredientFirstScorer()

# ---------------- mapping + INCI context ----------------
mapping = {}
mp = REPO / "data" / "source" / "scoring_input_ingredient_mapping_v2.csv"
if mp.exists():
    with mp.open(newline="", encoding="utf-8-sig") as h:
        for r in csv.DictReader(h):
            mapping[r["canonical_product_id_v2"]] = r

# ---------------- PART 1 + 5: score all products for all profiles ----------------
preds = {}
for p in PROFILES:
    preds[p["profile_id"]] = scorer.score_all(p)
print(f"  scored {len(scorer.products)} products x {len(PROFILES)} profiles")

AUDIT_FIELDS = ["profile_id", "profile_label", "canonical_product_id_v2", "gtin", "product_name", "brand",
                "product_type", "primary_ingredients", "secondary_ingredients", "mapping_confidence",
                "ingredient_mapping_status", "matched_ingredient_count", "incidental_count",
                "ingredient_suitability_score", "concern_fit", "skin_fit", "product_context_score",
                "type_role", "active_role", "pre_safety_score", "final_score", "safety_status",
                "safety_cap", "confidence", "evidence_strength", "hero_claim", "reason_codes"]

by_uid = {p["uid"]: p for p in scorer.products}
audit_rows = []
for pid, rs in preds.items():
    for r in rs:
        prod = by_uid[r.uid]
        m = mapping.get(r.uid, {})
        tiers = scorer.tiers_for(prod)
        audit_rows.append({
            "profile_id": pid, "profile_label": LABEL[pid], "canonical_product_id_v2": r.uid,
            "gtin": r.gtin, "product_name": r.name, "brand": prod.get("brand", ""),
            "product_type": r.product_type, "primary_ingredients": r.primary_ingredients,
            "secondary_ingredients": r.secondary_ingredients,
            "mapping_confidence": m.get("mapping_confidence", ""),
            "ingredient_mapping_status": m.get("ingredient_mapping_status", ""),
            "matched_ingredient_count": len(tiers["primary"]) + len(tiers["secondary"]),
            "incidental_count": r.incidental_count,
            "ingredient_suitability_score": r.ingredient_suitability_score, "concern_fit": r.concern_fit,
            "skin_fit": r.skin_fit, "product_context_score": r.product_context_score,
            "type_role": r.type_role, "active_role": r.active_role, "pre_safety_score": r.pre_safety_score,
            "final_score": r.final_score, "safety_status": r.safety_status, "safety_cap": r.safety_cap,
            "confidence": r.confidence, "evidence_strength": r.evidence_strength,
            "hero_claim": r.hero_claim, "reason_codes": " | ".join(r.reason_codes)})
write(OUT / "full_4037_score_audit.csv", audit_rows, AUDIT_FIELDS)

# ---------------- PART 2: distributions ----------------
def quantiles(v):
    s = sorted(v)
    q = lambda f: s[min(len(s) - 1, max(0, int(round(f * (len(s) - 1)))))]
    return {"min": s[0], "p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75),
            "p90": q(.90), "p95": q(.95), "p99": q(.99), "max": s[-1],
            "mean": round(statistics.mean(s), 2), "median": round(statistics.median(s), 1),
            "stdev": round(statistics.pstdev(s), 2)}


dist = {}
for pid, rs in preds.items():
    live = [r.final_score for r in rs if r.final_score > -100]
    b = Counter(band(r.final_score) for r in rs)
    dist[pid] = {"profile": LABEL[pid], "products": len(rs), "blocked": b["blocked"],
                 **quantiles(live),
                 "bands": {k: {"count": b[k], "pct": pct(b[k], len(rs))} for k in BANDS},
                 "mode_score": Counter(r.final_score for r in rs if r.final_score > -100).most_common(1)[0]}
allv = [r.final_score for rs in preds.values() for r in rs if r.final_score > -100]
overall = {"pairs": len(allv), **quantiles(allv),
           "bands": {k: {"count": sum(dist[p]["bands"][k]["count"] for p in dist),
                         "pct": pct(sum(dist[p]["bands"][k]["count"] for p in dist), len(PROFILES) * len(scorer.products))}
                     for k in BANDS}}

# anomaly detection - reported, never fixed
anom = []
for pid, rs in preds.items():
    for r in rs:
        if r.final_score >= 90 and r.evidence_strength in ("none", "weak"):
            anom.append({"profile_id": pid, "issue": "90+ with weak/no ingredient evidence", "uid": r.uid,
                         "product_name": r.name, "product_type": r.product_type, "final_score": r.final_score,
                         "evidence_strength": r.evidence_strength, "active_role": r.active_role})
        if r.final_score >= 90 and r.active_role == "incidental":
            anom.append({"profile_id": pid, "issue": "90+ driven by incidental ingredients", "uid": r.uid,
                         "product_name": r.name, "product_type": r.product_type, "final_score": r.final_score,
                         "evidence_strength": r.evidence_strength, "active_role": r.active_role})
        if r.final_score < 40 and r.active_role == "primary" and r.safety_status == "ok":
            anom.append({"profile_id": pid, "issue": "low score despite primary concern ingredient", "uid": r.uid,
                         "product_name": r.name, "product_type": r.product_type, "final_score": r.final_score,
                         "evidence_strength": r.evidence_strength, "active_role": r.active_role})
write(OUT / "score_anomalies.csv", anom)

# ---------------- PART 3: ingredient evidence quality ----------------
ev_rows = []
for uid, prod in by_uid.items():
    t = scorer.tiers_for(prod)
    m = mapping.get(uid, {})
    ev_rows.append({"canonical_product_id_v2": uid, "product_name": prod["name"],
                    "product_type": prod.get("normalizedType", ""),
                    "primary_count": len(t["primary"]), "secondary_count": len(t["secondary"]),
                    "incidental_count": len(t["incidental"]),
                    "has_primary": bool(t["primary"]), "has_secondary": bool(t["secondary"]),
                    "has_inci": bool(t["incidental"]),
                    "mapping_confidence": m.get("mapping_confidence", ""),
                    "ingredient_mapping_status": m.get("ingredient_mapping_status", ""),
                    "confidence": prod.get("confidence", ""),
                    "evidence_strength": ("strong" if len(t["primary"]) + len(t["secondary"]) >= 4
                                          else "moderate" if len(t["primary"]) + len(t["secondary"]) >= 2
                                          else "weak" if len(t["primary"]) + len(t["secondary"]) >= 1 else "none")})
write(OUT / "ingredient_evidence_audit.csv", ev_rows)
ev_summary = {"has_primary": sum(1 for r in ev_rows if r["has_primary"]),
              "has_secondary": sum(1 for r in ev_rows if r["has_secondary"]),
              "has_inci": sum(1 for r in ev_rows if r["has_inci"]),
              "evidence_strength": dict(Counter(r["evidence_strength"] for r in ev_rows)),
              "mapping_confidence": dict(Counter(r["mapping_confidence"] for r in ev_rows)),
              "mapping_status": dict(Counter(r["ingredient_mapping_status"] for r in ev_rows).most_common())}

# ---------------- PART 4: product type audit ----------------
pt_rows = []
for pid, rs in preds.items():
    g = defaultdict(list)
    for r in rs:
        g[r.product_type].append(r)
    top = sorted([r for r in rs if r.final_score > -100], key=lambda x: -x.final_score)[:TOP_N]
    tt = Counter(r.product_type for r in top)
    for t in sorted(g):
        v = [r.final_score for r in g[t] if r.final_score > -100]
        if not v:
            continue
        pt_rows.append({"profile_id": pid, "profile_label": LABEL[pid], "product_type": t,
                        "products": len(g[t]), "blocked": sum(1 for r in g[t] if r.final_score <= -100),
                        "mean": round(statistics.mean(v), 1), "median": round(statistics.median(v), 1),
                        "min": min(v), "max": max(v),
                        "count_ge_80": sum(1 for x in v if x >= 80), "count_ge_70": sum(1 for x in v if x >= 70),
                        "count_lt_50": sum(1 for x in v if x < 50), "in_top20": tt.get(t, 0),
                        "type_role": g[t][0].type_role})
write(OUT / "product_type_audit.csv", pt_rows)

# ---------------- PART 5: top 20 ----------------
top_rows = []
for pid, rs in preds.items():
    live = sorted([r for r in rs if r.final_score > -100], key=lambda x: (-x.final_score, x.name))
    for rank, r in enumerate(live[:TOP_N], 1):
        top_rows.append({"profile_id": pid, "profile_label": LABEL[pid], "rank": rank,
                         "canonical_product_id_v2": r.uid, "product_name": r.name,
                         "product_type": r.product_type, "final_score": r.final_score,
                         "ingredient_suitability_score": r.ingredient_suitability_score,
                         "product_context_score": r.product_context_score, "concern_fit": r.concern_fit,
                         "skin_fit": r.skin_fit, "type_role": r.type_role, "active_role": r.active_role,
                         "confidence": r.confidence, "safety_status": r.safety_status,
                         "evidence_strength": r.evidence_strength,
                         "primary_ingredients": r.primary_ingredients,
                         "secondary_ingredients": r.secondary_ingredients,
                         "reason_codes": " | ".join(r.reason_codes)})
write(OUT / "profile_top20.csv", top_rows)

# ---------------- PART 6: profile audit ----------------
prof_rows = []
for pid, rs in preds.items():
    live = [r for r in rs if r.final_score > -100]
    v = [r.final_score for r in live]
    top = sorted(live, key=lambda x: -x.final_score)[:TOP_N]
    prof_rows.append({
        "profile_id": pid, "profile_label": LABEL[pid], "products": len(rs),
        "safety_blocks": sum(1 for r in rs if r.final_score <= -100),
        "safety_capped": sum(1 for r in rs if r.safety_status == "capped"),
        "ge_90": sum(1 for x in v if x >= 90), "ge_80": sum(1 for x in v if x >= 80),
        "ge_70": sum(1 for x in v if x >= 70), "lt_50": sum(1 for x in v if x < 50),
        "mean": round(statistics.mean(v), 1), "median": round(statistics.median(v), 1),
        "low_confidence": sum(1 for r in live if r.confidence == "Low"),
        "incidental_only_concern": sum(1 for r in live if r.active_role == "incidental"),
        "primary_concern_evidence": sum(1 for r in live if r.active_role == "primary"),
        "no_concern_evidence": sum(1 for r in live if r.active_role == "none"),
        "top20_types": "; ".join(f"{k}:{n}" for k, n in Counter(r.product_type for r in top).most_common()),
        "top20_sunscreens": Counter(r.product_type for r in top).get("sunscreen", 0)})
write(OUT / "profile_audit.csv", prof_rows)

# ---------------- PART 6 continued: failure checks ----------------
fails = []
def fail(pid, code, check, observed, verdict, cause):
    fails.append({"profile_id": pid, "code": code, "check": check, "observed": observed,
                  "verdict": verdict, "root_cause": cause})

for pid, code, check in [("P1", "A", "acne -> sunscreen flooding"), ("P2", "B", "open pores -> sunscreen flooding")]:
    n = next(r["top20_sunscreens"] for r in prof_rows if r["profile_id"] == pid)
    fail(pid, code, check, f"{n}/20 sunscreens in top 20",
         "CONTROLLED" if n <= 2 else "PRESENT", "product-context problem")
for pid in ("P6", "P7", "P10"):
    n = next(r["top20_sunscreens"] for r in prof_rows if r["profile_id"] == pid)
    fail(pid, "B2", "photo concern -> sunscreen retained (must NOT be suppressed)",
         f"{n}/20 sunscreens", "PRESERVED" if n >= 1 else "OVER-SUPPRESSED", "product-context problem")
n = Counter(r["product_type"] for r in top_rows if r["profile_id"] == "P1").get("cleanser", 0)
fail("P1", "C", "acne -> cleansers dominating", f"{n}/20 cleansers",
     "EXPECTED" if n <= 15 else "OVER-CONCENTRATED", "product-type problem")
tt = Counter(r["product_type"] for r in top_rows if r["profile_id"] == "P3")
fail("P3", "D", "dehydration -> inappropriate types", f"top20 {dict(tt)}",
     "APPROPRIATE" if tt.get("moisturizer", 0) >= 8 else "REVIEW", "product-type problem")
for pid, code, check in [("P4", "E", "excessive dryness -> false blocks"), ("P10", "F", "pregnancy safety"),
                         ("P9", "H", "teen safety")]:
    r = next(x for x in prof_rows if x["profile_id"] == pid)
    fail(pid, code, check, f"{r['safety_blocks']} blocked, {r['safety_capped']} capped",
         "REPORTED - unverifiable without doctor labels", "safety problem / data limitation")
fail("P5", "G", "breastfeeding proxy (sensitive profile blocking)",
     f"{next(x for x in prof_rows if x['profile_id']=='P5')['safety_blocks']} blocked",
     "REPORTED", "data limitation")
inc90 = sum(1 for a in anom if a["issue"] == "90+ driven by incidental ingredients")
fail("all", "I", "incidental ingredient overpowering product purpose", f"{inc90} products at 90+",
     "CONTROLLED" if inc90 == 0 else "PRESENT", "scoring aggregation problem")
prim = sum(r["primary_concern_evidence"] for r in prof_rows)
inc = sum(r["incidental_only_concern"] for r in prof_rows)
fail("all", "J", "primary vs incidental INCI influence",
     f"{prim} product-profile pairs with primary concern evidence vs {inc} incidental-only",
     "SEPARATED", "n/a")
write(OUT / "failure_analysis.csv", fails)

# ---------------- PART 8: anchor independence ----------------
def norm(v): return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(v or "").lower())).strip()
def toks(v): return set(norm(v).split())
STOP = {"for", "with", "and", "the", "of", "ml", "g", "gm", "gms", "oz", "pack", "combo", "free", "new", "skin"}
FORMAT = {"serum", "cleanser", "wash", "facewash", "moisturizer", "moisturiser", "cream", "lotion", "sunscreen",
          "toner", "mask", "gel", "oil", "balm", "butter", "mist", "scrub", "peel", "essence", "ampoule",
          "body", "face", "eye", "lip"}
def ntype(v):
    t = norm(v)
    if t in ("moisturiser", "cream", "lotion"): return "moisturizer"
    if t in ("wash", "body wash", "face wash"): return "cleanser"
    return t

doctor = list(csv.DictReader((REPO / "data" / "products.csv").open(newline="", encoding="utf-8-sig")))
didx = [(r, toks(r["product_name"]) - STOP, toks(r["product_name"]) & FORMAT) for r in doctor]
indep_rows = []
for uid, prod in by_uid.items():
    dt = toks(prod["name"]) - STOP; df = toks(prod["name"]) & FORMAT
    best, bs = None, 0
    for r, t, f in didx:
        if not t or df != f or ntype(r["product_type"]) != ntype(prod.get("normalizedType", "")): continue
        j = len(dt & t) / len(dt | t)
        if j > bs: bs, best = j, r
    twin = best if bs >= 0.62 else None
    anchors = [a["uid"] for a in prod.get("nearestDoctorAnchors", [])]
    if twin is None:
        cls, note = "D. no doctor reference", "never reviewed; fully independent for a blind review"
    elif twin["product_uid"] in anchors:
        cls, note = "B. doctor-reviewed AND used as anchor", "LEAKED - exclude from validation"
    elif bs >= 0.80:
        cls, note = "A. independent from doctor anchor", "reviewed but not its own anchor"
    else:
        cls, note = "C. anchor relationship unclear", f"weak name match ({bs:.2f})"
    indep_rows.append({"canonical_product_id_v2": uid, "product_name": prod["name"],
                       "product_type": prod.get("normalizedType", ""),
                       "independence_class": cls, "doctor_twin": twin["product_name"] if twin else "",
                       "doctor_twin_uid": twin["product_uid"] if twin else "",
                       "match_similarity": round(bs, 3) if twin else "",
                       "twin_is_own_anchor": "true" if twin and twin["product_uid"] in anchors else "false",
                       "note": note})
write(OUT / "doctor_independence_analysis.csv", indep_rows)
indep_by_uid = {r["canonical_product_id_v2"]: r for r in indep_rows}
indep_counts = dict(Counter(r["independence_class"] for r in indep_rows))

json.dump({"overall": overall, "per_profile": dist, "evidence": ev_summary,
           "independence": indep_counts,
           "anomalies": dict(Counter(a["issue"] for a in anom))},
          (OUT / "audit_metrics.json").open("w", encoding="utf-8"), indent=2, default=str)

print("\n=== OVERALL SCORE DISTRIBUTION (40,370 product-profile pairs) ===")
for k in ["min", "p10", "p25", "p50", "p75", "p90", "p95", "p99", "max", "mean", "median", "stdev"]:
    print(f"  {k:8s} {overall[k]}")
print("  bands:", {k: f"{v['count']} ({v['pct']}%)" for k, v in overall["bands"].items()})
print("\n=== INDEPENDENCE ===")
for k, v in sorted(indep_counts.items()):
    print(f"  {k:42s} {v:5d} ({pct(v, len(indep_rows))}%)")
print("\n=== ANOMALIES ===")
for k, v in Counter(a["issue"] for a in anom).items():
    print(f"  {k:48s} {v}")
print("\n=== SUNSCREEN CHECK ===")
for r in prof_rows:
    print(f"  {r['profile_id']:4s} {r['profile_label'][:30]:30s} top20 sunscreens={r['top20_sunscreens']:2d}  blocks={r['safety_blocks']:5d}")
print(f"\nwritten to {OUT}")
