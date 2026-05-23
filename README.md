# 🦗 RTL_BUG_INJECT

> **给 AI 验证流程做的"体检工具" —— 往 RTL 里出考题，看 AI 能不能及格。**

IC Agent Hub 说它能自动生成 UVM 环境、自动写断言、自动跑回归、自动收敛覆盖率。

但你真的信吗？

**万一 AI 生成的验证流程漏掉了关键 bug 呢？**

RTL_BUG_INJECT 就是来回答这个问题的——往正确的 RTL 里**注入已知的人类常见 bug**，然后看你的验证流程能不能抓住。抓不住的，就是**验证盲区**。

---

## 🎯 一句话说清楚

**这不是测芯片能不能扛 bug，是测你的验证流程能不能抓 bug。**

| 传统故障注入 (Fault Injection) | RTL 变异测试 (Mutation Testing) |
|:---:|:---:|
| 测 DUT 的容错能力 | 测试验证流程的检测能力 |
| 往运行时注入单粒子翻转等 | 往源码里改人写的 bug |
| "芯片够不够硬" | "验证够不够严" |

**你在这里要测的，是后者。**

---

## 🧪 怎么用（核心流程）

```
  正确的 RTL          注入人类 Bug         AI 验证流程跑一遍         算分
 ┌──────────┐      ┌──────────────┐      ┌──────────────┐      ┌──────────┐
 │          │ ──▶  │  20种常见Bug  │ ──▶  │  Killed/     │ ──▶  │ Mutation │
 │  Golden  │      │  随机注入N个  │      │  Survived?   │      │  Score   │
 │  RTL     │      │              │      │              │      │          │
 └──────────┘      └──────────────┘      └──────────────┘      └──────────┘
```

1. **Golden 通过** — 用 AI 验证流程跑一遍正确 RTL，确保 baseline pass
2. **注入 Bug** — 从 20 种人类常见 bug 中随机挑 N 个注入
3. **再跑一遍** — 用**同一个**验证流程跑变异后的 RTL
4. **出成绩单** — Killed（抓到了）/ Survived（漏了！）

$$\text{Mutation Score} = \frac{\sum \text{killed\_points}}{\sum \text{total\_points}} \times 100\%$$

> 不是简单的 killed/total，每种 bug 有不同的分值（难度权重）。因为漏掉一个"复位值写错"和漏掉一个"跨信号连线"的严重程度显然不一样。

---

## 📊 成绩单：由夯到拉

| 等级 | 标签 | 分数区间 | 含义 |
|:---:|:---:|:---:|:---|
| 🏆 S | **夯** | 90-100% | 验证流程基本无盲区，可以放心交给 AI |
| 🥇 A | **强** | 75-90% | 绝大部分 bug 都能抓到，少数边界情况需补强 |
| 🥈 B | **中** | 60-75% | 及格线以上，但有明显盲区需要补测试 |
| 🥉 C | **弱** | 40-60% | 大量 bug 漏网，建议重新审视验证策略 |
| 💀 D | **拉** | 0-40% | 基本形同虚设，AI 验证流程是摆设 |

**Code is cheap, show me your Mutation Score.**

---

## 🐛 20 种人类常见 Bug

### IP 级（16 种）

