## RailRun：让 Agent 的流程不再脱轨

大模型 Agent 可以完成很多任务，但是在面对**长指令、复杂流程性任务**时，却常常遇到这些问题：

- 必要的步骤还没做完，Agent 已经开始给结论（*抢跑*）
- 流程步骤顺序被擅自改写、省略（*流程漂移*）
- 同样输入多次运行，执行路径不一致（*不可复现*）

这里的根本原因是：

- *模型越弱，越容易漏读或不理解上下文里的流程要求*;

- *模型越聪明，却越容易基于自己的判断重排流程*。

这些问题有时会带来明显成本，比如误判、审计压力，这时候你可能会：
- 反复调整、测试提示词，再在另一个模型上重复这些过程；
- 放弃 AI，把整个流程重新写成传统程序。
  
但更合适的方式，是给 Agent 的执行路径加上明确约束。

RailRun 正是为此而设计：它不会一次性把完整流程交给 Agent，而是*渐进式披露*流程的每个步骤，让 Agent 只能在当前步骤中行动。

这个做法能够**基本解决 Agent 在复杂流程中的“脱轨”问题**，让执行路径可控、可定位、可回溯。

---

## 你现在是否需要 RailRun？

适合立即引入：

- 流程错误代价高（合规、评测、审计、关键交付）
- 需要稳定复跑（同类输入希望路径一致）
- 需要可追溯（出错后必须定位到具体步骤）

可以暂缓：

- 任务以发散探索为主，不追求固定路径
- 错误代价低，复盘价值也低

---

## RailRun 解决问题的方式

RailRun 的核心思路是：
**把 Agent 当“单步执行器”，而不是“流程自主规划器”。**

> 标准执行循环：
>
> 1. Agent 把流程别名交给 RailRun
> 2. RailRun 返回当前唯一可执行步骤
> 3. Agent 执行该步骤
> 4. Agent 回到 RailRun 获取下一步
> 5. 循环直到终态（完成或人工接管）

关键点：

- Agent 看不到完整未来步骤
- 流程推进由外部状态机控制
- 分支基于执行结果推进

RailRun 放弃了“劝模型守规矩”，而在**执行控制面**上减少越权空间。

---

## 接入成本

你通常只需要：
- 在一个文件里描述你的流程设计；
- （也可跳过）参考 `PROTOCOL.md`，用 AI 转译成一个 `rail` 流程描述文件，然后检查；
- 在 `config.json` 的 `rails_alias_and_path` 中登记别名；
- 在 Agent 中用 `/RailRun [别名]` 触发执行

---

## 快速试用

下载并解压：

