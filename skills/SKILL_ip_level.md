---
name: rtl-mutation-ip
version: 1.0.0
lifecycle: stable
agent_created: true
---

# rtl-mutation-ip — IP-Level RTL Mutation Testing Skill

## 核心概念（先读这里）

**RTL Mutation Testing ≠ Fault Injection（故障注入）**

| 维度 | Fault Injection (fault_injector.py) | RTL Mutation Testing (本 skill) |
|------|--------------------------------------|----------------------------------|
| 目标 | 测试 DUT 自身的容错 / 恢复能力 | 测试 AI 验证流程的检测能力 |
| 操作对象 | 运行时信号（force/release） | RTL 源代码文本 |
| bug 来源 | 外部环境注入（SEU、电源噪声等） | 人类编码常见错误 |
| 产物 | 仿真波形，DUT 是否 survive | 流水线是否 kill mutant |
| 评分指标 | 恢复率 / MTTF | Mutation Score = killed / (total - equivalent) |

**工作流程：**
```
原始 RTL (golden) → 注入 1 个 bug → 得到 mutant RTL
                                    ↓
              把 mutant 当作 golden 跑 AI 验证流水线
                                    ↓
              流水线发现 bug → mutant killed ✓
              流水线没发现 → mutant alive ✗ → 验证流程漏洞
```

---

## 支持的 IP 级变异算子（22个）

### reg_bank — 寄存器堆

| 算子 | 严重度 | 描述 | Kill 预期 |
|------|--------|------|-----------|
| **RB-RST** | HIGH | 复位值低位翻转（8'hFF→8'hFE） | UVM reset sequence read-back 检查 |
| **RB-MASK** | HIGH | 字段 bit 范围上移 1 位（[7:0]→[8:1]） | 写字段后检查相邻字段无污染 |
| **RB-WE** | CRITICAL | 写使能取反（pwrite→!pwrite，写变读操作） | APB write→read-back 必须相等 |
| **RB-ADDR** | CRITICAL | 寄存器 offset +4（地址映射错） | 访问正确地址应该命中寄存器 |
| **RB-ACC** | HIGH | RO 保护条件移除（寄存器变可写） | 写 RO 寄存器后值不得改变 |

### fsm — 有限状态机

| 算子 | 严重度 | 描述 | Kill 预期 |
|------|--------|------|-----------|
| **FSM-ARC** | HIGH | 状态转移目标改为下一个状态 | FSM directed test + state coverpoint |
| **FSM-DEF** | MEDIUM | default case 注释掉（X 传播风险） | 非法状态断言检查 |
| **FSM-RST** | CRITICAL | 复位状态改为非 IDLE | 复位后查询状态寄存器必须是 IDLE |

### datapath — 数据通路

| 算子 | 严重度 | 描述 | Kill 预期 |
|------|--------|------|-----------|
| **DP-OP** | HIGH | 算术/逻辑运算符替换（+→-，&→\|，>>→<<） | 已知操作数的算术测试；scoreboard 比对结果 |
| **DP-MUX** | HIGH | 三目运算符两分支对调 | 两分支场景各测一次；输出不同 |
| **DP-CONST** | MEDIUM | 整数常量 -1（off-by-one 错误） | 边界值测试；计数器/比较器在 N 而非 N-1 转换 |

### interface — 总线接口

| 算子 | 严重度 | 描述 | Kill 预期 |
|------|--------|------|-----------|
| **IF-SEQ** | CRITICAL | APB PSEL/PENABLE 同周期拉高（跳过 setup） | SVA: `$rose(psel) \|=> $rose(penable)` |
| **IF-PROT** | HIGH | PSLVERR 在非法地址访问时不拉高 | 访问地址洞；PSLVERR 断言检查 |
| **IF-IDLE** | MEDIUM | 事务后 PSEL 不拉低（总线一直 busy） | Monitor 两事务间 PSEL 必须 =0 |

### irq — 中断控制器

| 算子 | 严重度 | 描述 | Kill 预期 |
|------|--------|------|-----------|
| **IRQ-POL** | HIGH | 中断极性取反（有效时输出 0 而非 1） | 触发中断源；检查 irq 输出电平 |
| **IRQ-MASK** | HIGH | mask 逻辑取反（使能=屏蔽，屏蔽=使能） | 设 mask 使能所有中断后触发中断 |
| **IRQ-PEND** | HIGH | W1C 清除逻辑移除（中断永远挂起） | 触发→ack 写；IRQ 应在 1 cycle 内下降 |

---

## 快速开始

### Step 1: 生成 mutant RTL 文件

```bash
# 对单个寄存器文件注入所有 reg_bank 级别 bug
python engines/rtl_mutator.py \
  --rtl rtl/pl061_gpio_regs.sv \
  --category reg_bank \
  --max 5 \
  --out output/mutants/

# 对整个 rtl/ 目录注入 FSM + 数据通路 bug
python engines/rtl_mutator.py \
  --rtl rtl/ \
  --category fsm,datapath \
  --max 3 \
  --out output/mutants/

# 只用特定算子
python engines/rtl_mutator.py \
  --rtl rtl/pl061_gpio_regs.sv \
  --op RB-RST,RB-WE,DP-OP \
  --out output/mutants/

# 列出所有算子
python engines/rtl_mutator.py --list-operators
```

