# RAIL -> CFG 编译指南

本指南面向编译器实现，目标是把 `.rail` 文件编译为可执行的控制流图（CFG JSON）。  
本指南与 `examples/K2K/rail/PROTOCOL.md` 对齐；若冲突，以 PROTOCOL 为准。

输出协议建议：
- `protocol`: `next-step-cfg/v1`
- `entry`: 入口节点 ID
- `nodes`: 节点字典（`Step` / `Branch` / `For` / `Ask` / `Guidance` / `Finished`）
- `meta.sources`: 参与编译的源文件及其 mtime（用于增量重编译）
- `meta.warnings`: 编译期兼容处理产生的告警（如 `step` 块调用自动拆分、逗号参数兼容归并）

---

## 1. 编译目标

输入：
- 一个入口 `xxx.rail` 文件（例如 `SKILL.rail`）

输出：
- 一个控制流图 `xxx-cfg.json` 文件，满足：
  - 节点通过 `next`、`on_true`、`on_false` 连接
  - 允许出现循环回边
  - 有统一终点 `Finished`
  - 所有 `include` 与函数调用在编译期展开

---

## 2. 推荐产物结构

```json
{
  "protocol": "next-step-cfg/v1",
  "entry": "1",
  "nodes": {
    "1": {"type": "Guidance", "instruction": "你是一个研究型 Agent。", "next": "2"},
    "2": {"type": "Ask", "instruction": "请回复继续", "next": "3"},
    "3": {
      "type": "Branch",
      "condition": "cond",
      "on_true": "4",
      "on_false": "5"
    },
    "4": {"type": "Step", "instruction": "执行动作", "next": "3"},
    "5": {"type": "Finished", "instruction": "所有指令已执行完毕。结束输出。"}
  },
  "meta": {
    "sources": {
      "<absolute_path_to_rail>": 1778598042.94
    }
  }
}
```

节点类型：
- `Step`: 执行普通指令。字段：`instruction`, `next`
- `Branch`: 条件分支。字段：`condition`, `on_true`, `on_false`
- `For`: 迭代分支。字段：`items_expr`, `item_key`, `index_key`, `on_iterate`, `on_done`（可选 `items`）
- `Ask`: 用户提问并暂停。字段：`instruction`, `next`
- `Guidance`: 注入作用域指导上下文。字段：`instruction`, `next`
- `Finished`: 结束。字段：`instruction`

---

## 3. 词法与语法归一化（必须先做）

为与 PROTOCOL 的“容错”一致，建议先做一轮规范化：

1. 标点容错：
- 关键语法中的中英文冒号等价：`:` 与 `：`
- 函数参数分隔中的中英文逗号等价：`,` 与 `，`
- 参数值边界规则：若命名参数值未加引号且包含顶层逗号，编译器可将被切碎片段并入前一个命名参数值，并产出 warning；建议始终为含逗号参数使用引号或三引号。
- 若无法安全并入（例如命名参数后仍出现无法归属的裸值片段），必须 fail-fast 报错，禁止静默改写。

2.1 多行函数调用：
- 函数调用允许跨多行书写（例如参数里使用三引号长文本）
- 只要括号未闭合，编译器应持续收集后续行，直到调用语句闭合
- 该语句在编译期仍视为“一次函数调用”，按调用点内联函数体
3. 注释剔除：
- `#` 注释在解析前移除
- 但必须保证三引号块内的 `#` 保留为正文

4. 缩进检查：
- 同一文件只允许一种缩进宽度（例如统一 2 空格或统一 4 空格）
- 缩进不一致直接报错（fail-fast）

5. include 路径字符串：
- 支持 `include "..."` / `include '...'`
- 相对路径按“当前被 include 的文件所在目录”解析，而不是入口文件目录

---

## 4. 总体编译流程

0. 前处理
- 读取入口文件
- 检查 include 循环引用（维护 include 栈）
- 处理 `include`：将目标文件原地展开
- 记录 `meta.sources`

1. 解析 AST
- 按缩进构建作用域树（类似 Python block）
- 识别：`step`、`ask`、`if/elif/else`、`while`、`for`、`break/continue`、`return`、`include`、`def`、函数调用、隐式 step、三引号 guidance step

2. 函数语义展开
- `def` 注册为符号表，不直接生成执行节点
- 在调用点内联函数体（参数替换 `{{name}}`）
- 为 `return` 绑定“调用点后继”
- 建议检测调用循环（A 调 B，B 调 A）并 fail-fast

3. CFG 构建
- 线性语句串接 `next`
- 分支生成 `Branch`
- 循环生成回边
- 统一收敛到 `Finished`

4. 产物落盘
- 生成 CFG JSON
- 附带 `meta.sources`

---

## 5. RAIL 到 CFG 的映射规则

### 5.1 guidance step (`"""..."""`)

必须编译为 `Guidance` 节点：
- `instruction` = 三引号块文本（去掉包裹符）
- `next` = 后继节点

### 5.2 显式 `step:`

默认情况下，整个缩进块编译为一个 `Step`：
- `instruction` = 块内多行拼接文本（保留换行）
- `next` = 后继节点

`step:` 块内函数调用特殊规则：
- 若块内识别到函数调用语句，编译器会自动拆分为：
  - 调用前文本（`Step`，可选）
  - 函数调用（按调用点内联展开）
  - 调用后文本（`Step`，可选）
- 即：函数调用不会被当作普通文本留在 `instruction` 中。
- 编译器需产生 warning，提示“step 块内函数调用已自动拆分为独立调用并展开”。建议写入 `meta.warnings`。