| 分类 | 算子 | 难度分 | 描述 |
|:---:|:---:|:---:|:---|
| **寄存器堆** | `RB-RST` | 75 | 复位值：翻转 LSB |
| | `RB-MASK` | 70 | 字段掩码：偏移 1 bit |
| | `RB-WE` | **100** | 写使能：取反 |
| | `RB-ADDR` | 90 | 寄存器地址：偏移 4 字节 |
| | `RB-ACC` | 65 | 访问保护：移除 RO 限制 |
| **状态机** | `FSM-ARC` | 88 | 状态跳转：交换目标 |
| | `FSM-DEF` | 55 | 默认分支：删除 |
| | `FSM-RST` | **95** | 复位状态：用错状态 |
| **数据通路** | `DP-OP` | 60 | 运算符：`+`变`-`，`&`变`|`，`>>`变`<<` |
| | `DP-MUX` | 72 | 多路选择：交换 true/false 分支 |
| | `DP-CONST` | 42 | 常量：±1 |
| **总线接口** | `IF-SEQ` | **92** | APB：跳过 setup 阶段 |
| | `IF-PROT` | 78 | APB：抑制 PSLVERR |
| | `IF-IDLE` | 48 | APB：永远不拉低 PSEL |
| **中断** | `IRQ-POL` | 62 | 中断极性：取反 |
| | `IRQ-MASK` | 58 | 使能逻辑：取反 |
| | `IRQ-PEND` | 64 | W1C 清除：删除 |

### 子系统级（4 种）

| 算子 | 难度分 | 描述 |
|:---:|:---:|:---|
| `SS-CONN` | **98** | 端口连线：两个信号交叉连接 |
| `SS-BASE` | 85 | 地址基址：偏移 `0x1000` |
| `SS-PARAM` | 70 | 参数：位宽/数量减半 |
| `SS-CLK` | 80 | 时钟域：手动注入跨域信号 |

> 💡 **为什么难度分不同？** 因为有些 bug 天然容易被验证环境抓到（比如改个常量），有些则很难（比如写使能取反、跨信号连线）。**难度分越高 = 越难被发现 = 漏掉越危险。**

---

## 🎮 两种模式

### Auto 模式（自动化评测）

全自动：注入 bug → iverilog 编译仿真 → 自动判定 killed/alive → 出分。

```bash
# 对 I2C slave 跑 auto 评测
python engines/batch_challenger.py auto \
    --rtl examples/i2c/i2c_slave_model.sv \
    --tb examples/i2c/tb_i2c_challenge.sv \
    --challenge-name auto_i2c \
    --num-bugs 5
```

适合：快速评估、CI/CD 集成、回归测试。

### Challenge 模式（人工 Debug 挑战）

注入多个 bug → 给你原始 RTL 和变异 RTL → 你用任意方式 debug → 提交你找到的 bug → 算分。

```bash
# 创建挑战
python engines/batch_challenger.py create \
    --rtl examples/i2c/i2c_slave_model.sv \
    --num-bugs 5 \
    --name my_challenge

# 查看挑战（给你原始代码和变异代码对比）
python engines/batch_challenger.py view --name my_challenge

# 提交你找到的 bug
python engines/batch_challenger.py submit \
    --name my_challenge \
    --found "FSM-ARC" "RB-WE" "DP-OP"

# 出分
python engines/batch_challenger.py score --name my_challenge

# 查看排行榜
python engines/batch_challenger.py leaderboard
```

适合：团队竞技、技能考核、极限 debug 挑战。

---

## 🏅 排行榜

```bash
python engines/batch_challenger.py leaderboard
```

生成一个暗色主题的 HTML 排行榜，包含：
- 每个人的总分、等级（S/A/B/C/D）
- Auto / Challenge 模式标注
- 按 operator / category 的得分拆解
- **漏掉的 bug 分析** — 你没抓到的都是哪些类型？

> 📸 *排行榜长这样（示意）：*
>
> | 排名 | 名字 | 模式 | 得分 | 等级 | 抓到 | 漏掉 |
> |:---:|:---:|:---:|:---:|:---:|:---:|:---:|
> | 1 | 老王 | CHALLENGE | 92.3% | 🏆 S | 23/25 | RB-WE, SS-CONN |
> | 2 | Auto-I2C | AUTO | 100% | 🏆 S | 5/5 | — |
> | 3 | 小李 | CHALLENGE | 68.5% | 🥈 B | 17/25 | FSM-RST, IF-SEQ, ... |

---

## 📁 项目结构

