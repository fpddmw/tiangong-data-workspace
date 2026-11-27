# 查找和保管工作材料

从原委派找这次用过的文件。作者保管科学正文，原生工具保管 Session、每次运行及写入回执，主审决定成果用于什么地方。

<a id="retrieval"></a>
## 找一份原件

先查具体指名的委派、候选、来源或回执。用 `bin/context.py --task ID`、`--request ID`、`--query TERM` 或 `--scope ID` 定位。索引缺项时，从已知包目录和出处用 `rg` 查找，必要时用 `rg --no-ignore-vcs` 查未入 Git 的科学材料。凭据和原生数据库单独保管。本机旧资产目录的可用情况见[历史说明](../../.control/HISTORY.md)。如果取到的是 LFS 指针，按[取回说明](versioning.md#本机准确取回-lfs-原件)拉取所需对象。要查历史交接，用 `bin/harness.py history`；当前接续走[原任务](../../workflows/business-flow/README.md)。

<a id="placement"></a>
## 保存到哪里

| 材料 | 保存位置 |
| --- | --- |
| 岗位说明、共用规则及可复用的方法 | `roles/`、`docs/`、`workflows/` |
| 须由本地保管的来源正文 | `materials/sources/` |
| 本包的委派、推导、差异和审阅证据 | 原包目录，通常在 `materials/packages/` 或已有的 `work/` 目录 |
| 独立的新目标、可复用研究、特定日期的观察 | 按用途放 `projects/`、`research/`、`reports/` |
| 导出与可重做的中间件 | `outputs/`、`tmp/` |
| 共享工具、配置、安装和验证材料 | `bin/`、`.control/`、`tests/`、`installation/` |

<a id="project-and-package-entries"></a>
## 原包和现行计划


同一件事使用原来的阅读入口。原委派或已有索引足够时，接手人直接从那里读目标、当前决定、材料和余项。更新前先读当前版本，把过期进度替换掉，另外保留冻结承诺。当改动影响验收终点时，由负责人沿原任务记录变化。另一个使用者需要同一份材料，直接链接原件。

<a id="topic-delivery-views"></a>
需要跨包查资料时，可以从[主题索引](../topics/README.md)或[主题示例](../topics/topic-template.md)出发。把当前进展和覆盖限制链接回负责的包；委派要求计算系统结果时才应用[系统规划方法](../../workflows/dataset-review/system-planning.md)。

<a id="historical-pointers"></a><a id="finish-extend-or-relate-an-operation"></a>
## 接续和结束

按原委派、原权限和实际运行状态判断可做的事。以前的索引、保存日期和旧计划只能帮助查找当时的材料。新增年份或多了一个消费者时，原件可继续共享。有独立目的、负责人及验收终点时再开新任务。

<a id="relocation"></a><a id="retention-and-cleanup-events"></a><a id="navigation-and-versioning"></a>
## 迁移、清理与版本

需迁移时，核对引用路径、字节及真实调用者。原生工具保留原 Session、尝试和未知效果。账号、安装、锁和正在使用的数据库通过原工具恢复。保留已采用的科学依据、完整读回、冻结稿和恢复历史；可重复生成且无人在引用的中间件才清理。提交共享源码、文档或获准证据前按[版本保管说明](versioning.md)核实际暂存内容。
