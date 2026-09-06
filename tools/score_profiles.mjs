// Score a dataset against validation profiles using the SHIPPED scoring engine.
//
// This deliberately executes static/app.js itself rather than reimplementing
// customerFacingScore()/computeScoredRows() in Python. A second implementation
// would drift from the one users actually get, and every validation number
// would then describe code that never ships.
//
// Usage:
//   node tools/score_profiles.mjs --dataset <path> --profiles <path> --out <path> [--uids <path>]
//
// Reads : dataset JSON (app.js shape), profiles JSON (list of app.js `state` patches)
// Writes: JSON { system, datasetPath, productCount, rows: [...] }
//
// Writes nothing else. It never mutates the dataset it is given.

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

function arg(name, fallback = null) {
  const index = process.argv.indexOf(`--${name}`);
  return index !== -1 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")), "..");
const datasetPath = arg("dataset");
const profilesPath = arg("profiles");
const outPath = arg("out");
const uidsPath = arg("uids");
const systemName = arg("system", "unknown");

if (!datasetPath || !profilesPath || !outPath) {
  console.error("usage: node tools/score_profiles.mjs --dataset X --profiles Y --out Z [--uids U] [--system NAME]");
  process.exit(2);
}

const dataset = JSON.parse(fs.readFileSync(datasetPath, "utf8"));
const profiles = JSON.parse(fs.readFileSync(profilesPath, "utf8"));
const wantedUids = uidsPath ? new Set(JSON.parse(fs.readFileSync(uidsPath, "utf8"))) : null;

// app.js reads dataset.quizOptions during render. The full scored population
// file has no quizOptions block, so supply the shipped defaults when absent.
if (!dataset.quizOptions) {
  dataset.quizOptions = {
    skinTypes: ["Oily", "Dry", "Normal", "Combination"],
    sensitivityOptions: ["No", "Yes"],
    faceBodyConcerns: dataset.scoreColumns.filter((c) =>
      ["Acne","Body Acne","Dryness","Open Pores","Uneven Skin Tone","Dark Spots/Pigmentation","Melasma",
       "Barrier Repair","Comedones","Wrinkles/Fine lines","Redness/Irritation","Dehydration","Dullness","Tanning"].includes(c),
    ).concat(["None"]),
    specialConditions: ["Excessive Dryness", "Pregnant", "Breastfeeding", "None"],
    ages: ["Teen", "Adult"],
    genders: ["female", "male", "other", "prefer not to say"],
  };
}

// Minimal DOM so app.js can finish loading. Scoring touches none of it.
const element = new Proxy(
  {
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    style: {},
    dataset: {},
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    setAttribute() {},
    getAttribute: () => null,
    closest: () => null,
    querySelector: () => element,
    querySelectorAll: () => [],
    focus() {},
    scrollIntoView() {},
  },
  { get: (target, key) => (key in target ? target[key] : () => element) },
);

const context = {
  document: {
    body: element,
    documentElement: element,
    querySelector: () => element,
    querySelectorAll: () => [],
    getElementById: () => element,
    createElement: () => element,
    addEventListener() {},
  },
  window: { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) },
  console: { log() {}, warn() {}, error() {} },
  fetch: async () => ({ ok: true, json: async () => dataset }),
  setTimeout,
  clearTimeout,
  Object, Array, Math, Number, String, Set, Map, JSON, Intl, Boolean, Error,
  isNaN, parseFloat, parseInt,
};
context.globalThis = context;
vm.createContext(context);

const source = fs.readFileSync(path.join(repoRoot, "static", "app.js"), "utf8");
// Top-level const/let never become context properties, so have app.js hand out
// the bindings we drive. Nothing is redefined or patched.
vm.runInContext(
  `${source}
;globalThis.__state = state;
 globalThis.__computeScoredRows = computeScoredRows;
 globalThis.__safetyAdjustment = safetyAdjustment;
 globalThis.__directProfileFit = directProfileFit;`,
  context,
  { filename: "app.js" },
);

await new Promise((resolve) => setTimeout(resolve, 250));

if (!context.__computeScoredRows) {
  console.error("app.js did not expose computeScoredRows; the dataset may be malformed");
  process.exit(1);
}

const rows = [];
for (const profile of profiles) {
  // Apply the profile exactly as the quiz would. `sensitive` is a boolean in
  // app.js (:95) -- a "No" string would be truthy and silently flip it on.
  context.__state.skinType = profile.skinType;
  context.__state.sensitive = Boolean(profile.sensitive);
  context.__state.age = profile.age;
  context.__state.gender = profile.gender;
  context.__state.concern = profile.concern;
  context.__state.specialConditions = profile.specialConditions;

  const scored = context.__computeScoredRows();

  // Rank over the WHOLE dataset before filtering to the sample, so a rank
  // reflects the real shelf the user sees rather than the sample alone.
  const ranked = scored.map((row, index) => ({ row, rank: index + 1 }));

  for (const { row, rank } of ranked) {
    const uid = row.product.uid;
    if (wantedUids && !wantedUids.has(uid)) continue;
    const safety = context.__safetyAdjustment(row.product, "anchor");
    rows.push({
      uid,
      gtin: row.product.gtin || "",
      profile_id: profile.profile_id,
      score: row.score,
      evidence_score: row.evidenceScore,
      ranking_score: Number(row.rankingScore?.toFixed?.(4) ?? row.rankingScore ?? 0),
      rank_overall: rank,
      hard_blocked: row.score <= -100,
      layer_baseline: row.featureScores.baseline,
      layer_v2: row.featureScores.v2,
      layer_anchor: row.featureScores.anchor,
      layer_type_family: row.featureScores.typeFamily,
      layer_type: row.featureScores.type,
      direct_fit: Boolean(context.__directProfileFit(row.product)),
      safety_cap: safety?.cap ?? null,
      safety_notes: (safety?.notes || []).join("; "),
      confidence: row.product.confidence,
      normalized_type: row.product.normalizedType,
      category: row.product.category,
      anchor_support: row.product.support?.anchor ?? null,
      exact_support: row.product.support?.exact ?? null,
      family_support: row.product.support?.family ?? null,
    });
  }
}

fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(
  outPath,
  JSON.stringify(
    {
      system: systemName,
      datasetPath,
      productCount: dataset.products.length,
      profileCount: profiles.length,
      rows,
    },
    null,
    0,
  ),
  "utf8",
);

process.stdout.write(
  `${systemName}: scored ${dataset.products.length} products x ${profiles.length} profiles -> ${rows.length} rows\n`,
);
