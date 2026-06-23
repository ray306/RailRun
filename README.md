## RailRun：让 Agent 的流程不再脱轨

你是否常常遇到这些问题：

- 明明 Prompt 或 Skill 中明确规定了流程，但是 Agent 在执行时却自作主张，删改了其中的步骤。
- 明明说了要怎样做、不要怎么做，但是上下文一长， Agent 就忘记了这些指示。
- 设计的流程很复杂很长，但是 Agent 执行途中会尝试节省 Token，缩短输出甚至中途停下。

所有的模型都可能有这些问题。小模型、弱模型确实会容易漏读或不理解上下文里的流程要求。但即使是最新的大模型，它越聪明，越容易基于自己的判断重排流程。

如果这些问题带来了明显出错成本，你可能会：
- 反复调整、测试提示词，再在另一个模型上重复这些过程；
- 放弃 AI，把整个流程重新写成传统程序。
  
但更合适的方式，是给 Agent 的执行路径加上明确约束。RailRun 正是为此而设计。

### RailRun 解决问题的方式

RailRun 的核心思路是：
**它不会一次性把完整流程交给 Agent，让 Agent 自主规划执行过程；而是*渐进式披露*流程的每个步骤，把 Agent 当“单步执行器”使用。**

> 标准执行循环：
>
> 1. Agent 把流程交给 RailRun
> 2. RailRun 返回当前唯一可执行步骤
> 3. Agent 执行该步骤
> 4. Agent 要求 RailRun 返回下一步
> 5. 循环2到4直到完成或人工接管

关键点：

- Agent 看不到完整未来步骤
- 流程推进由外部的真实状态机控制
- 分支基于执行结果推进

RailRun 放弃了“劝模型守规矩”，而在**执行控制面**上减少越权空间。

这个做法能够**基本解决 Agent 在复杂流程中的“脱轨”问题**，让执行路径可控、可定位、可回溯。


### 你现在是否需要 RailRun？

以下情况适合立即引入：

- 流程错误代价高，需要保证严格执行
- 流程设计过于复杂，Agent 无法自主正确规划
- 需要能稳定重复
- 需要可追溯

---

## 快速试用

