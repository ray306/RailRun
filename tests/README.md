# Agent Testing Guide

本目录包含两类测试：

1. 协议契约测试：`test_runtime_contract.py`
2. Agent 协作流程测试：`test_agent_weather_flows.py`
3. `For` 节点专项测试：`test_for_node.py`

## 你要做什么

1. 运行全部测试：

```shell
python -m unittest discover -s tests -p "test_*.py" -v
```

2. 若只验证 Agent 协作流程，运行：

```shell
python -m unittest discover -s tests -p "test_agent_weather_flows.py" -v
```

## 你要看什么

1. `OK` 且所有用例通过。
2. 失败时先看失败类型：
- `ValidationError`：通常是分支回传时机/类型错误。
- `HumanInterferenceRequest`：通常是重复错误触发重试上限。
- `did not reach Finished`：通常是循环分支推进逻辑异常。

3. 在 weather 协作测试里，重点确认：
- Sequential + call 能推进到 `Finished`。
- Script 分支能按上下文走到预期建议。
- Multi-weather 流程确实发生循环迭代并给出汇总结论。

## 通过标准

1. 所有测试通过。
2. 关键流程能到 `Finished`，且无意外 `ValidationError` / `HumanInterferenceRequest`。

## 如果失败

1. 先复现单个失败用例（优先用单文件运行）。
2. 检查对应 `examples/*.rail` 是否被改动。
3. 检查 `scripts/rail_compiler.py` 的分支/循环编译逻辑，以及 `scripts/session_runtime.py` 的分支推进逻辑。
