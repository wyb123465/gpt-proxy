# Local GPT API Proxy

这是一个本地 OpenAI 兼容代理。你的程序只需要调用本地地址，代理会按优先级自动尝试多个后端 API，并在额度耗尽或后端错误时回退到下一个 Key 或 provider。

## 已支持能力

### 多协议支持
- ✅ **OpenAI 兼容接口**：`POST /v1/chat/completions`、`POST /v1/responses`
- ✅ **Claude 原生接口**：`POST /v1/messages`（支持 Anthropic API）
- ✅ **Gemini 原生接口**：`POST /v1beta/models/{model}:generateContent`（支持 Google Gemini API）
- ✅ **国内大模型接口**：支持通义千问、智谱GLM、DeepSeek、百川、豆包、混元等（OpenAI 兼容格式）

### 核心功能
- 🔄 **自动回退**：遇到 `403`、`429`、`5xx` 会尝试下一个 Key 或下一个 API
- 🔁 **多 Key 轮询**：同一个 provider 可配置多个 API Key，成功后自动轮到下一个
- ❄️ **429 冷却**：某个 Key 触发 429 后会暂时跳过它
- 📊 **模型列表聚合**：`GET /v1/models` 聚合所有后端的可用模型
- 🌊 **流式输出**：支持客户端传 `stream: true`
- 🔀 **模型别名**：客户端请求 `gpt-4o`，后端可转成真实模型名，例如 `deepseek-chat`
- 🧪 **配置体检**：UI 可批量检查 API 连通性、密钥状态和失败原因
- 🧭 **批量模型同步**：一键获取所有可用 provider 的模型列表并去重汇总
- 🎯 **请求日志**：UI 显示最近请求的 provider、状态码、耗时、回退次数和流式状态
- 🎨 **Web 管理界面**：统一管理多个 API，支持启停、导入/导出配置

### 安全特性
- 🔒 **代理访问密钥**：保护本地代理不被未授权访问
- 🚦 **本地限流**：防止客户端滥用
- 📏 **请求体大小限制**：防止超大请求
- 🔐 **配置密钥加密**：API Key 加密存储到 `config.json`

## 启动

推荐使用 `uv`，不污染全局 Python 环境：

