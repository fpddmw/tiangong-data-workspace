# 负责人交接

原任务已记录负责人、运行和成果。交接直接使用这些原件，先核实际新负责人和原用户授权，确认旧写者在安全点且没有未知效果。明确暂停后由原总主执行 `work-transfer --task ID --owner NEW_THREAD_ID --note 原交接依据.md`；新负责人接续原计划和成果，恢复工作另按实际授权执行。

跨账号凭据不随管理职责迁移。技术身份来自实际宿主，不能通过改名、伪造环境变量或新请求替换未知原尝试。当前命令见[业务入口](../../workflows/business-flow/README.md)和[原生运行](production-host.md)。

旧 claim、generation、令牌和 Desktop 信箱协议退出执行；原记录保留在原库与 Git 历史，可用 `harness.py history` 查询。
