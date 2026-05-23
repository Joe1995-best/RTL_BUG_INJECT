---
name: rtl-mutation-subsystem
version: 1.0.0
lifecycle: stable
agent_created: true
---

# rtl-mutation-subsystem — 子系统级 RTL 变异测试 Skill

## 适用场景

IP 级验证通过后，多个 IP 集成成子系统时新引入的集成 bug：
- 端口连线错误（地址线接了数据线）
- 地址映射偏移（基地址差了一页）
- 参数传播错误（32-bit 总线被实例化成 16-bit）
- 时钟域分配错误（IP 接到了错误的时钟）

子系统级 mutant 通常改的是**顶层集成文件**（如 `gpio_subsystem.sv`），而不是 IP 本身。

---

## 算子详情

### SS-CONN — 端口交叉连接（CRITICAL）

**人类代码**（复制粘贴错误）：
```systemverilog
// 原始（正确）
gpio_ip u_gpio (
  .paddr  (s_paddr),    // 地址总线
  .pwdata (s_pwdata),   // 写数据总线
  ...
);

// 变异后（错误）—— addr 和 data 互换
gpio_ip u_gpio (
  .paddr  (s_pwdata),   // ← 数据线接到地址口
  .pwdata (s_paddr),    // ← 地址线接到数据口
  ...
);
```

**预期 Kill 方式：** 任何寄存器读写测试，scoreboard 读回值完全错乱

---

### SS-BASE — 基地址偏移（CRITICAL）

**人类代码**（地址规划出错）：
```systemverilog
// 原始（GPIO 基地址 0x4000_0000）
assign gpio_sel = (paddr[31:12] == 20'h40000);

// 变异后（+0x1000）
assign gpio_sel = (paddr[31:12] == 20'h40001);  // 实际 0x4000_1000
```

**预期 Kill 方式：** 访问 spec 里的基地址 0x4000_0000 会得到 PSLVERR，验证流水线的地址断言应触发

---

### SS-PARAM — 参数宽度减半（HIGH）

**人类代码**（复制实例化后忘记改参数）：
```systemverilog
// 原始（32-bit 数据总线）
gpio_ip #(.DATA_WIDTH(32)) u_gpio (...);

// 变异后（16-bit — 数据截断）
gpio_ip #(.DATA_WIDTH(16)) u_gpio (...);
```

**预期 Kill 方式：** 写入 0xDEAD_BEEF，读回 0x0000_BEEF（高 16 bit 丢失）；scoreboard 检查全部 32 bit

---

### SS-CLK — 时钟域错误（HIGH，需手动编写）

**人类代码**（多时钟域设计复制错误）：
```systemverilog
// 原始
gpio_ip u_gpio (.pclk(apb_clk), ...);

// 变异后（接到了 AXI 时钟，时钟域不匹配）
gpio_ip u_gpio (.pclk(axi_clk), ...);
```

> **注意**：SS-CLK 无法用正则匹配自动生成，需要根据具体 IP 手动写 mutant。

---

## 运行命令

```bash
# 生成子系统级 mutant（需要一个集成顶层文件）
python engines/rtl_mutator.py \
  --rtl rtl/gpio_subsystem.sv \
  --level subsystem \
  --category subsystem \
  --max 5 \
  --out output/mutants/subsystem/

# 也可以用 API
python -c "
from engines.rtl_mutator import RTLMutator, MutCategory
m = RTLMutator('rtl/gpio_subsystem.sv')
mutants = m.generate(categories=[MutCategory.SUBSYSTEM])
manifest = m.write_all(mutants, 'output/mutants/subsystem/')
print(f'Generated: {manifest[\"total_mutants\"]} mutants')
"
```

---

## 子系统级验证流程要求

子系统级 mutant 比 IP 级更难 kill，需要以下条件：

1. **集成测试序列**（不只是 IP 自测）：
   - `subsystem_reg_access_seq`：覆盖所有 slave 基地址
   - `cross_ip_transfer_seq`：IP-A 写数据，IP-B 读结果

2. **地址 decoder 断言**：
   ```systemverilog
   // 每个 slave 都应有 select 断言
   assert property (@(posedge clk) gpio_sel |-> (paddr inside {[GPIO_BASE:GPIO_BASE+GPIO_SIZE-1]}));
   ```

3. **宽数据 Scoreboard**：比对完整 32-bit 读回值，不只低字节

4. **时钟域检查**（CDC lint）：
   ```bash
   # 用 spyglass CDC 或 jaspergold 检查
   python engines/cdc_checker.py --rtl rtl/gpio_subsystem.sv
   ```

---

## Mutation Score 目标（子系统级）

| 阶段 | 建议分数 | 说明 |
|------|---------|------|
| 初次集成 | ≥ 50% | 至少 CONN/BASE 类能 kill |
| 集成验证成熟 | ≥ 75% | PARAM/CLK 类也能检测 |
| 芯片 tape-out 前 | ≥ 90% | 所有 CRITICAL 类必须 killed |
