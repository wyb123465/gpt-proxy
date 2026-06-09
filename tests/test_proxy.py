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


def test_provider_check_all_returns_summary(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0),
            make_provider("backup", 1),
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_check(provider, default_model):
        if provider["name"] == "official":
            return {"ok": True, "status": 200, "detail": "ok"}
        return {"ok": False, "status": 429, "detail": {"error": "quota"}}

    monkeypatch.setattr(main, "check_provider", fake_check)

    client = TestClient(main.app)
    response = client.post("/api/providers/check-all")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["ok"] == 1
    assert data["failed"] == 1
    assert [result["provider"] for result in data["results"]] == ["official", "backup"]


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


def test_proxy_access_token_protects_v1_endpoints(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "local-secret", raising=False)

    client = TestClient(main.app)
    denied = client.post("/v1/chat/completions", json={"messages": []})

    assert denied.status_code == 401

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    allowed = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer local-secret"},
        json={"messages": []},
    )

    assert allowed.status_code == 200


def test_v1_rate_limit_returns_429(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1, raising=False)
    monkeypatch.setattr(main, "RATE_LIMIT_BUCKETS", {}, raising=False)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 200
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 429


def test_v1_request_body_size_limit_returns_413(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 20, raising=False)

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": [{"content": "too large"}]})

    assert response.status_code == 413


def test_models_endpoint_aggregates_provider_models(tmp_path, monkeypatch):
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

    def handler(request):
        if str(request.url) == "https://official.example/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})
        if str(request.url) == "https://free.example/v1/models":
            return httpx.Response(200, json={"data": [{"id": "mimo-v2.5"}, {"id": "gpt-4o"}]})
        return httpx.Response(404)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)

    client = TestClient(main.app)
    response = client.get("/v1/models")

    assert response.status_code == 200
    assert [model["id"] for model in response.json()["data"]] == ["gpt-4o", "mimo-v2.5"]


def test_429_cools_down_key_for_next_request(tmp_path, monkeypatch):
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
    monkeypatch.setattr(main, "KEY_COOLDOWN_SECONDS", 60, raising=False)

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
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 200
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 200

    assert seen_auth == ["Bearer quota-key", "Bearer fresh-key", "Bearer fresh-key"]


def test_responses_endpoint_returns_response_shape(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        # backend speaks the native Responses API; proxy passes it through verbatim
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "output_text": "hi",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
        })

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/responses", json={"model": "gpt-4o", "input": "hello"})

    assert response.status_code == 200
    # request was passed through to {base}/responses with the raw body (input preserved)
    assert seen["url"].endswith("/responses")
    assert seen["body"]["input"] == "hello"
    # response returned verbatim
    data = response.json()
    assert data["object"] == "response"
    assert data["output_text"] == "hi"
    assert data["output"][0]["content"][0]["text"] == "hi"


