---
name: rtl-mutation-fsm
version: 1.0.0
lifecycle: stable
agent_created: true
---

# rtl-mutation-fsm — FSM 专项变异测试

## FSM Bug 的行业统计

FSM bug 在 RTL 设计中占比约 15-25%（来源：DVCon 2021 survey）：
- **状态转移缺失**：某个输入条件下没有触发正确的 next_state
- **默认态缺失**：illegal state 没有 default 处理，导致 X 锁死
- **复位状态错**：上电后进入错误状态，初始化序列失效

---

## 3 个算子详解

### FSM-ARC — 转移目标错误

```systemverilog
// I2C Start Detection FSM（简化示意）
// Golden：SDA 下降沿在 SCL 高时 → START 状态
case (current_state)
  IDLE: begin
    if (start_detect) next_state = START;  // ← 正确
    else              next_state = IDLE;
  end
  START: begin
    if (data_phase)   next_state = DATA;   // ← 正确
    else              next_state = START;
  end
  ...
endcase

// Mutant：转移目标改为下一个状态（FSM-ARC 轮换）
case (current_state)
  IDLE: begin
    if (start_detect) next_state = START;  // 未改（IDLE→START 仍然正确）
    else              next_state = IDLE;
  end
  START: begin
    if (data_phase)   next_state = STOP;   // ← 错了！DATA → STOP（跳过数据阶段）
    else              next_state = START;
  end
endcase
```

**Kill 条件**：
- FSM directed test：发送完整 I2C 帧，监控每个状态的停留
- State coverpoint：`DATA` 状态应该被覆盖到，覆盖率报告会显示 0%
- Sequence checker：`START → DATA → STOP` 顺序断言

---

### FSM-DEF — 默认态缺失

```systemverilog
// Golden：有 default case 处理所有非法状态
case (current_state)
  IDLE:  ...
  START: ...
  DATA:  ...
  STOP:  ...
  default: next_state = IDLE;  // ← 非法状态 → 回到 IDLE

// Mutant：default 被注释
case (current_state)
  IDLE:  ...
  START: ...
  DATA:  ...
  STOP:  ...
  //DEFAULT_REMOVED// default: next_state = IDLE;
```

**后果**：如果因为功耗门控、EMC 等原因 FSM 跳到 X state，会锁死。

**Kill 条件**：
```systemverilog
// SVA 断言：FSM 必须在已知状态集合内
assert property (@(posedge clk) disable iff (!rst_n)
  current_state inside {IDLE, START, DATA, STOP});
```

如果流水线生成了 FSM state assertion（`spec_rtl_tracker.py` 里有），就能 kill 此 mutant。

---

### FSM-RST — 复位状态错误

```systemverilog
// Golden：上电 / 复位后进入 IDLE
always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) current_state <= IDLE;   // ← 正确
  else        current_state <= next_state;
end

// Mutant：复位后进入 START（非初始状态）
always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) current_state <= START;  // ← 错误！
  else        current_state <= next_state;
end
```

**后果**：复位后 FSM 直接进入 START 状态，跳过 IDLE 的初始化逻辑（如 interrupt clear、counter reset）。

**Kill 条件**：
```systemverilog
// UVM test: assert reset, deassert, immediately read FSM state
apb_write(FSM_STATE_DEBUG_OFFSET, 0);  // trigger read
apb_read(FSM_STATE_DEBUG_OFFSET, state_val);
assert(state_val === IDLE_ENCODING);

// 或者 SVA
assert property (@(posedge clk) $fell(rst_n) |=> current_state == IDLE);
```

---

## FSM 变异测试要求

为了能 kill FSM mutant，AI 验证流程需要：

1. **State Coverpoints**（覆盖计划里）：
   ```systemverilog
   covergroup fsm_cg @(posedge clk);
     FSM_STATE: coverpoint current_state {
       bins IDLE  = {IDLE};
       bins START = {START};
       bins DATA  = {DATA};
       bins STOP  = {STOP};
     }
     // 转移对（cross coverage）
     FSM_TRANS: coverpoint {current_state, next_state} {
       bins IDLE_to_START  = {{IDLE,  START}};
       bins START_to_DATA  = {{START, DATA}};
       bins DATA_to_STOP   = {{DATA,  STOP}};
       bins STOP_to_IDLE   = {{STOP,  IDLE}};
     }
   endgroup
   ```

2. **FSM 状态断言**：
   ```systemverilog
   // illegal state check
   property fsm_valid_state;
     @(posedge clk) disable iff (!rst_n)
       current_state inside {IDLE, START, DATA, STOP};
   endproperty
   assert property (fsm_valid_state) else `uvm_error("FSM", "Illegal state!")
   ```

3. **复位后检查序列**：
   ```systemverilog
   // After deasserting reset, first check is state == IDLE
   task post_reset_check();
     @(negedge rst_n);       // reset asserted
     @(posedge clk);         // wait one cycle after reset deassert
     assert(tb_top.dut.current_state === IDLE);
   endtask
   ```

---

## 快速运行

```bash
# 生成所有 FSM 变异
python engines/rtl_mutator.py \
  --rtl rtl/ \
  --category fsm \
  --max 5 \
  --out output/mutants/fsm/

# 查看哪些文件有 FSM（含 next_state/current_state）
grep -rn "next_state\|current_state" rtl/ --include="*.sv"
```

## 已知局限

- `FSM-ARC` 依赖正则识别 `[A-Z_]+STATE[A-Z_]*` 格式的状态名。
  如果项目使用 `typedef enum` 或小写状态名（`idle`, `start`），算子可能不生效。
  **解决方法**：手动用 `--op FSM-ARC` 运行后查看 manifest，若 total=0 说明没有匹配的状态名格式。
- FSM-DEF 只能注释掉 `default:` 开头的行，不处理 `default : begin` 块。
