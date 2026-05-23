# RTL_BUG_INJECT

> RTL Mutation Testing for AI Verification Flow Evaluation

Inject realistic human bugs into SystemVerilog RTL to measure whether your AI-driven verification flow can catch them. **Not fault tolerance testing** -- this evaluates the *detection capability* of your verification environment.

## What is RTL Mutation Testing?

Traditional mutation testing modifies source code with small, plausible bugs (mutants) and checks if the test suite detects them. **RTL_BUG_INJECT** applies this concept to hardware design:

1. **Inject** common human bugs into correct RTL (reset value errors, operator flips, FSM transition mistakes, etc.)
2. **Run** your verification flow against each mutant
3. **Score** how many mutants are detected (killed) vs. missed (alive)

```
Mutation Score = killed / (total - equivalent) * 100%
```

A high mutation score means your verification flow is effective at catching real-world RTL bugs.

## 20 Mutation Operators

### IP-Level Mutations (16 operators)

| Category | Operator | Severity | Description |
|----------|----------|----------|-------------|
| **reg_bank** | `RB-RST` | high | Reset value: flip LSB |
| | `RB-MASK` | high | Field mask: shift by 1 bit |
| | `RB-WE` | critical | Write enable: invert |
| | `RB-ADDR` | critical | Register address: offset by 4 bytes |
| | `RB-ACC` | high | Access protection: remove RO guard |
| **fsm** | `FSM-ARC` | high | State transition: swap target |
| | `FSM-DEF` | medium | Default case: remove |
| | `FSM-RST` | critical | Reset state: use wrong state |
| **datapath** | `DP-OP` | high | Operator: `+` to `-`, `&` to `\|`, `>>` to `<<` |
| | `DP-MUX` | high | Mux: swap true/false branches |
| | `DP-CONST` | medium | Constant: +/- 1 |
| **interface** | `IF-SEQ` | critical | APB: skip setup phase |
| | `IF-PROT` | high | APB: suppress PSLVERR |
| | `IF-IDLE` | medium | APB: never deassert PSEL |
| **irq** | `IRQ-POL` | high | Interrupt polarity: invert |
| | `IRQ-MASK` | high | Enable logic: invert |
| | `IRQ-PEND` | high | W1C clear: remove |

### Subsystem-Level Mutations (4 operators)

| Operator | Severity | Description |
|----------|----------|-------------|
| `SS-CONN` | critical | Port connection: cross-wire two signals |
| `SS-BASE` | critical | Address base: offset by `0x1000` |
| `SS-PARAM` | high | Parameter: halve width/count |
| `SS-CLK` | high | Clock domain: manual cross-domain injection |

## Quick Start

```bash
# Generate mutants from RTL files
python engines/rtl_mutator.py --rtl rtl/ --category all --max 5 --out output/mutants/

# Generate only register bank mutations
python engines/rtl_mutator.py --rtl rtl/ --category reg_bank --max 10 --out output/mutants/

# List all available operators
python engines/rtl_mutator.py --list-operators

# Calculate mutation score (after filling in mutant statuses)
python engines/rtl_mutator.py --score output/mutants/manifest.json
```

### Python API

```python
from engines.rtl_mutator import RTLMutator, MutCategory

mutator = RTLMutator('rtl/my_ip.sv')
mutants = mutator.generate(
    categories=[MutCategory.REG_BANK, MutCategory.DATAPATH],
    max_per_op=3,
    seed=42
)

for mutant_file in mutants:
    print(f"{mutant_file.spec.mut_id}: {mutant_file.spec.description}")

# Write all mutants to disk with manifest
manifest = mutator.write_all(mutants, 'output/mutants/')
```

## Project Structure

```
RTL_BUG_INJECT/
├── engines/
│   └── rtl_mutator.py          # Core mutation engine (CLI + API)
├── skills/
│   ├── SKILL.md                # Quick decision tree & index
│   ├── SKILL_ip_level.md       # Complete IP-level usage guide
│   ├── SKILL_regbank.md        # Register bank mutation deep-dive
│   ├── SKILL_fsm.md            # FSM mutation deep-dive
│   └── SKILL_subsystem.md      # Subsystem-level mutation guide
├── examples/
│   ├── i2c/
│   │   └── i2c_slave_model.sv  # I2C slave RTL (test target)
│   └── ot_dma/
│       └── dma.sv              # OpenTitan DMA RTL (complex test target)
├── schemas/
│   └── ip_spec_example.yml     # IP spec YAML example
└── README.md
```

## Mutation Workflow

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐     ┌─────────────┐
│  Correct     │────>│   Inject     │────>│   Run        │────>│   Score     │
│  RTL (.sv)   │     │   Mutants    │     │   Verification│    │   Results   │
└─────────────┘     └──────────────┘     └──────────────┘     └─────────────┘
                          │                     │
                          v                     v
                   manifest.json          killed / alive
```

1. **Generate mutants** with `rtl_mutator.py`
2. **Run your verification** (UVM, cocotb, formal, etc.) against each mutant
3. **Record results** in `manifest.json` (`killed`, `alive`, `equivalent`)
4. **Calculate score** with `--score`

## Tested Targets

| Target | Description | Mutants Generated |
|--------|-------------|-------------------|
| `i2c_slave_model.sv` | I2C slave with register bank | 15+ across all categories |
| `dma.sv` (OpenTitan) | Complex DMA controller | 7+ high-quality mutants |

## Requirements

- Python 3.8+
- No external dependencies (uses only `re`, `json`, `pathlib`, `argparse`, `random`)

## License

MIT
