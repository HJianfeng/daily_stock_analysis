"""Select fresh AI-reviewed A-share candidates and optionally send the AlphaSift brief."""
import argparse
import json
import math
import os
import re
from pathlib import Path


def load_run(track: Path, entry: dict, expected_track: str) -> dict:
    run_id = entry["run_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid screening run id")
    run = json.loads((track / "data/runs" / f"{run_id}.json").read_text())
    if (run.get("market"), run.get("track"), run.get("strategy")) != (
        "cn", expected_track, entry["strategy"]
    ):
        raise ValueError("Unexpected screening market, track or strategy")
    return run


def select_candidates(track: Path, trade_date: str, report: dict) -> list[dict]:
    if any(report.get(key) != trade_date for key in (
        "trade_date", "pack_trade_date", "expected_trade_date"
    )) or report.get("pack_is_stale") is not False:
        raise ValueError("AlphaSift report or daily pack is not from the requested date")
    if report.get("ai_brief", {}).get("mode") != "llm":
        raise ValueError("AlphaSift AI brief degraded; refusing rule-only output")
    log = json.loads((track / "data/logs" / f"{trade_date}-asia.json").read_text())
    if log.get("effective_dates", {}).get("cn") != trade_date:
        raise ValueError("AlphaSift snapshot is not from the requested trade date")
    rules = [e for e in log.get("entries", []) if e.get("market") == "cn"]
    if not rules or any(e.get("status") != "ok" for e in rules):
        raise ValueError("A-share screening incomplete")
    required = {e["strategy"] for e in rules if e.get("picks", 0) > 0}
    ai_entries = {
        e["strategy"]: e for e in log.get("ai_entries", [])
        if e.get("market") == "cn" and e.get("strategy") in required
    }
    if not required or set(ai_entries) != required or any(
        e.get("status") != "ok" for e in ai_entries.values()
    ):
        raise ValueError("AI review missing or degraded for a nonempty rule strategy")
    risk_by_code = report.get("announcement_risks", {}).get("by_code", {})
    candidates = {}
    for strategy in sorted(required):
        run = load_run(track, ai_entries[strategy], "ai")
        if run.get("llm_ranked") is not True:
            raise ValueError("AI run has no successful model ranking")
        for pick in run.get("picks", []):
            code = pick.get("code", "")
            if not re.fullmatch(r"[0-9]{6}", code):
                continue
            # Unscored tail picks were not reviewed by the model.
            if pick.get("llm_score") is None:
                continue
            if not math.isfinite(float(pick["llm_score"])):
                raise ValueError("Invalid model score")
            item = candidates.setdefault(code, {
                "code": code, "name": pick.get("name", ""),
                "strategies": set(), "scores": [], "flags": set(),
            })
            item["strategies"].add(strategy)
            item["flags"].update(pick.get("risk_flags") or [])
            item["flags"].update(pick.get("announcement_risk_flags") or [])
            item["flags"].update(risk_by_code.get(code, {}).get("negative_event_flags") or [])
            if pick.get("risk_level") not in (None, "", "low"):
                item["flags"].add("elevated_risk")
            score = float(pick["final_score"])
            if not math.isfinite(score):
                raise ValueError("Invalid candidate score")
            item["scores"].append(score)
    ranked = [
        {"code": p["code"], "name": p["name"], "strategies": sorted(p["strategies"]),
         "mean_score": sum(p["scores"]) / len(p["scores"])}
        for p in candidates.values() if not p["flags"]
    ]
    ranked.sort(key=lambda p: (-len(p["strategies"]), -p["mean_score"], p["code"]))
    if not ranked:
        raise ValueError("No unflagged AI-reviewed candidates; no DSA analysis dispatched")
    return ranked[:5]


def push_succeeded(result: dict) -> bool:
    if result.get("errors"):
        return False
    if result.get("status") == "ok":
        return result.get("sent") == ["feishu"]
    skipped = result.get("skipped", [])
    return result.get("status") == "skipped_duplicate" and bool(skipped) and all(
        s.get("channel") == "feishu" and s.get("reason") == "duplicate_same_trade_date"
        for s in skipped
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("track", type=Path)
    parser.add_argument("trade_date")
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    picks = select_candidates(args.track, args.trade_date, report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "trade_date": args.trade_date, "track": "ai",
        "codes": [p["code"] for p in picks], "picks": picks,
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"Selected {len(picks)} fresh AI-reviewed candidates for DSA")
    if args.push:
        from alphasift.notify import push_daily_report

        selected = "、".join(f"{p['name']}({p['code']})" for p in picks)
        report["im_card_markdown"] = (
            f"**AlphaSift AI 已复核 · DSA 待分析**\n{selected}\n\n"
            + report["im_card_markdown"]
        )
        keyword = os.environ.get("FEISHU_WEBHOOK_KEYWORD", "").strip()
        if keyword:
            report["im_card_markdown"] = keyword + "\n" + report["im_card_markdown"]
        result = push_daily_report(report, channels=["feishu"])
        args.output.with_name("alphasift-push.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        if not push_succeeded(result):
            raise RuntimeError("AlphaSift Feishu push failed; inspect alphasift-push.json")
        print(f"AlphaSift Feishu brief: {result['status']}")


if __name__ == "__main__":
    main()
