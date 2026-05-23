#!/usr/bin/env python3
"""
mutation_eval.py - RTL Mutation Testing Evaluation & Leaderboard System

Evaluates AI verification flow detection capability by:
1. Generating mutants from RTL
2. Running iverilog simulation against each mutant
3. Auto-detecting failures (assertions, $finish code, VCD signals)
4. Scoring with per-operator difficulty points
5. Generating leaderboard HTML report with tier rankings

Tier System (based on weighted detection score):
  S  (90-100%) - Excellent: verification catches nearly all injected bugs
  A  (75-89%)  - Good: catches most bugs, minor gaps in coverage
  B  (60-74%)  - Acceptable: catches majority but misses several bug types
  C  (40-59%)  - Weak: significant detection gaps, needs improvement
  D  (0-39%)   - Poor: verification flow has critical blind spots

Operator Point System:
  Each operator has a unique difficulty score (0-100).
  Higher points = harder bug to detect = more penalty if missed.
  Score = sum(killed_points) / sum(total_points) x 100

  Top difficulty operators:
    RB-WE(100), SS-CONN(98), RB-ADDR(95), SS-BASE(94), FSM-RST(92)
  Easiest operators:
    DP-CONST(42), IF-IDLE(45), FSM-DEF(48)

Usage:
    python engines/mutation_eval.py --rtl rtl/ --tb tb/ --out eval_report/
    python engines/mutation_eval.py --demo               # Generate demo report
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import the mutator engine (includes POINTS dict with per-operator difficulty scores)
sys.path.insert(0, str(Path(__file__).parent))
from rtl_mutator import RTLMutator, MutCategory, MutantFile, MutantSpec, POINTS, _OPERATORS

# Severity weight is still used for severity-level breakdown display
SEVERITY_WEIGHT = {"critical": 3.0, "high": 2.0, "medium": 1.0, "low": 0.5}

TIER_TABLE = [
    ("S", 90, 100, "#10b981", "Excellent", "Verification catches nearly all injected bugs"),
    ("A", 75, 89,  "#3b82f6", "Good",       "Catches most bugs, minor gaps in coverage"),
    ("B", 60, 74,  "#f59e0b", "Acceptable", "Catches majority but misses several bug types"),
    ("C", 40, 59,  "#f97316", "Weak",       "Significant detection gaps, needs improvement"),
    ("D",  0, 39,  "#ef4444", "Poor",       "Verification flow has critical blind spots"),
]


@dataclass
class MutantResult:
    """Result of running verification against one mutant."""
    mut_id: str
    operator: str
    category: str
    severity: str
    points: int = 50              # operator difficulty score (from POINTS dict)
    description: str = ""
    original_text: str = ""
    mutated_text: str = ""
    status: str = "alive"         # killed / alive / equivalent / error
    kill_method: str = ""         # assertion / scoreboard / vcd_check / crash / timeout
    sim_exit_code: int = -1
    sim_stderr: str = ""
    sim_duration_ms: float = 0.0
    notes: str = ""


@dataclass
class CategoryScore:
    """Per-category scoring using operator points."""
    category: str
    label: str
    total: int = 0
    killed: int = 0
    alive: int = 0
    equivalent: int = 0
    pts_total: int = 0           # total possible points
    pts_killed: int = 0          # points earned by killing mutants
    score: float = 0.0

    def compute(self):
        denom = self.pts_total
        self.score = (self.pts_killed / denom * 100) if denom > 0 else 0.0


@dataclass
class EvalReport:
    """Complete evaluation report."""
    project_name: str = "Unnamed Project"
    timestamp: str = ""
    rtl_files: List[str] = field(default_factory=list)
    tb_files: List[str] = field(default_factory=list)
    total_mutants: int = 0
    killed: int = 0
    alive: int = 0
    equivalent: int = 0
    error: int = 0
    raw_score: float = 0.0        # unweighted killed/total
    score: float = 0.0            # points-weighted detection score
    tier: str = "D"
    tier_label: str = "Poor"
    tier_color: str = "#ef4444"
    category_scores: List[Dict] = field(default_factory=list)
    operator_scores: List[Dict] = field(default_factory=list)
    severity_scores: Dict[str, Dict] = field(default_factory=dict)
    mutant_results: List[Dict] = field(default_factory=list)
    sim_total_time_ms: float = 0.0
    eval_duration_s: float = 0.0
    recommendations: List[str] = field(default_factory=list)


# ===========================================================================
# Simulation Runner
# ===========================================================================

def run_sim(
    mutant_sv: str,
    tb_files: List[str],
    include_dirs: List[str],
    timeout: float = 30.0,
    work_dir: Optional[str] = None,
) -> Tuple[int, str, float]:
    """
    Run iverilog simulation against a mutant RTL file.

    Returns:
        (exit_code, stderr, duration_seconds)
    """
    cmd = ["iverilog", "-o", "sim_out", "-g2005"]
    for d in include_dirs:
        cmd.extend(["-I", d])
    cmd.append(mutant_sv)
    cmd.extend(tb_files)

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=work_dir or ".",
            shell=False,
        )
        duration = (time.time() - start) * 1000  # ms

        # If compilation succeeds, try to run vvp
        if result.returncode == 0:
            try:
                run_result = subprocess.run(
                    ["vvp", "sim_out"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=work_dir or ".",
                    shell=False,
                )
                duration = (time.time() - start) * 1000
                return run_result.returncode, (result.stderr or "") + "\n" + (run_result.stderr or ""), duration
            except subprocess.TimeoutExpired:
                return -2, "Simulation timeout", (time.time() - start) * 1000
            except Exception as e:
                return -3, str(e), (time.time() - start) * 1000

        return result.returncode, result.stderr or "", duration

    except subprocess.TimeoutExpired:
        return -2, "Compilation timeout", (time.time() - start) * 1000
    except Exception as e:
        return -3, str(e), (time.time() - start) * 1000


def analyze_sim_result(
    exit_code: int,
    stderr: str,
    tb_content: str = "",
) -> Tuple[str, str]:
    """
    Analyze simulation result to determine if mutant was killed.

    Returns:
        (status, kill_method)
        status: killed / alive / equivalent / error
    """
    stderr_lower = stderr.lower()

    # Compilation error → mutant likely killed (broke the design)
    if exit_code != 0 and exit_code != -2 and exit_code != -3:
        # Check if it's a meaningful compile error vs cosmetic
        if any(kw in stderr_lower for kw in [
            "syntax error", "undeclared", "unknown", "error",
            "fatal", "mismatch", "width mismatch", "port mismatch"
        ]):
            return "killed", "compile_error"

    # VVP runtime failure
    if exit_code == 1:
        # Check for assertion failures
        if "assert" in stderr_lower or " assertion" in stderr_lower:
            return "killed", "assertion"
        if "fatal" in stderr_lower or "error" in stderr_lower:
            return "killed", "runtime_error"
        # Any non-zero exit from vvp means something went wrong
        if stderr.strip():
            return "killed", "runtime_error"

    # Timeout → likely hung (could be killed or alive, mark as alive)
    if exit_code == -2:
        return "alive", "timeout"

    # Process error
    if exit_code == -3:
        return "error", "process_error"

    # Exit code 0 → mutant survived (alive)
    # Unless we see assertion pass messages etc.
    return "alive", "survived"


# ===========================================================================
# Evaluation Engine
# ===========================================================================

class MutationEvaluator:
    """
    Full evaluation pipeline:
    1. Generate mutants
    2. Run simulation against each
    3. Auto-detect kill/alive
    4. Compute weighted scores
    5. Assign tier
    6. Generate recommendations
    """

    def __init__(
        self,
        rtl_path: str,
        tb_path: str,
        include_dirs: Optional[List[str]] = None,
        sim_timeout: float = 30.0,
    ):
        self.rtl_path = Path(rtl_path)
        self.tb_path = Path(tb_path)
        self.include_dirs = include_dirs or []
        self.sim_timeout = sim_timeout
        self.mutator = RTLMutator(str(rtl_path))
        self.results: List[MutantResult] = []

    def _collect_tb_files(self) -> List[str]:
        if self.tb_path.is_dir():
            return [str(f) for f in sorted(self.tb_path.glob("**/*.sv")) +
                    sorted(self.tb_path.glob("**/*.v"))]
        return [str(self.tb_path)]

    def run_full_eval(
        self,
        project_name: str = "Unnamed Project",
        categories: Optional[List[MutCategory]] = None,
        max_per_op: int = 3,
        seed: int = 42,
    ) -> EvalReport:
        """Run the complete evaluation pipeline."""
        start_time = time.time()
        report = EvalReport(project_name=project_name)
        report.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report.rtl_files = [str(f) for f in self.mutator._files]
        tb_files = self._collect_tb_files()
        report.tb_files = tb_files

        # Step 1: Generate mutants
        mutants = self.mutator.generate(
            categories=categories,
            max_per_op=max_per_op,
            seed=seed,
        )

        if not mutants:
            report.total_mutants = 0
            report.recommendations = ["No mutants generated. Check RTL files have matching patterns."]
            return report

        report.total_mutants = len(mutants)
        print(f"[eval] Generated {len(mutants)} mutants from {len(self.mutator._files)} RTL file(s)")

        # Step 2: Run simulation for each mutant
        work_dir = str(Path(__file__).parent.parent / "output" / "_eval_tmp")
        os.makedirs(work_dir, exist_ok=True)

        for idx, mf in enumerate(mutants):
            spec = mf.spec
            print(f"  [{idx+1}/{len(mutants)}] {spec.mut_id:<22} ", end="", flush=True)

            # Write mutant to temp file
            mutant_path = os.path.join(work_dir, f"mut_{spec.mut_id}.sv")
            with open(mutant_path, "w", encoding="utf-8") as f:
                f.write(mf.content)

            # Run simulation
            exit_code, stderr, duration = run_sim(
                mutant_sv=mutant_path,
                tb_files=tb_files,
                include_dirs=self.include_dirs,
                timeout=self.sim_timeout,
                work_dir=work_dir,
            )

            status, kill_method = analyze_sim_result(exit_code, stderr)

            result = MutantResult(
                mut_id=spec.mut_id,
                operator=spec.operator,
                category=spec.category,
                severity=spec.severity,
                points=POINTS.get(spec.operator, 50),
                description=spec.description,
                original_text=spec.original_text,
                mutated_text=spec.mutated_text,
                status=status,
                kill_method=kill_method,
                sim_exit_code=exit_code,
                sim_stderr=stderr[:500] if stderr else "",
                sim_duration_ms=round(duration, 1),
                notes=f"exit={exit_code}" if exit_code != 0 and status == "alive" else "",
            )
            self.results.append(result)

            icon = {"killed": "X", "alive": "O", "equivalent": "=", "error": "!"}.get(status, "?")
            print(f"[{icon} {status:<10}] {kill_method}  ({duration:.0f}ms)")

            report.sim_total_time_ms += duration

        # Step 3: Compute scores
        self._compute_scores(report)

        # Step 4: Generate recommendations
        self._generate_recommendations(report)

        report.eval_duration_s = round(time.time() - start_time, 1)

        # Cleanup
        try:
            for f in os.listdir(work_dir):
                os.remove(os.path.join(work_dir, f))
            os.rmdir(work_dir)
        except:
            pass

        return report

    def _compute_scores(self, report: EvalReport):
        """Compute all scoring metrics using per-operator difficulty points."""
        cat_data = defaultdict(lambda: {
            "total": 0, "killed": 0, "alive": 0, "equivalent": 0,
            "pts_total": 0, "pts_killed": 0,
        })
        op_data = defaultdict(lambda: {
            "total": 0, "killed": 0, "alive": 0, "pts_total": 0, "pts_killed": 0,
        })
        sev_data = defaultdict(lambda: {
            "total": 0, "killed": 0, "alive": 0,
        })

        for r in self.results:
            pts = r.points  # operator difficulty score

            # Category aggregation
            cat_data[r.category]["total"] += 1
            cat_data[r.category][r.status] = cat_data[r.category].get(r.status, 0) + 1
            if r.status not in ("equivalent", "error"):
                cat_data[r.category]["pts_total"] += pts
                if r.status == "killed":
                    cat_data[r.category]["pts_killed"] += pts

            # Operator aggregation
            op_data[r.operator]["total"] += 1
            op_data[r.operator][r.status] = op_data[r.operator].get(r.status, 0) + 1
            if r.status not in ("equivalent", "error"):
                op_data[r.operator]["pts_total"] += pts
                if r.status == "killed":
                    op_data[r.operator]["pts_killed"] += pts

            # Severity aggregation (for display only, still uses severity weight)
            sev_data[r.severity]["total"] += 1
            sev_data[r.severity][r.status] = sev_data[r.severity].get(r.status, 0) + 1

        # Category scores
        category_labels = {
            "reg_bank": "Register Bank",
            "fsm": "FSM",
            "datapath": "Datapath",
            "interface": "Interface",
            "irq": "IRQ",
            "subsystem": "Subsystem",
        }
        for cat, d in cat_data.items():
            cs = CategoryScore(
                category=cat,
                label=category_labels.get(cat, cat),
                total=d["total"],
                killed=d["killed"],
                alive=d["alive"],
                equivalent=d.get("equivalent", 0),
                pts_total=d["pts_total"],
                pts_killed=d["pts_killed"],
            )
            cs.compute()
            report.category_scores.append({
                "category": cat,
                "label": cs.label,
                "total": cs.total,
                "killed": cs.killed,
                "alive": cs.alive,
                "equivalent": cs.equivalent,
                "pts_total": cs.pts_total,
                "pts_killed": cs.pts_killed,
                "score": round(cs.score, 1),
            })

        # Operator scores (sorted by points for leaderboard — high value = hard = top)
        for op, d in op_data.items():
            effective = d["total"] - d.get("equivalent", 0) - d.get("error", 0)
            kill_rate = (d["killed"] / effective * 100) if effective > 0 else 0.0
            pts_earned = d["pts_killed"]
            pts_possible = d["pts_total"]
            report.operator_scores.append({
                "operator": op,
                "total": d["total"],
                "killed": d["killed"],
                "alive": d["alive"],
                "pts_earned": pts_earned,
                "pts_possible": pts_possible,
                "points": POINTS.get(op, 50),  # base difficulty of this operator
                "score": round(kill_rate, 1),
            })
        # Sort: by detection score ascending (weakest first = most concerning at top for "weakness view")
        # But for leaderboard, we want strongest first (highest score). Keep both views in HTML.
        report.operator_scores.sort(key=lambda x: x["score"], reverse=True)

        # Severity scores
        for sev, d in sev_data.items():
            report.severity_scores[sev] = {
                "total": d["total"],
                "killed": d["killed"],
                "alive": d["alive"],
                "score": round(d["killed"] / max(1, d["total"]) * 100, 1),
            }

        # Overall scores
        total_killed = sum(1 for r in self.results if r.status == "killed")
        total_alive = sum(1 for r in self.results if r.status == "alive")
        total_equiv = sum(1 for r in self.results if r.status == "equivalent")
        total_error = sum(1 for r in self.results if r.status == "error")

        report.killed = total_killed
        report.alive = total_alive
        report.equivalent = total_equiv
        report.error = total_error

        effective = len(self.results) - total_equiv - total_error
        report.raw_score = round(total_killed / max(1, effective) * 100, 1)

        # Weighted score using operator difficulty points
        total_pts = 0
        killed_pts = 0
        for r in self.results:
            if r.status not in ("equivalent", "error"):
                total_pts += r.points
                if r.status == "killed":
                    killed_pts += r.points
        report.score = round(killed_pts / max(0.01, total_pts) * 100, 1)

        # Assign tier based on points-weighted score
        for tier_name, lo, hi, color, label, desc in TIER_TABLE:
            if lo <= report.score <= hi:
                report.tier = tier_name
                report.tier_label = label
                report.tier_color = color
                break

        # Mutant results detail
        for r in self.results:
            report.mutant_results.append({
                "mut_id": r.mut_id,
                "operator": r.operator,
                "category": r.category,
                "severity": r.severity,
                "points": r.points,
                "description": r.description,
                "original_text": r.original_text[:120],
                "mutated_text": r.mutated_text[:120],
                "status": r.status,
                "kill_method": r.kill_method,
                "sim_exit_code": r.sim_exit_code,
                "sim_stderr": r.sim_stderr[:200],
                "sim_duration_ms": r.sim_duration_ms,
                "notes": r.notes,
            })

    def _generate_recommendations(self, report: EvalReport):
        """Generate actionable recommendations based on gaps."""
        recs = []

        # Check category-level gaps
        for cs in report.category_scores:
            if cs["total"] == 0:
                continue
            if cs["score"] < 50:
                recs.append(
                    f"[CRITICAL] {cs['label']} detection rate is {cs['score']}% — "
                    f"verification has major blind spots for {cs['category']} bugs"
                )
            elif cs["score"] < 75:
                recs.append(
                    f"[WARNING] {cs['label']} detection rate is {cs['score']}% — "
                    f"consider adding targeted tests for {cs['category']} category"
                )

        # Check severity-level gaps
        for sev, data in report.severity_scores.items():
            if data["total"] > 0 and data["score"] < 80:
                recs.append(
                    f"[GAP] {sev.upper()} severity bug detection: {data['score']}% "
                    f"({data['killed']}/{data['total']} killed)"
                )

        # Check for alive mutants with critical severity
        alive_critical = [r for r in self.results if r.status == "alive" and r.severity == "critical"]
        if alive_critical:
            ops = set(r.operator for r in alive_critical)
            recs.append(
                f"[RISK] {len(alive_critical)} CRITICAL mutants survived: {', '.join(sorted(ops))} — "
                f"these are the most dangerous bugs that your verification cannot detect"
            )

        # Overall recommendation
        if report.score >= 90:
            recs.append("[OK] Verification flow is robust. Maintain current test quality.")
        elif report.score >= 75:
            recs.append("[OK] Good verification coverage. Focus on surviving mutants to reach S-tier.")
        elif report.score >= 60:
            recs.append("[ACTION] Add assertion-based checks and scoreboard comparisons for weak categories.")
        elif report.score >= 40:
            recs.append("[ACTION] Significant gaps detected. Review and enhance verification strategy.")
        else:
            recs.append("[ACTION] Verification flow needs major improvements. Consider redesigning test strategy.")

        report.recommendations = recs


# ===========================================================================
# HTML Report Generator
# ===========================================================================

def generate_html_report(report: EvalReport, output_path: str):
    """Generate a complete HTML leaderboard report."""

    # Sort operator scores for leaderboard (descending)
    sorted_ops = sorted(report.operator_scores, key=lambda x: (-x["score"], x["operator"]))
    sorted_cats = sorted(report.category_scores, key=lambda x: x["score"], reverse=True)

    # Severity badge colors
    sev_colors = {
        "critical": "#dc2626",
        "high": "#ea580c",
        "medium": "#d97706",
        "low": "#65a30d",
    }
    status_colors = {
        "killed": "#10b981",
        "alive": "#ef4444",
        "equivalent": "#6b7280",
        "error": "#f59e0b",
    }
    status_icons = {
        "killed": "X",
        "alive": "O",
        "equivalent": "=",
        "error": "!",
    }

    # Category radar chart data
    cat_labels = [cs["label"] for cs in sorted_cats]
    cat_scores = [cs["score"] for cs in sorted_cats]

    # Build mutant detail rows
    mutant_rows = ""
    for mr in report.mutant_results:
        sev_bg = sev_colors.get(mr["severity"], "#6b7280")
        stat_bg = status_colors.get(mr["status"], "#6b7280")
        stat_icon = status_icons.get(mr["status"], "?")
        pts = mr.get("points", 50)
        mutant_rows += f"""
        <tr>
            <td><code>{mr['mut_id']}</code></td>
            <td style="text-align:center;font-size:12px;font-weight:bold;color:#94a3b8;">{pts}pts</td>
            <td><span style="background:{sev_bg};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;">{mr['severity']}</span></td>
            <td>{mr['category']}</td>
            <td style="max-width:250px;font-size:13px;">{mr['description']}</td>
            <td style="text-align:center;">
                <span style="background:{stat_bg};color:#fff;padding:2px 10px;border-radius:12px;font-weight:bold;">
                    {stat_icon} {mr['status']}
                </span>
            </td>
            <td style="font-size:12px;color:#888;">{mr['kill_method']}</td>
            <td style="font-size:12px;">{mr['sim_duration_ms']:.0f}ms</td>
        </tr>"""

    # Build operator leaderboard rows — sorted by detection score (strongest first)
    op_rows = ""
    for rank, op in enumerate(sorted_ops, 1):
        # Assign tier to each operator
        op_tier = "D"
        op_color = "#ef4444"
        for t_name, t_lo, t_hi, t_color, _, _ in TIER_TABLE:
            if t_lo <= op["score"] <= t_hi:
                op_tier = t_name
                op_color = t_color
                break

        bar_width = min(100, op["score"])
        pts_base = op.get("points", 50)
        pts_earned = op.get("pts_earned", 0)
        pts_possible = op.get("pts_possible", pts_base)
        op_rows += f"""
        <tr>
            <td style="text-align:center;font-weight:bold;color:#666;">{rank}</td>
            <td><code style="font-size:14px;font-weight:bold;">{op['operator']}</code></td>
            <td style="font-size:12px;color:#94a3b8;">{pts_base} pts</td>
            <td>{op['killed']}/{op['total']}</td>
            <td>
                <div style="display:flex;align-items:center;gap:8px;">
                    <div style="flex:1;background:#f1f5f9;border-radius:4px;height:24px;overflow:hidden;">
                        <div style="width:{bar_width}%;background:{op_color};height:100%;border-radius:4px;display:flex;align-items:center;justify-content:flex-end;padding-right:6px;">
                            <span style="color:#fff;font-size:12px;font-weight:bold;">{op['score']}%</span>
                        </div>
                    </div>
                </div>
            </td>
            <td style="font-size:12px;color:#94a3b8;">{pts_earned}/{pts_possible}</td>
            <td style="text-align:center;">
                <span style="display:inline-block;width:32px;height:32px;line-height:32px;text-align:center;
                    background:{op_color};color:#fff;border-radius:6px;font-weight:bold;font-size:16px;">{op_tier}</span>
            </td>
        </tr>"""

    # Build category score cards
    cat_cards = ""
    for cs in sorted_cats:
        cs_tier = "D"
        cs_color = "#ef4444"
        for t_name, t_lo, t_hi, t_color, _, _ in TIER_TABLE:
            if t_lo <= cs["score"] <= t_hi:
                cs_tier = t_name
                cs_color = t_color
                break
        cat_cards += f"""
        <div class="cat-card" style="border-top:4px solid {cs_color};">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <h3>{cs['label']}</h3>
                <span class="tier-badge" style="background:{cs_color};">{cs_tier}</span>
            </div>
            <div class="score-ring" data-score="{cs['score']}" data-color="{cs_color}"></div>
            <div class="cat-stats">
                <span class="killed">X {cs['killed']}</span>
                <span class="alive">O {cs['alive']}</span>
                <span class="equiv">= {cs['equivalent']}</span>
                <span class="total">{cs['total']} total</span>
            </div>
        </div>"""

    # Recommendations
    rec_items = ""
    for rec in report.recommendations:
        if rec.startswith("[CRITICAL]"):
            rec_bg = "#fef2f2"
            rec_border = "#dc2626"
        elif rec.startswith("[RISK]"):
            rec_bg = "#fff7ed"
            rec_border = "#ea580c"
        elif rec.startswith("[WARNING]"):
            rec_bg = "#fffbeb"
            rec_border = "#d97706"
        elif rec.startswith("[ACTION]"):
            rec_bg = "#eff6ff"
            rec_border = "#3b82f6"
        else:
            rec_bg = "#f0fdf4"
            rec_border = "#10b981"
        rec_items += f"""
        <div style="background:{rec_bg};border-left:4px solid {rec_border};padding:12px 16px;border-radius:0 8px 8px 0;margin-bottom:8px;font-size:14px;">
            {rec}
        </div>"""

    # Severity breakdown bars
    sev_bars = ""
    for sev in ["critical", "high", "medium"]:
        data = report.severity_scores.get(sev, {"total": 0, "killed": 0, "score": 0})
        if data["total"] == 0:
            continue
        color = sev_colors[sev]
        killed_pct = data["score"]
        sev_bars += f"""
        <div style="margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
                <span style="font-weight:600;color:{color};">{sev.upper()}</span>
                <span>{data['killed']}/{data['total']} killed ({killed_pct}%)</span>
            </div>
            <div style="background:#f1f5f9;border-radius:4px;height:20px;overflow:hidden;">
                <div style="width:{killed_pct}%;background:{color};height:100%;border-radius:4px;transition:width 0.8s;"></div>
            </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RTL Mutation Testing - {report.project_name}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0; line-height: 1.6;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}

  /* Header */
  .header {{
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    border: 1px solid #334155; border-radius: 16px; padding: 32px;
    margin-bottom: 24px; text-align: center;
  }}
  .header h1 {{ font-size: 28px; font-weight: 800; margin-bottom: 8px; }}
  .header .subtitle {{ color: #94a3b8; font-size: 14px; }}

  /* Main Score */
  .main-score {{
    display: flex; align-items: center; justify-content: center;
    gap: 48px; margin: 32px 0;
  }}
  .tier-circle {{
    width: 180px; height: 180px; border-radius: 50%;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    border: 4px solid; font-size: 64px; font-weight: 900;
    background: rgba(0,0,0,0.3);
    box-shadow: 0 0 40px rgba(0,0,0,0.3);
  }}
  .tier-label {{ font-size: 16px; font-weight: 600; margin-top: 4px; }}
  .score-details {{ text-align: left; }}
  .score-details .big-score {{ font-size: 48px; font-weight: 900; }}
  .score-details .score-sub {{ color: #94a3b8; font-size: 14px; margin-top: 4px; }}
  .score-grid {{
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
    margin-top: 16px;
  }}
  .score-cell {{
    background: rgba(255,255,255,0.05); border-radius: 8px; padding: 12px; text-align: center;
  }}
  .score-cell .num {{ font-size: 24px; font-weight: 800; }}
  .score-cell .lbl {{ font-size: 12px; color: #94a3b8; }}

  /* Tier Legend */
  .tier-legend {{
    display: flex; gap: 12px; justify-content: center; margin: 16px 0; flex-wrap: wrap;
  }}
  .tier-badge {{
    display: inline-block; padding: 4px 16px; border-radius: 20px;
    font-weight: 700; font-size: 14px; color: #fff;
  }}

  /* Sections */
  .section {{
    background: #1e293b; border: 1px solid #334155; border-radius: 16px;
    padding: 24px; margin-bottom: 24px;
  }}
  .section h2 {{
    font-size: 20px; font-weight: 700; margin-bottom: 16px;
    padding-bottom: 12px; border-bottom: 1px solid #334155;
  }}

  /* Category Cards */
  .cat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
  .cat-card {{
    background: rgba(255,255,255,0.03); border-radius: 12px; padding: 20px; text-align: center;
  }}
  .cat-card h3 {{ font-size: 14px; color: #94a3b8; font-weight: 600; margin-bottom: 12px; }}

  /* Score Ring (SVG) */
  .score-ring {{ margin: 8px auto; }}

  .cat-stats {{ display: flex; gap: 12px; justify-content: center; margin-top: 12px; font-size: 13px; }}
  .cat-stats .killed {{ color: #10b981; font-weight: 700; }}
  .cat-stats .alive {{ color: #ef4444; font-weight: 700; }}
  .cat-stats .equiv {{ color: #6b7280; }}
  .cat-stats .total {{ color: #64748b; }}

  /* Leaderboard Table */
  table {{
    width: 100%; border-collapse: collapse; font-size: 14px;
  }}
  th {{
    text-align: left; padding: 10px 12px; font-weight: 600;
    color: #94a3b8; border-bottom: 2px solid #334155; font-size: 12px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #1e293b; }}
  tr:hover {{ background: rgba(255,255,255,0.03); }}

  /* Severity & Status colors */
  .killed {{ color: #10b981; }}
  .alive {{ color: #ef4444; }}
  .equiv {{ color: #6b7280; }}

  /* Responsive */
  @media (max-width: 768px) {{
    .main-score {{ flex-direction: column; gap: 24px; }}
    .score-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .cat-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <h1>RTL Mutation Testing Report</h1>
    <div class="subtitle">{report.project_name} &mdash; Generated {report.timestamp}</div>
  </div>

  <!-- Main Score -->
  <div class="section">
    <div class="main-score">
      <div class="tier-circle" style="border-color: {report.tier_color}; color: {report.tier_color};">
        {report.tier}
        <div class="tier-label">{report.tier_label}</div>
      </div>
      <div class="score-details">
        <div class="big-score" style="color: {report.tier_color};">{report.score}%</div>
        <div class="score-sub">Points-Weighted Mutation Score (operator difficulty adjusted)</div>
        <div class="score-sub">Raw Score: {report.raw_score}% &bull; Eval Duration: {report.eval_duration_s}s &bull; Sim Time: {report.sim_total_time_ms:.0f}ms</div>
        <div class="score-grid">
          <div class="score-cell">
            <div class="num" style="color:#10b981;">{report.killed}</div>
            <div class="lbl">Killed (X)</div>
          </div>
          <div class="score-cell">
            <div class="num" style="color:#ef4444;">{report.alive}</div>
            <div class="lbl">Alive (O)</div>
          </div>
          <div class="score-cell">
            <div class="num" style="color:#6b7280;">{report.equivalent}</div>
            <div class="lbl">Equivalent (=)</div>
          </div>
          <div class="score-cell">
            <div class="num" style="color:#94a3b8;">{report.total_mutants}</div>
            <div class="lbl">Total</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Tier Legend -->
    <div class="tier-legend">
      {" ".join(f'<span class="tier-badge" style="background:{c};">{n} ({lo}-{hi}%) {l}</span>' for n, lo, hi, c, l, _ in TIER_TABLE)}
    </div>

    <!-- Severity Breakdown -->
    <h3 style="margin: 24px 0 16px; font-size: 16px;">Severity Breakdown</h3>
    {sev_bars}
  </div>

  <!-- Category Scores -->
  <div class="section">
    <h2>Category Detection Rates</h2>
    <div class="cat-grid">{cat_cards}</div>
  </div>

  <!-- Operator Leaderboard (from strong to weak) -->
  <div class="section">
    <h2>Operator Leaderboard <span style="font-size:14px;color:#94a3b8;font-weight:400;">(Strong to Weak)</span></h2>
    <table>
      <thead>
        <tr><th style="width:50px;">#</th><th>Operator</th><th>Difficulty</th><th>Killed</th><th>Detection Rate</th><th>Points Earned</th><th style="width:60px;">Tier</th></tr>
      </thead>
      <tbody>{op_rows}</tbody>
    </table>
  </div>

  <!-- Mutant Detail Table -->
  <div class="section">
    <h2>Mutant Detail <span style="font-size:14px;color:#94a3b8;font-weight:400;">({len(report.mutant_results)} mutants)</span></h2>
    <table>
      <thead>
        <tr>
          <th>ID</th><th style="text-align:center;">Pts</th><th>Sev</th><th>Category</th><th>Description</th>
          <th style="text-align:center;">Status</th><th>Kill Method</th><th>Time</th>
        </tr>
      </thead>
      <tbody>{mutant_rows}</tbody>
    </table>
  </div>

  <!-- Recommendations -->
  <div class="section">
    <h2>Recommendations</h2>
    {rec_items if rec_items else '<div style="color:#94a3b8;">No recommendations.</div>'}
  </div>

  <!-- Footer -->
  <div style="text-align:center;color:#475569;font-size:12px;padding:16px;">
    RTL Mutation Testing Engine &bull; digital-verify-pro &bull; {report.timestamp}
  </div>

</div>

<script>
// Animate score rings
document.querySelectorAll('.score-ring').forEach(el => {{
    const score = parseFloat(el.dataset.score);
    const color = el.dataset.color;
    const r = 40;
    const circ = 2 * Math.PI * r;
    const offset = circ - (score / 100) * circ;

    el.innerHTML = `
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="${{r}}" fill="none" stroke="#334155" stroke-width="8"/>
        <circle cx="50" cy="50" r="${{r}}" fill="none" stroke="${{color}}" stroke-width="8"
          stroke-dasharray="${{circ}}" stroke-dashoffset="${{offset}}"
          stroke-linecap="round" transform="rotate(-90 50 50)"
          style="transition: stroke-dashoffset 1.2s ease-out;"/>
        <text x="50" y="54" text-anchor="middle" fill="${{color}}"
          font-size="22" font-weight="800">${{score}}%</text>
      </svg>`;
}});
</script>

</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[eval] HTML report written to: {output_path}")


# ===========================================================================
# Demo Mode (for testing without real RTL)
# ===========================================================================

def generate_demo_report(output_dir: str):
    """Generate a demo report with realistic-looking data."""
    import random
    random.seed(42)

    report = EvalReport(
        project_name="digital-verify-pro (Demo)",
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        rtl_files=["examples/i2c/i2c_slave_model.sv", "examples/ot_dma/dma.sv"],
        tb_files=["tb/tb_i2c_full.sv"],
        total_mutants=18,
        killed=11,
        alive=5,
        equivalent=1,
        error=1,
        raw_score=68.8,
        score=62.3,
        tier="B",
        tier_label="Acceptable",
        tier_color="#f59e0b",
        eval_duration_s=12.4,
        sim_total_time_ms=3200.0,
    )

    # Demo category scores
    demo_cats = [
        {"category": "reg_bank", "label": "Register Bank", "total": 5, "killed": 4, "alive": 1, "equivalent": 0, "score": 80.0},
        {"category": "fsm", "label": "FSM", "total": 3, "killed": 1, "alive": 1, "equivalent": 1, "score": 50.0},
        {"category": "datapath", "label": "Datapath", "total": 4, "killed": 3, "alive": 1, "equivalent": 0, "score": 75.0},
        {"category": "interface", "label": "Interface", "total": 3, "killed": 2, "alive": 1, "equivalent": 0, "score": 66.7},
        {"category": "irq", "label": "IRQ", "total": 2, "killed": 0, "alive": 2, "equivalent": 0, "score": 0.0},
        {"category": "subsystem", "label": "Subsystem", "total": 1, "killed": 1, "alive": 0, "equivalent": 0, "score": 100.0},
    ]
    report.category_scores = demo_cats

    # Demo operator scores (with points)
    demo_ops = [
        {"operator": "SS-PARAM", "total": 1, "killed": 1, "alive": 0, "score": 100.0, "points": 80, "pts_earned": 80, "pts_possible": 80},
        {"operator": "RB-RST", "total": 2, "killed": 2, "alive": 0, "score": 100.0, "points": 78, "pts_earned": 156, "pts_possible": 156},
        {"operator": "RB-MASK", "total": 1, "killed": 1, "alive": 0, "score": 100.0, "points": 68, "pts_earned": 68, "pts_possible": 68},
        {"operator": "DP-OP", "total": 2, "killed": 2, "alive": 0, "score": 100.0, "points": 75, "pts_earned": 150, "pts_possible": 150},
        {"operator": "DP-CONST", "total": 1, "killed": 1, "alive": 0, "score": 100.0, "points": 42, "pts_earned": 42, "pts_possible": 42},
        {"operator": "IF-SEQ", "total": 1, "killed": 1, "alive": 0, "score": 100.0, "points": 88, "pts_earned": 88, "pts_possible": 88},
        {"operator": "IF-IDLE", "total": 1, "killed": 1, "alive": 0, "score": 100.0, "points": 45, "pts_earned": 45, "pts_possible": 45},
        {"operator": "FSM-ARC", "total": 1, "killed": 1, "alive": 0, "score": 100.0, "points": 72, "pts_earned": 72, "pts_possible": 72},
        {"operator": "RB-ACC", "total": 1, "killed": 1, "alive": 0, "score": 100.0, "points": 58, "pts_earned": 58, "pts_possible": 58},
        {"operator": "RB-WE", "total": 1, "killed": 0, "alive": 1, "score": 0.0, "points": 100, "pts_earned": 0, "pts_possible": 100},
        {"operator": "DP-MUX", "total": 1, "killed": 0, "alive": 1, "score": 0.0, "points": 55, "pts_earned": 0, "pts_possible": 55},
        {"operator": "IF-PROT", "total": 1, "killed": 0, "alive": 1, "score": 0.0, "points": 70, "pts_earned": 0, "pts_possible": 70},
        {"operator": "IRQ-POL", "total": 1, "killed": 0, "alive": 1, "score": 0.0, "points": 85, "pts_earned": 0, "pts_possible": 85},
        {"operator": "IRQ-MASK", "total": 1, "killed": 0, "alive": 1, "score": 0.0, "points": 76, "pts_earned": 0, "pts_possible": 76},
        {"operator": "FSM-RST", "total": 1, "killed": 0, "alive": 1, "score": 0.0, "points": 92, "pts_earned": 0, "pts_possible": 92},
        {"operator": "FSM-DEF", "total": 1, "killed": 0, "alive": 0, "score": 0.0, "points": 48, "pts_earned": 0, "pts_possible": 0},
    ]
    report.operator_scores = sorted(demo_ops, key=lambda x: x["score"], reverse=True)

    report.severity_scores = {
        "critical": {"total": 4, "killed": 2, "alive": 2, "score": 50.0},
        "high": {"total": 10, "killed": 7, "alive": 3, "score": 70.0},
        "medium": {"total": 3, "killed": 2, "alive": 1, "score": 66.7},
    }

    report.recommendations = [
        "[CRITICAL] IRQ detection rate is 0.0% — verification has major blind spots for irq bugs",
        "[WARNING] FSM detection rate is 50.0% — consider adding targeted tests for fsm category",
        "[RISK] 2 CRITICAL mutants survived: FSM-RST, RB-WE — these are the most dangerous bugs that your verification cannot detect",
        "[ACTION] Significant gaps detected. Review and enhance verification strategy.",
    ]

    # Demo mutant results
    demo_mutants = [
        {"mut_id": "RB-RST_0015", "operator": "RB-RST", "category": "reg_bank", "severity": "high", "points": 78,
         "description": "Reset value changed: 8'hA0 -> 8'hA1", "status": "killed", "kill_method": "compile_error",
         "original_text": "ctrl_q <= 8'hA0;", "mutated_text": "ctrl_q <= 8'hA1;",
         "sim_exit_code": 1, "sim_stderr": "", "sim_duration_ms": 120, "notes": ""},
        {"mut_id": "RB-WE_0028", "operator": "RB-WE", "category": "reg_bank", "severity": "critical", "points": 100,
         "description": "Write-enable inverted: pwrite -> !pwrite", "status": "alive", "kill_method": "survived",
         "original_text": "if (psel && pwrite)", "mutated_text": "if (psel && !pwrite)",
         "sim_exit_code": 0, "sim_stderr": "", "sim_duration_ms": 85, "notes": ""},
        {"mut_id": "FSM-RST_0005", "operator": "FSM-RST", "category": "fsm", "severity": "critical", "points": 92,
         "description": "Reset state changed: IDLE -> ACTIVE", "status": "alive", "kill_method": "survived",
         "original_text": "state <= IDLE;", "mutated_text": "state <= ACTIVE;",
         "sim_exit_code": 0, "sim_stderr": "", "sim_duration_ms": 90, "notes": ""},
        {"mut_id": "DP-OP_0032", "operator": "DP-OP", "category": "datapath", "severity": "high", "points": 75,
         "description": "Operator mutated: '+' -> '-'", "status": "killed", "kill_method": "compile_error",
         "original_text": "data_out <= a + b;", "mutated_text": "data_out <= a - b;",
         "sim_exit_code": 1, "sim_stderr": "width mismatch", "sim_duration_ms": 95, "notes": ""},
        {"mut_id": "IRQ-POL_0001", "operator": "IRQ-POL", "category": "irq", "severity": "high", "points": 85,
         "description": "IRQ polarity inverted: status_q -> ~status_q", "status": "alive", "kill_method": "survived",
         "original_text": "irq_o <= status_q;", "mutated_text": "irq_o <= ~status_q;",
         "sim_exit_code": 0, "sim_stderr": "", "sim_duration_ms": 75, "notes": ""},
    ]
    report.mutant_results = demo_mutants

    os.makedirs(output_dir, exist_ok=True)
    html_path = os.path.join(output_dir, "mutation_report.html")
    json_path = os.path.join(output_dir, "mutation_report.json")

    generate_html_report(report, html_path)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False, default=str)

    return html_path, json_path


# ===========================================================================
# CLI
# ===========================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="RTL Mutation Testing Evaluation & Leaderboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--rtl", help="RTL file or directory")
    ap.add_argument("--tb", help="Testbench file or directory")
    ap.add_argument("--include", nargs="*", default=[], help="Include directories for iverilog")
    ap.add_argument("--name", default="RTL Mutation Test", help="Project name for report")
    ap.add_argument("--out", default="output/eval_report", help="Output directory")
    ap.add_argument("--max", type=int, default=3, help="Max mutants per operator")
    ap.add_argument("--seed", type=int, default=42, help="Random seed")
    ap.add_argument("--timeout", type=float, default=30.0, help="Sim timeout per mutant (seconds)")
    ap.add_argument("--demo", action="store_true", help="Generate demo report with sample data")
    args = ap.parse_args(argv)

    if args.demo:
        print("[eval] Generating demo report...")
        html_path, json_path = generate_demo_report(args.out)
        print(f"[eval] Done! Report: {html_path}")
        return 0

    if not args.rtl or not args.tb:
        ap.error("--rtl and --tb are required (use --demo for sample report)")

    evaluator = MutationEvaluator(
        rtl_path=args.rtl,
        tb_path=args.tb,
        include_dirs=args.include,
        sim_timeout=args.timeout,
    )

    report = evaluator.run_full_eval(
        project_name=args.name,
        max_per_op=args.max,
        seed=args.seed,
    )

    os.makedirs(args.out, exist_ok=True)
    html_path = os.path.join(args.out, "mutation_report.html")
    json_path = os.path.join(args.out, "mutation_report.json")

    generate_html_report(report, html_path)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False, default=str)

    print(f"\n{'='*60}")
    print(f"  Final Score: {report.score}% (Tier {report.tier} - {report.tier_label})")
    print(f"  Killed: {report.killed}/{report.total_mutants}")
    print(f"  HTML:    {html_path}")
    print(f"  JSON:    {json_path}")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
