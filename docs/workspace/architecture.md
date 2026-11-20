# 工作区架构与维护接口

Agent 对完整结果负责。总主安排父目标和投入，主审推进整包、协调依赖并承担采用与平台送审，作者在原 Session／Goal 内制作、自查、独审返修，独审提供可核科学意见，维护者修共享能力。职责原文见[角色入口](../../roles/README.md)，当前实施和未决边界见[唯一计划](agent-autonomy-plan.md)。

harness 保存任务、明确动作、原文、Session／attempt、工具回执和停止事实；读取这些事实不需要恢复旧工具版本或原聊天登记。调用者选择当前工具，原生系统承担其命令与数据契约。实际操作核账号、对象、权限、并发和未知效果，管理标签、报告格式及目录名称不决定科学资格或协作权限。

## 日常接口

```sh
.venv/bin/python bin/harness.py work-maintenance
.venv/bin/python bin/harness.py work-packages
.venv/bin/python bin/context.py --task TASK_ID
.venv/bin/python bin/run_worker.py --request REQUEST_ID --read-result
```

[业务入口](../../workflows/business-flow/README.md)记录明确的委派、采用和接续；[生产宿主](../tools/production-host.md)执行原生调用。索引和结构摘要供定位，原报告和未能解释的原文始终可读。技术运行成功、Goal结束、科学交付及平台完成分别依据各自事实。

## 原件和工具

在同一工作目录直接读原件。已有包原址继续；必要新材料按[NAVIGATION](../../NAVIGATION.md)选择位置，在原任务链接。共享消费者复用同一来源。存储登记、生命周期表单与兼容迁址程序已退出执行，历史记录保持原址和Git可查。

[本地工具](../../LOCAL_TOOLS.md)选择当前安装；只初始化实际调用的工具。安装和解析缺口限制对应能力，已有成果和任务不依赖旧安装。保留具名写入授权、原数据操作及恢复状态，工具升级不重放写入。天工系列上游缺陷经英文 issue 和正式更新解决；其他工具的本地接入由维护者负责，对外协作按用户另行委派执行。

旧 handoff、管理阶段、上下文装配和控制协议不再作为执行入口。实际搬迁或换机使用所属工具支持的恢复方式，保全原 Session／attempt、停止与未知效果。版本、备份与LFS材料见[版本管理](versioning.md)。
