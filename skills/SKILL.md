---
name: rtl-mutation-index
version: 1.0.0
lifecycle: stable
agent_created: true
---

# RTL Mutation Testing — Skill 索引

本目录包含 digital-verify-pro 的 RTL 变异测试（Mutation Testing）Skill 体系。

## 核心概念

> **RTL Mutation Testing = 往正确的 RTL 里注入常见人类 bug，测试 AI 验证流程的检测能力。**
>
> 不是测 DUT 的容错，而是测验证流程的"测试充分性"。

**评分：Mutation Score** = (Killed Mutants) / (Total - Equivalent) × 100%

---

## 文件导航

### 引擎（核心实现）

| 文件 | 说明 |
|------|------|
| `../../engines/rtl_mutator.py` | 核心 Python 引擎，22 个算子 |
| `../../engines/rtl_mutator_SKILL.md` | IP 级完整使用手册（入口文档） |

### 分类专项 Skill（按模块类型）

| 文件 | 分类 | 算子数 | 典型 bug |
|------|------|--------|---------|
| `SKILL_regbank.md` | 寄存器堆 | 5 (RB-*) | 复位值错、掩码偏移、写使能反 |
| `SKILL_fsm.md` | 有限状态机 | 3 (FSM-*) | 状态转移错、default 缺失、复位状态错 |
| `SKILL_subsystem.md` | 子系统集成 | 4 (SS-*) | 端口交叉、基地址偏移、参数错、时钟域错 |

> **数据通路（DP-\*）和接口（IF-\*）** 的详细说明在 `../../engines/rtl_mutator_SKILL.md` 的算子表格中。

---

## 快速决策树

```
要测什么？
│
├── 单个 IP 的 RTL 正确性
│   ├── 寄存器文件 → SKILL_regbank.md + --category reg_bank
│   ├── FSM 逻辑   → SKILL_fsm.md + --category fsm
│   ├── 数据通路   → rtl_mutator_SKILL.md + --category datapath
│   ├── APB 接口   → rtl_mutator_SKILL.md + --category interface
│   └── 中断控制   → rtl_mutator_SKILL.md + --category irq
│
└── 多 IP 集成的连接正确性
    └── SKILL_subsystem.md + --level subsystem --category subsystem
```

---

## 一键全量测试

```bash
# 生成所有 IP 级 mutant
python engines/rtl_mutator.py \
  --rtl rtl/ \
  --level ip \
  --category reg_bank,fsm,datapath,interface,irq \
  --max 5 \
  --out output/mutants/ip-level/ \
  --seed 42

# 生成子系统级 mutant
python engines/rtl_mutator.py \
  --rtl rtl/gpio_subsystem.sv \
  --level subsystem \
  --category subsystem \
  --max 5 \
  --out output/mutants/subsystem-level/

# 列出所有算子
python engines/rtl_mutator.py --list-operators
```

---

## Mutation Score 解读

| 分数 | 含义 | 行动建议 |
|------|------|---------|
| < 50% | 验证流程严重不足 | 检查是否有 write→read-back / assertion |
| 50–70% | 基本功能覆盖，细节不足 | 补充 directed test 和 boundary value |
| 70–85% | 良好，有少量盲区 | 分析 alive mutant，定向补充 |
| 85–95% | 成熟验证流程 | 聚焦 CRITICAL 级 alive mutant |
| > 95% | 优秀（safety-critical 标准） | 检查剩余 alive 是否是 equivalent mutant |

---

## 与其他工具的对比

| 工具 | 适用场景 |
|------|---------|
| `fault_injector.py` | 运行时故障注入 → 测 DUT 容错 |
| `rtl_mutator.py`（本工具）| 源码级 bug 注入 → 测验证流程质量 |
| `coverage_engine.py` | 代码覆盖率统计 → 测哪些代码被跑到 |
| `spec_rtl_tracker.py` | spec 与 RTL 的 traceability → 测有没有漏实现 |

四个工具互补，共同构成完整的"验证质量度量"体系。
