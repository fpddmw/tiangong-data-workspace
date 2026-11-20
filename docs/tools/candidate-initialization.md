# 本地候选与身份

作者先确认是复用、修订现有对象，还是新科学对象，再按 `dataset <type> contract` 编写规范正文。已有对象保留准确 UUID／版本；新对象选择一次身份并在整个修订过程中复用。CLI `create` 是远端创建动作，不是本地初始化命令。

在原包保存候选，使用 `dataset <type> validate --input /ABS/CONTENT.json --json` 核当前合同。SDK 工厂可用于明确的本地编程任务；普通作者无需创建初始化收据或构建计划。早期 `dataset candidate init`、build-plan 和本地私有模块适配器已退出当前入口。

新建与修订的真实权限、前像及未知恢复见[保存与读回](../../workflows/dataset-review/保存与读回.md)。原候选及初始化历史记录保留，接续不重新分配身份。
