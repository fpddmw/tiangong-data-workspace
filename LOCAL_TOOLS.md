# 本地数据工具

工作区当前绑定官方 LCA CLI 0.4.4／SDK 0.8.0 和天工 AI CLI 0.0.64；直接 launcher、普通 Skill 和受控 worker 均已核对。原生管道仍可触发 #536，受管保存沿已验证的普通文件描述符传输。文件参数使用原包绝对路径。[本轮安装、复现与受管入口回验](installation/maintenance-20261119-cli-536/README.md)

## 同一公共入口

受控作者的原生工具使用 `tiangong_cli`（Devin为htam MCP同名工具），例如 `{"role":"editor","surface":"cli","argv":["auth","whoami","--json"]}`。具备原native_cli能力的作者可用editor→cli或research→cli/ai；独审仅有只读候选及离线local_check。argv只含公共CLI参数，文件用绝对路径和正式`--output`，不传解释器、shell、cwd或env。宿主以项目Python调用原launcher，按当前绑定选版本、保留完整原run证据；核`exit_code`及完整输出路径，工具被接收不等于CLI成功。普通terminal仍不能读.control/.env或凭据，不再靠shell命令拼写取得额外权限。已在途原进程保留原加载配置，新能力在原负责人下一次明确接续时采用；不要因此重放已成功或未知写入。下列命令仍是维护者和人工公共入口。

```sh
.venv/bin/python bin/launch.py --role research cli --help --json
.venv/bin/python bin/launch.py --role editor cli dataset process get --help --json
.venv/bin/python bin/launch.py --role research ai research search --help
.venv/bin/python bin/lca_gate.py --doctor
```

`research` 提供本地处理及已配置研究服务；`editor`／`manager` 选择各自原账号。普通 Skill 通过 `bin/env_run.py` 使用相同公共 CLI 和 Node，例如：

```sh
.venv/bin/python bin/env_run.py research --without-project-env -- node \
  .agents/skills/scripts/run-cli.mjs --runtime-info
.venv/bin/python bin/env_run.py editor -- tiangong-lca dataset contact get \
  --id UUID --version EXACT_VERSION --output /ABS/CONTACT.json --json
```

普通 Skill 无需另交安装资格文件。安装信息在 `.control/runtime.json`，Skill 文件来源在 `skills-lock.json`。`--without-project-env` 只用于本地研究检查；正常账号继续使用原 session。旧任务账本保留在 `.harness-local/cli-state`；当前原生写入账本位于 `~/.local/state/tiangong-lca/dataset-operations` 和 `review-operations`，宿主仅为 executor 开放这些准确目录，不扩大到凭据目录。受控 `tiangong_cli` 以 editor 调用 `dataset model result-group` 时，另开放同级 `model-result-groups` 给正式 CLI 写入及读回成组保存的恢复文件。原 Foundry Task 继续使用自己的 manifest、runtime、attempt 和公共动作，`launch.py foundry` 保有原绑定。DSH Session／Goal 不因本次 CLI 升级重建。

## 从原件自主制作与修订

在 `.agents/skills/<名称>/SKILL.md` 读取当前动作的方法。`source-evidence-dataset-development` 支持证据研究；`process-automated-builder` 支持作者自己制作 Process；`current-account-dataset-review` 支持精确查看和修订。这些 Skill 调用原子能力，科学判断、迭代与完整交付由 agent 负责。

```sh
.venv/bin/python bin/launch.py --role research cli dataset process contract \
  --include schema,methodology,ruleset --output /ABS/CONTRACT.json --json
.venv/bin/python bin/launch.py --role research cli dataset process validate \
  --input /ABS/PROCESS.json --output /ABS/VALIDATION.json --json
.venv/bin/python bin/launch.py --role research cli dataset process list-references \
  --input /ABS/PROCESS.json --limit 100 --json
```

类型为 `process`、`flow`、`model`、`source`、`contact`、`flowproperty`、`unitgroup`、`lciamethod`，动作因类型而异。`get --output` 写出规范正文；列表及标准结果信封不直接当校验正文。`--output` 的完整报告与 stdout 的 `data`、`completeness` 一起读。批量原件可用 [候选报告](workflows/dataset-review/门禁与引用检查.md)；数量、单位或分母变化按同页完整修订方法同步说明与报告。