### 5.3 隐式 step

若一行不属于其他结构（`def/include/if/while/...`），编译为一个 `Step`：
- `instruction` = 该行文本
- `next` = 后继节点

### 5.4 `ask:`

必须编译为 `Ask` 节点：
- `instruction` = ask 缩进块全文
- `next` = 继续执行的后继节点

运行时语义：
- 到达 `Ask` 时暂停执行并请求用户输入

### 5.5 `if/elif/else`

编译为一组 `Branch`：
- `condition` 为表达式文本
- `on_true` 指向该分支块入口
- `on_false` 串到下一个 `elif`，或 `else`，或 after

### 5.6 `while`

编译为循环 `Branch`：
- `on_true` -> 循环体入口
- 循环体正常末尾回到该 `Branch`
- `on_false` -> 循环后继

`while True:` 特殊规则：
- 不编译为 `Branch`
- 编译为 `Guidance` 节点，`instruction` 固定为：`准备开始循环执行`
- 该 `Guidance` 的 `next` 指向循环体入口
- 循环体正常末尾直接回到“循环体第一条语句”（不回到 `while True` 节点）
- `break` 跳到循环后继
- `continue` 跳到循环体第一条语句

### 5.7 `break` / `continue`

跳转语义：
- `break` -> 当前循环出口
- `continue` -> 当前循环头 `Branch`

### 5.8 `return`

- 函数体内：跳到“调用点后继”
- 顶层：跳到统一 `Finished`

### 5.9 `include path`

- 编译期原地展开，不创建新作用域
- 需要保留来源映射，便于报错时给出“错误位置 + 引用位置”

### 5.10 `def` 与函数调用

- `def` 仅注册定义，不直接生成执行节点
- 调用支持位置参数与命名参数
- 形参替换 `{{参数名}}` 后内联展开

### 5.11 `for` 的处理策略

`for item in items:` 编译为 `For` 节点：
- `items_expr` = `items`（原始表达式文本）
- `item_key` = `item`
- `index_key` = `item_index`（默认规则）
- `on_iterate` -> 循环体入口
- `on_done` -> 循环后继
- 循环体正常末尾回到该 `For` 节点

运行时语义（配套约定）：
- `For` 不需要 Agent 回传 `branch_value`
- runtime 负责推进游标并写入当前轮变量（`item_key` / `index_key`）
- 后续 `Step`/`Branch` 可通过模板变量读取当前轮值（例如 `{{item}}`）

### 5.12 `params(...)`

支持在入口 RAIL 文件最顶部使用“文件签名”来声明默认编译期常量参数：
- **语法**：`params(name1=value1, name2=value2)`
- **支持类型**：布尔值（`true`/`false`）、数字、单双引号字符串。
- **解析行为**：
  1. 编译器在语法分析前提取顶部的 `params(...)` 并将其从逻辑语句流中移除，以此获得默认参数集。
  2. 运行时初始化 Session 时如果传入了覆盖参数（Invocation Constants），则对其进行合并，并校验是否有未定义的参数（Typos 检查）。

### 5.13 编译期常量折叠与死代码消除 (Dead Code Elimination)

合并后的编译期常量（Merged Constants）将用于编译阶段的控制流优化：
- **静态估值**：如果 `if` 或 `elif` 条件表达式在编译期能被静态求值（使用内置 AST 求值器）：
  - **静态真 (Statically True)**：直接将该条件分支体作为 Fallback 编译，剪掉后续所有 `elif`/`else`，且**不生成任何 `Branch` 节点**。
  - **静态假 (Statically False)**：直接剔除该分支（即死代码消除 DCE），不为其生成任何节点。
- **混合表达式**：对于不包含编译期常量的动态表达式，不进行常量折叠，正常编译为 `Branch` 节点在运行时进行动态判断。

---

## 6. 控制流拼接细节

1. 语句块编译函数返回 `(entry_id, exits)`：
- `entry_id`: 块入口
- `exits`: 所有“待回填后继”的出口边集合

2. 顺序拼接：
- 前一语句的 `exits` 回填到后一语句 `entry_id`

3. 分支拼接：
- `if/elif/else` 每个分支体都产出各自 `exits`
- 合并后统一回填到 after 节点

4. 循环拼接：
- 循环体普通出口回填到循环头
- `break` 出口进入循环 after
- `continue` 出口进入循环头

5. 终止语句：
- `return` / 顶层结束直接连向 `Finished`
- 这些语句不再产生普通顺序出口

---

## 7. 编译期校验（Fail-Fast）

建议检查：
- 未定义函数
- 参数数量或命名参数不匹配
- include 文件不存在
- include 循环引用
- 函数调用循环引用
- 条件表达式不完整（缺少 `if <expr>:` / `while <expr>:`）
- `break/continue` 出现在循环外
- `return` 出现在非法位置
- `ask`/`step` 空块
- 三引号未闭合
- 缩进不一致

错误格式建议包含：
- `错误位置`: `<file>:<line>`
- `引用位置`: `<caller_file>:<line>`（可选，include/call 触发时建议带上）
- `错误原因`: `<message>`

---

## 8. 测试建议

至少覆盖：
- 纯线性 step
- guidance step 节点生成与作用域传播
- ask 节点暂停/恢复
- if/elif/else
- while + break + continue（验证回边）
- include 单层与多层
- include 循环引用报错
- 函数调用（位置参数、命名参数）
- return（函数内与顶层）
- 中英文逗号/冒号容错
- 三引号内注释符保留
