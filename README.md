# RTL_BUG_INJECT

> **Mutation Testing for AI-Driven RTL Verification — Quantify how well your verification flow catches real bugs.**

[中文文档](README_zh.md)

## What Problem Does This Solve?

IC Agent Hub claims it can automatically generate UVM environments, write assertions, run regression, and close coverage. But can chip companies really trust it?

**What if the AI-generated verification flow misses critical bugs?**

RTL_BUG_INJECT answers this question by injecting known, realistic human bugs into correct RTL and measuring whether your verification flow detects them. Missed bugs = **verification blind spots**.

---

## Core Concept

**This is NOT fault tolerance testing. This is verification quality assessment.**

| Fault Injection | RTL Mutation Testing |
|:---:|:---:|
| Tests DUT resilience | Tests verification effectiveness |
| Injects runtime errors (SEU, etc.) | Modifies RTL source with human-like bugs |
| "Is the chip robust enough?" | "Is the verification thorough enough?" |

### Workflow

```
 Correct RTL      Inject Human Bugs     Run Verification       Score Results
 +----------+    +--------------+     +--------------+     +----------+
 |          |---> |  20 operator |---> |  Killed /    |---> | Mutation |
 |  Golden  |    |  types,      |     |  Survived?   |     |  Score   |
 |  RTL     |    |  N injected  |     |              |     |          |
 +----------+    +--------------+     +--------------+     +----------+
```

1. **Baseline pass** — Run your verification flow against correct RTL, confirm it passes
2. **Inject bugs** — Randomly select N bugs from 20 operator types and inject into RTL
3. **Re-run verification** — Run the **same** verification flow against each mutant
4. **Score** — Killed (detected) vs. Survived (missed — verification blind spot)

> **Mutation Score** = Sum(killed_points) / Sum(total_points) x 100%
>
> Each bug type carries a different difficulty weight (0-100). Missing a "reset value flip" is less dangerous than missing a "cross-wired port connection."

---

## Scoring Tiers

| Tier | Score Range | Interpretation |
|:---:|:---:|:---|
| **S** | 90-100% | Near-zero blind spots. Verification flow is highly effective. |
| **A** | 75-90% | Most bugs caught. Minor edge cases may need additional tests. |
| **B** | 60-75% | Above baseline, but notable blind spots require attention. |
| **C** | 40-60% | Significant gaps. Verification strategy needs re-evaluation. |
| **D** | 0-40% | Largely ineffective. AI verification flow cannot be trusted. |

---

## 20 Mutation Operators

### IP-Level (16 operators)

| Category | Operator | Points | Description |
|:---:|:---:|:---:|:---|
| **Register Bank** | `RB-RST` | 75 | Reset value: flip LSB |
| | `RB-MASK` | 70 | Field mask: shift by 1 bit |
| | `RB-WE` | **100** | Write enable: invert |
| | `RB-ADDR` | 90 | Register address: offset by 4 bytes |
| | `RB-ACC` | 65 | Access protection: remove RO guard |
| **FSM** | `FSM-ARC` | 88 | State transition: swap target |
| | `FSM-DEF` | 55 | Default case: remove |
| | `FSM-RST` | **95** | Reset state: use wrong state |
| **Datapath** | `DP-OP` | 60 | Operator: + to -, & to |, >> to << |
| | `DP-MUX` | 72 | Mux: swap true/false branches |
| | `DP-CONST` | 42 | Constant: +/- 1 |
| **Interface** | `IF-SEQ` | **92** | APB: skip setup phase |
| | `IF-PROT` | 78 | APB: suppress PSLVERR |
| | `IF-IDLE` | 48 | APB: never deassert PSEL |
| **Interrupt** | `IRQ-POL` | 62 | Interrupt polarity: invert |
| | `IRQ-MASK` | 58 | Enable logic: invert |
| | `IRQ-PEND` | 64 | W1C clear: remove |

### Subsystem-Level (4 operators)

| Operator | Points | Description |
|:---:|:---:|:---|
| `SS-CONN` | **98** | Port connection: cross-wire two signals |
| `SS-BASE` | 85 | Address base: offset by 0x1000 |
| `SS-PARAM` | 70 | Parameter: halve width/count |
| `SS-CLK` | 80 | Clock domain: manual cross-domain injection |

---

## Two Evaluation Modes

### Auto Mode (Automated)