1. 下载并解压：

   - [Zip安装包](https://github.com/ray306/RailRun/archive/refs/heads/main.zip)

2. 让 Agent 读取解压目录中的 `SKILL.md`。

   如果已安装为 Skill，可直接使用 `/RailRun`。示例：

   ```text
   /RailRun [simple_todo_flow]
   我明天得交论文。
   ```
   
   如果未安装，在 Prompt 中明确要求 Agent 读取该文件。示例：

   ```text
   请读取 `xxx\railrun\SKILL.md` 并按其中说明执行。
   流程：[simple_todo_flow]
   我明天得交论文。
   ```

3. Agent 会先初始化一个 Session，然后逐步执行流程。

---

## 标准用法：

```bash
/RailRun [{{这里填写rail文件的地址或别名}}]
如果有补充信息，请写在这里
```

如：
```bash
/RailRun [simple_todo_flow]
我明天得交论文。
```

预期行为：Agent 不会自行规划完整任务，而是由 RailRun 逐步披露待办整理流程；执行过程中可在 Web UI 或 session 记录中看到每一步。

> 试用后你应能直接观察到：
>
> - Agent 严格按照流程走（在其它例子里甚至包括循环和判断）
> - 出错时可定位到具体步骤并回溯重跑

### 通过别名指定流程
`[]` 包裹了已有流程的名称或者路径。
 
无扩展名时，RailRun 会把`[]` 的内容视为别名，尝试从 `config.json` 读取真实 rail 路径。所以使用别名前请先在 `config.json` 中登记 rail 文件地址：

```json
{
  "rails_alias_and_path": {
    "simple_todo_flow": "examples/simple_todo_flow.rail"
  }
}
```

### 通过参数自由定制流程执行风格

无需为了微调同一个流程而复制、维护多份文件。你可以在流程里定义不同分支，然后直接在别名后面加上控制参数来决定分支。示例：

```bash
/RailRun [K2K](no_pause=True, auto_rewrite=True)
```

#### 指定输出语言
为了让 Agent 输出符合自己需要的语言，，RailRun 提供了系统级保留参数`language`（默认为`中文`）。

可在 `config.json` 中修改默认值。

```bash
/railrun [alias](language='English')
```

可在 `config.json` 中修改默认值。

#### 记录输出的内容（持久化）

为了便于审计和导出，RailRun 提供了系统级保留参数`persistence`（默认为`true`），用来控制整个 Skill 会话是否会在 Session 文件中记录每个步骤的正式输出。正式输出只包含展示给用户的答复，不应包含思维链或工具调用细节。

可在 `config.json` 中修改默认值。

```bash
/railrun [alias](persistence=true)
```

目前 RailRun 支持通过读取 Codex 的聊天记录持久化正式输出。如果宿主不是 Codex 但又需要持久化，RailRun会使用 `--output` 向内部工具显式传入上一执行步骤的正式答复，但这样会额外消耗近一倍的 Token。

---

### 创建流程

用户可使用已有的 `rail` 流程文件。但如果用户需要自己创建新的流程，则需要在 Prompt 中描述需求或流程，或者在`[]` 中给出描述描述需求或流程的 txt 或 markdown 文件。

从 Prompt 直接生成流程：

```text
/RailRun 请创建一个流程：先收集用户目标，再拆解任务，最后输出检查清单。
```

从文件生成流程：

```text
/RailRun [docs/my_process.md]
```

了解到需求或流程后，RailRun 会使用内置 `examples\generate_rail_flow.rail` 编写新的 `rail` 流程文件。

#### `.rail` 示例

```rail
step:
  询问用户今天最重要的一件事。
  根据用户回答，拆成 3 个可执行动作。
ask:
  这 3 个动作是否需要调整？
step:
  根据用户反馈输出最终待办清单。
```

如果用户希望自己编写 `rail` 流程文件，可参考`PROTOCOL.md` 与 `examples` 目录。

---

## 运行模式与网页 UI
如果需要可视化或者需要人工干预流程，且 RailRun 运行在本地，可在终端运行 `python next_step.py --ui` 并在浏览器中访问 http://127.0.0.1:8799/。

* **核心功能**：
  - *会话总览*：实时列出所有 Session 的状态 （Session 如果超过 10 分钟未更新，会自动标记为已终止）。
  - *进度可视化*：选定会话后，直观展示该会话对应的原始控制流节点（CFG）以及详细的演进历史轨迹，并标记当前步骤游标。
  - *控制台调度*：在网页端一键点击“暂停流程”（转为人工介入）、“终止”会话，或选择历史中的任意一步“跳转回滚”。

---
## 运行时变量回传 (Variables Feedback)

在执行步骤中，如果 Agent 提取到了关键数据（例如抓取到的文本、金额等），可以通过在推进下一步时附加 `--var name=value` 参数将数据传回系统：

```bash
python next_step.py --session <session_id> --var price=150 --var ticker='AAPL'
```

回传的变量会自动写入会话的变量池，后续步骤可通过 `{{price}}` 模板插值直接引用，或在条件分支（如 `if price > 100:`）中由系统自动在本地进行确定性的逻辑计算。

---

## 步骤回溯

如果需要让下一次“获取下一步”从历史中的指定步骤重新开始，可使用 `--step-index`：

```bash
python next_step.py --session <session_id> --step-index <step_index>
```

`next_step.py` 返回结构中的 `step_index` 字段就是可用于回溯的历史游标。回溯会重建 session 游标，之后继续按正常 `--session` 调用推进。

---

## **【实验性功能】**

我们提供了实验性方案（需要支持子智能体的环境，如 Codex），通过“流程控制子智能体”向主 Agent 逐步披露步骤。

> 执行流由一个专门的“流程控制子智能体 (Flow Control Subagent)”掌控；
> 主 Agent 处于信息盲区，绝不直接读取任务描述文件；
> 流程由子智能体对主 Agent 进行渐进式披露（每次一步）；
> 主 Agent 每次只执行当前看到的步骤；
> 执行完成后，主 Agent 将执行结果反馈给子智能体，由子智能体智能分析并披露下一步。

Prompt：

```text
请读取 `[EXP] Subagent.md` 的指令，将 {{流程描述文件路径}} 作为参数。
[可选补充信息]
```

目前该方案在某些场景更灵活，但会削弱执行确定性，不建议在高确定性任务中作为默认路径。
