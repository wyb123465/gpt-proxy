import json

import httpx
from fastapi.testclient import TestClient

import main


def write_config(path, providers):
    path.write_text(
        json.dumps({"providers": providers, "default_model": "default-model"}),
        encoding="utf-8",
    )


def make_provider(name, priority, base_url=None, api_key="test-key", model=None):
    provider = {
        "name": name,
        "base_url": base_url or f"https://{name}.example/v1",
        "api_key": api_key,
        "priority": priority,
    }
    if model:
        provider["model"] = model
    return provider


def test_load_config_sorts_by_priority(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    write_config(
        config_path,
        [
            make_provider("free", 20),
            make_provider("official", 0),
            make_provider("backup", 10),
        ],
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert [provider["name"] for provider in config["providers"]] == [
        "official",
        "backup",
        "free",
    ]


def test_fallback_after_quota_error(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0),
            make_provider("free", 1),
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "official.example" in str(request.url):
            return httpx.Response(429, json={"error": "quota"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            headers={"x-ratelimit-remaining": "7"},
        )

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["free"]["calls"] == 1
    assert state["free"]["last_remaining"] == 7


def test_model_is_overridden_by_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, model="provider-model")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen_body = {}

    def handler(request):
        seen_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": []})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "caller-model", "messages": []},
    )

    assert response.status_code == 200
    assert seen_body["model"] == "provider-model"


def test_provider_status_does_not_expose_api_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text(
        json.dumps({"official": {"calls": 3, "last_remaining": 4}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["calls"] == 3
    assert provider["last_remaining"] == 4
    assert "api_key" not in provider
    assert "secret-key" not in response.text


def test_dashboard_config_masks_and_preserves_existing_key(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["api_key"] == ""
    assert provider["has_api_key"] is True
    assert "secret-key" not in response.text

    response = client.post(
        "/api/config",
        json={
            "default_model": "new-default",
            "providers": [
                {
                    "name": "official",
                    "base_url": "https://official.example/v1",
                    "model": "new-model",
                    "priority": 2,
                    "api_key": "",
                }
            ],
        },
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["default_model"] == "new-default"
    assert saved["providers"][0]["api_key"] == "secret-key"
    assert saved["providers"][0]["model"] == "new-model"


def test_dashboard_config_can_replace_key(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="old-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.post(
        "/api/config",
        json={
            "default_model": "default-model",
            "providers": [
                {
                    "name": "official",
                    "base_url": "https://official.example/v1",
                    "model": "provider-model",
                    "priority": 0,
                    "api_key": "new-key",
                }
            ],
        },
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "new-key"


def test_provider_check_returns_ok(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)

    client = TestClient(main.app)
    response = client.post("/api/providers/official/check")

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "official"
    assert data["ok"] is True
    assert data["status"] == 200


def test_provider_check_returns_error_on_quota(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def handler(request):
        return httpx.Response(429, json={"error": {"message": "quota exceeded"}})

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)

    client = TestClient(main.app)
    response = client.post("/api/providers/free/check")

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "free"
    assert data["ok"] is False
    assert data["status"] == 429


def test_provider_check_returns_404_for_unknown(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.post("/api/providers/nonexistent/check")

    assert response.status_code == 404


def test_provider_check_returns_no_api_key_when_key_missing(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [{"name": "free-1", "base_url": "https://free.example/v1", "priority": 1}])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.post("/api/providers/free-1/check")

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "free-1"
    assert data["ok"] is False
    assert data["status"] == "no_api_key"


def test_model_alias_replaces_model_name(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "fengl",
                "base_url": "https://fengl.example/v1",
                "api_key": "test-key",
                "priority": 0,
                "model": "mimo-v2.5",
                "model_aliases": {"gpt-4o": "mimo-v2.5", "gpt-3.5-turbo": "mimo-v2.5"},
            }
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen_body = {}

    def handler(request):
        seen_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert seen_body["model"] == "mimo-v2.5"


def test_disabled_provider_is_skipped(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {**make_provider("disabled", 0), "enabled": False},
            make_provider("active", 1),
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    assert calls == ["https://active.example/v1/chat/completions"]


def test_provider_rotates_multiple_api_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "free",
                "base_url": "https://free.example/v1",
                "priority": 0,
                "api_keys": ["key-1", "key-2"],
            }
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen_auth = []

    def handler(request):
        seen_auth.append(request.headers["authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 200
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 200

    assert seen_auth == ["Bearer key-1", "Bearer key-2"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["free"]["calls"] == 2
    assert state["free"]["key_index"] == 0


def test_provider_tries_next_key_after_quota_error(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "free",
                "base_url": "https://free.example/v1",
                "priority": 0,
                "api_keys": ["quota-key", "fresh-key"],
            }
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen_auth = []

    def handler(request):
        seen_auth.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer quota-key":
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    assert seen_auth == ["Bearer quota-key", "Bearer fresh-key"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["free"]["key_index"] == 0


def test_request_log_records_recent_attempts(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})
    logs = client.get("/api/requests")

    assert response.status_code == 200
    assert logs.status_code == 200
    entry = logs.json()["requests"][0]
    assert entry["provider"] == "official"
    assert entry["status"] == 200
    assert entry["path"] == "/v1/chat/completions"
    assert entry["latency_ms"] >= 0


def test_streaming_chat_completion_proxies_event_stream(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def handler(request):
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [], "stream": True},
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'data: {"choices"' in body
    assert "data: [DONE]" in body