### Step 2: 对每个 mutant 运行验证流水线

```bash
# 保存原始 golden RTL
cp rtl/pl061_gpio_regs.sv rtl/pl061_gpio_regs.sv.golden

# 对每个 mutant 文件：
for mutant_file in output/mutants/RB-RST/*.sv; do
  # 替换 DUT
  cp "$mutant_file" rtl/pl061_gpio_regs.sv

  # 运行 AI 验证流水线（只跑关键阶段）
  python cli.py run --spec specs/pl061_gpio_spec.yml \
    --phases test-generator,assertion-gen,coverage-plan \
    --log "output/mutants/logs/$(basename $mutant_file .sv).log"

  # 检查流水线是否报错
  if grep -q "FAIL\|ERROR\|assertion_failed" "output/mutants/logs/..."; then
    echo "KILLED: $mutant_file" >> output/mutants/results.txt
  else
    echo "ALIVE: $mutant_file" >> output/mutants/results.txt
  fi
done

# 恢复 golden
cp rtl/pl061_gpio_regs.sv.golden rtl/pl061_gpio_regs.sv
```

### Step 3: 计算 Mutation Score

在 `output/mutants/manifest.json` 里更新每个 mutant 的 `status` 字段：
- `"killed"` — 验证流水线发现了这个 bug
- `"alive"` — 流水线没有发现（= 验证漏洞）
- `"equivalent"` — 行为上与 golden 等价（不计入分母）

```bash
# 计算评分
python engines/rtl_mutator.py --score output/mutants/manifest.json
```

输出示例：
```
==================================================
  Mutation Score: 78.3%
  Killed:         18
  Alive:           5
  Equivalent:      0
  Total:          23

  By Category:
    reg_bank        85.7%  killed=6  alive=1
    fsm             66.7%  killed=4  alive=2
    datapath        80.0%  killed=4  alive=1
    interface      100.0%  killed=3  alive=0
    irq             83.3%  killed=5  alive=1
==================================================
```

---

## 解读 Alive Mutants（验证漏洞定位）

如果一个 mutant 是 alive，说明验证流程存在漏洞：

| 算子 alive | 根本原因 | 修复方向 |
|-----------|---------|---------|
| RB-RST alive | 没有 reset sequence 或 RAL model read-back | 在 UVM test 里加 reset-and-check sequence |
| RB-WE alive | 没有 write→read-back 测试 | 在 regression 里加 APB write/read pair |
| FSM-ARC alive | 状态覆盖不足（缺对应 arc 的 directed test） | 补充 FSM 定向测试序列 |
| DP-OP alive | 没有算术结果对比（scoreboard 缺 ref model） | 给 scoreboard 加 reference model |
| IF-SEQ alive | APB 协议断言缺失或未使能 | 检查 `psel_penable_order` 断言是否生成 |
| IRQ-POL alive | 中断测试没有检查 irq 极性 | 在 interrupt test 里加 polarity check |

---

## manifest.json 格式说明

```json
{
  "version": "1.0.0",
  "total_mutants": 23,
  "mutants": [
    {
      "mut_id": "RB-RST_0042",
      "operator": "RB-RST",
      "category": "reg_bank",
      "level": "ip",
      "source_file": "rtl/pl061_gpio_regs.sv",
      "description": "Reset value changed: 8'hFF → 8'hFE",
      "line_no": 42,
      "original_text": "      GPIODR2R_q <= 8'hFF;",
      "mutated_text":  "      GPIODR2R_q <= 8'hFE;",
      "kill_hint": "UVM reset_seq: read-back register after reset should equal spec reset value",
      "severity": "high",
      "status": "alive"    ← 更新这个字段
    }
  ]
}
```

---

## 与 pipeline 的集成建议

在 `track2.py` 中添加 `mutation-test` 阶段（在 `doc-gen` 之后）：

```python
{
  "name": "mutation-test",
  "script": "engines/rtl_mutator.py",
  "args": ["--rtl", "{rtl_dir}", "--category", "all", "--max", "3", "--out", "{out_dir}/mutants"],
  "description": "RTL mutation testing: inject bugs, measure verification pipeline coverage"
}
```

**推荐在 CI 中设置 Mutation Score 门槛：**
- 新 IP 首次验证：≥ 60%（基准）
- 成熟 IP 迭代：≥ 80%（进阶）
- Safety-critical IP：≥ 95%（严苛）

---

## 注意事项

1. **等价变异（Equivalent Mutant）**：某些变异在行为上与 golden 等价（如对常量 0 翻转 LSB 还是 0），这类不计入分母，手动标 `"equivalent"`
2. **每文件一个变异**：每个 mutant 文件只改了一处，方便精确定位漏洞
3. **顺序执行**：每次只用一个 mutant 替换 DUT，跑完恢复 golden，再换下一个
4. **不要 commit mutant**：mutant 文件只用于本地测试，不应进入代码仓库