```powershell
cd C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy
$env:UV_CACHE_DIR="C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy\.uv-cache"
uv sync
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

之后再次启动时，只需要执行最后一行 `uv run uvicorn main:app --host 127.0.0.1 --port 8000`。

或者直接运行脚本：

```powershell
.\start.ps1   # 启动服务
.\stop.ps1    # 停止服务
```

Windows 上想双击启动并自动打开管理台，可以运行：

```powershell
.\start-ui.bat
```

启动后打开管理台：

```text
http://127.0.0.1:8000/
```

## 可选安全环境变量

```powershell
$env:GPT_PROXY_ACCESS_TOKEN="your-local-proxy-token"
$env:GPT_PROXY_RATE_LIMIT_PER_MINUTE="60"
$env:GPT_PROXY_MAX_REQUEST_BYTES="2097152"
$env:GPT_PROXY_KEY_COOLDOWN_SECONDS="60"
$env:GPT_PROXY_CONFIG_SECRET="your-config-encryption-passphrase"
```

- `GPT_PROXY_ACCESS_TOKEN`：启用后，调用 `/v1/*` 和管理接口 `/api/*` 都需要 `Authorization: Bearer <token>` 或 `x-api-key: <token>`。
- `GPT_PROXY_RATE_LIMIT_PER_MINUTE`：本地代理每分钟请求限制，`0` 表示关闭。
- `GPT_PROXY_MAX_REQUEST_BYTES`：请求体大小限制，默认 `2 MB`。
- `GPT_PROXY_KEY_COOLDOWN_SECONDS`：Key 返回 `429` 后冷却秒数。
- `GPT_PROXY_CONFIG_SECRET`：启用后写入 `config.json` 的 API Key 会加密保存；忘记设置该值时无法读取已加密配置。

## 在 UI 中配置 API

打开 `http://127.0.0.1:8000/` 后可以直接添加、删除、启停、保存、导入和导出 API：

- 如果启用了 `GPT_PROXY_ACCESS_TOKEN`，先在页面“安全与兼容”区域填写本地代理访问密钥。
- `名称`：随便起，例如 `official`、`free-1`。
- `Base URL`：服务商给你的 OpenAI 兼容地址，例如 `https://api.example.com/v1`。
- `模型`：该服务商真实支持的模型；也可以点击“获取模型”后选择。
- `优先级`：数字越小越先使用，质量好的 API 建议填 `0`。
- `API Keys`：每行一个 Key；保存后 UI 不会回显，留空保存会保留旧密钥。
- `启用该 API`：关闭后代理不会使用它，但配置仍保留。
- `使用 curl 传输`：遇到 Cloudflare 保护导致 `httpx` 不通时可尝试开启。
- `模型别名`：左边填客户端发来的模型名，右边填该站点真实模型名。
- `保存并测试全部`：保存当前配置后逐个测试已启用 provider，并在页面顶部显示通过数量。
- `配置体检`：集中查看配置摘要、批量连通性检测和模型同步报告。

## 统一调用方式

Python OpenAI SDK：

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

如果设置了 `GPT_PROXY_ACCESS_TOKEN`，这里的 `api_key` 要改成你的本地代理访问密钥。

其他软件里把 API 地址改成：

```text
http://127.0.0.1:8000/v1
```

## 状态接口

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/providers -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8000/api/requests -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8000/v1/models -UseBasicParsing
```

如果启用了 `GPT_PROXY_ACCESS_TOKEN`，这些请求也需要带本地代理访问密钥。

## 配置文件

本地真实配置写在 `config.json`，它已被 `.gitignore` 忽略，不会上传密钥。公开模板是 `config.example.json`。

### 四种协议配置说明

代理支持四种协议类型，每种协议有不同的调用端点和认证方式：

#### 1. OpenAI 协议 (`protocol: "openai"`)

适用于 OpenAI 官方 API 和第三方兼容 API。

```json
{
  "name": "openai-official",
  "protocol": "openai",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "priority": 0,
  "enabled": true,
  "api_keys": ["sk-your-openai-key"],
  "model_aliases": {
    "gpt-4": "gpt-4o"
  }
}
```

**调用端点**：
- `POST /v1/chat/completions`
- `POST /v1/responses`

**认证方式**：`Authorization: Bearer {api_key}`

#### 2. Claude 协议 (`protocol: "claude"`)

适用于 Anthropic Claude 官方 API。

```json
{
  "name": "claude-official",
  "protocol": "claude",
  "base_url": "https://api.anthropic.com/v1",
  "model": "claude-sonnet-4-20250514",
  "priority": 1,
  "enabled": true,
  "api_keys": ["sk-ant-your-claude-key"]
}
```

**调用端点**：
- `POST /v1/messages`

**认证方式**：`x-api-key: {api_key}` + `anthropic-version: 2023-06-01`

#### 3. Gemini 协议 (`protocol: "gemini"`)

适用于 Google Gemini 官方 API。

```json
{
  "name": "gemini-official",
  "protocol": "gemini",
  "base_url": "https://generativelanguage.googleapis.com/v1beta",
  "model": "gemini-2.0-flash",
  "priority": 2,
  "enabled": true,
  "api_keys": ["your-gemini-api-key"]
}
```

**调用端点**：
- `POST /v1beta/models/{model}:generateContent`
- `POST /v1beta/models/{model}:streamGenerateContent`

**认证方式**：`x-goog-api-key: {api_key}`

#### 4. 国内大模型协议 (`protocol: "domestic"`)

适用于国内主流大模型，使用 OpenAI 兼容格式，但可能有特定的认证或参数要求。

**支持的国内大模型**：
- 🚀 **DeepSeek**：`https://api.deepseek.com/v1`
- 🌟 **通义千问（Qwen）**：`https://dashscope.aliyuncs.com/compatible-mode/v1`
- 🤖 **智谱 GLM**：`https://open.bigmodel.cn/api/paas/v4`
- 🎯 **百川（Baichuan）**
- 🔥 **豆包（Doubao）**
- ⚡ **腾讯混元（Hunyuan）**
- 🌈 **零一万物（Yi）**

**配置示例（DeepSeek）**：

```json
{
  "name": "deepseek",
  "protocol": "domestic",
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "priority": 3,
  "enabled": true,
  "api_keys": ["sk-your-deepseek-key"],
  "model_aliases": {
    "gpt-3.5-turbo": "deepseek-chat",
    "gpt-4": "deepseek-chat"
  }
}
```

**调用端点**：
- `POST /v1/chat/completions`（通过代理统一调用）

**认证方式**：`Authorization: Bearer {api_key}`

### 完整配置示例

```json
{
  "providers": [
    {
      "name": "openai-official",
      "protocol": "openai",
      "base_url": "https://api.openai.com/v1",
      "model": "gpt-4o",
      "priority": 0,
      "enabled": true,
      "api_keys": ["sk-xxx", "sk-yyy"],
      "use_curl": false,
      "model_aliases": {
        "gpt-4": "gpt-4o"
      }
    },
    {
      "name": "claude",
      "protocol": "claude",
      "base_url": "https://api.anthropic.com/v1",
      "model": "claude-sonnet-4-20250514",
      "priority": 1,
      "enabled": true,
      "api_keys": ["sk-ant-xxx"]
    },
    {
      "name": "deepseek",
      "protocol": "domestic",
      "base_url": "https://api.deepseek.com/v1",
      "model": "deepseek-chat",
      "priority": 2,
      "enabled": true,
      "api_keys": ["sk-xxx"]
    }
  ],
  "default_model": "gpt-4o"
}
```

## Docker

首次使用建议先创建空文件，避免 Docker 把挂载目标当成目录：

```powershell
if (!(Test-Path .\config.json)) { Copy-Item .\config.example.json .\config.json }
if (!(Test-Path .\state.json)) { Set-Content .\state.json "{}" -Encoding UTF8 }
Copy-Item .\.env.example .\.env
```

然后启动：

```powershell
docker compose up --build -d
```

Docker 启动后打开 `http://127.0.0.1:8000/` 配置。`docker-compose.yml` 会把 `config.json` 和 `state.json` 挂载为可写文件，因此 UI 保存配置、导入配置和调用统计都能正常持久化。

## 测试

```powershell
$env:UV_CACHE_DIR="C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy\.uv-cache"
uv run python -m pytest -q -p no:cacheprovider
```
