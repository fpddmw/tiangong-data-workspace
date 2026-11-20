# Utilities 结果诊断

Utilities 为特定计算问题提供诊断证据。Agent 先明确要解释的结果、范围和假设，再选择工具；普通数据制作按原目标推进，系统诊断不成为所有工作包的前置流程。

## 当前可用性

2026-11-15：此 Mac 未安装 `lcia-auditor`。历史记录指向 `tiangong-lca/utilities`，当前连接查询该仓库返回404，无法确认可采用发行；这不证明仓库已删除。历史安装与验证保存在[原恢复记录](../../installation/restore-20261030-local-utilities/README.md)。当前尚未完成 Utilities 的本机安装和实际计算验收。

`launch.py utilities` 只选择 `TIANGONG_UTILITIES_BIN` 指定的可执行程序，或 PATH 中的 `lcia-auditor`，原样传递参数。上游负责命令帮助、输入契约与输出。没有本机安装时明确报出该缺口；CLI、AI 和原成果读取继续独立可用。旧机器逐文件资格清单、两命令白名单和重复报告字段门槛已经退出。

```sh
TIANGONG_UTILITIES_BIN=/ABS/lcia-auditor .venv/bin/python bin/launch.py utilities --help
```

## 使用结果

提供当前原包的准确输入和输出路径。比较前后结果时，由负责agent核用途、功能需求、范围、方法和来源是否可比较；缺少某项就说明该比较能支持什么、还缺什么。数值诊断和退出码提供该命令的证据，科学结论由作者和独审核实。

此 launcher 采用无账号凭据的环境，适用于离线分析。获准的联网操作使用上游明确的连接接口，核实际身份和数据范围；数据写入、发布和新计算仍分别依据具名授权及原请求。保留未知效果，不靠换工具重放。普通数据研制继续使用[公共 CLI／Skill](../../LOCAL_TOOLS.md)。上游安装或能力缺陷通过英文 issue 及正式更新维护。
