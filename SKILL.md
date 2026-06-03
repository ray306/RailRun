---
name: RailRun
Author: Jinbiao Yang
Version: 2026.06.03
argument-hint: "[这里填写流程别名，例如：[K2K]、[simple_todo_flow]（别名在 config.json 的 rails_alias_and_path 字段中配置）]"
arguments: [procedure_name]
description: Agent 经常自作主张调整复杂的流程。本 Skill 可通过向 Agent 渐进式披露每个步骤，让 Agent 只能逐步执行当前看到的步骤，以保证流程确定性。

---

# RailRun

在给定流程后，Agent 通常会根据上下文自行思考、规划。这种方式灵活，但在确定性要求极高的场景中，容易出现流程漂移、误判、或其它不符合预期的执行。

本 Skill 采用相反的控制模型：**不再将完整上下文的流程直接交给大语言模型 Agent，而是向 Agent 渐进式披露需要执行的步骤，将 Agent 作为“单步执行解释器”使用。**

具体来说：
> 用 `next_step` 替代 Agent 的流程规划能力。
> 流程的步骤被 `next_step` 工具控制，对 Agent 渐进式披露（每次一步，看不到未来步骤）
> 用状态机控制替代提示词约束。

因此，Agent 只能执行当前步骤，而不知道、不能决定、也不应预期流程在未来什么时候走到哪里。流程的确定性不是建立在 Agent “自觉不乱来”之上，而是建立在渐进式披露流程的机制之上。

## 调用方式

- 默认参数格式：`/railrun [procedure_name]`
- 带参数格式：`/railrun [procedure_name](param1=value1, param2=value2, ...)`
- `procedure_name` 是 rail 流程的别名或路径。将收到的 `procedure_name` 原样传给 `next_step.py --procedure`。

---

> 为避免中文输出乱码，中文编码环境中python需要加上 -X utf8 参数。

## Agent 的工作过程：
说明：以下命令默认在 `next_step.py` 所在目录执行（CLI 入口文件）。

1. 初始化：
   ```
   python -X utf8 next_step.py --procedure "<procedure_name>"
   ```

2. 获取下一步：
   ```
   python -X utf8 next_step.py --session <session_id> [--branch-value true|false]
   ```

3. 执行返回的 `instruction`。

4. 重复步骤 2-3，直到 `Finished` 或 `HumanInterferenceRequest`。

5. 停止 session：
   ```
   python -X utf8 next_step.py --session <session_id> --shutdown
   ```

---

# 输出内容

- **隐藏内部控制信息**：`next_step` 的调用与返回内容属于运行时内部控制信息，初始化/停止 session 这些也属于运行时内部控制信息，不需要被用户看到，可以隐藏在思维链中。
- **展示`instruction`的正式执行过程**：得到`next_step` 返回的 `instruction` 之后，Agent 需要结束思维链，以正式回答的方式向用户展示自己的分析执行过程。

## 硬约束（必须遵守！）
- 禁止读取用`--procedure`指定文件、或者扩展名是 `.rail` 的文件的内容！这样的文件只能在 `next_step` 的返回值里逐步披露！禁止任何形式的提前泄露！
- 在同一 `session_id` 上，**一次循环只允许发出 1 条 `next_step` 命令**，必须等待返回的指令，然后**执行指令**，结束再发下一条（阻塞式）。严禁使用并行工具同时发 `next_step`。
- 严禁“抢跑”：看到 `Step` 后不执行步骤内容，直接继续调用。
- 无视输出长度限制，完整输出，禁止在上下文内容很长时擅自精简输出。

## 补充规则

- `branch_value` 必须由 Agent 根据“已执行结果”自行给出；`while True` 这类条件直接回传 `true`；`for` 由 runtime 通过 `For` 节点自动推进，不需要 Agent回传循环条件。
- **默认必须自动跑到终态**：除非`instruction`明确指定需要暂停或停止，否则持续执行直到 `Finished` 或 `HumanInterferenceRequest`。
- 步骤回溯（让下一次的“获取下一步”从指定步骤重新开始）：

```
python [-X utf8] next_step.py --session <session_id> --step-index <step_index>
```

（“获取下一步”的返回结构中包含 `"step_index"` 字段。它充当了步骤的历史游标。）
