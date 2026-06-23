# Local GPT API Proxy

一个轻量、本地优先的多供应商 API 代理。你可以把多个 API Key 集中配置到本机，然后让 OpenAI 兼容客户端统一调用 `http://127.0.0.1:8000/v1`。

项目重点不是做“大而全平台”，而是解决个人本地使用时最麻烦的三件事：

- 多个 API 自动按优先级回退。
- 同一个供应商多个 Key 自动轮询。
- 在 UI 中管理配置、测试连通性、查看请求日志。

## 已支持能力

- **OpenAI 兼容接口**：`POST /v1/chat/completions`、`POST /v1/responses`、`GET /v1/models`
- **Claude 原生接口**：`POST /v1/messages`
- **Gemini 原生接口**：`POST /v1beta/models/{model}:generateContent`
- **国内大模型**：DeepSeek、通义千问兼容模式、智谱 GLM 等 OpenAI 兼容入口
- **轻量智能路由**：保留优先级配置，同时根据 429 冷却、成功率和平均耗时暂避不稳定 provider
- **路由预览**：管理台和 `/api/routing/preview?target=chat` 可查看下一次请求会优先尝试哪个 provider，并提示停用、缺 Key、协议不匹配等未参与原因
- **流式输出**：支持 `stream: true`，原样透传 SSE
- **多 Key 轮询**：成功调用后自动切换到下一个 Key，`429` 后进入冷却
- **模型获取与覆盖**：可获取单个 provider 模型、批量同步，也可查看每个模型由哪些 API 覆盖
- **Provider 健康状态**：显示成功率、平均耗时、最近状态和 429 Key 冷却倒计时
- **请求日志**：显示 provider、状态码、耗时、回退次数、选择理由、流式状态和错误摘要，并可一键清空本地观测数据
- **请求统计**：聚合展示总尝试、成功/失败、平均耗时、常用模型和常用 provider，可随日志一起重置
- **客户端 Key**：可给不同本地客户端单独发 Key，并限制允许/排除的模型
- **供应商模板**：内置 OpenAI、DeepSeek、Qwen、GLM、Moonshot、OpenRouter、Claude、Gemini 等常用模板
- **安全选项**：本地访问 Token、请求限流、请求体大小限制、配置密钥加密
- **Web 管理台**：添加、编辑、删除、导入、导出、脱敏导出、测试 API 配置

> 说明：本项目不做 OpenAI / Claude / Gemini 之间的协议转换。不同协议使用各自原生端点。

## 快速启动（推荐 uv）

```powershell
cd C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy
$env:UV_CACHE_DIR="C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy\.uv-cache"
uv sync
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

启动后打开：

```text
http://127.0.0.1:8000/
```

确认本地服务是否健康：

```powershell
.\scripts\smoke.ps1
```

如果启用了 `GPT_PROXY_ACCESS_TOKEN`，脚本会自动读取 `.env`；也可以显式传入：

```powershell
.\scripts\smoke.ps1 -Token "your-local-proxy-token"
```

Windows 上也可以双击：

- `start-ui.bat`：启动服务并打开管理台
- `stop.bat`：停止本项目占用的服务进程

如果需要固定本地访问 Token、限流或配置加密，可以复制 `.env.example` 为 `.env` 后填写；`start.ps1`、`start-ui.ps1` 和 Docker Compose 都会读取它。

## 本机统一调用方式

OpenAI 兼容客户端（Codex++、ChatBox、OpenAI SDK 等）一般填写：

```text
Base URL: http://127.0.0.1:8000/v1
API Key: local-proxy
```

如果设置了 `GPT_PROXY_ACCESS_TOKEN`，这里的 API Key 要填你的本地代理访问密钥。

Python 示例：

```python
from openai import OpenAI

