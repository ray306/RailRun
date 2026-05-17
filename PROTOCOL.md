# Agent Rail Prompt Protocol

Agent Rail Prompt Protocol(ARP) 是一种面向 Agent prompt 文件的极简流程协议，用接近 Python 的缩进语法描述：

* 步骤
* 作用域指导
* 条件
* 循环
* 用户提问
* 文件复用
* 函数复用
* 错误中止

文件后缀建议：

```text
.arp
```

---

# 1. 基本模型

arp 文件由一组语句组成，按顺序执行。

```arp
"""
你是一个研究型 Agent。
回答需要区分事实、判断和不确定性。
"""

理解用户问题
提取用户约束

if user.need_research:
  step:
    搜索资料
    提取关键信息
else:
  直接基于已有上下文回答
```

---

# 2. 缩进

arp 使用缩进表示作用域，类似 Python。

```arp
if user.has_file:
  读取文件
  总结文件
else:
  ask:
    请提供文件
```

规则：

* 同一文件内缩进必须一致。
* 推荐 2 个空格或 4 个空格。
* 控制语句后面的缩进块属于该语句。

---

# 3. Step

## 3.1 隐式 step

和 `step:` 处在同一缩进层级的普通内容（除了函数定义和调用），**每一行都是一个 step**。

```arp
理解用户问题
提取关键约束
生成回答
```

等价于：

```arp
step:
  理解用户问题

step:
  提取关键约束

step:
  生成回答
```

---

## 3.2 显式 `step:`

`step:` 用于把后续缩进块合并成**一个 step**。

```arp
step:
  阅读用户输入
  提取目标、约束和输出格式
  不要立刻回答
```

语义：

* 整个缩进块是一个完整执行步骤。
* 块内多行不会被拆成多个 step。
* 适合表达一个复杂动作。

编译器兼容规则（推荐）：
* 如果 `step:` 块内出现函数调用语句，编译器可自动将该块拆分为“调用前文本 step + 函数调用 + 调用后文本 step”。
* 函数调用应按正常规则展开函数体，不应作为纯文本保留在 step 指令中。
* 建议编译器给出 warning，提示发生了自动拆分。

---

# 4. 作用域整体指导

在任意作用域开头，可以写三引号块：

```arp
"""
你是一个严谨的信息分析助手。
所有回答都要说明不确定性。
"""
```

它表示当前作用域的整体指导。

它也被视为一种特殊 step：**guidance step**。

它和普通 step 的区别是：

* 普通 step：执行一个动作。
* guidance step：更新当前作用域的指导上下文，可以影响后续 step，但 Agent 在这里不执行任何动作。

---

## 4.1 顶层指导

```arp
"""
你是一个研究型 Agent。
不要编造来源。
"""

理解用户问题
生成回答
```

作用于整个文件。

---

## 4.2 函数指导

```arp
def summarize_doc(doc):
  """
  只总结文档内容，不评价立场。
  """

  阅读 {{doc}}
  输出摘要
```

只作用于该函数内部。

---

## 4.3 分支或循环指导

```arp
if user.need_research:
  """
  当前分支必须基于资料回答。
  """

  搜索资料
  综合结论
else:
  """
  当前分支只能基于已有上下文回答。
  """

  直接回答
```

只作用于当前分支。

---

# 5. 注释（仅文档）

所有 `#` 注释都在解析后被忽略，不会被 Agent 看到，也不会进入执行流程。

示例：

```arp
# 这是一条给读者看的说明，不参与执行
理解用户问题
```


---

# 6. 条件控制

```arp
if condition:
  ...
elif other_condition:
  ...
else:
  ...
```

示例：

```arp
if user.has_file:
  读取文件
  总结文件
elif user.has_url:
  打开链接
  提取信息
else:
  ask:
    请提供文件或链接
```

规则：

* 条件语法接近 Python。
* 只执行第一个为真的分支。
* `else` 可选。

---

# 7. 循环

`while`

```arp
while condition:
  ...
```

```arp
break
continue
return
```

语义：

* `break`：退出当前循环。
* `continue`：进入下一轮循环。
* `return`：结束当前函数；如果在顶层使用，则结束整个流程。

示例：

```arp
while True:
  执行下一步
  if 条件满足:
    break
```

建议解释器设置最大循环次数，防止死循环。

`for`

```arp
for item in items:
  ...
```

语义：

* `for` 用于遍历序列。
* 每一轮会暴露“当前元素”和“当前索引”给后续步骤（可用于 `{{item}}` / `{{item_index}}` 模板替换）。
* 迭代是否结束由运行时自动判断，不依赖 Agent 手工回传分支值。

---

# 8. `ask`

`ask` 用于向用户提问，并暂停当前流程，等待用户回答。

```arp
ask:
  你希望最终达成什么目标？
```

语义：

* Agent 向用户提出问题。
* 当前流程暂停。
* 用户回答后，流程从 `ask` 后继续。

---

# 9. 函数

使用 `def` 定义可复用流程块，使用时等同于展开函数体的内容到该位置。但注意：
- {{xxx}}会被替换为参数内容
- 函数签名`def f(a,b):`只是为了方便解析，不会被加入该位置

