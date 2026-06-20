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
- **自动回退**：遇到 `403`、`429`、`5xx` 或网络错误时尝试下一个 Key / provider
- **流式输出**：支持 `stream: true`，原样透传 SSE
- **多 Key 轮询**：成功调用后自动切换到下一个 Key，`429` 后进入冷却
- **模型获取**：可在 UI 中获取单个 provider 模型，也可批量同步
- **请求日志**：显示 provider、状态码、耗时、回退次数、流式状态和错误摘要
- **安全选项**：本地访问 Token、请求限流、请求体大小限制、配置密钥加密
- **Web 管理台**：添加、编辑、删除、导入、导出、测试 API 配置

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

Windows 上也可以双击：

- `start-ui.bat`：启动服务并打开管理台
- `stop.bat`：停止本项目占用的服务进程

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

配置保存后，API Key 不会在页面回显；留空保存会保留旧密钥，输入新 Key 会替换旧密钥。

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
      "enabled": true,
      "api_keys": ["sk-your-key"],
      "model_aliases": {
        "gpt-4o": "deepseek-chat",
        "gpt-3.5-turbo": "deepseek-chat"
      }
    }
  ],
  "default_model": "gpt-4o"
}
```

公开模板文件是 `config.example.json`。真实本地配置写入 `config.json`，该文件已被 `.gitignore` 忽略，不应提交到 GitHub。

## 可选安全环境变量

```powershell
$env:GPT_PROXY_ACCESS_TOKEN="your-local-proxy-token"
$env:GPT_PROXY_RATE_LIMIT_PER_MINUTE="60"
$env:GPT_PROXY_MAX_REQUEST_BYTES="2097152"
$env:GPT_PROXY_KEY_COOLDOWN_SECONDS="60"
$env:GPT_PROXY_CONFIG_SECRET="your-config-encryption-passphrase"
```

- `GPT_PROXY_ACCESS_TOKEN`：保护 `/v1/*` 和 `/api/*`
- `GPT_PROXY_RATE_LIMIT_PER_MINUTE`：本地每分钟请求限制，`0` 表示关闭
- `GPT_PROXY_MAX_REQUEST_BYTES`：请求体大小限制，默认 `2 MB`
- `GPT_PROXY_KEY_COOLDOWN_SECONDS`：Key 返回 `429` 后的冷却时间
- `GPT_PROXY_CONFIG_SECRET`：启用后将 API Key 加密写入 `config.json`

## Docker 可选部署

```powershell
copy config.example.json config.json
New-Item state.json -ItemType File -Force
docker compose up --build
```

Docker 版本默认监听：

```text
http://127.0.0.1:8000/
```

`.dockerignore` 会排除 `config.json`、`state.json`、`.env` 等本地敏感文件，避免真实密钥被打进镜像。

## 开发与测试

```powershell
uv sync
uv run python -m pytest -q -p no:cacheprovider
node --check static\app.js
```

## 停止服务

推荐双击 `stop.bat`。如果使用命令行：

```powershell
.\stop.ps1
```

如果端口仍被占用，可以检查：

```powershell
netstat -ano | findstr :8000
```
