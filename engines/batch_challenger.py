#!/usr/bin/env python3
"""batch_challenger.py - Multi-Bug Batch Injection Challenge System

CORE: Inject multiple bugs into one RTL. Verifier runs simulations,
finds bugs across multiple rounds. Score based on bug difficulty points.
Tier: S(夯) A(强) B(中) C(弱) D(拉)
"""

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from rtl_mutator import (
    RTLMutator, MutCategory, MutLevel, MutantFile, MutantSpec,
    POINTS, _OPERATORS
)

# ===========================================================================
# Data Structures
# ===========================================================================

@dataclass
class Bug:
    bug_id: str = ""
    operator: str = ""
    category: str = ""
    level: str = ""
    points: int = 50
    severity: str = "medium"
    description: str = ""
    line_no: int = 0
    original_text: str = ""
    mutated_text: str = ""
    source_file: str = ""
    kill_hint: str = ""
    status: str = "hidden"   # hidden / found

@dataclass
class Challenge:
    challenge_id: str = ""
    rtl_source: str = ""
    created_at: str = ""
    verifier_name: str = "anonymous"
    max_bugs: int = 8
    bugs: List[Bug] = field(default_factory=list)
    challenge_file: str = ""
    submissions: List[Dict] = field(default_factory=list)
    completed: bool = False
    score: float = -1.0
    total_points: int = 0
    earned_points: int = 0
    tier: str = ""
    tier_label: str = ""
    tier_color: str = ""

# ===========================================================================
# Tier System
# ===========================================================================

TIER_TABLE = [
    ("S", 90, 101, "#10b981", "\u592f", "Excellent"),
    ("A", 75, 90,  "#3b82f6", "\u5f3a", "Good"),
    ("B", 60, 75,  "#f59e0b", "\u4e2d", "Acceptable"),
    ("C", 40, 60,  "#f97316", "\u5f31", "Weak"),
    ("D",  0,  40,  "#ef4444", "\u62c9", "Poor"),
]

def compute_tier(score_pct):
    for name, lo, hi, color, label, _ in TIER_TABLE:
        if lo <= score_pct <= hi:
            return name, label, color
    return "D", "\u62c9", "#ef4444"

# ===========================================================================
# Challenge Engine
# ===========================================================================