Fully automated: inject bugs -> iverilog compile and simulate -> auto-classify killed/alive -> generate score.

```bash
python engines/batch_challenger.py auto \
    --rtl examples/i2c/i2c_slave_model.sv \
    --tb examples/i2c/tb_i2c_challenge.sv \
    --challenge-name auto_i2c \
    --num-bugs 5
```

Best for: quick evaluation, CI/CD integration, regression testing.

### Challenge Mode (Manual Debug)

Inject multiple bugs -> provide original and mutated RTL -> debug by any means -> submit found bugs -> get scored.

```bash
# Create a challenge
python engines/batch_challenger.py create \
    --rtl examples/i2c/i2c_slave_model.sv \
    --num-bugs 5 \
    --name my_challenge

# View challenge (original vs. mutated diff)
python engines/batch_challenger.py view --name my_challenge

# Submit bugs you found
python engines/batch_challenger.py submit \
    --name my_challenge \
    --found "FSM-ARC" "RB-WE" "DP-OP"

# Get your score
python engines/batch_challenger.py score --name my_challenge

# View leaderboard
python engines/batch_challenger.py leaderboard
```

Best for: team competition, skill assessment, extreme debug challenges.

---

## Leaderboard

```bash
python engines/batch_challenger.py leaderboard
```

Generates a dark-themed HTML leaderboard with:

- Total score and tier (S/A/B/C/D) per participant
- Auto / Challenge mode annotation
- Score breakdown by operator and category
- **Missed bug analysis** — which bug types slipped through

---

## Project Structure

```
RTL_BUG_INJECT/
├── engines/
│   ├── rtl_mutator.py          # Core mutation engine (20 operators, CLI + API)
│   ├── batch_challenger.py     # Dual-mode evaluation (Auto + Challenge)
│   └── mutation_eval.py        # Evaluation engine (HTML reports, scoring)
├── examples/
│   ├── i2c/
│   │   ├── i2c_slave_model.sv  # I2C slave RTL (test target)
│   │   └── tb_i2c_challenge.sv # I2C testbench (auto mode)
│   └── ot_dma/
│       └── dma.sv              # OpenTitan DMA (complex test target)
├── skills/
│   ├── SKILL.md                # Quick decision tree and index
│   ├── SKILL_ip_level.md       # IP-level usage guide
│   ├── SKILL_fsm.md            # FSM mutation deep-dive
│   └── SKILL_subsystem.md      # Subsystem-level mutation guide
└── challenges/                 # Challenge archive (answer.json / task.json)
```

---

## Quick Start

```bash
# Clone
git clone https://github.com/Joe1995-best/RTL_BUG_INJECT.git
cd RTL_BUG_INJECT

# Generate mutants
python engines/rtl_mutator.py --rtl examples/i2c/ --category all --max 5 --out output/mutants/

# Run auto evaluation (requires iverilog)
python engines/batch_challenger.py auto \
    --rtl examples/i2c/i2c_slave_model.sv \
    --tb examples/i2c/tb_i2c_challenge.sv \
    --challenge-name auto_i2c \
    --num-bugs 5

# View leaderboard
python engines/batch_challenger.py leaderboard
```

### Python API

```python
from engines.rtl_mutator import RTLMutator, MutCategory

mutator = RTLMutator("rtl/my_ip.sv")
mutants = mutator.generate(
    categories=[MutCategory.REG_BANK, MutCategory.DATAPATH],
    max_per_op=3,
    seed=42
)

for m in mutants:
    print(f"{m.spec.mut_id}: {m.spec.description} (difficulty: {m.spec.points})")

manifest = mutator.write_all(mutants, "output/mutants/")
```

---

## Why This Matters

Software engineering has mature mutation testing tools (PIT, MutPy), but the hardware/RTL domain remains largely uncovered.

| Tool | Limitation |
|:---:|:---|
| MCY | Netlist-level only; no UVM integration; no semantic bug classification |
| PIT / MutPy | Software tools; no SystemVerilog support |
| Manual bug injection | No systematic taxonomy; not quantifiable; not reproducible |

**RTL_BUG_INJECT fills the gap: RTL source-level + semantic bug classification + difficulty weighting + AI verification flow evaluation.**

---

## Requirements

- Python 3.8+
- No external dependencies (`re`, `json`, `pathlib`, `argparse`, `random`)
- Auto mode requires: `iverilog` + `vvp`

## License

MIT
