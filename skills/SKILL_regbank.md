---
name: rtl-mutation-regbank
version: 1.0.0
lifecycle: stable
agent_created: true
---

# rtl-mutation-regbank — 寄存器堆专项变异测试

## 为什么寄存器堆是最高优先级

寄存器堆（CSR bank）是 RTL 中最常见的 bug 来源：
- **复位值错**：IP 上电后寄存器初始值和文档不符，驱动程序假设 reset = 0 但实际是 1
- **字段掩码错**：写字段 A 同时污染字段 B（最常见的硬件 bug 之一）
- **写使能反**：读操作写入，写操作无效
- **地址错**：偏移 +4，导致软件访问错误的寄存器

这些是最容易引入、最难通过代码审查发现、但通过测试必须能 kill 的 bug。

---

## 5 个算子详解

### RB-RST — 复位值翻转

```systemverilog
// Golden（规格：GPIODR2R 复位后 = 8'hFF）
if (!presetn) begin
  GPIODR2R_q <= 8'hFF;   // ← 正确
end

// Mutant（翻转 LSB）
if (!presetn) begin
  GPIODR2R_q <= 8'hFE;   // ← 差 1，软件可能读到错误初始状态
end
```

**Kill 条件**：UVM reset sequence 里必须有：
```systemverilog
// apb_reset_check_seq.sv
task body();
  apb_read(GPIODR2R_OFFSET, read_val);
  if (read_val !== 8'hFF)
    `uvm_error("RESET_CHECK", $sformatf("GPIODR2R reset mismatch: exp=8'hFF got=%0h", read_val))
endtask
```

---

### RB-MASK — 字段掩码错位

```systemverilog
// Golden：写 pwdata[7:0] 到 GPIODATA[7:0]
GPIODATA_q <= {GPIODATA_q[31:8], pwdata[7:0]};

// Mutant：掩码上移 1 位，写入错误范围
GPIODATA_q <= {GPIODATA_q[31:9], pwdata[8:1]};
//                                    ^^^^ bit0 丢失，bit8 意外写入
```

**Kill 条件**：
```systemverilog
// 写 0x01（bit0=1），读回应该还是 0x01
apb_write(GPIODATA_OFFSET, 8'h01);
apb_read(GPIODATA_OFFSET, read_val);
assert(read_val[0] == 1'b1);   // 如果掩码错，bit0 读回为 0
```

---

### RB-WE — 写使能取反（最严重）

```systemverilog
// Golden：pwrite=1 时写寄存器
if (psel && penable && pwrite) begin
  case (paddr)
    GPIODATA: GPIODATA_q <= pwdata[7:0];
  endcase
end

// Mutant：pwrite 取反 —— 读操作才写，写操作无效！
if (psel && penable && !pwrite) begin  // ← 极性反了
  case (paddr)
    GPIODATA: GPIODATA_q <= pwdata[7:0];
  endcase
end
```

**Kill 条件**：任何写后读测试。这是最容易 kill 的 mutant：
```systemverilog
apb_write(GPIODATA_OFFSET, 8'hA5);
apb_read(GPIODATA_OFFSET, read_val);
assert(read_val === 8'hA5);  // 如果 WE 反了，读回仍是复位值
```

---

### RB-ADDR — 寄存器地址偏移

```systemverilog
// Golden：GPIODIR 在 offset 0x400
12'h400: GPIODIR_q <= {GPIODIR_q[31:8], pwdata[7:0]};

// Mutant：地址 +4，变成 0x404（与下一个寄存器 GPIOIS 冲突）
12'h404: GPIODIR_q <= {GPIODIR_q[31:8], pwdata[7:0]};
```

**Kill 条件**：
- 访问 0x400 写入值，读回应该命中（but goes to X）
- 访问 0x404（GPIOIS 地址），不应该被 GPIODIR 响应
- 地址 decoder 断言：`assert(access_at_0x400 |-> GPIODIR_selected)`

---

### RB-ACC — RO 保护移除

```systemverilog
// Golden：GPIOMIS 是只读寄存器
// 写操作被 !pwrite guard 保护，只有读路径有效

// Mutant：guard 被移除
// 现在写入 GPIOMIS 会改变内部状态，破坏只读语义
if (psel && penable) begin  // ← !pwrite 条件消失了
  GPIOMIS_q <= pwdata;
end
```

**Kill 条件**：
```systemverilog
// 读取原值
apb_read(GPIOMIS_OFFSET, orig_val);
// 尝试写入不同的值
apb_write(GPIOMIS_OFFSET, ~orig_val);
// 再读，应该等于原值（RO 不应被写入改变）
apb_read(GPIOMIS_OFFSET, read_val);
assert(read_val === orig_val);
```

---

## 批量运行寄存器堆变异测试

```bash
# Step 1: 生成所有 reg_bank mutant
python engines/rtl_mutator.py \
  --rtl rtl/pl061_gpio_regs.sv \
  --category reg_bank \
  --max 10 \
  --out output/mutants/regbank/

# Step 2: 查看生成的 mutant 列表
cat output/mutants/regbank/manifest.json | python -c "
import json, sys
m = json.load(sys.stdin)
for mut in m['mutants']:
    print(f\"  {mut['mut_id']:<20} line {mut['line_no']:<5} {mut['description']}\")
"

# Step 3: 对每个 mutant 跑 reset + write/read 测试序列
# (详见 rtl_mutator_SKILL.md Step 2)

# Step 4: 查看 score
python engines/rtl_mutator.py --score output/mutants/regbank/manifest.json
```

---

## 快速 Kill 率参考

基于 digital-verify-pro 流水线能力分析：

| 算子 | 预测 Kill 率 | 原因 |
|------|------------|------|
| RB-WE | ~100% | write→read-back 测试必然发现 |
| RB-RST | ~85% | 流水线有 reset sequence，但可能没有所有寄存器的 read-back |
| RB-ADDR | ~90% | 地址洞断言和访问测试都能 catch |
| RB-MASK | ~70% | 需要 bit-level 精确检查，部分 scoreboard 只检查字节 |
| RB-ACC | ~65% | 需要专门的 RO write 测试，默认 regression 可能没有 |

如果 RB-MASK 或 RB-ACC 是 alive，建议在测试序列里补充对应 directed test。
