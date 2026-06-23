---
name: RailRun
Author: Jinbiao Yang
Version: 2026.06.23
argument-hint: "[流程别名/路径](persistence=true|false, language='中文', ...)，或者直接描述要生成的流程"
arguments: [procedure_name, params, prompt]
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

- 默认格式：`/railrun [procedure_name]`
- 带参数格式：`/railrun [procedure_name](param1=value1, param2=value2, ...)`

提示词示例：

```text
/railrun [K2K]
/railrun [invest](period='3m', persistence=true, language='English')
```

## 输入路由

执行任何 `next_step.py` 命令前，先判断输入类型：

- Prompt 中存在 `[...]`：把其中内容和括号参数原样作为 `--procedure` 传给 `next_step.py`。
- Prompt 中没有 `[...]`，但看起来是在描述要创建的需求或流程：启动内置生成流程：
  ```text
  python -X utf8 next_step.py --procedure "[rail_generator](input_kind='<prompt>')"
  ```
- 其他情况：停止并提示用户提供流程别名、流程文件路径，或清晰的流程生成需求。

不得使用独立转换命令；生成流程本身也必须通过 RailRun 执行。

---

# 输出内容

- **隐藏内部控制信息**：`next_step` 的调用与返回内容属于运行时内部控制信息，初始化/停止 session 这些也属于运行时内部控制信息，不需要被用户看到，可以隐藏在思维链中。
- **展示`instruction`的正式执行过程**：得到`next_step` 返回的 `instruction` 之后，Agent 需要结束思维链，以正式回答的方式向用户展示自己的分析执行过程。

## 硬约束（必须遵守！）
- 禁止读取用`--procedure`指定文件、或者扩展名是 `.rail` 的文件的内容！这样的文件只能在 `next_step` 的返回值里逐步披露！禁止任何形式的提前泄露！
- 上述禁止读取规则不适用于 `rail_generator` 的生成、验证和用户审阅阶段；但一旦开始执行新 session，恢复 `.rail` 文件禁止提前读取的硬约束。
- 在同一 `session_id` 上，**一次循环只允许发出 1 条 `next_step` 命令**，必须等待返回的指令，然后**执行指令**，结束再发下一条（阻塞式）。严禁使用并行工具同时发 `next_step`。
- 严禁“抢跑”：看到 `Step` 后不执行步骤内容，直接继续调用。
- 无视输出长度限制，完整输出。就算上下文内容很长也禁止擅自精简输出。

## 补充规则

- 如果返回内容要求 `branch_value`，必须由 Agent 根据已执行结果自行判断，并在下一次调用中传入 `--branch-value true|false`。
- 如果步骤产生了后续步骤需要复用的数据，可在下一次调用中用 `--var name=value` 回传。

---
## 开始执行：

现在请你按照下面的工作过程执行任务：

1. 初始化：
   ```
   python -X utf8 next_step.py --procedure "procedure_name(persistence=<true|false>, language='<language>')"
   ```

2. 获取下一步：
   ```
   python -X utf8 next_step.py --session <session_id> [--branch-value true|false] [--var name=value]
   ```

3. 执行返回的 `instruction`。

4. 重复步骤 2-3，直到 `Finished` 或 `HumanInterferenceRequest`。

5. 停止 session：
   ```
   python -X utf8 next_step.py --session <session_id> --shutdown
   ```

注意：
- 以下命令默认在 `next_step.py` 所在目录执行；
- 为避免中文输出乱码，中文编码环境中python需要加上 -X utf8 参数；
- 如果没有出现程序错误，禁止去读取本目录下的任何文件；
- **默认必须自动跑到终态**：除非`instruction`明确指定需要暂停或停止，否则持续执行直到 `Finished` 或 `HumanInterferenceRequest`。
- 不要擅自读取其他文件！