def test_responses_streaming_returns_sse_events(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    # native Responses API SSE bytes — proxy must pass these through unchanged
    sse_body = (
        b'event: response.created\ndata: {"type":"response.created"}\n\n'
        b'event: response.output_text.delta\ndata: {"delta":"Hello"}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
    )

    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        assert str(request.url).endswith("/responses")
        assert body["input"] == "hi"
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    with client.stream("POST", "/v1/responses", json={"model": "gpt-4o", "input": "hi", "stream": True}) as response:
        text = response.read().decode("utf-8")

    assert response.status_code == 200
    # SSE bytes passed through verbatim
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    assert "Hello" in text
    assert "event: response.completed" in text


def test_config_export_and_import_roundtrip(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    original = [make_provider("official", 0, api_key="secret-key")]
    write_config(config_path, original)
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    exported = client.get("/api/config/export")

    assert exported.status_code == 200
    assert exported.json()["providers"][0]["api_key"] == "secret-key"

    imported = client.post(
        "/api/config/import",
        json={
            "default_model": "imported-model",
            "providers": [
                {
                    "name": "imported",
                    "base_url": "https://imported.example/v1",
                    "priority": 0,
                    "api_keys": ["new-secret"],
                }
            ],
        },
    )

    assert imported.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["default_model"] == "imported-model"
    assert saved["providers"][0]["api_key"] == "new-secret"


def test_proxy_access_token_protects_management_config_export(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "local-secret", raising=False)

    client = TestClient(main.app)
    denied = client.get("/api/config/export")
    allowed = client.get("/api/config/export", headers={"Authorization": "Bearer local-secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["providers"][0]["api_key"] == "secret-key"


def test_dashboard_config_preserves_provider_protocol(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text("{}", encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.post(
        "/api/config",
        json={
            "default_model": "claude-sonnet-4-20250514",
            "providers": [
                {
                    "name": "claude",
                    "protocol": "claude",
                    "base_url": "https://api.anthropic.com/v1",
                    "model": "claude-sonnet-4-20250514",
                    "priority": 0,
                    "api_keys": ["sk-ant-test-key"],
                }
            ],
        },
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["protocol"] == "claude"
    assert response.json()["providers"][0]["protocol"] == "claude"


def test_config_keys_can_be_encrypted_at_rest(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "CONFIG_ENCRYPTION_SECRET", "passphrase", raising=False)
    state_path.write_text("{}", encoding="utf-8")

    client = TestClient(main.app)
    response = client.post(
        "/api/config",
        json={
            "default_model": "default-model",
            "providers": [
                {
                    "name": "official",
                    "base_url": "https://official.example/v1",
                    "priority": 0,
                    "api_keys": ["secret-key"],
                }
            ],
        },
    )

    assert response.status_code == 200
    raw = config_path.read_text(encoding="utf-8")
    assert "secret-key" not in raw

    config = main.load_config()
    assert main.provider_api_keys(config["providers"][0]) == ["secret-key"]


def test_encrypted_config_requires_secret(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "CONFIG_ENCRYPTION_SECRET", "passphrase", raising=False)
    state_path.write_text("{}", encoding="utf-8")

    client = TestClient(main.app)
    response = client.post(
        "/api/config",
        json={
            "default_model": "default-model",
            "providers": [
                {
                    "name": "official",
                    "base_url": "https://official.example/v1",
                    "priority": 0,
                    "api_keys": ["secret-key"],
                }
            ],
        },
    )
    assert response.status_code == 200

    monkeypatch.setattr(main, "CONFIG_ENCRYPTION_SECRET", "", raising=False)

    try:
        main.load_config()
    except RuntimeError as exc:
        assert "GPT_PROXY_CONFIG_SECRET" in str(exc)
    else:
        raise AssertionError("Encrypted config loaded without GPT_PROXY_CONFIG_SECRET")


def test_claude_protocol_uses_correct_headers(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "claude",
                "protocol": "claude",
                "base_url": "https://api.anthropic.com/v1",
                "model": "claude-sonnet-4-20250514",
                "priority": 0,
                "api_keys": ["sk-ant-test-key"],
            }
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen_headers = {}

    def handler(request):
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json={"id": "msg_123", "type": "message", "content": [{"type": "text", "text": "Hello"}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/messages", json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100})

    assert response.status_code == 200
    assert "x-api-key" in seen_headers
    assert seen_headers["x-api-key"] == "sk-ant-test-key"
    assert "anthropic-version" in seen_headers


def test_gemini_protocol_routes_correctly(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "gemini",
                "protocol": "gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": "gemini-2.0-flash",
                "priority": 0,
                "api_keys": ["gemini-test-key"],
            }
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen_request = {}

    def handler(request):
        seen_request["url"] = str(request.url)
        seen_request["headers"] = dict(request.headers)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Response"}]}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1beta/models/gemini-2.0-flash:generateContent", json={"contents": [{"parts": [{"text": "Hello"}]}]})

    assert response.status_code == 200
    assert "gemini-2.0-flash:generateContent" in seen_request["url"]
    assert "x-goog-api-key" in seen_request["headers"]
    assert seen_request["headers"]["x-goog-api-key"] == "gemini-test-key"


def test_domestic_protocol_with_deepseek(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "deepseek",
                "protocol": "domestic",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "priority": 0,
                "api_keys": ["sk-deepseek-test"],
            }
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen_request = {}

    def handler(request):
        seen_request["url"] = str(request.url)
        seen_request["headers"] = dict(request.headers)
        seen_request["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Response from DeepSeek"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]})

    assert response.status_code == 200
    assert "deepseek.com" in seen_request["url"]
    assert "authorization" in seen_request["headers"]
    assert "Bearer sk-deepseek-test" in seen_request["headers"]["authorization"]
    assert seen_request["body"]["model"] == "deepseek-chat"


def test_multiple_protocols_fallback(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "openai",
                "protocol": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o",
                "priority": 0,
                "api_keys": ["sk-openai-test"],
            },
            {
                "name": "deepseek",
                "protocol": "domestic",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "priority": 1,
                "api_keys": ["sk-deepseek-test"],
            },
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "openai.com" in str(request.url):
            return httpx.Response(429, json={"error": "rate_limit_exceeded"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Response from DeepSeek"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]})

    assert response.status_code == 200
    assert len(calls) == 2
    assert "openai.com" in calls[0]
    assert "deepseek.com" in calls[1]


def test_provider_check_claude_uses_messages_endpoint(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "claude",
                "protocol": "claude",
                "base_url": "https://api.anthropic.com/v1",
                "model": "claude-3-5-sonnet-20241022",
                "priority": 0,
                "api_keys": ["claude-test-key"],
            }
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"content": [{"text": "pong"}]})

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)

    client = TestClient(main.app)
    response = client.post("/api/providers/claude/check")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["status"] == 200
    # Claude check must hit /messages with the x-api-key header, not Bearer auth
    assert seen["url"].endswith("/messages")
    assert seen["headers"].get("x-api-key") == "claude-test-key"
    assert "authorization" not in seen["headers"]


def test_provider_check_gemini_uses_generate_content(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "gemini",
                "protocol": "gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": "gemini-2.0-flash",
                "priority": 0,
                "api_keys": ["gemini-test-key"],
            }
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "pong"}]}}]})

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)

    client = TestClient(main.app)
    response = client.post("/api/providers/gemini/check")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["status"] == 200
    # Gemini check must resolve the model into the URL and use x-goog-api-key
    assert "gemini-2.0-flash:generateContent" in seen["url"]
    assert seen["headers"].get("x-goog-api-key") == "gemini-test-key"
    assert "authorization" not in seen["headers"]
