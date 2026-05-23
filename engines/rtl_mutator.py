#!/usr/bin/env python3
"""
rtl_mutator.py — RTL Mutation Testing Engine for digital-verify-pro

PURPOSE
-------
RTL Mutation Testing ≠ Fault Injection (fault_injector.py)

  fault_injector  : Injects runtime faults into a CORRECT DUT to test
                    the DUT's own fault-tolerance / recovery behaviour.

  rtl_mutator     : Edits the RTL SOURCE to model common HUMAN CODING BUGS,
                    then runs the AI verification pipeline to check whether
                    the pipeline can DETECT the introduced bug.
                    Score = (killed mutants) / (total mutants).

This is the industry-standard Mutation Testing metric applied to an AI-driven
hardware verification flow.

MUTATION TAXONOMY
-----------------
Level 1 — IP-level mutations (single-module scope)
  reg_bank  : Register file / CSR bank bugs
  fsm       : State-machine transition / encoding bugs
  datapath  : Arithmetic / logic / shift / mux bugs
  interface : Bus protocol handshake / timing bugs
  irq       : Interrupt polarity / mask / enable bugs

Level 2 — Subsystem-level mutations (cross-module scope)
  interconnect : Wrong port connection between instances
  addr_map     : Base address / offset shift in integration
  param        : Wrong parameter propagation across instances
  clock_domain : Missing CDC sync / wrong clock domain assignment

Mutation Operators (per category)
----------------------------------
reg_bank:
  RB-RST   Reset value wrong (off-by-one, bit-flip)
  RB-MASK  Field mask incorrect (extra bit included / excluded)
  RB-ACC   Access type swapped (rw↔ro, r1c treated as rw)
  RB-ADDR  Register offset wrong (±4 bytes)
  RB-WE    Write-enable condition inverted or missing

fsm:
  FSM-ARC  Transition arc missing or target state wrong
  FSM-DEF  Default/illegal state not defined or wrong action
  FSM-ENC  One-hot encoding error (two bits set)
  FSM-OUT  Moore output registered when should be combinational
  FSM-RST  Reset state wrong

datapath:
  DP-OP    Operator replaced (+ → -, & → |, >> → <<)
  DP-WIDTH Bit-width truncation (dropped MSBs)
  DP-MUX   Mux select inverted or swapped
  DP-CONST Wrong constant value (off-by-one, wrong mask)
  DP-SIGN  Signed/unsigned mismatch

interface:
  IF-CLK   Missing @(posedge) vs @(negedge) clock edge
  IF-SEQ   Signal assertion order wrong (PSEL before PENABLE swapped)
  IF-TIM   Missing 1-cycle setup/access phase
  IF-PROT  PSLVERR not driven on illegal address
  IF-IDLE  Signals not driven idle after transaction

irq:
  IRQ-POL  Interrupt polarity inverted
  IRQ-MASK Enable/mask logic inverted (all masked vs all enabled)
  IRQ-LATCH Level-triggered vs edge-triggered confusion
  IRQ-PEND Pending register not cleared on acknowledge

subsystem:
  SS-CONN  Wrong port connected (addr ↔ data swapped)
  SS-BASE  Base address offset wrong in address decoder
  SS-PARAM Wrong parameter value propagated to child instance
  SS-CLK   Wrong clock signal connected to instance

Usage (CLI):
    python engines/rtl_mutator.py --rtl rtl/pl061_gpio_regs.sv --category reg_bank --out ./mutants/
    python engines/rtl_mutator.py --rtl rtl/ --category all --level ip --out ./mutants/ --max 20
    python engines/rtl_mutator.py --list-operators
    python engines/rtl_mutator.py --rtl rtl/pl061_gpio.sv --op DP-OP,FSM-ARC --out ./mutants/

Usage (API):
    from engines.rtl_mutator import RTLMutator, MutCategory
    m = RTLMutator(rtl_path="rtl/pl061_gpio_regs.sv")
    mutants = m.generate(categories=[MutCategory.REG_BANK], max_per_op=3)
    m.write_all(mutants, output_dir="./mutants/")
"""

import argparse
import copy
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class MutCategory(Enum):
    REG_BANK    = "reg_bank"
    FSM         = "fsm"
    DATAPATH    = "datapath"
    INTERFACE   = "interface"
    IRQ         = "irq"
    SUBSYSTEM   = "subsystem"

    @classmethod
    def from_str(cls, s: str) -> "MutCategory":
        for m in cls:
            if m.value == s:
                return m
        raise ValueError(f"Unknown category: {s!r}. Valid: {[x.value for x in cls]}")


class MutLevel(Enum):
    IP         = "ip"           # single-module
    SUBSYSTEM  = "subsystem"    # cross-module integration


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class MutantSpec:
    """Describes one mutation applied to a source file."""
    mut_id: str                  # e.g. "RB-RST_0003"
    operator: str                # e.g. "RB-RST"
    category: str                # e.g. "reg_bank"
    level: str                   # "ip" | "subsystem"
    source_file: str             # original RTL file
    description: str             # human-readable description
    line_no: int                 # 1-based line number in original
    original_text: str           # original line/fragment
    mutated_text: str            # mutated line/fragment
    kill_hint: str               # what checker/test should catch this
    severity: str = "medium"     # "low" | "medium" | "high" | "critical"
    status: str = "alive"        # "alive" | "killed" | "equivalent"


@dataclass
class MutantFile:
    """A complete mutated RTL file ready to write to disk."""
    spec: MutantSpec
    content: str                 # full file content with mutation applied


# ---------------------------------------------------------------------------
# Operator Registry
# ---------------------------------------------------------------------------

# Each operator is a function:
#   (lines: List[str], rng_seed: int) -> List[MutantSpec]
# returning zero or more mutation specs (not yet applied).

_OPERATORS: Dict[str, Dict[str, Any]] = {}

