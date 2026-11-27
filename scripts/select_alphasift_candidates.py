"""Select up to five fresh, unflagged A-share research candidates."""
import json
import math
import re
import sys
from pathlib import Path


def select_candidates(track: Path, trade_date: str) -> list[dict]:
    log = json.loads((track / "data/logs" / f"{trade_date}-asia.json").read_text())
    if log.get("effective_dates", {}).get("cn") != trade_date:
        raise ValueError("AlphaSift snapshot is not from the requested trade date")
    entries = [e for e in log.get("entries", []) if e.get("market") == "cn"]
    if not entries or any(e.get("status") != "ok" for e in entries):
        raise ValueError("A-share screening incomplete; refusing partial or stale picks")
    candidates = {}
    for entry in entries:
        run_id = entry["run_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("Invalid screening run id")
        run = json.loads((track / "data/runs" / f"{run_id}.json").read_text())
        if run.get("market") != "cn" or run.get("track") != "rules":
            raise ValueError("Unexpected screening market or track")
        for pick in run.get("picks", []):
            code = pick.get("code", "")
            if not re.fullmatch(r"[0-9]{6}", code):
                continue
            item = candidates.setdefault(code, {"code": code, "name": pick.get("name", ""), "strategies": set(), "scores": [], "flags": set()})
            item["strategies"].add(run["strategy"])
            item["flags"].update(pick.get("risk_flags") or [])
            item["flags"].update(pick.get("announcement_risk_flags") or [])
            if pick.get("risk_level") not in (None, "", "low"):
                item["flags"].add("elevated_risk")
            score = float(pick["final_score"])
            if not math.isfinite(score):
                raise ValueError("Invalid candidate score")
            item["scores"].append(score)
    ranked = []
    for item in candidates.values():
        if item["flags"]:
            continue
        ranked.append({"code": item["code"], "name": item["name"], "strategies": sorted(item["strategies"]), "mean_score": sum(item["scores"]) / len(item["scores"])})
    ranked.sort(key=lambda p: (-len(p["strategies"]), -p["mean_score"], p["code"]))
    if not ranked:
        raise ValueError("No unflagged candidates; no DSA analysis dispatched")
    return ranked[:5]


if __name__ == "__main__":
    picks = select_candidates(Path(sys.argv[1]), sys.argv[2])
    output = Path(sys.argv[3])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"trade_date": sys.argv[2], "codes": [p["code"] for p in picks], "picks": picks}, ensure_ascii=False, indent=2) + "\n")
    print(f"Selected {len(picks)} fresh candidates for DSA")