```arp
def analyze_source(source):
  """
  只分析单个来源，不做综合判断。
  """

  阅读 {{source}}
  提取事实、观点和不确定性
```

调用函数：

```arp
请求用户输入

analyze_source(输入内容)

输出 “分析已完成”
```

多行定义或多行调用（参数跨行）也合法，例如：

```arp
角色扮演(role=孩童🎨, goal='''
  这是一个长文本参数，
  可以跨多行书写。
''')
```

规则：
- 只要函数定义/调用的括号未闭合，后续行都属于同一条定义/调用语句。
- 三引号文本可作为参数值；其中内容按原样传入参数。

结果：

```arp

请求用户输入

"""
只分析单个来源，不做综合判断。
"""

阅读输入内容
提取事实、观点和不确定性

输出 “分析已完成”
```

规则：

* 函数可以有参数。
* 函数体可以包含 step、指导块、条件、循环、ask、函数调用。

---

# 10. 文件复用

`include` 表示原地展开整个文件。

```arp
include "./shared/safety.arp"
include "/prompts/common/research.arp"
```

语义：

> `include path` 等同于把目标文件内容插入当前位置。

示例：

```arp
理解用户问题

include "./shared/safety.arp"

生成最终回答
```

等价于：

```arp
理解用户问题

# ./shared/safety.arp 的内容展开在这里

生成最终回答
```

规则：

* 支持相对路径和绝对路径。
* 相对路径基于当前文件所在目录解析。
* `include` 不创建新作用域。
* 需要检测循环 include。

---

---

# 11. 错误模型

arp 默认使用 fail-fast 机制。

> 任意运行错误都会立即中止当前流程，并向用户报告错误位置和原因。

示例错误：

```text
流程执行中止。

错误位置：
  ./main.arp:12

错误原因：
  未定义变量：source
```

如果错误发生在 `include` 文件中：

```text
流程执行中止。

错误位置：
  ./shared/safety.arp:8

引用位置：
  ./main.arp:3

错误原因：
  未定义函数：check_policy
```

常见运行错误包括：

```text
变量未定义
函数未定义
参数数量不匹配
include 文件不存在
include 循环引用
条件表达式无法求值
break / continue 出现在循环外
return 出现在非法位置
ask 恢复失败
```

---

# 12. 最小语法草案

```text
program     := scope

scope       := (guidance_step | statement)*

guidance_step
            := triple_quote_text

statement   := implicit_step
             | explicit_step
             | ask
             | if
             | while
             | break
             | continue
             | return
             | include
             | def
             | func_call
             | comment

implicit_step
            := text_line

explicit_step
            := "step:" NEWLINE INDENT text_block DEDENT

ask         := "ask:" NEWLINE INDENT text_block DEDENT

if          := "if" expr ":" block
               ("elif" expr ":" block)*
               ("else:" block)?

while       := "while" expr ":" block

def         := "def" name "(" argument_list? ")" ":" block

include     := "include" string_path

func_call   := name "(" argument_list? ")"

argument_list
            := argument ("," argument)*
argument    := value | (name "=" value)

comment     := "#" text_line

block       := NEWLINE INDENT scope DEDENT
```

说明：
- `comment` 会在执行前被忽略。
- `name`、`value`、`expr`、`string_path` 作为词法单元由解释器处理，此处不展开。

容错：
- 条件、参数的写法不做任何限制
- def和func_call里的参数之间同时支持中英文逗号,，
- 各种语法都同时支持中英文冒号:：
- func_call 支持参数跨多行书写（包含三引号长文本）
- 若命名参数值未加引号且包含顶层逗号，编译器可将逗号后的片段并入前一个命名参数值，并给出 warning；建议对含逗号参数值使用引号或三引号，避免歧义
- 若片段无法安全归并，解释器/编译器应直接报错（fail-fast），不应静默改写

---

# 13. 完整示例

```arp
"""
你是一个研究型 Agent。
所有回答都要区分事实、判断和不确定性。
不要编造来源。
"""

include "./shared/safety.arp"

def clarify_requirement():
  """
  澄清用户需求。
  信息不足时优先提问，不要直接假设。
  """

  if not user.goal:
    ask:
      你希望最终达成什么目标？

  if not user.audience:
    ask:
      这个结果主要给谁看？

  if not user.format:
    ask:
      你希望输出为文档、表格、JSON，还是清单？

def analyze_sources(sources):
  """
  分析多个来源。
  每个来源先独立处理，再统一综合。
  """

  for source in sources:
    # 每个来源单独分析，避免交叉污染
    analyze_source(source)

  synthesize(sources)

clarify_requirement()

if user.sources:
  analyze_sources(user.sources)
else:
  ask:
    请提供需要分析的资料。

生成最终回答
```

---

# 14. 一句话总结

arp 的核心规则可以压缩成：

> 每行普通内容是一个 step；`step:` 把缩进块合成一个 step；`""" ... """` 是作用域级指导 step；`# ...` 仅供读者阅读且不参与执行；控制流接近 Python；`ask` 暂停等待用户回答；`include` 原地展开文件；`def` 定义可复用流程块；任意运行错误默认中止并报告位置和原因。

---

# 15. CFG 编译约定补充（实现侧）

ARP 编译到 CFG 时的约定见 COMPILER_GUIDE.md