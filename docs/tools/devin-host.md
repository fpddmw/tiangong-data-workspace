# Devin / SWE-2 本地 worker

先用 `devin auth status` 与 `devin models list --format json` 核当前账号和可用模型。在单独的宿主配置中选择版本、工具能力及审阅配置，通过 `work-dispatch --config CONFIG --execute` 对已登记请求选择宿主。SWE-2 的选项用正式 ACP 核实。

新宿主不能冒充原DSH Session。为已有业务选择Devin时，主审先读原完整成果、当前范围和有效决定，再通过原 `work-request` 为明确未完成工作登记同任务的新委派并选择本配置；把仍在途、已停止或未知尝试先沿原run查清。原DSH请求和返回保留；不要把带DSH前驱Session的continuation链交给Devin。Devin内的恢复只用精确原run：

有原stop时按同一公共入口给 `--instructions 原负责人恢复说明.md --resume-stopped`；普通continue保留停止。管理进程退出、安静或EOF不等于子进程已结束；原执行和各终端/离线工具的OS启动身份及进程组都必须退出，原生Session也须无实际写锁，才可显式接续。未知写入保留原attempt，只读回原效果；重复派发只观察，不重放原输入或重建Session。

作者直接读原brief、验收终点、当前计划和原成果，在同一次原生调用中持续工作、自查、用 `htam` MCP的 `request_review` / `review_result` 请求独审及返修。MCP只是传输，五个协作工具调用原 `ControlledTools` 与 `ReviewService`，没有另一套审阅逻辑。独审在真实只读文件/命令权限和无网络工具环境下取证；普通final由运行器自动保存并登记run/return，作者和独审无需写 `.control` 或再执行 `work-return`。主审从原 `work-show --detail`、`work-packages` 和准确报告入口接回，仍负责科学采用与正式送审。

Devin当前没有已验证的Goal API；一次调用的结束、部分报告与父任务完成分别记录。宿主自行执行本次模型/工具迭代，harness不自动发送下一prompt。若本次调用结束而合同余项尚在，主审据原报告决定同Session接续、补输入或调整投入。
