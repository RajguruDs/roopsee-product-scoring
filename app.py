"""Local development server for the Roopsee static testing UI.

Also hosts a LOCALHOST-ONLY Experimental V3 scoring endpoint, opt-in via
`?scorer=v3` in the browser. The production entrypoint is api/index.py, which is
untouched and has no V3 code, so nothing here can reach a deployment.

Default behaviour is unchanged: without the query flag the UI loads the shipped
dataset and scores with the existing production JavaScript scorer.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
TOOLS_DIR = ROOT / "tools"
SHIPPED_DATASET = STATIC_DIR / "data" / "final_scored_products.json"

# --------------------------------------------------------------------------
# Experimental V3 - localhost only
# --------------------------------------------------------------------------
# Loaded lazily on first use so the plain static server keeps starting instantly
# and stays usable even if the experimental modules are absent.

_v3_lock = threading.Lock()
_v3_state: dict = {"scorer": None, "dataset": None, "error": None}


def _load_v3():
    """Load the ORIGINAL validated V3 scorer and build its UI-shaped dataset.

    Deliberately imports `ingredient_first_v3_experimental` only. The reverted
    broad tie-refinement (`v3_tie_refinement_experimental`) is never imported and
    would refuse to run anyway.
    """
    with _v3_lock:
        if _v3_state["scorer"] is not None or _v3_state["error"] is not None:
            return _v3_state
        try:
            if str(TOOLS_DIR) not in sys.path:
                sys.path.insert(0, str(TOOLS_DIR))
            import ingredient_first_v3_experimental as v3  # noqa: PLC0415

            scorer = v3.IngredientFirstV3(verbose=False)

            # The UI needs quizOptions and scoreColumns; the canonical population
            # itself comes from the same list V3 scores, so the products shown and
            # the products scored cannot diverge.
            shipped = json.loads(SHIPPED_DATASET.read_text(encoding="utf-8"))
            _v3_state["dataset"] = {
                "metadata": {
                    **shipped["metadata"],
                    "scorer": "experimental_v3",
                    "productCount": len(scorer.products),
                    "note": "Experimental V3 (localhost only). Score ceiling is 90.",
                },
                "quizOptions": shipped["quizOptions"],
                "scoreColumns": shipped["scoreColumns"],
                "products": scorer.products,
            }
            _v3_state["scorer"] = scorer
        except Exception:
            _v3_state["error"] = traceback.format_exc()
        return _v3_state


def _v3_score(profile: dict) -> dict:
    """Score every canonical product for one profile with the validated V3 scorer.

    Ranking note: V3 has no ranking implementation. The old 55%-doctor-anchor rank
    fusion is deliberately NOT reused here, so `rankingScore` is returned as null
    and ordering is by V3 score with a deterministic name tie-break.
    """
    state = _load_v3()
    if state["error"]:
        return {"ok": False, "error": state["error"]}

    scorer = state["scorer"]
    normalised = {
        "profile_id": "ui",
        "skinType": profile.get("skinType", "Oily"),
        "sensitive": bool(profile.get("sensitive", False)),
        "age": profile.get("age", "Adult"),
        "gender": profile.get("gender", "female"),
        "concern": profile.get("concern", "None"),
        "specialConditions": profile.get("specialConditions") or ["None"],
    }

    rows, failures = [], []
    for product in scorer.products:
        try:
            p = scorer.score(product, normalised)
        except Exception as exc:  # a scoring failure must be visible, never silently defaulted
            failures.append({"uid": product.get("uid"), "error": repr(exc)})
            continue
        rows.append({
            "uid": p.uid,
            "score": p.final_score,
            # V3's own components, for the existing detail view. No doctor-anchor,
            # calibrated or type-prior layer contributes to a V3 score.
            "ingredientSuitability": p.ingredient_suitability_score,
            "concernEvidence": p.concern_evidence,
            "skinFit": p.skin_fit,
            "productContext": p.product_context_score,
            "safetyCap": p.safety_cap,
            "typeRole": p.type_role,
            "activeRole": p.active_role,
            "strongestPositiveIngredient": p.strongest_positive_ingredient,
            "strongestPositiveValue": p.strongest_positive_value,
            "strongestNegativeIngredient": p.strongest_negative_ingredient,
            "negativePenalty": p.negative_penalty,
            "neutralIngredientCount": p.neutral_ingredient_count,
            "strongPositiveCount": p.strong_positive_count,
            "evidenceStrength": p.evidence_strength,
            "safetyStatus": p.safety_status,
            "reasonCodes": p.reason_codes,
            "rankingScore": None,
        })

    scores = [r["score"] for r in rows if r["score"] > -100]
    return {
        "ok": True,
        "scorer": "experimental_v3",
        "profile": normalised,
        "productsScored": len(rows),
        "failures": failures,
        "fallbacks": 0,
        "scoreRange": {"min": min(scores), "max": max(scores)} if scores else None,
        "blocked": sum(1 for r in rows if r["score"] <= -100),
        "ranking": "v3_score_desc_then_name (V3 has no rank-fusion implementation)",
        "rows": rows,
    }


class RoopseeStaticHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".html": "text/html",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=300")
        super().end_headers()

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({
                "ok": True,
                "service": "roopsee-final-match-platform",
                "dataset": "static/data/final_scored_products.json",
            })
            return

        # Localhost-only: the canonical 4,037-product population, shaped like the
        # shipped dataset so the existing UI can render it without any changes.
        if parsed.path == "/api/v3/dataset":
            state = _load_v3()
            if state["error"]:
                self._send_json({"ok": False, "error": state["error"]},
                                HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(state["dataset"])
            return

        requested = STATIC_DIR / parsed.path.lstrip("/")
        if parsed.path == "/" or not requested.exists():
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/v3/score":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            profile = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._send_json({"ok": False, "error": f"bad request: {exc!r}"},
                            HTTPStatus.BAD_REQUEST)
            return
        result = _v3_score(profile)
        self._send_json(result, HTTPStatus.OK if result.get("ok")
                        else HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    port = int(os.getenv("PORT", "8020"))
    host = os.getenv("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), RoopseeStaticHandler)
    print(f"Roopsee final match platform running at http://{host}:{port}")
    print(f"  production scorer : http://127.0.0.1:{port}/")
    print(f"  Experimental V3   : http://127.0.0.1:{port}/?scorer=v3   (localhost only)")
    server.serve_forever()


if __name__ == "__main__":
    main()
