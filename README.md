# Local GPT API Proxy

这是一个本地 OpenAI 兼容代理。你的程序只需要调用本地地址，代理会按 `priority` 从高质量 API 到免费 API 依次尝试，遇到额度耗尽或后端错误时自动切换下一个。

## 已支持能力

- OpenAI 兼容接口：`POST /v1/chat/completions`
- 基础 Responses 兼容：`POST /v1/responses`
- 模型列表聚合：`GET /v1/models`
- 流式输出：支持客户端传 `stream: true`
- 自动回退：遇到 `403`、`429`、`5xx` 会尝试下一个 Key 或下一个 API
- 多 Key 轮询：同一个 provider 可配置多个 API Key，成功后自动轮到下一个
- 429 冷却：某个 Key 触发 429 后会暂时跳过它
- Provider 启停：临时不用某个 API 时无需删除
- 请求日志：UI 显示最近请求的 provider、状态码、耗时、回退次数、流式状态
- 模型别名：客户端请求 `gpt-4o`，后端可转成真实模型名，例如 `mimo-v2.5`
- 可选安全：代理访问密钥、本地限流、请求体大小限制、配置密钥加密
- 配置导入/导出：方便备份和迁移

## 启动

推荐使用 `uv`，不污染全局 Python 环境：

```powershell
cd C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy
$env:UV_CACHE_DIR="C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy\.uv-cache"
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

或者直接运行脚本：

```powershell
.\start.ps1   # 启动服务
.\stop.ps1    # 停止服务
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
- `GPT_PROXY_CONFIG_SECRET`：启用后写入 `config.json` 的 API Key 会加密保存。

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

## 配置文件

本地真实配置写在 `config.json`，它已被 `.gitignore` 忽略，不会上传密钥。公开模板是 `config.example.json`。

```json
{
  "providers": [
    {
      "name": "example",
      "base_url": "https://api.example.com/v1",
      "model": "mimo-v2.5",
      "priority": 0,
      "enabled": true,
      "api_keys": ["sk-xxx", "sk-yyy"],
      "use_curl": false,
      "model_aliases": {
        "gpt-4o": "mimo-v2.5"
      }
    }
  ],
  "default_model": "mimo-v2.5"
}
```

## Docker

```powershell
docker compose up --build -d
```

Docker 启动后同样打开 `http://127.0.0.1:8000/` 配置。

## 测试

```powershell
uv run python -m pytest -q -p no:cacheprovider
```
