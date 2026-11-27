# 安装与验证

在项目根目录创建 Python 虚拟环境并运行离线检查。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
make check PYTHON=.venv/bin/python
```

[Skill 清单](../skills-lock.json)固定项目内已提供的 Skill 及其上游版本。打开 [`.agents/skills`](../.agents/skills) 阅读各入口及工具要求。Node.js 和相应 CLI 只在对应 Skill 或宿主使用时安装。外部服务的密钥由本机安全配置提供。

## 本机初始化

运行 `python3 bin/setup_local.py --init` 生成缺失的本机配置。按实际安装填写[运行配置](../.control/runtime.template.json)和[账号绑定](../.control/accounts.template.json)，在本机 `.env` 提供服务凭据。用 `python3 bin/setup_local.py --doctor` 检查。直接 CLI、受管 Skill、派工、诊断及失败记录 Hook 优先使用 `.control/runtime.local.json`，旧配置文件可回退读取。

## 配置受管派工

将[派工模板](../.control/programmatic-worker.template.json)复制为 `.control/programmatic-worker.local.json`，填写本机验证过的可执行文件、provider ID 和模型 ID。模板默认禁用宿主，两个角色均为只读。核实宿主和角色权限后再启用。默认绑定在 `.control/runtime.local.json` 中填写 `"programmatic_worker": {"path": ".control/programmatic-worker.local.json"}`。单次派工可用 `work-dispatch --config` 选择配置。需要 Codex Hook 时，检查[示例配置](../.codex/hooks.example.json)后再安装。

公开版的 `make check` 检查仓库收录规则和凭据格式、核对 Skill 文件哈希，并运行可独立运行的离线测试组。测试源码中仍有依赖原项目记录、已安装宿主或私有运行状态的用例，完整私库套件需要相应环境。
