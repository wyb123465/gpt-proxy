# Local GPT API Proxy

这是一个本地 OpenAI 兼容代理。你的程序只需要调用本地地址，代理会按 `priority` 从高质量 API 到免费 API 依次尝试，遇到额度耗尽或后端错误时自动切换下一个。

## 启动

```powershell
cd C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy
$env:UV_CACHE_DIR="C:\Users\lenovo\Desktop\zhongzhuan\gpt-proxy\.uv-cache"
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

或者直接双击运行脚本：

```powershell
.\start.ps1   # 启动服务
.\stop.ps1    # 停止服务
```

启动后打开管理台：

```text
http://127.0.0.1:8000/
```

健康检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing
```

## 在 UI 中配置 API

打开 `http://127.0.0.1:8000/` 后可以直接添加、删除和保存 API：

- `名称`：随便起，例如 `official`、`free-1`。
- `Base URL`：服务商给你的 OpenAI 兼容地址，例如 `https://api.example.com/v1`。
- `模型`：该服务商支持的模型，例如 `gpt-4o-mini`、`gpt-3.5-turbo`。
- `优先级`：数字越小越先使用，正版/质量好的 API 建议填 `0`。
- `API Key`：第一次填真实 key；以后留空保存会保留旧 key。

UI 不会回显已有密钥，只会显示是否已设置。

每个 API 卡片上都有一个 **测试** 按钮，点击后会向该 API 发一个最小请求，验证连接是否正常。

## 调用方式

Python OpenAI SDK：

```python
from openai import OpenAI

client = OpenAI(
    api_key="local-proxy",
    base_url="http://127.0.0.1:8000/v1",
)

response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)
```

其他软件里把 API 地址改成：

```text
http://127.0.0.1:8000/v1
```

接口路径是：

```text
http://127.0.0.1:8000/v1/chat/completions
```

## 状态接口

查看当前可用 provider 和调用统计：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/providers -UseBasicParsing
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