class BatchChallenger:

    def create_challenge(self, rtl_path, num_bugs=8, output_dir="",
                         categories=None, exclude_ops=None, seed=42,
                         challenge_id="", verifier_name="anonymous"):
        """Inject N bugs into RTL, create challenge artifacts."""
        rtl_path = str(Path(rtl_path).resolve())
        mutator = RTLMutator(rtl_path)
        mutants = mutator.generate(categories=categories, max_per_op=10, seed=seed)

        if not mutants:
            raise ValueError(f"No mutants generated from {rtl_path}")
        if exclude_ops:
            mutants = [m for m in mutants if m.spec.operator not in exclude_ops]

        # One mutant per line (avoid overlap)
        used_lines = set()
        deduped = []
        for mf in mutants:
            if mf.spec.line_no not in used_lines:
                deduped.append(mf)
                used_lines.add(mf.spec.line_no)

        rng = random.Random(seed)
        rng.shuffle(deduped)
        selected = deduped[:num_bugs]
        selected.sort(key=lambda m: m.spec.line_no)

        if not selected:
            raise ValueError(f"Cannot select {num_bugs} non-overlapping bugs")

        # Read original RTL
        with open(rtl_path, "r", encoding="utf-8") as f:
            original_lines = f.readlines()
        injected_lines = list(original_lines)

        # Build bug list and apply mutations
        bugs = []
        for idx, mf in enumerate(selected):
            spec = mf.spec
            bug = Bug(
                bug_id=f"BUG-{idx+1:03d}",
                operator=spec.operator,
                category=spec.category,
                level=spec.level,
                points=POINTS.get(spec.operator, 50),
                severity=spec.severity,
                description=spec.description,
                line_no=spec.line_no,
                original_text=spec.original_text,
                mutated_text=spec.mutated_text,
                source_file=str(Path(rtl_path).name),
                kill_hint=spec.kill_hint,
            )
            bugs.append(bug)

        # Apply mutations in reverse line order to preserve line numbers
        for bug in sorted(bugs, key=lambda b: -b.line_no):
            line_idx = bug.line_no - 1
            if 0 <= line_idx < len(injected_lines):
                injected_lines[line_idx] = bug.mutated_text + "\n"

        # Create challenge ID and output dir
        if not challenge_id:
            challenge_id = f"ch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{seed:04d}"
        if not output_dir:
            output_dir = os.path.join("challenges", challenge_id)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # Write challenge RTL (with bugs injected)
        challenge_rtl = out / f"{Path(rtl_path).stem}_challenge.sv"
        with open(challenge_rtl, "w", encoding="utf-8") as f:
            f.writelines(injected_lines)

        # Build and save answer
        answer = Challenge(
            challenge_id=challenge_id, rtl_source=rtl_path,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            verifier_name=verifier_name, max_bugs=num_bugs,
            bugs=bugs, challenge_file=str(challenge_rtl),
        )
        answer.total_points = sum(b.points for b in bugs)

        with open(out / "answer.json", "w", encoding="utf-8") as f:
            json.dump(asdict(answer), f, indent=2, ensure_ascii=False, default=str)

        # Task file (verifier sees this - no answers)
        task = {
            "challenge_id": challenge_id,
            "created_at": answer.created_at,
            "verifier_name": verifier_name,
            "rtl_file": str(challenge_rtl),
            "original_rtl": str(rtl_path),
            "total_bugs": len(bugs),
            "total_points": answer.total_points,
            "bug_list": [
                {
                    "bug_id": b.bug_id, "operator": b.operator,
                    "category": b.category, "points": b.points,
                    "severity": b.severity, "description": b.description,
                    "line_no": b.line_no,
                    "original_text_hint": (b.original_text[:60] + "...")
                        if len(b.original_text) > 60 else b.original_text,
                }
                for b in bugs
            ],
        }
        with open(out / "task.json", "w", encoding="utf-8") as f:
            json.dump(task, f, indent=2, ensure_ascii=False)

        # Copy original RTL for reference
        ref = out / f"{Path(rtl_path).stem}_original.sv"
        with open(ref, "w", encoding="utf-8") as f:
            with open(rtl_path, "r", encoding="utf-8") as orig:
                f.write(orig.read())

        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  Challenge Created: {challenge_id}")
        print(f"  Bugs Injected:     {len(bugs)}")
        print(f"  Total Points:      {answer.total_points}")
        print(f"  Challenge RTL:     {challenge_rtl}")
        print(f"  Task File:         {out}/task.json")
        print(f"  Answer Key:        {out}/answer.json")
        print(f"{sep}")
        print(f"\n  Bug Breakdown:")
        for b in bugs:
            print(f"    {b.bug_id}  {b.operator:<10} {b.points:>3}pts  "
                  f"{b.severity:<10} {b.description}")
        print()

        return answer

    def submit_found_bugs(self, challenge_dir, found_bug_ids, round_num=None):
        """Verifier submits bug IDs found in this round."""
        ans_path = Path(challenge_dir) / "answer.json"
        with open(ans_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        bugs_data = data.get("bugs", [])
        submissions = data.get("submissions", [])
        if round_num is None:
            round_num = len(submissions) + 1

        all_ids = {b["bug_id"] for b in bugs_data}
        already_found = set()
        for sub in submissions:
            already_found.update(sub.get("correct_new", []))

        correct, wrong, dup = [], [], []
        for bid in found_bug_ids:
            bid = bid.strip()
            if bid not in all_ids:
                wrong.append(bid)
            elif bid in already_found:
                dup.append(bid)
            else:
                correct.append(bid)

        submission = {
            "round": round_num,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "submitted": found_bug_ids,
            "correct_new": correct,
            "wrong": wrong,
            "duplicate": dup,
        }
        submissions.append(submission)
        data["submissions"] = submissions

        all_found = set()
        for s in submissions:
            all_found.update(s.get("correct_new", []))
        for b in bugs_data:
            b["status"] = "found" if b["bug_id"] in all_found else "hidden"

        with open(ans_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        print(f"\n  Round {round_num} Result:")
        print(f"    New bugs found:  {len(correct)} "
              f"({', '.join(correct) if correct else '-'})")
        if wrong:
            print(f"    Wrong IDs:        {', '.join(wrong)}")
        if dup:
            print(f"    Already found:    {', '.join(dup)}")
        print(f"    Total found:      {len(all_found)}/{len(bugs_data)}")
        print(f"    Remaining:        {len(bugs_data) - len(all_found)}")

        return {
            "round": round_num, "correct_new": correct,
            "wrong": wrong, "duplicate": dup,
            "total_found_so_far": len(all_found),
            "total_bugs": len(bugs_data),
        }

    def score_challenge(self, challenge_dir):
        """Final scoring and tier assignment."""
        ans_path = Path(challenge_dir) / "answer.json"
        with open(ans_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        bugs = data.get("bugs", [])
        tp = sum(b["points"] for b in bugs)
        found_bugs = [b for b in bugs if b.get("status") == "found"]
        missed_bugs = [b for b in bugs if b.get("status") != "found"]
        ep = sum(b["points"] for b in found_bugs)
        sp = round(ep / tp * 100, 1) if tp > 0 else 0.0
        tier, tl, tc = compute_tier(sp)

        # Category breakdown
        cat_data = defaultdict(lambda: {"t": 0, "f": 0, "pt": 0, "pf": 0})
        for b in bugs:
            c = b.get("category", "?")
            cat_data[c]["t"] += 1
            cat_data[c]["pt"] += b["points"]
            if b.get("status") == "found":
                cat_data[c]["f"] += 1
                cat_data[c]["pf"] += b["points"]

        cat_labels = {
            "reg_bank": "Register Bank", "fsm": "FSM", "datapath": "Datapath",
            "interface": "Interface", "irq": "IRQ", "subsystem": "Subsystem",
        }
        cat_scores = []
        for cat, d in cat_data.items():
            s = round(d["pf"] / d["pt"] * 100, 1) if d["pt"] > 0 else 0.0
            cat_scores.append({
                "category": cat, "label": cat_labels.get(cat, cat),
                "total": d["t"], "found": d["f"],
                "pts_total": d["pt"], "pts_earned": d["pf"],
                "score": s, "tier": compute_tier(s)[0],
            })

        data.update({
            "completed": True, "score": sp, "total_points": tp,
            "earned_points": ep, "tier": tier, "tier_label": tl,
            "tier_color": tc,
            "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "category_scores": cat_scores,
        })
        with open(ans_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  RESULT: {data.get('challenge_id', '?')}")
        print(f"  Score:  {sp}%  |  Tier {tier} ({tl})")
        print(f"  Points: {ep}/{tp}  |  Bugs: {len(found_bugs)}/{len(bugs)}")
        print(f"  Rounds: {len(data.get('submissions', []))}")
        print(f"{sep}")
        print(f"  Found:")
        for b in found_bugs:
            print(f"    [V] {b['bug_id']}  {b['operator']:<10} "
                  f"{b['points']:>3}pts  {b['description']}")
        print(f"  Missed:")
        for b in missed_bugs:
            print(f"    [X] {b['bug_id']}  {b['operator']:<10} "
                  f"{b['points']:>3}pts  {b['description']}")
            print(f"        Hint: {b.get('kill_hint', 'N/A')}")
        print(f"\n  Category Breakdown:")
        for cs in sorted(cat_scores, key=lambda x: -x["score"]):
            print(f"    {cs['label']:<16} {cs['score']:>5.1f}%  "
                  f"({cs['found']}/{cs['total']} found, "
                  f"{cs['pts_earned']}/{cs['pts_total']} pts)")
        print(sep)

        return {
            "score": sp, "tier": tier, "tier_label": tl, "tier_color": tc,
            "total_points": tp, "earned_points": ep,
            "bugs": bugs, "category_scores": cat_scores,
        }

    def view_challenge(self, challenge_dir, show_answers=False):
        """Display challenge info."""
        tp = Path(challenge_dir) / "task.json"
        ap = Path(challenge_dir) / "answer.json"
        if tp.exists():
            t = json.load(open(tp, "r", encoding="utf-8"))
            print(f"\n  Challenge: {t['challenge_id']}  "
                  f"|  Bugs: {t['total_bugs']}  |  Points: {t['total_points']}")
            print(f"  Verifier:  {t['verifier_name']}  |  RTL: {t['rtl_file']}")
            print()
            for b in t["bug_list"]:
                print(f"  {b['bug_id']}  {b['operator']:<10} {b['points']:>3}pts  "
                      f"[{b['severity']}]  {b['description']}")
        if show_answers and ap.exists():
            a = json.load(open(ap, "r", encoding="utf-8"))
            if a.get("completed"):
                print(f"\n  FINAL: {a['score']}% Tier {a['tier']} ({a['tier_label']})")


# ===========================================================================
# Leaderboard HTML Generator
# ===========================================================================

def generate_leaderboard(challenge_dirs, output_dir,
                         title="RTL Mutation Testing Leaderboard"):
    """Aggregate challenges into HTML leaderboard."""
    all_results = []
    for cdir in challenge_dirs:
        ap = Path(cdir) / "answer.json"
        if not ap.exists():
            continue
        data = json.load(open(ap, "r", encoding="utf-8"))
        if not data.get("completed"):
            bugs = data.get("bugs", [])
            found = [b for b in bugs if b.get("status") == "found"]
            tp = sum(b["points"] for b in bugs)
            ep = sum(b["points"] for b in found)
            sp = round(ep / tp * 100, 1) if tp > 0 else 0.0
            t, tl, tc = compute_tier(sp)
            data.update({"score": sp, "tier": t, "tier_label": tl,
                         "tier_color": tc, "total_points": tp, "earned_points": ep})
        all_results.append(data)

    if not all_results:
        print("No valid challenges found.")
        return ""

    os.makedirs(output_dir, exist_ok=True)
    html = _build_html(all_results, title)
    hp = os.path.join(output_dir, "leaderboard.html")
    with open(hp, "w", encoding="utf-8") as f:
        f.write(html)
    jp = os.path.join(output_dir, "leaderboard.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    print(f"[leaderboard] HTML: {hp}")
    return hp


def _build_html(results, title):
    """Build leaderboard HTML."""
    n = len(results)
    avg = sum(r["score"] for r in results) / n if n else 0
    best = max(r["score"] for r in results) if results else 0
    worst = min(r["score"] for r in results) if results else 0
    bt = compute_tier(best)

    all_bugs = []
    cats = defaultdict(lambda: {"f": 0, "m": 0, "pf": 0, "pm": 0})
    ops = defaultdict(lambda: {"f": 0, "m": 0, "pf": 0, "pm": 0})

    for r in results:
        for b in r.get("bugs", []):
            all_bugs.append({**b, "ch": r.get("challenge_id", "?")})
            found = b.get("status") == "found"
            pts = b.get("points", 50)
            key = "f" if found else "m"
            pkey = "pf" if found else "pm"
            cats[b.get("category", "?")][key] += 1
            cats[b.get("category", "?")][pkey] += pts
            ops[b.get("operator", "?")][key] += 1
            ops[b.get("operator", "?")][pkey] += pts

    cl = {"reg_bank": "Register Bank", "fsm": "FSM", "datapath": "Datapath",
          "interface": "Interface", "irq": "IRQ", "subsystem": "Subsystem"}
    sc = {"critical": "#dc2626", "high": "#ea580c", "medium": "#d97706"}

    # Challenge rows
    chr_rows = ""
    for rank, r in enumerate(sorted(results, key=lambda x: -x["score"]), 1):
        c = r.get("tier_color", "#ef4444")
        s = r.get("score", 0)
        bgs = r.get("bugs", [])
        fd = sum(1 for b in bgs if b.get("status") == "found")
        tot = len(bgs)
        ep = r.get("earned_points", 0)
        tp = r.get("total_points", 0)
        rds = len(r.get("submissions", []))
        done = r.get("completed", False)
        st = '<span style="color:#10b981">Done</span>' if done else \
             '<span style="color:#f59e0b">In Progress</span>'
        bw = min(100, s)
        chr_rows += f"""<tr>
          <td style="text-align:center;font-weight:bold;color:#666">{rank}</td>
          <td><code>{r.get('challenge_id','?')[:20]}</code></td>
          <td>{r.get('verifier_name','anonymous')}</td>
          <td>{r.get('created_at','')[:16]}</td>
          <td>{fd}/{tot}</td><td>{ep}/{tp} pts</td><td>{rds}</td>
          <td><div style="display:flex;align-items:center;gap:6px">
            <div style="flex:1;background:#f1f5f9;border-radius:4px;height:22px;overflow:hidden">
              <div style="width:{bw}%;background:{c};height:100%;border-radius:4px;
                display:flex;align-items:center;justify-content:flex-end;padding-right:6px">
                <span style="color:#fff;font-size:12px;font-weight:bold">{s}%</span>
              </div>
            </div></div></td>
          <td style="text-align:center"><span style="display:inline-block;
            width:36px;height:36px;line-height:36px;text-align:center;
            background:{c};color:#fff;border-radius:8px;font-weight:bold;font-size:18px">
            {r.get('tier','D')}</span></td>
          <td style="text-align:center">{st}</td></tr>"""

    # Category cards
    cat_cards = ""
    for cat, d in cats.items():
        tot = d["f"] + d["m"]
        s = round(d["pf"]/(d["pf"]+d["pm"])*100, 1) if (d["pf"]+d["pm"])>0 else 0
        ti, la, co = compute_tier(s)
        cat_cards += f"""<div class="cat-card" style="border-top:4px solid {co}">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h3>{cl.get(cat,cat)}</h3>
            <span class="tier-badge" style="background:{co}">{ti} {la}</span>
          </div>
          <div class="score-ring" data-score="{s}" data-color="{co}"></div>
          <div class="cat-stats">
            <span class="killed">{d['f']} found</span>
            <span class="alive">{d['m']} missed</span>
            <span class="total">{tot} total</span>
          </div>
          <div style="margin-top:8px;font-size:13px;color:#94a3b8">
            {d['pf']}/{d['pf']+d['pm']} pts</div></div>"""

    # Operator rows
    op_rows = ""
    for op, d in sorted(ops.items(), key=lambda x: -(x[1]["pf"]+x[1]["pm"])):
        tot = d["f"] + d["m"]
        s = round(d["pf"]/(d["pf"]+d["pm"])*100, 1) if (d["pf"]+d["pm"])>0 else 0
        ti, la, co = compute_tier(s)
        pb = POINTS.get(op, 50)
        bw = min(100, s)
        op_rows += f"""<tr>
          <td><code style="font-size:14px;font-weight:bold">{op}</code></td>
          <td style="font-size:12px;color:#94a3b8">{pb} pts</td>
          <td>{d['f']}/{tot}</td>
          <td style="font-size:12px;color:#94a3b8">{d['pf']}/{d['pf']+d['pm']}</td>
          <td><div style="display:flex;align-items:center;gap:6px">
            <div style="flex:1;background:#f1f5f9;border-radius:4px;height:22px;overflow:hidden">
              <div style="width:{bw}%;background:{co};height:100%;border-radius:4px;
                display:flex;align-items:center;justify-content:flex-end;padding-right:6px">
                <span style="color:#fff;font-size:12px;font-weight:bold">{s}%</span>
              </div></div></div></td>
          <td style="text-align:center"><span style="display:inline-block;
            width:32px;height:32px;line-height:32px;text-align:center;
            background:{co};color:#fff;border-radius:6px;font-weight:bold;font-size:16px">
            {ti}</span></td></tr>"""

    # Bug detail rows
    bug_rows = ""
    for b in all_bugs:
        isf = b.get("status") == "found"
        sbg = sc.get(b.get("severity", "medium"), "#6b7280")
        stbg = "#10b981" if isf else "#ef4444"
        si = "V" if isf else "X"
        stt = "FOUND" if isf else "MISSED"
        bug_rows += f"""<tr>
          <td><code>{b.get('bug_id','?')}</code></td>
          <td style="font-size:12px;color:#666">{b.get('ch','?')[:15]}</td>
          <td><code style="font-size:13px">{b.get('operator','?')}</code></td>
          <td style="text-align:center;font-size:12px;font-weight:bold;color:#94a3b8">
            {b.get('points',50)}pts</td>
          <td><span style="background:{sbg};color:#fff;padding:2px 8px;
            border-radius:4px;font-size:12px">{b.get('severity','?')}</span></td>
          <td style="max-width:280px;font-size:13px">{b.get('description','')}</td>
          <td style="text-align:center"><span style="background:{stbg};color:#fff;
            padding:2px 10px;border-radius:12px;font-weight:bold;font-size:12px">
            {si} {stt}</span></td>
          <td style="font-size:13px">{b.get('kill_hint','')[:50]}</td></tr>"""

    # Missed analysis
    missed = [b for b in all_bugs if b.get("status") != "found"]
    missed_html = ""
    if missed:
        mbo = defaultdict(list)
        for b in missed:
            mbo[b.get("operator", "?")].append(b)
        missed_html = f"""<div class="section">
          <h2>Missed Bug Analysis <span style="font-size:14px;color:#ef4444">
            ({len(missed)} bugs missed)</span></h2>
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">"""
        for op, bgs in sorted(mbo.items(),
                key=lambda x: -sum(b.get("points",0) for b in x[1])):
            pl = sum(b.get("points", 0) for b in bgs)
            desc = "; ".join(b.get("description","")[:40] for b in bgs[:3])
            if len(bgs) > 3: desc += "..."
            missed_html += f"""
          <div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);
            border-radius:8px;padding:12px">
            <div style="font-weight:700;font-size:14px;color:#ef4444">
              {op} <span style="font-size:12px;color:#94a3b8">- {pl} pts lost</span>
            </div>
            <div style="font-size:12px;color:#94a3b8;margin-top:4px">{desc}</div>
          </div>"""
        missed_html += "</div></div>"

    tleg = " ".join(f'<span class="tier-badge" style="background:{c}">{n} {l} ({lo}-{hi}%)</span>'
                    for n, lo, hi, c, l, _ in TIER_TABLE)

    total_found = sum(1 for b in all_bugs if b.get("status") == "found")
    total_completed = sum(1 for r in results if r.get("completed"))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#0f172a;color:#e2e8f0;line-height:1.6}}
.container{{max-width:1200px;margin:0 auto;padding:24px}}
.header{{background:linear-gradient(135deg,#1e293b,#0f172a);border:1px solid #334155;
  border-radius:16px;padding:32px;margin-bottom:24px;text-align:center}}
.header h1{{font-size:28px;font-weight:800;margin-bottom:8px}}
.header .subtitle{{color:#94a3b8;font-size:14px}}
.main-score{{display:flex;align-items:center;justify-content:center;gap:48px;margin:32px 0}}
.tier-circle{{width:180px;height:180px;border-radius:50%;display:flex;flex-direction:column;
  align-items:center;justify-content:center;border:4px solid;font-size:64px;font-weight:900;
  background:rgba(0,0,0,0.3);box-shadow:0 0 40px rgba(0,0,0,0.3)}}
.tier-label{{font-size:16px;font-weight:600;margin-top:4px}}
.score-details{{text-align:left}}
.score-details .big-score{{font-size:48px;font-weight:900}}
.score-details .score-sub{{color:#94a3b8;font-size:14px;margin-top:4px}}
.score-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}}
.score-cell{{background:rgba(255,255,255,0.05);border-radius:8px;padding:12px;text-align:center}}
.score-cell .num{{font-size:24px;font-weight:800}}
.score-cell .lbl{{font-size:12px;color:#94a3b8}}
.tier-legend{{display:flex;gap:12px;justify-content:center;margin:16px 0;flex-wrap:wrap}}
.tier-badge{{display:inline-block;padding:4px 16px;border-radius:20px;font-weight:700;font-size:14px;color:#fff}}
.section{{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:24px;margin-bottom:24px}}
.section h2{{font-size:20px;font-weight:700;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #334155}}
.cat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px}}
.cat-card{{background:rgba(255,255,255,0.03);border-radius:12px;padding:20px;text-align:center}}
.cat-card h3{{font-size:14px;color:#94a3b8;font-weight:600;margin-bottom:12px}}
.cat-stats{{display:flex;gap:12px;justify-content:center;margin-top:12px;font-size:13px}}
.cat-stats .killed{{color:#10b981;font-weight:700}}
.cat-stats .alive{{color:#ef4444;font-weight:700}}
.cat-stats .total{{color:#64748b}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;padding:10px 12px;font-weight:600;color:#94a3b8;border-bottom:2px solid #334155;
  font-size:12px;text-transform:uppercase;letter-spacing:0.5px}}
td{{padding:10px 12px;border-bottom:1px solid #1e293b}}
tr:hover{{background:rgba(255,255,255,0.03)}}
.score-ring{{margin:8px auto}}
@media(max-width:768px){{.main-score{{flex-direction:column;gap:24px}}
  .score-grid{{grid-template-columns:repeat(2,1fr)}}
  .cat-grid{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><div class="container">

<div class="header">
  <h1>{title}</h1>
  <div class="subtitle">Multi-Bug Batch Injection Challenge</div>
</div>

<div class="section">
  <div class="main-score">
    <div class="tier-circle" style="border-color:{bt[2]};color:{bt[2]}">
      {bt[0]}<div class="tier-label">{bt[1]}</div>
    </div>
    <div class="score-details">
      <div class="big-score" style="color:{bt[2]}">{avg:.1f}%</div>
      <div class="score-sub">Average across {n} challenge(s)</div>
      <div class="score-sub">Best: {best}% | Worst: {worst}% | Done: {total_completed}/{n}</div>
      <div class="score-grid">
        <div class="score-cell"><div class="num" style="color:#10b981">{total_completed}</div>
          <div class="lbl">Completed</div></div>
        <div class="score-cell"><div class="num" style="color:#3b82f6">{len(all_bugs)}</div>
          <div class="lbl">Total Bugs</div></div>
        <div class="score-cell"><div class="num" style="color:#10b981">{total_found}</div>
          <div class="lbl">Bugs Found</div></div>
        <div class="score-cell"><div class="num" style="color:#ef4444">{len(missed)}</div>
          <div class="lbl">Bugs Missed</div></div>
      </div>
    </div>
  </div>
  <div class="tier-legend">{tleg}</div>
</div>

<div class="section"><h2>Challenge Results</h2>
  <table><thead><tr><th>#</th><th>Challenge</th><th>Verifier</th><th>Date</th>
    <th>Found</th><th>Points</th><th>Rounds</th><th>Score</th><th>Tier</th><th>Status</th>
    </tr></thead><tbody>{chr_rows}</tbody></table></div>

<div class="section"><h2>Category Detection Rates</h2>
  <div class="cat-grid">{cat_cards}</div></div>

<div class="section"><h2>Operator Detection <span style="font-size:14px;color:#94a3b8">
  (by difficulty)</span></h2>
  <table><thead><tr><th>Operator</th><th>Difficulty</th><th>Found</th><th>Points</th>
    <th>Detection Rate</th><th style="width:60px">Tier</th></tr></thead>
    <tbody>{op_rows}</tbody></table></div>

<div class="section"><h2>All Bugs <span style="font-size:14px;color:#94a3b8">
  ({len(all_bugs)} total)</span></h2>
  <table><thead><tr><th>ID</th><th>Challenge</th><th>Operator</th>
    <th style="text-align:center">Pts</th><th>Sev</th><th>Description</th>
    <th style="text-align:center">Status</th><th>Hint</th></tr></thead>
    <tbody>{bug_rows}</tbody></table></div>

{missed_html}

<div style="text-align:center;color:#475569;font-size:12px;padding:16px">
  RTL Mutation Testing - Multi-Bug Batch Challenge &bull; {now_str}
</div>
</div>
<script>
document.querySelectorAll('.score-ring').forEach(el=>{{
  const s=parseFloat(el.dataset.score),c=el.dataset.color,r=40,
    circ=2*Math.PI*r,off=circ-(s/100)*circ;
  el.innerHTML=`<svg width="100" height="100" viewBox="0 0 100 100">
    <circle cx="50" cy="50" r="${{r}}" fill="none" stroke="#334155" stroke-width="8"/>
    <circle cx="50" cy="50" r="${{r}}" fill="none" stroke="${{c}}" stroke-width="8"
      stroke-dasharray="${{circ}}" stroke-dashoffset="${{off}}" stroke-linecap="round"
      transform="rotate(-90 50 50)" style="transition:stroke-dashoffset 1.2s ease-out"/>
    <text x="50" y="54" text-anchor="middle" fill="${{c}}" font-size="22" font-weight="800">${{s}}%</text>
  </svg>`;
}});
</script>
</body></html>"""


# ===========================================================================
# CLI
# ===========================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description="RTL Multi-Bug Batch Challenge")
    sub = ap.add_subparsers(dest="cmd")

    pc = sub.add_parser("create")
    pc.add_argument("--rtl", required=True)
    pc.add_argument("--bugs", type=int, default=8)
    pc.add_argument("--out", default="")
    pc.add_argument("--seed", type=int, default=42)
    pc.add_argument("--id", default="")
    pc.add_argument("--verifier", default="anonymous")

    pv = sub.add_parser("view")
    pv.add_argument("dir")
    pv.add_argument("--answers", action="store_true")

    ps = sub.add_parser("submit")
    ps.add_argument("dir")
    ps.add_argument("--found", required=True)
    ps.add_argument("--round", type=int, default=None)

    pp = sub.add_parser("score")
    pp.add_argument("dir")

    pl = sub.add_parser("leaderboard")
    pl.add_argument("--all", nargs="+", required=True)
    pl.add_argument("--out", default="output/leaderboard")

    args = ap.parse_args(argv)
    e = BatchChallenger()

    if args.cmd == "create":
        e.create_challenge(args.rtl, args.bugs, args.out, seed=args.seed,
                           challenge_id=args.id, verifier_name=args.verifier)
    elif args.cmd == "view":
        e.view_challenge(args.dir, show_answers=args.answers)
    elif args.cmd == "submit":
        ids = [x.strip() for x in args.found.split(",") if x.strip()]
        e.submit_found_bugs(args.dir, ids, args.round)
    elif args.cmd == "score":
        e.score_challenge(args.dir)
    elif args.cmd == "leaderboard":
        hp = generate_leaderboard(args.all, args.out)
        if hp: print(f"\nLeaderboard: {hp}")
    else:
        ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
