# 当前送审与预检能力

`review submit/resubmit --input /ABS/REQUEST.json --dry-run --json` 检查当前可读前提。受管入口沿原采用和原生请求，宿主核授权、前像及准确关联范围后送审。0.4.5 的预检将服务端关联范围列为未核，当前具名逐根授权下，该根暂缓并保留原选择。只有服务端在写入时能约束到已授权的范围，才能恢复这类正式提交。[天工 CLI #548](https://github.com/tiangong-lca/cli/issues/548)。本人审核用 `review list --role submitter --json`，已知审核号用 `review get --id REVIEW_ID --json`。[当前安装](data-cli.md) · [受管送审](../../workflows/dataset-review/送审准备与实际送审.md#执行并核真实效果) · [预检资格记录](../../installation/maintenance-20261121-board/wave-execution-20261122.md)

读取平台可见意见，用 `review opinions --id REVIEW_ID --json`；只有对象版本时，用 `review opinions --type process --data-id UUID --data-version 01.00.000 --json`，按实际类型和版本替换。按版本查询时，检查 `completeness.hasMore`，有续页则沿原筛选传 `--cursor`。返回的是当前账号实际可见的意见；作者可读平台退回的 Process 意见，可见意见为空不能说明没有评审。正式作者对一条退回审核按两种入口读到同样的 3 条意见，且提交正文校验和与 `review get` 一致。[本机实际回验](../../installation/maintenance-20261119-cli-536/release-opinions-readback.json)

原 `qry_review_get_items` 的 HTTP 403／42501 是 OAuth 路由拒绝。当前部署已恢复配置作者的本人读取。用原样本在隔离环境复验，0.4.4 仍保留原 HTTP 状态、服务 code 与允许公开的授权原因。生产查询与原生产提交分别用各自回执判断。[Database #984](https://github.com/tiangong-lca/database/issues/984) · [CLI #373](https://github.com/tiangong-lca/cli/issues/373) · [隔离回验](../../installation/maintenance-20261119-cli-536/review-recovery-0.4.4.json)

主审接作者的完整候选、科学意见处理及保存读回，按照[送审准备与实际送审](../../workflows/dataset-review/送审准备与实际送审.md)组织获准提交并核实回执。此后由[轮询维护者](../../roles/platform-feedback.md)按包汇总平台意见。单次 submit 从一个准确对象开始并包含相关业务数据；多根集合、withdraw 等未开放能力按真实帮助处理。正文问题归作者修订，工具问题沿 board 返回维护者。

已知审核 ID 时查原记录及其实际关系。原请求恢复依赖同账号、同环境、同一份请求和原私有操作账本。隔离回验用 0.4.3 发送原请求、0.4.4 查询同一回执。查到后返回同一审核号，无回执或不可读时仍保留 `unknown`，恢复阶段零新增提交。原账本缺失时同一提交命令可能发送新请求，应继续沿原审核号或可归属的只读证据查证。使用 Platform 前，先核界面保存、计算或提交的真实副作用与授权，并完整读回提交正文。[原请求恢复与边界](../../installation/maintenance-20261119-cli-536/README.md#切换后的独立复核)

旧专用 `process review-preflight` 的实现与资格仅供[历史查证](../../work/reviews/dataset-candidate-review-workflow/engineering-readonly-preflight-20261002/README.md)，不作为当前额外前置流程。
