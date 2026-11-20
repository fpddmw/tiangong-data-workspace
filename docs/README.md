# 长期操作文档

只在对应工作发生时读取。角色职责在 `roles/`，机器流程在 `workflows/`；本目录不保存运行状态或逐次排障日志。

- [工作区架构与维护](workspace/architecture.md)：放置、晋升、检索、保留及回退入口。
- [个人私有仓库与恢复](workspace/versioning.md)：Git 收录、自检、依赖及新机器恢复。
- [候选初始化](tools/candidate-initialization.md)：SDK 创建、固定 UUID、修订与原生接入。
- [工具入口](tools/README.md)：运行时资格、安装及送审预检说明。
- [数据约定](data/README.md)：原则、状态/版本、Model 派生和分类。
- [机器流程与人读说明](../workflows/dataset-review/README.md)。
