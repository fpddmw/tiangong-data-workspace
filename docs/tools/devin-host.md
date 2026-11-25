# Devin / SWE-2 本地 worker

负责人从原 `harness.py work-dispatch` 为一个原请求选择配置；原任务、完整范围、合同、成果、独审和返回继续由同一harness保管。CLI 路径以所选宿主配置和本机安装为准。适配器只接受标准SWE-2的high/medium/max精确ID，不使用Flash、Lightning、cloud、隐式最近Session或替代模型。账户资格以当次正式CLI为准，不把目录或配置当登录证明。

下面的 `DEVIN_RUNTIME` 指本次选择的 Devin 运行配置在当前工作区中的路径。

```sh
devin auth status
devin models list --format json
.venv/bin/python bin/harness.py work-dispatch --request REQUEST_ID \
  --config "$DEVIN_RUNTIME" --execute
```

该配置的作者与独审都使用独立的SWE-2 Session。恢复时沿用原运行绑定。新建或载入原 Session 时，适配器在发送 prompt 前核对原生配置，需要切换时调用 `session/set_config_option`。标准 Max 的族选择器可返回 `swe-2-high`，推理档位为 `thought_level=max`。有 Speed 控件时核对 `speed=standard`；无此控件时，从本次模型选项核对标准族与同族 Lightning 分列、当前选中标准族。无法区分时，原运行保留具体诊断。

Devin 制作与独审各有 6 席，按角色分别先进先出。原派工、原 Session 继续和独审都在启动 ACP 前取席。作者在受控 `review_result` 等意见时让席，意见交回模型前由同一运行排队重新取作者席。用 `.venv/bin/python bin/harness.py work-capacity --host devin` 看占用、排队和等审中的原运行。切换前已启动的运行按原执行租约计入，原进程自然结束。

若原任务选择DSH独审，改选同目录的 `swe-2-dsh-review.json`；Devin作者不占DSH席，DSH独审进入[独审容量池](production-host.md#dsh共享容量)。DSH另有6席worker池，同角色FIFO，总额12。负责人可为本次任务使用更窄的配置。全局 `.control/runtime.json` 的DSH绑定保持原选定值。

新宿主不能冒充原DSH Session。为已有业务选择Devin时，主审先读原完整成果、当前范围和有效决定，再通过原 `work-request` 为明确未完成工作登记同任务的新委派并选择本配置；把仍在途、已停止或未知尝试先沿原run查清。原DSH请求和返回保留；不要把带DSH前驱Session的continuation链交给Devin。Devin内的恢复只用精确原run：

```sh
.venv/bin/python bin/harness.py work-dispatch --request REQUEST_ID --execute \
  --config "$DEVIN_RUNTIME" \
  --continue --run-id ORIGINAL_RUN_ID
.venv/bin/python bin/run_worker.py --request REQUEST_ID --read-result --run-id ORIGINAL_RUN_ID
.venv/bin/python bin/run_worker.py --request REQUEST_ID --run-id ORIGINAL_RUN_ID --stop '明确停止依据'
```

有原stop时按同一公共入口给 `--instructions 原负责人恢复说明.md --resume-stopped`；普通continue保留停止。管理进程退出、安静或EOF不等于子进程已结束；原执行和各终端/离线工具的OS启动身份及进程组都必须退出，原生Session也须无实际写锁，才可显式接续。未知写入保留原attempt，只读回原效果；重复派发只观察，不重放原输入或重建Session。

`session/load`若有对应的原生拒绝响应且本次没有发出prompt或工具请求，读回显示`failed / not_started`、准确`target_session`及原锁持有者。它没有自动重放授权。负责人解决实际占用后，以拒绝run为`--run-id`显式continue；运行器沿原launch保留同一Session。单纯超时、EOF、prompt后断连或执行子孙未退出仍保留原保护。

正式ACP的`session/close`能由**加载该Session的同一连接**正常释放；正常持有者退出后，隔离同Session已实际以Max恢复。另一个连接不能代关桌面持有的Session。桌面 Session 仍被占用时，由原持有方正常释放，随后检查进程和写锁并按原运行接续。

作者交回后，主审读完整报告、候选/意见适用条件及具体未完依赖，按实际缺口决定同Session继续或执行保存/送审。一次审核get失败只约束该次读取；新submit、Flow保存、退回及正文恢复各查自身原输入、授权与回执。board记录不自动唤醒blocked主审，工程交回不等于业务已恢复。

作者直接读原brief、验收终点、当前计划和原成果，在同一次原生调用中持续工作、自查、用 `htam` MCP的 `request_review` / `review_result` 请求独审及返修。MCP只是传输，五个协作工具调用原 `ControlledTools` 与 `ReviewService`，没有另一套审阅逻辑。独审在真实只读文件/命令权限和无网络工具环境下取证；普通final由运行器自动保存并登记run/return，作者和独审无需写 `.control` 或再执行 `work-return`。主审从原 `work-show --detail`、`work-packages` 和准确报告入口接回，仍负责科学采用与正式送审。

Devin当前没有已验证的Goal API；一次调用的结束、部分报告与父任务完成分别记录。宿主自行执行本次模型/工具迭代，harness不自动发送下一prompt。若本次调用结束而合同余项尚在，主审据原报告决定同Session接续、补输入或调整投入。

可信launcher共用完整原生CLI证据收集器，原输入、stdout/stderr和本次输出留在原run目录，管理关联由宿主完成，ACP文件权限不开放管理库写入。后续保存轮复用原准确候选/意见/检查，补文件用公共`work-return --artifact`；自动关联故障不重放原操作。晚到独审在共同视图显示可取，作者以原review_id读取完整意见后另记该实际Session的投递，候选已改时仍保留原冻结版本及适用性边界。

文件读写经ACP客户端检查原工作区和明确写域；命令经macOS OS沙箱执行，独审候选不可写。原生grep/glob禁用，搜索使用沙箱terminal。作者的天工CLI使用htam MCP `tiangong_cli`，例如 `{"role":"editor","surface":"cli","argv":["dataset","process","get","--id","UUID","--version","EXACT","--output","/ABS/FILE.json","--json"]}`。同一实现也服务DSH和Codex；仅editor→cli及research→cli/ai，工具不接受可执行程序、cwd、环境或manager角色。宿主调用固定项目Python和原launcher，当前安装及账号选择仍由其负责，核`exit_code`、完整stdout/stderr和原生操作回执。新加载profile已移除shell文本识别提权；普通terminal不能读.control/.env或宿主凭据。离线`local_check`不承载凭据刷新；身份问题用`refresh_editor_identity`，不复制token或私有CLI/noenv wrapper。已在途进程保留原加载配置，由原负责人明确同Session接续后采用新能力，不为升级而重放数据动作。

每次调用使用独立的Devin配置文件和私有MCP socket，保留官方组织权限，不写全局bypass或workspace trust。作者与独审Hook复用原 `harness_snapshot` 的完整信息块/8KiB呈现预算。实测已观察到UserPromptSubmit/PostToolUse的派生缓存；压缩事件只令缓存失效，下一支持的工具/提示边界重取。真实压缩后模型接收尚未实测，不把配置存在或缓存写成端到端投递确认。

