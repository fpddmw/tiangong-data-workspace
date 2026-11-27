# 工具使用与能力入口

- [任务入口与阶段上下文（协调维护）](deepseek-data-workflow.md)：协调者选包/阶段并给出作者视图，核实际路径和配套资格；有界作者从原 brief 与 data 角色进入，当前不启动试点。
- [持续执行宿主](production-host.md)：实际已实现的目标定位、暂停及宿主资格边界。
- [Devin 宿主适配](devin-host.md)：ACP 会话接入与隔离边界。
- [持久交接](persistent-handoff.md)：真实负责人交接、原尝试和原件。
- [候选初始化](candidate-initialization.md)：新建/保留 UUID 的正式使用说明。
- [送审预检能力现状](review-preflight.md)：公开 CLI 的实测服务边界与主审的 Platform 接续。
- [Utilities 结果诊断与修复验证](utilities.md)：`lcia-auditor` 的适用阶段、联网/写入边界、原生调查与 Foundry 往返；两条离线命令已受管验证，其他路径未开放。
- [当前本地工具接口](data-cli.md)：实际调用方式和边界。
- [原 Task 和显式本地工具](legacy-local-tools.md)：准确原绑定和历史功能。
- [安装与资格入口](../../installation/NAVIGATION.md)：本机运行时绑定模板及升级摘要；一次性升级/调试日志保存在本机 `.gitignore` 范围，不入 Git。
- [科学方法入口](../../workflows/dataset-review/README.md)：送审前检查和相关工具映射。
- [上游工具维护与回验](engineering-dependencies.md)：本仓共享基建由维护者直接修复回验；天工上游仓库缺陷以英文脱敏 Issue 为先并跟踪官方新版本。

新长期使用说明维护在本目录，独立工程观察报告放 `reports/tooling`。现有安装包、原生资格与恢复路径原位保持。