```
RTL_BUG_INJECT/
├── engines/
│   ├── rtl_mutator.py          # 🔧 核心：变异引擎（20种算子 + CLI + API）
│   ├── batch_challenger.py     # 🎮 双模式：Auto 自动评测 + Challenge 挑战
│   └── mutation_eval.py        # 📊 评测引擎（HTML 报告 + 分级）
├── examples/
│   ├── i2c/
│   │   ├── i2c_slave_model.sv  # 🧪 I2C slave RTL（测试靶机）
│   │   └── tb_i2c_challenge.sv # 🧪 I2C testbench（auto 模式用）
│   └── ot_dma/
│       └── dma.sv              # 🧪 OpenTitan DMA（复杂测试靶机）
├── skills/
│   ├── SKILL.md                # 📖 快速决策树 & 索引
│   ├── SKILL_ip_level.md       # 📖 IP 级完整指南
│   ├── SKILL_fsm.md            # 📖 FSM 变异详解
│   └── SKILL_subsystem.md      # 📖 子系统级变异指南
└── challenges/                 # 📂 挑战存档（answer.json / task.json）
```

---

## 🚀 Quick Start

```bash
# 1. 克隆
git clone https://github.com/Joe1995-best/RTL_BUG_INJECT.git
cd RTL_BUG_INJECT

# 2. 生成变异体
python engines/rtl_mutator.py --rtl examples/i2c/ --category all --max 5 --out output/mutants/

# 3. 或者直接跑 auto 评测（需要 iverilog）
python engines/batch_challenger.py auto \
    --rtl examples/i2c/i2c_slave_model.sv \
    --tb examples/i2c/tb_i2c_challenge.sv \
    --challenge-name auto_i2c \
    --num-bugs 5

# 4. 查看排行榜
python engines/batch_challenger.py leaderboard
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

for m in mutants:
    print(f"{m.spec.mut_id}: {m.spec.description} (难度 {m.spec.points}分)")

manifest = mutator.write_all(mutants, 'output/mutants/')
```

---

## 💬 给不同的人说不同的话

### 📨 转发给你的 DE

> "嘿，你不是总说验证写的 testbench 太弱吗？来，这里有个真正的挑战——5 个 bug 藏在 I2C slave 里，你能用 debug 全找出来吗？计时开始 ⏱️"
>
> **他终于有大展身手的机会了。**

### 📨 发给你的验证主管

> "老大，我们的验证流程 Mutation Score 是 92%，S 级。这是量化数据，不是拍脑袋。附 HTML 报告。"
>
> **数据说话，升职加薪。**

### 📨 发给 IC Agent Hub 平台开发者

> "你们的 AI 生成的 UVM 环境，跑我的 Mutation Score 只有 45%。SS-CONN（跨信号连线）全漏了。建议加强 connectivity check 的断言。"
>
> **精准定位盲区，推动改进。**

### 📨 发给你自己

> **"Code is cheap, show me your Mutation Score."**

---

## 🔮 Why This Matters

软件领域有成熟的 mutation testing 工具（PIT、MutPy），但硬件/RTL 领域几乎空白。

| 工具 | 问题 |
|:---:|:---|
| MCY | 面向网表层，不是源码级；不集成 UVM；不区分 bug 语义类型 |
| PIT/MutPy | 软件工具，不支持 SystemVerilog |
| 手动造 bug | 没有系统化 taxonomy，无法量化，不可复现 |

**RTL_BUG_INJECT 填补的是：RTL 源码级 + 语义 bug 分类 + 难度权重 + AI 验证流程评估。**

---

## 📋 Requirements

- Python 3.8+
- 无外部依赖（仅用 `re`, `json`, `pathlib`, `argparse`, `random`）
- Auto 模式额外需要：`iverilog` + `vvp`（用于编译仿真）

## 📜 License

MIT

---

<p align="center">
  <strong>你敢测你的验证流程吗？</strong><br/>
  <code>python engines/batch_challenger.py auto --rtl your_design.sv --tb your_tb.sv --challenge-name dare_to_test</code>
</p>
