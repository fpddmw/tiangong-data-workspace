# 安装与验证

在项目根目录创建 Python 虚拟环境并运行离线检查。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
make check PYTHON=.venv/bin/python
```

[Skill 清单](../skills-lock.json)固定项目内已提供的 Skill 及其上游版本。打开 [`.agents/skills`](../.agents/skills) 阅读各入口及工具要求。Node.js 和相应 CLI 只在对应 Skill 或宿主使用时安装。外部服务的密钥由本机安全配置提供。

公开检出不预载原任务、生产账本、真实账号或宿主连接。[运行配置示例](../workflows/business-flow/runtime.example.json)中的远端宿主默认禁用。需要 Codex Hook 时，检查 [示例配置](../.codex/hooks.example.json)中的解释器和工作目录后再安装。

公开版的 `make check` 检查仓库收录规则和凭据格式、核对 Skill 文件哈希，并运行可独立运行的离线测试组。测试源码中仍有依赖原项目记录、已安装宿主或私有运行状态的用例，完整私库套件需要相应环境。