- [最新安装包（2026.05.17）](https://raw.githubusercontent.com/ray306/RailRun/main/install/railrun2026.05.17.zip)

#### 标准用法：

请先在 `config.json` 中保存 rail 文件地址：

```json
{
  "rails_alias_and_path": {
    "K2K": "examples/K2K/SKILL.rail",
    "simple_todo_flow": "examples/simple_todo_flow.rail"
  }
}
```

```text
/RailRun [{{这里填写rail文件的别名}}]
[如果有补充信息，请写在这里]
```

调用示例：

```text
/RailRun [K2K](no_pause=true, env='prod')
```

已有流程的名称或者路径需要被 `[]` 包裹。无扩展名时，RailRun 会从 `config.json` 读取真实 rail 路径。

*   *自由定制执行风格（高级）*：无需为了微调同一个流程而复制、维护多份文件。你可以在流程里定义不同分支，然后直接在别名后面加上控制参数来决定分支 （👉示例：`/RailRun [K2K](no_pause=True, auto_rewrite=True)`）

##### 示例 A：（已安装 Skill）

```text
/RailRun [simple_todo_flow]
我明天得交论文。
```

##### 示例 B：（未安装 Skill）

```text
请读取 `xxx\railrun\SKILL.md` 并按其中说明执行。
流程文件：[simple_todo_flow]
补充信息：我明天得交论文。
```

（如果没有被安装为Skill，需要把 `/RailRun` 替换为 `xxx\railrun\SKILL.md`，让 Agent 了解 RailRun 。）

> 试用后你应能直接观察到：
>
> - Agent 严格按照流程走（在其它例子里甚至包括循环和判断）
> - 出错时可定位到具体步骤并回溯重跑

---

## 运行时变量回传 (Variables Feedback)

在执行步骤中，如果 Agent 提取到了关键数据（例如抓取到的文本、金额等），可以通过在推进下一步时附加 `--var name=value` 参数将数据传回系统：

```bash
python next_step.py --session <session_id> --var price=150 --var ticker='AAPL'
```

回传的变量会自动写入会话的变量池，后续步骤可通过 `{{price}}` 模板插值直接引用，或在条件分支（如 `if price > 100:`）中由系统自动在本地进行确定性的逻辑计算。

---

## 步骤正式输出记录

正式输出持久化默认开启。Codex 宿主会优先从 `CODEX_THREAD_ID` 对应且经
`session_meta.payload.id` 校验的 transcript 中，增量读取 `event_msg` /
`agent_message`，因此不需要重复传递输出文本。无法定位或校验 transcript 时，
回退到原有 `--output` 方式：

```bash
python next_step.py --session <session_id> --output "已完成当前步骤的正式答复"
```

输出会原样记录到对应的 `history[].output`。该内容只应包含正式答复，不应包含思维链或工具调用细节。首次获取步骤，以及上一返回类型为 `Guidance` 或 `For` 时不需要传入 `--output`。

可在 Skill 调用提示词阶段控制正式输出持久化：

```bash
/railrun [alias] persistence=false
/railrun [alias](flow_param=value) persistence=true
```

Agent 必须在生成初始化命令之前解析该开关，并始终显式调用：

```bash
python next_step.py --procedure "[alias]" --persistence false
```

未指定时提示词层默认使用 `true`。该设置随后保存在 RailRun session 中。关闭后
不会读取宿主 transcript，也不会要求 `--output`。宿主集成位于
`scripts/host_output.py`，当前实现 Codex，并保留了其他宿主 provider 的接口。

---

## 创建流程

如果 `[]` 中指明了txt或md文件，或者没有 `[]`、但 Prompt 中描述了需求或流程，RailRun 会使用内置 `examples\generate_rail_flow.rail` 编写 `rail` 流程文件。

如果用户希望自己编写 `rail` 流程文件，可参考`PROTOCOL.md` 与 `examples` 目录。

---

## 运行模式与网页 UI
如果需要可视化或者需要人工干预流程，且 RailRun 运行在本地，可在终端运行 `python next_step.py --ui` 并在浏览器中访问 http://127.0.0.1:8799/。

* **核心功能**：
  - *会话总览*：实时列出所有 Session 的状态（进行中、已完成、需人工介入、已终止）；运行中 Session 如果超过 10 分钟未更新，会自动标记为已终止。
  - *进度可视化*：选定会话后，直观展示该会话对应的原始控制流节点（CFG）以及详细的演进历史轨迹，并标记当前步骤游标。
  - *控制台调度*：在网页端一键点击“暂停流程”（转为人工介入）、“终止”会话，或选择历史中的任意一步“跳转回滚（Rewind）”。
---

## 适用边界与可信度说明

我们承诺的是：在高确定性流程中，RailRun 能提供更强的执行轨道约束。
但 RailRun 当前处于早期阶段，**需要各位用户反馈案例与背书**。

---

## RailRun & Harness

RailRun 可嵌入 Harness 的执行控制层，专门处理“执行确定性”问题。

- Harness：组织并运行 agent系统
- RailRun：保证系统按轨道运行

如果说 Harness 负责把系统跑起来，那 RailRun 负责让系统始终沿着轨道跑下去。

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