用 `list-references` 取得带路径的直接引用，按准确类型、UUID、版本 `get` 支撑，再逐层定位间接缺件。需要在线引用准入时显式选择 `validate --checks schema,business,references`；离线与在线覆盖分别说明。已存在旧版本引用的在线准入受 [CLI #504](https://github.com/tiangong-lca/cli/issues/504) 限制：其精确正文可读，但存在新版时会返回 `version_outdated`。保留作者选择及原件，将受影响保存交上游修复；本地制作、独审和其他对象继续。新版未提供旧 `qa process` 的单位量账；均值／结果量冲突仍受 [CLI #425](https://github.com/tiangong-lca/cli/issues/425) 影响，作者从原件核其关系和数量计算。

0.4.0 的 Process business 检查新增交换引用名称核对。离线调用通过 `--reference-documents /ABS/REFERENCES.json` 提供 `{ "documents": [...] }` 内的精确 Flow 正文；原候选报告的 `--reference-rows-file`／`--support-rows` 也可传同批支撑。报告区分缺证据的 `unresolved` 与名称不符的具体路径，作者对照选定 Flow 的完整名称处理当前候选。原成果和旧报告保留其实际覆盖，工具版本变化本身不撤销科学采用。

Process 精确读取用 `dataset process get`。CLI 0.4.1 的回执在 `data.subject.model_id/model_version` 给出持久关联，LS 原件实读到准确 Model 与版本，A09 独立草稿实读两个 null。历史回执缺字段时保留当时的读取范围。[原件复验](installation/maintenance-20261116-noon/cli-0.4.1-20261117/model-skill-owner-revalidation.json)

Model 本地生成用 `dataset model build-resulting-process`，按原请求读取 `bundle.projected_processes` 的准确成员。CLI 0.4.1 在 LS 原请求保留了全部 27 项交换、Model 自述、用途和权利；生成说明另有追加。已保存结果组用 `dataset model result-group` 的 `action: inspect` 读取原 Model 和成员快照。现行 `lifecyclemodel-resulting-process-builder` 的 `--result-group` 入口已在准确线上版本验证只读 inspect。整组保存、恢复与送审沿各自授权和回执。[原件复验](installation/maintenance-20261116-noon/cli-0.4.1-20261117/model-skill-owner-revalidation.json)

0.4.0 的本地 Model 请求可在原 `semantic_context.flow_documents` 提供精确 Flow 正文，用于生成和核对交换名称；`process_sources.allow_remote_lookup` 为真时允许公共入口精确读取 Process 和 Flow。选择本地或在线依据原任务条件。原 LS 请求仍输出相同27条交换事实，但缺少 Flow 证据时资格明确为 `unresolved`，这不抹去已有结果或授权重新计算远端数据。

## 账号、保存与送审

`auth whoami --json` 或 `auth identity-receipt --json` 返回公开身份，`context.actor.id` 和 `context.environment` 对应新原生请求的 `expectedActor`／`expectedEnvironment`，不再需要旧保存合同的邮箱字段。核既定账号可显式传 `--expected-project-ref` 与 `--expected-user-id`；凭据由 CLI 处理。

新建用 `dataset <type> create`，修已有草稿用 `save`。原请求包括准确身份、完整 `content`、稳定 `requestId`、预期 actor/environment；`save` 另含鲜读 `expectedContent`。先用同一原请求 `--dry-run`，实际执行时去掉它；没有旧 `--commit`。后续未知结果保留原请求查证，成功写入不重放。见 [保存与读回](workflows/dataset-review/保存与读回.md)。

读回保存到新的证据文件；确需更新已有快照时显式用`get --output /ABS/FILE.json --force --json`。当前CLI遇已有输出文件会返回exit 1、`status:partial`和`OUTPUT_UNAVAILABLE`，stdout仍可包含新鲜远端正文，磁盘文件却保持旧内容。核退出码、完整warnings及本次实际输出后再采用文件；管道末端成功或旧路径存在都不证明读回已落盘。修改过正文属于新的明确保存动作，保留此前成功requestId及操作回执。

受harness管理的原run通过可信launcher或普通Skill子进程调用CLI时，共用`delivery_evidence.py`留完整输入、stdout/stderr及本次新输出，原`delivery/operation-*/record.json`由宿主自动关联。普通CLI不要求run上下文，原退出结果不因归档/索引故障改变；原run重读可补关联，也可用`work-return --artifact /ABS/record.json`或准确候选/回执文件。没有lca-gate外壳的原生完整报告同样可用；stdout明确截短时读其完整输出原件，不从预览判通过。自动归档不扩大worker的管理库权限，不授予远端写入。

主审负责获准送审和回执核实。送审后的平台意见由用户指定的[轮询维护者](roles/platform-feedback.md)定期汇总给各主审。用 `review list --role submitter` 查本人记录，用 `review get --id` 查已知审核号。0.4.4 用 `review opinions` 按审核号或准确数据集版本读取账号可见意见，见[命令和可见范围](docs/tools/review-preflight.md)。

新提交须有当前具名授权，`review submit/resubmit --dry-run` 检查可读前提，正式准入由服务端决定。原效果为 `unknown` 时，先确认原私有操作账本及同账号、同环境、同一份请求；0.4.4 已隔离复验沿原请求只读查审核号，查不到可信回执仍为 `unknown`。原账本缺失时，同一命令可能进入初次提交路径，继续沿原审核号或可归属的只读证据查证。[送审与原请求恢复](workflows/dataset-review/送审准备与实际送审.md)

## 证据研究

已装 `tiangong-kb-sci-search`、`tiangong-kb-report-search`、`academic-paper-download`、`document-granular-decompose`。用 `bin/env_run.py research` 调用其脚本；SCI／Report wrapper 通过已绑定 `TIANGONG_AI_CLI_BIN` 使用 0.0.64，不临时下载旧版。论文下载按 Skill 的独立 Python runtime 使用。项目未配置 SCI、Report、Unstructured 服务凭据时，在线调用是明确的外部边界；可继续公开来源研究、已下载材料和本地处理。

Flow 治理、生命周期、语言专项、删除、发布等继续读对应已装 Skill 及 [实际授权](AGENTS.md#权限和恢复)。历史工具说明见 [原绑定记录](docs/tools/legacy-local-tools.md)；Utilities 见 [专用入口](docs/tools/utilities.md)。历史回执只证明当时结果，当前命令以当前帮助和实际运行结果为准。
