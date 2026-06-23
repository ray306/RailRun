---
name: RailRun
Author: Jinbiao Yang
Version: 2026.06.21
argument-hint: "[流程别名/路径]，或者直接描述要生成的流程；可附加 persistence=true|false language=中文"
arguments: [procedure_name, persistence, language, prompt]
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
- 提示词级持久化开关：`/railrun [procedure_name] persistence=true|false`
- 提示词级输出语言：`/railrun [procedure_name] language=中文`
- 流程参数格式：`/railrun [procedure_name](param1=value1, param2=value2, ...) persistence=true|false language=中文`
- 生成流程格式：`/railrun <直接描述需求或流程>`
- `persistence` 是 RailRun 的提示词级保留参数，控制整个 Skill 会话是否记录正式输出，默认 `true`。它不是 `.rail` 流程常量，不得放入传给流程的常量集合。
- `language` 是 RailRun 的提示词级保留参数，控制 Agent 展示正式执行过程时使用的语言，默认 `中文`。它不是 `.rail` 流程常量，不得放入传给流程的常量集合。
- runtime 会在第一个 `Step` 出现前的每次 `next_step` 响应中附加语言要求；第一个 `Step` 本身也会附加，并从此停止后续语言提示。
- 兼容旧写法：`[procedure_name](persistence=false, language='English')`。Agent 必须先提取并移除这些保留参数，再调用 runtime。
- `[xxx]` 中可以是 `.rail` 路径、流程别名，或者待转换的 `.md`/`.txt` 文件路径。

## 输入路由（必须先判断）

执行任何 `next_step.py` 命令前，必须按以下顺序判断：

1. Prompt 中存在 `[...]`：
   - 内容是 `.md` 或 `.txt` 路径：调用：
     ```text
     python -X utf8 next_step.py --procedure "<路径>" --persistence <true|false> --language "<language>"
     ```
     `next_step.py` 会自动路由到内置 `rail_generator` 流程，并把输入路径、建议输出路径和原流程参数写入生成 session 变量。
   - 内容是 `.rail` 路径或无扩展名流程别名：进入正常执行流程。
   - 其他文件类型：停止并提示只支持 `.rail`、`.md`、`.txt` 或已配置别名。
2. Prompt 中没有 `[...]`：
   - 如果 Prompt 本身可能直接描述了要创建的需求或流程，启动内置生成流程：
     ```text
     python -X utf8 next_step.py --procedure "[rail_generator](input_kind='prompt')" --persistence <true|false> --language "<language>"
     ```
   - `rail_generator` 的第一步会要求 Agent 判断当前原始 Prompt 是否足以构成流程说明；不足时按流程输出错误并结束。

不得使用独立转换命令；转换本身也是一个普通 RailRun 流程。

## rail_generator 生成后的交互

启动 `rail_generator` session 后：

1. 严格按 `next_step` 返回的 `instruction` 执行。
2. 需要时读取 md/txt 输入或当前用户原始 Prompt，依据 `PROTOCOL.md` 转写。
3. 验证失败时修正并重新验证；不得执行未通过验证的流程。
4. 验证成功后，把生成的流程和结构讲解展示给用户：
   - 内容较短时展示完整流程；
   - 内容过长时展示摘要、主要节点、分支、循环、提问点和输出文件路径。
5. 询问用户要：
   - 继续修改现有结果；或
   - 执行任务。
6. 用户要求修改时，直接修改同一个 `output_rail`，重新验证并再次展示。
7. 只有用户明确要求执行时，先停止当前生成 session，再用新生成的 `.rail` 文件开启新的 RailRun session。

生成、验证和用户审阅阶段允许读取刚生成的 `.rail`。一旦开始执行新 session，恢复 `.rail` 文件禁止提前读取的硬约束。

## 提示词阶段的保留参数

在执行任何 `next_step.py` 命令之前，Agent 必须完成以下处理：

1. 从 Skill 调用提示词中读取独立参数 `persistence`；未提供时取 `true`。
2. 从 Skill 调用提示词中读取独立参数 `language`；未提供时取 `中文`。
3. 将 `persistence` 规范化为严格布尔值 `true` 或 `false`。其他值必须停止并向用户报告参数错误。
4. 将 `language` 规范化为非空字符串。空值必须停止并向用户报告参数错误。
5. 将流程名和流程自身参数保留为 `procedure_name`，不得把 `persistence` 或 `language` 当作流程常量。
6. 初始化命令必须始终显式包含 `--persistence true|false --language "<language>"`，禁止依赖 runtime 默认值。
7. 初始化后以 session 保存的设置为准，后续不得根据 Agent 自行判断修改保留参数。

提示词示例：

```text
/railrun [K2K] persistence=false
/railrun [invest](period='3m') persistence=true language=English
```

---

# 输出内容

- **隐藏内部控制信息**：`next_step` 的调用与返回内容属于运行时内部控制信息，初始化/停止 session 这些也属于运行时内部控制信息，不需要被用户看到，可以隐藏在思维链中。
- **展示`instruction`的正式执行过程**：得到`next_step` 返回的 `instruction` 之后，Agent 需要结束思维链，以正式回答的方式向用户展示自己的分析执行过程。

## 硬约束（必须遵守！）
- 禁止读取用`--procedure`指定文件、或者扩展名是 `.rail` 的文件的内容！这样的文件只能在 `next_step` 的返回值里逐步披露！禁止任何形式的提前泄露！
- 上述禁止读取规则不适用于 `rail_generator` 的生成、验证和用户审阅阶段；但开始执行生成结果后立即生效。
- 在同一 `session_id` 上，**一次循环只允许发出 1 条 `next_step` 命令**，必须等待返回的指令，然后**执行指令**，结束再发下一条（阻塞式）。严禁使用并行工具同时发 `next_step`。
- 严禁“抢跑”：看到 `Step` 后不执行步骤内容，直接继续调用。
- 无视输出长度限制，完整输出，禁止在上下文内容很长时擅自精简输出。

## 补充规则

- `branch_value` 必须由 Agent 根据“已执行结果”自行给出；`while True` 这类条件直接回传 `true`；`for` 由 runtime 通过 `For` 节点自动推进，不需要 Agent回传循环条件。

- **回传运行时变量**：Agent 可在推进下一步时回传新提取的数据或状态变量（通过多次指定 `--var name=value`），这些变量会被写入 Session 的变量池并用于后续步骤中 `{{name}}` 的模板替换。
- 步骤回溯（让下一次的“获取下一步”从指定步骤重新开始）：

```
python [-X utf8] next_step.py --session <session_id> --step-index <step_index>
```

（“获取下一步”的返回结构中包含 `"step_index"` 字段。它充当了步骤的历史游标。）

---
## 开始执行：

现在请你按照下面的工作过程执行任务：

1. 初始化：
   ```
   python -X utf8 next_step.py --procedure "procedure_name" --persistence <true|false> --language "<language>"
   ```
   `<true|false>` 与 `<language>` 必须在提示词阶段确定。即使用户未提供，也必须显式传入默认值 `true` 与 `中文`。

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