def _op(name: str, category: MutCategory, level: MutLevel,
        description: str, kill_hint: str, severity: str = "medium"):
    """Decorator to register a mutation operator."""
    def decorator(fn: Callable):
        _OPERATORS[name] = {
            "fn": fn,
            "name": name,
            "category": category,
            "level": level,
            "description": description,
            "kill_hint": kill_hint,
            "severity": severity,
        }
        return fn
    return decorator


# ===========================================================================
# REG_BANK Operators
# ===========================================================================

@_op("RB-RST", MutCategory.REG_BANK, MutLevel.IP,
     "Reset value wrong: flip one bit in a reset assignment",
     "Reset value check in UVM RAL model or reset sequence",
     severity="high")
def _op_rb_rst(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Find `<= N'hXXX` or `<= N'bXXX` in reset blocks, flip LSB."""
    specs = []
    in_reset = False
    for i, line in enumerate(lines):
        # Detect reset block
        if re.search(r'if\s*\(\s*[!~]', line):
            in_reset = True
        if in_reset and re.search(r'(end\b|else\b)', line) and '=>' not in line:
            if 'begin' not in line:
                in_reset = False

        # Look for hex/binary constants in reset block
        if in_reset:
            m = re.search(r"(<=\s*)(\d+'h[0-9a-fA-F_]+)", line)
            if m:
                orig_val = m.group(2)
                # Flip the last hex digit
                mutated_val = _flip_hex_lsb(orig_val)
                if mutated_val != orig_val:
                    mutated_line = line[:m.start(2)] + mutated_val + line[m.end(2):]
                    specs.append(MutantSpec(
                        mut_id=f"RB-RST_{i:04d}",
                        operator="RB-RST",
                        category="reg_bank",
                        level="ip",
                        source_file="",
                        description=f"Reset value changed: {orig_val} → {mutated_val}",
                        line_no=i + 1,
                        original_text=line.rstrip(),
                        mutated_text=mutated_line.rstrip(),
                        kill_hint="UVM reset_seq: read-back register after reset should equal spec reset value",
                        severity="high",
                    ))
    return specs


@_op("RB-MASK", MutCategory.REG_BANK, MutLevel.IP,
     "Field mask incorrect: shift mask left by 1 bit",
     "Write to a field and verify adjacent field is not corrupted",
     severity="high")
def _op_rb_mask(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Find bit-mask assignments like `{x[31:8], pwdata[7:0]}` and shift range."""
    specs = []
    for i, line in enumerate(lines):
        # Match field range extraction like pwdata[7:0] or q[15:8]
        m = re.search(r'(\w+)\[(\d+):(\d+)\]', line)
        if m and ('pdata' in line.lower() or 'wdata' in line.lower() or
                  'pwdata' in line.lower() or '_q' in line):
            hi = int(m.group(2))
            lo = int(m.group(3))
            if hi > lo and lo > 0:  # avoid shifting into negative
                # Shift range up by 1 — models off-by-one mask error
                new_hi = hi + 1
                new_lo = lo + 1
                orig_frag = f"{m.group(1)}[{hi}:{lo}]"
                new_frag  = f"{m.group(1)}[{new_hi}:{new_lo}]"
                mutated_line = line[:m.start()] + new_frag + line[m.end():]
                specs.append(MutantSpec(
                    mut_id=f"RB-MASK_{i:04d}",
                    operator="RB-MASK",
                    category="reg_bank",
                    level="ip",
                    source_file="",
                    description=f"Field mask shifted: [{hi}:{lo}] → [{new_hi}:{new_lo}]",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="Write field value, read back; check adjacent field bits unchanged",
                    severity="high",
                ))
                break  # one mutation per operator per file pass
    return specs


@_op("RB-WE", MutCategory.REG_BANK, MutLevel.IP,
     "Write-enable condition inverted: pwrite → !pwrite",
     "Write to register must update value; read must not",
     severity="critical")
def _op_rb_we(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Flip pwrite condition in register write-enable logic."""
    specs = []
    for i, line in enumerate(lines):
        if re.search(r'\bpwrite\b', line) and '&&' in line and '<=' in line:
            # Invert pwrite
            mutated_line = re.sub(r'\bpwrite\b', '!pwrite', line, count=1)
            if mutated_line != line:
                specs.append(MutantSpec(
                    mut_id=f"RB-WE_{i:04d}",
                    operator="RB-WE",
                    category="reg_bank",
                    level="ip",
                    source_file="",
                    description="Write-enable inverted: pwrite → !pwrite (writes only happen on reads)",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="APB write sequence: write value X, read back should equal X not original reset",
                    severity="critical",
                ))
                break
    return specs


@_op("RB-ADDR", MutCategory.REG_BANK, MutLevel.IP,
     "Register offset wrong by 4 bytes (next register address)",
     "Register at address N overlaps with register at N+4",
     severity="critical")
def _op_rb_addr(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Find `case (paddr)` hex addresses and shift one by +4."""
    specs = []
    for i, line in enumerate(lines):
        m = re.search(r"(\d+'h)([0-9a-fA-F]+)(\s*:)", line)
        if m and ('case' not in line) and re.search(r'case', ''.join(lines[max(0,i-5):i])):
            orig_hex = m.group(2)
            try:
                addr_val = int(orig_hex, 16)
                new_val  = addr_val + 4
                new_hex  = format(new_val, f'0{len(orig_hex)}X')
                mutated_line = line[:m.start(2)] + new_hex + line[m.end(2):]
                specs.append(MutantSpec(
                    mut_id=f"RB-ADDR_{i:04d}",
                    operator="RB-ADDR",
                    category="reg_bank",
                    level="ip",
                    source_file="",
                    description=f"Register address shifted: 'h{orig_hex} → 'h{new_hex} (+4 bytes)",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="Access register at correct address; scoreboard should flag unexpected no-decode",
                    severity="critical",
                ))
                break
            except ValueError:
                pass
    return specs


@_op("RB-ACC", MutCategory.REG_BANK, MutLevel.IP,
     "Read-only register becomes writable (ro guard condition removed)",
     "Write to RO register should be ignored; PSLVERR or silent drop expected",
     severity="high")
def _op_rb_acc(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Remove a read-only guard `if (!pwrite)` that protects a register."""
    specs = []
    for i, line in enumerate(lines):
        # Pattern: assign or always reading without write condition — look for
        # pwrite guard that protects a register from being written
        if re.search(r'!pwrite', line) and ('<=' in line or 'assign' in line):
            # Remove the !pwrite guard entirely
            mutated_line = re.sub(r'&&\s*!pwrite|!pwrite\s*&&', '', line)
            mutated_line = re.sub(r'if\s*\(\s*!pwrite\s*\)', 'if (1\'b1)', mutated_line)
            if mutated_line != line:
                specs.append(MutantSpec(
                    mut_id=f"RB-ACC_{i:04d}",
                    operator="RB-ACC",
                    category="reg_bank",
                    level="ip",
                    source_file="",
                    description="RO guard removed: register is now writable when it should be read-only",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="Write to RO register, read back; value must not change",
                    severity="high",
                ))
                break
    return specs


# ===========================================================================
# FSM Operators
# ===========================================================================

@_op("FSM-ARC", MutCategory.FSM, MutLevel.IP,
     "State transition target wrong: replace one next-state assignment",
     "FSM must reach target state; coverage should show missing state transitions",
     severity="high")
def _op_fsm_arc(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Find `next_state = STATE_X` and replace with a neighbouring state."""
    specs = []
    # Collect all state names first
    state_names = []
    for line in lines:
        m = re.findall(r'\b([A-Z_]+STATE[A-Z_]*|STATE_[A-Z_]+|S_[A-Z_]+|[A-Z]+_ST)\b', line)
        state_names.extend(m)
    state_names = list(dict.fromkeys(state_names))  # deduplicate, preserve order

    if len(state_names) < 2:
        return specs

    for i, line in enumerate(lines):
        m = re.search(r'(next_state\s*(?:<=|=)\s*)(\w+)(\s*;)', line)
        if m and m.group(2) in state_names:
            orig_state = m.group(2)
            idx = state_names.index(orig_state)
            new_state = state_names[(idx + 1) % len(state_names)]
            if new_state != orig_state:
                mutated_line = line[:m.start(2)] + new_state + line[m.end(2):]
                specs.append(MutantSpec(
                    mut_id=f"FSM-ARC_{i:04d}",
                    operator="FSM-ARC",
                    category="fsm",
                    level="ip",
                    source_file="",
                    description=f"FSM arc target changed: {orig_state} → {new_state}",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="FSM directed test: apply input that triggers this arc; check state coverpoint",
                    severity="high",
                ))
                break
    return specs


@_op("FSM-DEF", MutCategory.FSM, MutLevel.IP,
     "Default state missing: remove `default:` clause in state case",
     "X-propagation check or assertion on illegal-state detection",
     severity="medium")
def _op_fsm_def(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Comment out a `default:` clause in a case statement."""
    specs = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('default') and ':' in stripped:
            mutated_line = line.replace('default', '//DEFAULT_REMOVED// default', 1)
            specs.append(MutantSpec(
                mut_id=f"FSM-DEF_{i:04d}",
                operator="FSM-DEF",
                category="fsm",
                level="ip",
                source_file="",
                description="Default case removed from state machine (X-propagation risk)",
                line_no=i + 1,
                original_text=line.rstrip(),
                mutated_text=mutated_line.rstrip(),
                kill_hint="Directed test to illegal state; assertion `assert(state != X)` should fire",
                severity="medium",
            ))
            break
    return specs


@_op("FSM-RST", MutCategory.FSM, MutLevel.IP,
     "Reset state wrong: initial state set to second state instead of IDLE/RESET",
     "After reset, FSM must be in IDLE; check state register read via debug port",
     severity="critical")
def _op_fsm_rst(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Replace the reset-state assignment in the reset block."""
    specs = []
    in_reset = False
    state_names = []
    for line in lines:
        m = re.findall(r'\b(IDLE|RESET|ST_IDLE|S_IDLE|S0|STATE_IDLE)\b', line, re.IGNORECASE)
        state_names.extend(m)
    state_names = list(dict.fromkeys(state_names))

    for i, line in enumerate(lines):
        if re.search(r'if\s*\(\s*[!~]', line):
            in_reset = True
        if in_reset:
            m = re.search(r'((?:current_state|state|cs)\s*<=\s*)(\w+)(\s*;)', line, re.IGNORECASE)
            if m and state_names:
                orig_st = m.group(2)
                # Find a different state to substitute
                candidates = [s for s in state_names if s.upper() != orig_st.upper()]
                if candidates:
                    new_st = candidates[0]
                    mutated_line = line[:m.start(2)] + new_st + line[m.end(2):]
                    specs.append(MutantSpec(
                        mut_id=f"FSM-RST_{i:04d}",
                        operator="FSM-RST",
                        category="fsm",
                        level="ip",
                        source_file="",
                        description=f"Reset state changed: {orig_st} → {new_st} (wrong initial state after reset)",
                        line_no=i + 1,
                        original_text=line.rstrip(),
                        mutated_text=mutated_line.rstrip(),
                        kill_hint="Apply reset; poll FSM state register / debug bus; must show IDLE state",
                        severity="critical",
                    ))
                    break
    return specs


# ===========================================================================
# DATAPATH Operators
# ===========================================================================

@_op("DP-OP", MutCategory.DATAPATH, MutLevel.IP,
     "Arithmetic/logic operator replaced (+ → -, & → |, >> → <<)",
     "Directed arithmetic test with known operands; scoreboard checks result",
     severity="high")
def _op_dp_op(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Replace an operator in a datapath assignment."""
    OP_PAIRS = [(r'\+', '-'), (r'\b&\b', '|'), (r'\b\|\b', '&'),
                (r'>>',  '<<'), (r'<<', '>>'), (r'\^', '&')]
    specs = []
    for i, line in enumerate(lines):
        # Skip comment lines, pure assignments without computation
        if line.strip().startswith('//'):
            continue
        if '<=' not in line and '=' not in line:
            continue
        for orig_pat, replacement in OP_PAIRS:
            m = re.search(orig_pat, line)
            if m:
                mutated_line = re.sub(orig_pat, replacement, line, count=1)
                if mutated_line != line:
                    specs.append(MutantSpec(
                        mut_id=f"DP-OP_{i:04d}",
                        operator="DP-OP",
                        category="datapath",
                        level="ip",
                        source_file="",
                        description=f"Operator mutated: '{m.group(0)}' → '{replacement}' on line {i+1}",
                        line_no=i + 1,
                        original_text=line.rstrip(),
                        mutated_text=mutated_line.rstrip(),
                        kill_hint="Arithmetic test: apply known operands, expect known result; scoreboard catches mismatch",
                        severity="high",
                    ))
                    return specs  # one per call
    return specs


@_op("DP-MUX", MutCategory.DATAPATH, MutLevel.IP,
     "Mux select inverted: condition ? a : b → condition ? b : a",
     "Test both branches of the mux; one will produce wrong output",
     severity="high")
def _op_dp_mux(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Swap the two branches of a ternary mux expression."""
    specs = []
    for i, line in enumerate(lines):
        # Match ternary: <cond> ? <true_branch> : <false_branch>
        # true/false branches can be: identifier, bit-literal (1'b0, 1'bz), number
        m = re.search(
            r'(\w[\w\s\[\]:]*)\s*\?\s*'                    # condition
            r'((?:\d+\'[bodh][\w]+|\w[\w\[\]]*|\'[01z]+))'  # true branch
            r'\s*:\s*'
            r'((?:\d+\'[bodh][\w]+|\w[\w\[\]]*|\'[01z]+))',  # false branch
            line
        )
        if m:
            cond = m.group(1).strip()
            true_br = m.group(2).strip()
            false_br = m.group(3).strip()
            if true_br != false_br:
                # Preserve everything after the matched false branch
                mutated_frag = f"{cond} ? {false_br} : {true_br}"
                mutated_line = line[:m.start()] + mutated_frag + line[m.end():]
                specs.append(MutantSpec(
                    mut_id=f"DP-MUX_{i:04d}",
                    operator="DP-MUX",
                    category="datapath",
                    level="ip",
                    source_file="",
                    description=f"Mux branches swapped: '{true_br}' ↔ '{false_br}'",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="Test both mux-select conditions; output should differ; scoreboard catches swap",
                    severity="high",
                ))
                break
    return specs


@_op("DP-CONST", MutCategory.DATAPATH, MutLevel.IP,
     "Constant off-by-one: integer literal ± 1",
     "Boundary value test (e.g. counter overflow); expect N not N-1",
     severity="medium")
def _op_dp_const(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Decrement a numeric constant in a datapath comparison."""
    specs = []
    for i, line in enumerate(lines):
        if line.strip().startswith('//'):
            continue
        # Find integer comparisons: == N, >= N, <= N, != N
        m = re.search(r'([><=!]=\s*)(\d{2,})\b', line)
        if m:
            orig_val = int(m.group(2))
            new_val  = orig_val - 1
            mutated_line = line[:m.start(2)] + str(new_val) + line[m.end(2):]
            if mutated_line != line:
                specs.append(MutantSpec(
                    mut_id=f"DP-CONST_{i:04d}",
                    operator="DP-CONST",
                    category="datapath",
                    level="ip",
                    source_file="",
                    description=f"Constant decremented by 1: {orig_val} → {new_val} (off-by-one error)",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="Boundary value test at N and N-1; FSM/counter should transition at N not N-1",
                    severity="medium",
                ))
                break
    return specs


# ===========================================================================
# INTERFACE Operators
# ===========================================================================

@_op("IF-SEQ", MutCategory.INTERFACE, MutLevel.IP,
     "APB: PENABLE asserted same cycle as PSEL (setup phase skipped)",
     "APB protocol checker assertion: PSEL must precede PENABLE by one cycle",
     severity="critical")
def _op_if_seq(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Find the cycle where PSEL and PENABLE are separately driven; collapse into one."""
    specs = []
    for i, line in enumerate(lines):
        # Look for the PSEL assertion line (setup phase)
        if re.search(r'psel\s*<=\s*1', line) and 'penable' not in line.lower():
            # Look ahead for penable assertion
            for j in range(i + 1, min(i + 5, len(lines))):
                if re.search(r'penable\s*<=\s*1', lines[j]):
                    # Combine: add penable to the PSEL line
                    mutated_line = line.rstrip() + '  // MUTANT: penable added same cycle\n'
                    mutated_line = mutated_line.replace(
                        'psel  <= 1',
                        'psel  <= 1;\n      penable <= 1  // MUT:IF-SEQ'
                    )
                    specs.append(MutantSpec(
                        mut_id=f"IF-SEQ_{i:04d}",
                        operator="IF-SEQ",
                        category="interface",
                        level="ip",
                        source_file="",
                        description="APB setup phase skipped: PSEL and PENABLE asserted same cycle",
                        line_no=i + 1,
                        original_text=line.rstrip(),
                        mutated_text=f"{line.rstrip()} // + penable same cycle [MUT:IF-SEQ]",
                        kill_hint="APB SVA: `$rose(psel) |=> $rose(penable)` must fire (1-cycle setup required)",
                        severity="critical",
                    ))
                    break
            if specs:
                break
    return specs


@_op("IF-PROT", MutCategory.INTERFACE, MutLevel.IP,
     "PSLVERR not driven on illegal address access",
     "Access reserved/unimplemented address; PSLVERR should be asserted",
     severity="high")
def _op_if_prot(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Remove pslverr default-drive or error signalling."""
    specs = []
    for i, line in enumerate(lines):
        if re.search(r'pslverr\s*<=?\s*1', line) or \
           re.search(r'pslverr\s*=\s*1', line):
            mutated_line = re.sub(r'pslverr\s*<=?\s*1', 'pslverr <= 0  // MUT:IF-PROT', line)
            if mutated_line != line:
                specs.append(MutantSpec(
                    mut_id=f"IF-PROT_{i:04d}",
                    operator="IF-PROT",
                    category="interface",
                    level="ip",
                    source_file="",
                    description="PSLVERR suppressed: illegal address access no longer signals error",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="Access address hole; PSLVERR assertion `address_hole_check` must fire",
                    severity="high",
                ))
                break
    return specs


@_op("IF-IDLE", MutCategory.INTERFACE, MutLevel.IP,
     "Bus signals not driven idle after transaction ends",
     "Monitor detects lingering PSEL/PENABLE high between transactions",
     severity="medium")
def _op_if_idle(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Remove the PSEL/PENABLE de-assertion lines after a transaction."""
    specs = []
    for i, line in enumerate(lines):
        if re.search(r'psel\s*<=\s*0', line):
            mutated_line = '      // MUT:IF-IDLE — psel de-assert removed\n'
            specs.append(MutantSpec(
                mut_id=f"IF-IDLE_{i:04d}",
                operator="IF-IDLE",
                category="interface",
                level="ip",
                source_file="",
                description="PSEL not de-asserted after transaction (bus held busy)",
                line_no=i + 1,
                original_text=line.rstrip(),
                mutated_text="// MUT:IF-IDLE: psel <= 0; — REMOVED",
                kill_hint="Monitor between-transaction check: PSEL must be 0 in IDLE; SVA detects violation",
                severity="medium",
            ))
            break
    return specs


# ===========================================================================
# IRQ Operators
# ===========================================================================

@_op("IRQ-POL", MutCategory.IRQ, MutLevel.IP,
     "Interrupt polarity inverted: active-high treated as active-low",
     "Trigger interrupt condition; verify IRQ output polarity in scoreboard",
     severity="high")
def _op_irq_pol(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Invert the interrupt output driving condition."""
    specs = []
    for i, line in enumerate(lines):
        # Look for interrupt assign or drive
        if re.search(r'\b(irq|interrupt|int_out)\s*<=?\s*(\w)', line, re.IGNORECASE):
            m = re.search(r'(irq|interrupt|int_out)\s*(<=?)\s*(\w+)', line, re.IGNORECASE)
            if m and '!' not in line:
                sig = m.group(3)
                mutated_line = line[:m.start(3)] + '~' + sig + line[m.end(3):]
                specs.append(MutantSpec(
                    mut_id=f"IRQ-POL_{i:04d}",
                    operator="IRQ-POL",
                    category="irq",
                    level="ip",
                    source_file="",
                    description=f"IRQ polarity inverted: {sig} → ~{sig}",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="Trigger interrupt source; CPU ISR should receive IRQ=1 not IRQ=0; scoreboard checks polarity",
                    severity="high",
                ))
                break
    return specs


@_op("IRQ-MASK", MutCategory.IRQ, MutLevel.IP,
     "Interrupt mask logic inverted: all interrupts masked instead of enabled",
     "Enable interrupt, trigger source; IRQ should propagate to output",
     severity="high")
def _op_irq_mask(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Invert the mask/enable AND condition on interrupt path."""
    specs = []
    for i, line in enumerate(lines):
        # Pattern: irq = condition & mask_reg or irq = status & ~mask
        m = re.search(r'(\w+)\s*(&)\s*(\w+(?:_mask|_en|_ie)\w*)', line, re.IGNORECASE)
        if m and re.search(r'irq|int|interrupt', line, re.IGNORECASE):
            mask_sig = m.group(3)
            mutated_line = line[:m.start(3)] + '~' + mask_sig + line[m.end(3):]
            specs.append(MutantSpec(
                mut_id=f"IRQ-MASK_{i:04d}",
                operator="IRQ-MASK",
                category="irq",
                level="ip",
                source_file="",
                description=f"IRQ mask inverted: &{mask_sig} → &~{mask_sig} (active when masked, silent when enabled)",
                line_no=i + 1,
                original_text=line.rstrip(),
                mutated_text=mutated_line.rstrip(),
                kill_hint="Set mask register to enable all interrupts; trigger source; IRQ output should be 1 not 0",
                severity="high",
            ))
            break
    return specs


@_op("IRQ-PEND", MutCategory.IRQ, MutLevel.IP,
     "Interrupt pending register not cleared on acknowledge write",
     "Write-to-clear to pending register; IRQ should de-assert after ack",
     severity="high")
def _op_irq_pend(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Remove W1C (write-1-to-clear) clearing logic for interrupt pending register."""
    specs = []
    in_reset = False
    for i, line in enumerate(lines):
        # Detect W1C pattern: `pending_q <= pending_q & ~pwdata;`
        if re.search(r'pending\w*\s*<=\s*\w+\s*&\s*~\w', line, re.IGNORECASE):
            # Replace with: never clear (always latch once set)
            mutated_line = re.sub(
                r'(pending\w*)\s*<=\s*(\w+)\s*&\s*~\w+',
                r'\1 <= \2  // MUT:IRQ-PEND W1C removed',
                line
            )
            specs.append(MutantSpec(
                mut_id=f"IRQ-PEND_{i:04d}",
                operator="IRQ-PEND",
                category="irq",
                level="ip",
                source_file="",
                description="W1C clearing removed from interrupt pending register (IRQ will never de-assert)",
                line_no=i + 1,
                original_text=line.rstrip(),
                mutated_text=mutated_line.rstrip(),
                kill_hint="Trigger IRQ, write-1-to-clear pending register; IRQ should de-assert within 1 cycle",
                severity="high",
            ))
            break
    return specs


# ===========================================================================
# SUBSYSTEM Operators
# ===========================================================================

@_op("SS-CONN", MutCategory.SUBSYSTEM, MutLevel.SUBSYSTEM,
     "Wrong port connected: addr and data bus swapped in instance",
     "Any register access will produce wrong data; scoreboard read-back check",
     severity="critical")
def _op_ss_conn(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Swap .paddr and .pwdata port connections in an instance."""
    specs = []
    paddr_line = None
    pwdata_line = None
    paddr_idx = -1
    pwdata_idx = -1
    for i, line in enumerate(lines):
        if re.search(r'\.paddr\s*\(', line) and paddr_line is None:
            paddr_line = line
            paddr_idx = i
        if re.search(r'\.pwdata\s*\(', line) and pwdata_line is None:
            pwdata_line = line
            pwdata_idx = i
    if paddr_line and pwdata_line:
        # Swap the RHS signals
        m_addr  = re.search(r'\.paddr\s*\((\w+)\)', paddr_line)
        m_wdata = re.search(r'\.pwdata\s*\((\w+)\)', pwdata_line)
        if m_addr and m_wdata:
            new_paddr  = paddr_line.replace(m_addr.group(1), m_wdata.group(1))
            new_pwdata = pwdata_line.replace(m_wdata.group(1), m_addr.group(1))
            specs.append(MutantSpec(
                mut_id=f"SS-CONN_{paddr_idx:04d}",
                operator="SS-CONN",
                category="subsystem",
                level="subsystem",
                source_file="",
                description=f"Port swap: paddr←{m_wdata.group(1)}, pwdata←{m_addr.group(1)} (addr/data crossed)",
                line_no=paddr_idx + 1,
                original_text=f"Line {paddr_idx+1}: {paddr_line.rstrip()}  |  Line {pwdata_idx+1}: {pwdata_line.rstrip()}",
                mutated_text=f"Line {paddr_idx+1}: {new_paddr.rstrip()}  |  Line {pwdata_idx+1}: {new_pwdata.rstrip()}",
                kill_hint="Any read/write test will return wrong data; scoreboard ref-model catches mismatch immediately",
                severity="critical",
            ))
    return specs


@_op("SS-BASE", MutCategory.SUBSYSTEM, MutLevel.SUBSYSTEM,
     "Base address offset wrong by 0x1000 in address decoder",
     "Access IP at expected base address; transaction goes to wrong slave",
     severity="critical")
def _op_ss_base(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Shift a base address compare value by 0x1000."""
    specs = []
    for i, line in enumerate(lines):
        m = re.search(r"(32'h|20'h|16'h)([0-9a-fA-F]{4,8})", line)
        if m and re.search(r'base|offset|addr_sel|select', line, re.IGNORECASE):
            orig_hex = m.group(2)
            try:
                orig_val = int(orig_hex, 16)
                new_val  = orig_val + 0x1000
                new_hex  = format(new_val, f'0{len(orig_hex)}X')
                mutated_line = line[:m.start(2)] + new_hex + line[m.end(2):]
                specs.append(MutantSpec(
                    mut_id=f"SS-BASE_{i:04d}",
                    operator="SS-BASE",
                    category="subsystem",
                    level="subsystem",
                    source_file="",
                    description=f"Base address shifted: 0x{orig_hex} → 0x{new_hex} (+0x1000)",
                    line_no=i + 1,
                    original_text=line.rstrip(),
                    mutated_text=mutated_line.rstrip(),
                    kill_hint="Access IP at spec base address; PSLVERR at correct address but not at +0x1000",
                    severity="critical",
                ))
                break
            except ValueError:
                pass
    return specs


@_op("SS-PARAM", MutCategory.SUBSYSTEM, MutLevel.SUBSYSTEM,
     "Wrong parameter value propagated: data width or address width off by 8",
     "Wide-data test: write 32-bit value; truncated read reveals parameter bug",
     severity="high")
def _op_ss_param(lines: List[str], _seed: int) -> List[MutantSpec]:
    """Halve a DATA_WIDTH or ADDR_WIDTH parameter in an instance override."""
    specs = []
    for i, line in enumerate(lines):
        m = re.search(r'(DATA_WIDTH|ADDR_WIDTH|BUS_WIDTH)\s*\((\d+)\)', line)
        if m:
            orig_w = int(m.group(2))
            new_w  = orig_w // 2 if orig_w > 8 else orig_w - 1
            mutated_line = line[:m.start(2)] + str(new_w) + line[m.end(2):]
            specs.append(MutantSpec(
                mut_id=f"SS-PARAM_{i:04d}",
                operator="SS-PARAM",
                category="subsystem",
                level="subsystem",
                source_file="",
                description=f"{m.group(1)} halved: {orig_w} → {new_w} (bus truncation)",
                line_no=i + 1,
                original_text=line.rstrip(),
                mutated_text=mutated_line.rstrip(),
                kill_hint="Write full-width data, read back; truncated bits should be zero if parameter bug present",
                severity="high",
            ))
            break
    return specs


# ===========================================================================
# Helper Functions
# ===========================================================================

def _flip_hex_lsb(hex_str: str) -> str:
    """Flip the LSB of a hex constant like `8'hFF` → `8'hFE`."""
    m = re.match(r"(\d+'h)([0-9a-fA-F_]+)", hex_str)
    if not m:
        return hex_str
    prefix = m.group(1)
    digits = m.group(2).replace('_', '')
    try:
        val = int(digits, 16)
        new_val = val ^ 1  # flip LSB
        new_digits = format(new_val, f'0{len(digits)}X')
        return prefix + new_digits
    except ValueError:
        return hex_str


def _apply_mutation(lines: List[str], spec: MutantSpec) -> List[str]:
    """Apply a single mutation spec to a list of lines."""
    result = list(lines)
    idx = spec.line_no - 1  # convert 1-based to 0-based
    if 0 <= idx < len(result):
        result[idx] = spec.mutated_text + '\n'
    return result


# For SS-CONN which modifies two lines, we need special handling
def _apply_ss_conn(lines: List[str], spec: MutantSpec) -> List[str]:
    """Apply SS-CONN swap which modifies two separate lines."""
    result = list(lines)
    # The spec original_text encodes both lines; find them and swap
    paddr_line_idx = -1
    pwdata_line_idx = -1
    m_addr = None
    m_wdata = None
    for i, line in enumerate(result):
        if re.search(r'\.paddr\s*\(', line) and paddr_line_idx < 0:
            paddr_line_idx = i
            m_addr = re.search(r'\.paddr\s*\((\w+)\)', line)
        if re.search(r'\.pwdata\s*\(', line) and pwdata_line_idx < 0:
            pwdata_line_idx = i
            m_wdata = re.search(r'\.pwdata\s*\((\w+)\)', line)
    if paddr_line_idx >= 0 and pwdata_line_idx >= 0 and m_addr and m_wdata:
        result[paddr_line_idx] = result[paddr_line_idx].replace(
            m_addr.group(1), m_wdata.group(1), 1
        )
        result[pwdata_line_idx] = result[pwdata_line_idx].replace(
            m_wdata.group(1), m_addr.group(1), 1
        )
    return result


# ---------------------------------------------------------------------------
# Main Engine Class
# ---------------------------------------------------------------------------

class RTLMutator:
    """
    RTL Mutation Testing Engine.

    Generates mutant RTL files by applying one mutation per file,
    following the standard mutation testing convention (each mutant
    differs from the golden by exactly one change).
    """

    def __init__(self, rtl_path: str):
        """
        Args:
            rtl_path: Path to a single .sv/.v file or a directory of RTL files.
        """
        self.rtl_path = Path(rtl_path)
        self._files: List[Path] = self._collect_files()

    def _collect_files(self) -> List[Path]:
        if self.rtl_path.is_dir():
            files = list(self.rtl_path.glob("**/*.sv")) + \
                    list(self.rtl_path.glob("**/*.v"))
            return sorted(files)
        elif self.rtl_path.is_file():
            return [self.rtl_path]
        else:
            return []

    def _read_file(self, path: Path) -> List[str]:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()

    def generate(
        self,
        categories: Optional[List[MutCategory]] = None,
        operators: Optional[List[str]] = None,
        max_per_op: int = 5,
        seed: int = 42,
    ) -> List[MutantFile]:
        """
        Generate mutant files.

        Args:
            categories: Limit to these categories (default: all)
            operators:  Limit to these operator codes (default: all in category)
            max_per_op: Max mutants per operator per source file
            seed:       Random seed for reproducibility

        Returns:
            List of MutantFile objects, each representing one mutated RTL file.
        """
        if categories is None:
            categories = list(MutCategory)

        # Filter operators
        target_ops = {}
        for op_name, op_info in _OPERATORS.items():
            if op_info["category"] in categories:
                if operators is None or op_name in operators:
                    target_ops[op_name] = op_info

        mutant_files: List[MutantFile] = []

        for rtl_file in self._files:
            lines = self._read_file(rtl_file)
            for op_name, op_info in target_ops.items():
                fn = op_info["fn"]
                try:
                    specs = fn(lines, seed)
                except Exception as e:
                    continue  # skip operator if it errors on this file

                for spec in specs[:max_per_op]:
                    spec.source_file = str(rtl_file)
                    # Apply mutation
                    if spec.operator == "SS-CONN":
                        mutated_lines = _apply_ss_conn(lines, spec)
                    else:
                        mutated_lines = _apply_mutation(lines, spec)
                    mutant_files.append(MutantFile(
                        spec=spec,
                        content="".join(mutated_lines),
                    ))

        return mutant_files

    def write_all(
        self,
        mutants: List[MutantFile],
        output_dir: str,
        write_manifest: bool = True,
    ) -> Dict[str, Any]:
        """
        Write all mutant files to disk.

        Directory layout:
            output_dir/
              manifest.json           — full mutation metadata
              <op_name>/
                <stem>_<mut_id>.sv    — one mutated file per mutant

        Returns:
            Manifest dict with all mutation metadata.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        manifest: Dict[str, Any] = {
            "version": "1.0.0",
            "generator": "digital-verify-pro/rtl_mutator.py",
            "total_mutants": len(mutants),
            "mutants": [],
        }

        for mf in mutants:
            op_dir = out / mf.spec.operator
            op_dir.mkdir(exist_ok=True)
            stem = Path(mf.spec.source_file).stem
            filename = f"{stem}_{mf.spec.mut_id}.sv"
            file_path = op_dir / filename
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(mf.content)
            entry = asdict(mf.spec)
            entry["mutant_file"] = str(file_path)
            manifest["mutants"].append(entry)

        if write_manifest:
            manifest_path = out / "manifest.json"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)

        return manifest

    def score(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compute mutation score from a manifest where 'status' fields have been
        updated by the verification pipeline.

        Mutation Score = killed / (total - equivalent)
        """
        mutants = manifest.get("mutants", [])
        total = len(mutants)
        killed = sum(1 for m in mutants if m.get("status") == "killed")
        equivalent = sum(1 for m in mutants if m.get("status") == "equivalent")
        alive = sum(1 for m in mutants if m.get("status") == "alive")
        score_val = killed / (total - equivalent) if (total - equivalent) > 0 else 0.0

        by_category: Dict[str, Dict[str, int]] = {}
        for m in mutants:
            cat = m.get("category", "unknown")
            if cat not in by_category:
                by_category[cat] = {"killed": 0, "alive": 0, "equivalent": 0, "total": 0}
            by_category[cat]["total"] += 1
            by_category[cat][m.get("status", "alive")] = \
                by_category[cat].get(m.get("status", "alive"), 0) + 1

        return {
            "mutation_score": round(score_val * 100, 1),
            "killed": killed,
            "alive": alive,
            "equivalent": equivalent,
            "total": total,
            "by_category": by_category,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _list_operators():
    print(f"\n{'Operator':<12} {'Category':<12} {'Level':<12} {'Sev':<10} Description")
    print("-" * 90)
    for name, info in sorted(_OPERATORS.items()):
        print(f"  {name:<12} {info['category'].value:<12} {info['level'].value:<12} "
              f"{info['severity']:<10} {info['description']}")
    print(f"\nTotal: {len(_OPERATORS)} operators\n")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="RTL Mutation Testing Engine — digital-verify-pro",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
PURPOSE:
  Inject common human coding bugs into RTL source files.
  Each mutant differs from golden by exactly one change.
  Run your AI verification pipeline on each mutant.
  Mutation Score = killed / (total - equivalent) × 100%%

EXAMPLES:
  # Generate all IP-level mutations for a register file
  python engines/rtl_mutator.py --rtl rtl/pl061_gpio_regs.sv --category reg_bank --out mutants/

  # Generate FSM + datapath mutations, max 3 per operator
  python engines/rtl_mutator.py --rtl rtl/ --category fsm,datapath --max 3 --out mutants/

  # Target specific operators
  python engines/rtl_mutator.py --rtl rtl/gpio.sv --op RB-RST,RB-WE,DP-OP --out mutants/

  # Subsystem level (top-level integration file)
  python engines/rtl_mutator.py --rtl rtl/gpio_subsystem.sv --level subsystem --out mutants/

  # List all operators
  python engines/rtl_mutator.py --list-operators
        """
    )
    ap.add_argument("--rtl", help="RTL file or directory (.sv/.v)")
    ap.add_argument("--category", default="all",
                    help="Comma-separated categories: reg_bank,fsm,datapath,interface,irq,subsystem")
    ap.add_argument("--level", choices=["ip", "subsystem", "all"], default="all",
                    help="Mutation level scope (default: all)")
    ap.add_argument("--op", help="Comma-separated operator codes (e.g. RB-RST,DP-OP)")
    ap.add_argument("--max", type=int, default=5,
                    help="Max mutants per operator per file (default: 5)")
    ap.add_argument("--out", default="output/mutants",
                    help="Output directory (default: output/mutants)")
    ap.add_argument("--seed", type=int, default=42, help="Random seed")
    ap.add_argument("--list-operators", action="store_true",
                    help="Print all operators and exit")
    ap.add_argument("--score", metavar="MANIFEST",
                    help="Compute mutation score from existing manifest.json")
    args = ap.parse_args(argv)

    if args.list_operators:
        _list_operators()
        return 0

    if args.score:
        with open(args.score, encoding="utf-8") as f:
            manifest = json.load(f)
        from engines.rtl_mutator import RTLMutator
        m = RTLMutator.__new__(RTLMutator)
        result = m.score(manifest)
        print(f"\n{'='*50}")
        print(f"  Mutation Score: {result['mutation_score']}%")
        print(f"  Killed:         {result['killed']}")
        print(f"  Alive:          {result['alive']}")
        print(f"  Equivalent:     {result['equivalent']}")
        print(f"  Total:          {result['total']}")
        print(f"\n  By Category:")
        for cat, stats in result["by_category"].items():
            score = stats["killed"] / max(1, stats["total"] - stats["equivalent"]) * 100
            print(f"    {cat:<15} {score:5.1f}%  killed={stats['killed']} alive={stats.get('alive',0)}")
        print(f"{'='*50}\n")
        return 0

    if not args.rtl:
        ap.error("--rtl is required (use --list-operators to see operators)")

    # Parse categories
    if args.category == "all":
        categories = list(MutCategory)
    else:
        categories = [MutCategory.from_str(c.strip()) for c in args.category.split(",")]

    # Filter by level
    if args.level != "all":
        level_filter = MutLevel(args.level)
        filtered_ops = {k: v for k, v in _OPERATORS.items()
                        if v["level"] == level_filter}
    else:
        filtered_ops = None

    # Parse operator list
    op_list = None
    if args.op:
        op_list = [o.strip() for o in args.op.split(",")]

    mutator = RTLMutator(args.rtl)
    print(f"[rtl_mutator] Found {len(mutator._files)} RTL file(s)")
    print(f"[rtl_mutator] Categories: {[c.value for c in categories]}")

    mutants = mutator.generate(
        categories=categories,
        operators=op_list,
        max_per_op=args.max,
        seed=args.seed,
    )

    print(f"[rtl_mutator] Generated {len(mutants)} mutants")
    manifest = mutator.write_all(mutants, args.out)
    print(f"[rtl_mutator] Written to: {args.out}/")
    print(f"[rtl_mutator] Manifest:   {args.out}/manifest.json")
    print(f"\nNext steps:")
    print(f"  1. For each mutant in {args.out}/<OP>/<name>.sv:")
    print(f"     Replace the golden RTL and run: python cli.py run --spec <spec.yml>")
    print(f"  2. Check whether the AI pipeline catches the bug (killed vs alive)")
    print(f"  3. After marking status in manifest.json, compute score:")
    print(f"     python engines/rtl_mutator.py --score {args.out}/manifest.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