client = OpenAI(
    api_key="local-proxy",
    base_url="http://127.0.0.1:8000/v1",
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

## 协议与端点

| 类型 | 本机端点 | 适用场景 |
| --- | --- | --- |
| OpenAI / 国内兼容 | `/v1/chat/completions` | OpenAI SDK、DeepSeek、Qwen 兼容模式、GLM 兼容模式 |
| OpenAI Responses | `/v1/responses` | OpenAI Responses API |
| Claude 原生 | `/v1/messages` | Anthropic Claude Messages API |
| Gemini 原生 | `/v1beta/models/{model}:generateContent` | Google Gemini API |

## UI 配置建议

打开 `http://127.0.0.1:8000/` 后，在「API 配置」中：

1. 点击「常用模板」或「按协议添加 API」。
2. 填写 `Base URL` 和 `API Keys`，多个 Key 每行一个。
3. 模型可以手填，也可以点击「获取模型」后选择。
4. 优先级数字越小越先用，质量更高或更稳定的 API 建议排前面。
5. 点击「保存配置」或「保存并测试全部」。
6. 点击「刷新模型覆盖」可以查看不同 API 对模型的覆盖关系。

配置保存后，API Key 不会在页面回显；留空保存会保留旧密钥，输入新 Key 会替换旧密钥。

「导出配置」会包含真实密钥，适合本机备份；「导出脱敏配置」不会包含真实 provider/client key，适合排查问题时分享配置结构，不能作为恢复备份导入。

## 本地客户端 Key

在「安全设置」里可以新增本地客户端 Key。配置后：

- `/v1/*` 调用需要携带 `Authorization: Bearer <client-key>` 或 `x-api-key: <client-key>`
- 每个 Key 可设置 `allowed_models` 和 `excluded_models`
- 模型规则支持通配符，例如 `gpt-*`、`gpt-image-*`
- 如果没有配置客户端 Key，仍保持原来的本地开放模式
- 客户端 Key 只用于 `/v1/*`，不会打开管理端 `/api/*`
- 如果设置了 `GPT_PROXY_ACCESS_TOKEN`，该环境变量 Key 可访问 `/v1/*` 和 `/api/*`；管理台会显示管理端与 V1 的当前认证模式

## 常用供应商示例

```json
{
  "providers": [
    {
      "name": "deepseek",
      "protocol": "domestic",
      "base_url": "https://api.deepseek.com/v1",
      "model": "deepseek-chat",
      "priority": 0,
      "enabled": false,
      "api_keys": [],
      "model_aliases": {
        "gpt-4o": "deepseek-chat",
        "gpt-3.5-turbo": "deepseek-chat"
      }
    }
  ],
  "client_keys": [
    {
      "id": "chatbox",
      "label": "ChatBox",
      "key": "",
      "enabled": false,
      "allowed_models": ["gpt-4o", "gpt-4o-mini"],
      "excluded_models": ["gpt-image-*"]
    }
  ],
  "default_model": "gpt-4o"
}
```

公开模板文件是 `config.example.json`。真实本地配置写入 `config.json`，该文件已被 `.gitignore` 忽略，不应提交到 GitHub。
`config.example.json` 默认不启用 provider，也不包含占位密钥；复制后请先填入真实 API Key，再启用对应 provider 或客户端 Key。

## 可选环境变量

```powershell
$env:GPT_PROXY_ACCESS_TOKEN="your-local-proxy-token"
$env:GPT_PROXY_RATE_LIMIT_PER_MINUTE="60"
$env:GPT_PROXY_MAX_REQUEST_BYTES="2097152"
$env:GPT_PROXY_KEY_COOLDOWN_SECONDS="60"
$env:GPT_PROXY_CONFIG_SECRET="your-config-encryption-passphrase"
$env:GPT_PROXY_CONFIG="C:\path\to\config.json"
$env:GPT_PROXY_STATE="C:\path\to\state.json"
$env:LOG_LEVEL="INFO"
```

- `GPT_PROXY_ACCESS_TOKEN`：保护 `/v1/*` 和 `/api/*`
- `GPT_PROXY_RATE_LIMIT_PER_MINUTE`：本地每分钟请求限制，`0` 表示关闭
- `GPT_PROXY_MAX_REQUEST_BYTES`：请求体大小限制，默认 `2 MB`
- `GPT_PROXY_KEY_COOLDOWN_SECONDS`：Key 返回 `429` 后的冷却时间
- `GPT_PROXY_CONFIG_SECRET`：启用后将 API Key 加密写入 `config.json`
- `GPT_PROXY_CONFIG`：自定义配置文件路径，默认是项目目录下的 `config.json`
- `GPT_PROXY_STATE`：自定义运行状态文件路径，默认是项目目录下的 `state.json`
- `LOG_LEVEL`：服务日志级别，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR`

## Docker 可选部署

```powershell
New-Item data -ItemType Directory -Force
copy config.example.json data\config.json
docker compose up --build
```

复制后先在管理台或 `data/config.json` 中填入真实 API Key 并启用 provider；模板默认保持禁用，避免占位密钥被误当成真实凭据。

Docker 版本默认监听：

```text
http://127.0.0.1:8000/
```

Docker Compose 会把本机 `./data` 挂载到容器 `/data`，配置和状态分别写入 `data/config.json` 与 `data/state.json`。`.dockerignore` 会排除 `config.json`、`state.json`、`.env`、`data/` 等本地敏感文件，避免真实密钥被打进镜像。

## 开发与测试

```powershell
uv sync
uv run python -m pytest -q -p no:cacheprovider
node --check static\app.js
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-powershell.ps1
```

服务启动后可以运行本地冒烟检查：

```powershell
.\scripts\smoke.ps1
```

默认冒烟检查只访问本地健康、配置和路由预览接口，不会请求上游 provider 的模型接口。需要一起验证 `/v1/models` 和模型覆盖时再加：

```powershell
.\scripts\smoke.ps1 -IncludeUpstream
```

如果仓库配置了 CI，建议在 push 和 pull request 时运行 Python 测试、前端 JavaScript 语法检查、PowerShell 脚本检查、无上游请求的本地服务冒烟检查，以及 Docker 镜像构建。

## 停止服务

推荐双击 `stop.bat`。如果使用命令行：

```powershell
.\stop.ps1
```

如果端口仍被占用，可以检查：

```powershell
netstat -ano | findstr :8000
```
