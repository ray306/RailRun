---
name: railrun
Alias: RailRun
Author: Jinbiao Yang
Version: 2026.05.17
argument-hint: "[这里填写流程描述文件的名称（如果不在工作区根目录，请填写完整路径）]"
arguments: [procedure_name]
description: Agent 经常自作主张调整复杂的流程。本 Skill 可通过向 Agent 渐进式披露每个步骤，让 Agent 只能逐步执行当前看到的步骤，以保证流程确定性。

---

# RailRun

本 Skill 的核心思想是：

> **将大语言模型 Agent 作为“单步执行解释器”使用，而不是作为自主规划器使用。**

在给定流程后，Agent 通常会根据上下文自行思考、规划。这种方式灵活，但在确定性要求极高的场景中，容易出现流程漂移、误判、或其它不符合预期的执行。

本 Skill 采用相反的控制模型：

> 执行流由 `next_step` 工具掌控
> 流程被 `next_step` 对 Agent 渐进式披露（每次一步）
> Agent 每次只执行当前看到的步骤
> 执行完成后，再次调用 `next_step` 获取下一步

因此，流程安全性不是建立在 Agent “自觉不乱来”之上，而是建立在：

```text
Agent 看不到未来步骤；
next_step 掌控执行游标；
状态机决定下一步；
协议校验阻断非法推进。
```

本 Skill 的本质是：

> 用 `next_step` 替代 Agent 的流程规划能力。
> 用渐进式披露替代完整上下文暴露。
> 用状态机控制替代提示词约束。

核心目标是：

> 在严谨复杂、不能乱跳步骤的流程中，让 Agent 只能执行当前步骤。Agent不知道、不能决定、也不应预期流程在未来什么时候走到哪里。

---
### 中文编码环境

为避免中文输出乱码，python需要加上 -X utf8 参数
---

## 快速开始

Prompt 标准写法：
```
/RailRun {{procedure_name}}
[可能的补充信息]
```

说明：以下命令默认在 `next_step.py` 所在目录执行（CLI 入口文件）。

1. 启动并初始化 session：

把收到的 `procedure_name` 直接作为参数传入：

```
python [-X utf8] next_step.py --procedure <procedure_name>
```

2. 获取下一步（把 `<session_id>` 替换成上一步返回的 session）：

```
python [-X utf8] next_step.py --session <session_id> [--branch-value true|false]
```

（如果上一步的返回值里要求了回传分支值，调用时需要参数`branch-value`）

3. 停止 session：

```
python [-X utf8] next_step.py --session <session_id> --shutdown
```

4. 步骤回溯（让下一次的“获取下一步”从指定步骤重新开始）：

```
python [-X utf8] next_step.py --session <session_id> --step-index <step_index>
```

（“获取下一步”的返回结构中包含 `"step_index"` 字段。它充当了步骤的历史游标。）

## 硬约束（必须遵守！）

- 禁止读取用`--procedure`指定文件、或者扩展名是 `.arp` 的文件的内容！这样的文件只能在 `next_step` 的返回值里逐步披露！禁止任何形式的提前泄露！
- 在同一 `session_id` 上，**一次循环只允许发出 1 条 `next_step` 命令**，必须等待返回的指令，然后**执行指令**，结束再发下一条（阻塞式）。严禁使用并行工具同时发 `next_step`。
- 严禁“抢跑”：看到 `Step` 后不执行步骤内容，直接继续调用。
- 无视输出长度限制，完整输出，禁止在上下文内容很长时擅自精简输出。

# 输出内容

- **隐藏内部控制信息**：`next_step` 的调用与返回内容属于运行时内部控制信息，初始化/停止 session 这些也属于运行时内部控制信息，不需要被用户看到，可以隐藏在思维链中。
- **展示分析执行过程**：`next_step` 返回的 `instruction` 需要被 Agent 分析并执行。请向用户展示每次的分析执行过程（除非属于内部控制信息），禁止隐藏在思维链中。

## 补充规则

- `branch_value` 必须由 Agent 根据“已执行结果”自行给出；`while True` 这类条件直接回传 `true`；`for` 由 runtime 通过 `For` 节点自动推进，不需要 Agent 回传循环条件。
- **默认必须自动跑到终态**：持续执行直到 `Finished` 或 `HumanInterferenceRequest`。
