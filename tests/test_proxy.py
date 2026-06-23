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


def test_invalid_numeric_env_vars_fall_back_to_defaults():
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "GPT_PROXY_RATE_LIMIT_PER_MINUTE": "not-a-number",
        "GPT_PROXY_MAX_REQUEST_BYTES": "also-bad",
        "GPT_PROXY_KEY_COOLDOWN_SECONDS": "bad-too",
    }
    script = (
        "import json, main; "
        "print(json.dumps({"
        "'rate_limit': main.RATE_LIMIT_PER_MINUTE, "
        "'max_request_bytes': main.MAX_REQUEST_BYTES, "
        "'key_cooldown_seconds': main.KEY_COOLDOWN_SECONDS"
        "}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(main.BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values == {
        "rate_limit": 0,
        "max_request_bytes": 2 * 1024 * 1024,
        "key_cooldown_seconds": 60,
    }


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


def test_load_config_treats_malformed_priority_as_default(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    make_provider("bad-priority", "not-a-number"),
                    make_provider("official", 0),
                ],
                "default_model": "default-model",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert [provider["name"] for provider in config["providers"]] == ["official", "bad-priority"]
    assert config["providers"][1]["priority"] == 1000


def test_load_config_treats_string_false_provider_enabled_as_disabled(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {**make_provider("disabled", 0), "enabled": "false"},
                    make_provider("official", 1),
                ],
                "default_model": "default-model",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert [provider["name"] for provider in config["providers"]] == ["official"]


def test_load_config_skips_providers_with_malformed_name_or_base_url(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    write_config(
        config_path,
        [
            {**make_provider("bad-name", 0), "name": {"value": "bad-name"}},
            {**make_provider("bad-shape", 0), "base_url": {"url": "https://bad.example/v1"}},
            {**make_provider("bad-scheme", 1), "base_url": "ftp://bad.example/v1"},
            make_provider("official", 2),
        ],
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert [provider["name"] for provider in config["providers"]] == ["official"]


def test_load_config_ignores_malformed_api_key_env(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    provider = make_provider("official", 0, api_key="direct-key")
    provider["api_key_env"] = ["BAD_ENV_SHAPE"]
    write_config(config_path, [provider])
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert [provider["name"] for provider in config["providers"]] == ["official"]
    assert config["providers"][0]["api_key"] == "direct-key"


def test_load_config_ignores_malformed_provider_api_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    malformed = make_provider("malformed", 0)
    malformed["api_key"] = ["bad-key"]
    malformed["api_keys"] = [{"key": "also-bad"}]
    valid = make_provider("official", 1, api_key="direct-key")
    write_config(config_path, [malformed, valid])
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert [provider["name"] for provider in config["providers"]] == ["official"]


def test_dashboard_config_treats_malformed_priority_as_default(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    make_provider("bad-priority", "not-a-number"),
                    make_provider("official", 0),
                ],
                "default_model": "default-model",
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    providers = response.json()["providers"]
    assert [provider["name"] for provider in providers] == ["official", "bad-priority"]
    assert providers[1]["priority"] == 1000


def test_dashboard_config_treats_malformed_model_aliases_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0)
    provider["model_aliases"] = ["bad-shape"]
    write_config(config_path, [provider])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["providers"][0]["model_aliases"] == {}


def test_dashboard_config_sanitizes_malformed_provider_strings(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0)
    provider["name"] = ["official"]
    provider["base_url"] = ["https://official.example/v1"]
    provider["model"] = ["bad-model"]
    write_config(config_path, [provider])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/api/config")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["name"] == ""
    assert provider["base_url"] == ""
    assert provider["model"] == "default-model"


def test_dashboard_config_treats_malformed_api_key_env_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0, api_key="direct-key")
    provider["api_key_env"] = ["BAD_ENV_SHAPE"]
    write_config(config_path, [provider])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["providers"][0]["api_key_env"] == ""


def test_dashboard_config_treats_malformed_protocol_as_openai(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0, api_key="direct-key")
    provider["protocol"] = ["bad-shape"]
    write_config(config_path, [provider])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["providers"][0]["protocol"] == "openai"


def test_load_config_treats_malformed_config_root_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert config["default_model"] == "gpt-3.5-turbo"
    assert config["providers"] == []
    assert config["client_keys"] == []


def test_load_config_treats_malformed_default_model_as_default(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"providers": [], "default_model": ["bad-model"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert config["default_model"] == "gpt-3.5-turbo"


def test_load_config_treats_invalid_config_json_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert config["default_model"] == "gpt-3.5-turbo"
    assert config["providers"] == []
    assert config["client_keys"] == []


def test_load_config_treats_invalid_config_encoding_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_bytes(b"\xff")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert config["default_model"] == "gpt-3.5-turbo"
    assert config["providers"] == []
    assert config["client_keys"] == []


def test_dashboard_config_treats_malformed_config_root_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert data["default_model"] == "gpt-3.5-turbo"
    assert data["providers"] == []
    assert data["client_keys"] == []


def test_dashboard_config_treats_invalid_config_json_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text("{bad json", encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert data["default_model"] == "gpt-3.5-turbo"
    assert data["providers"] == []
    assert data["client_keys"] == []


def test_dashboard_config_treats_invalid_config_encoding_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_bytes(b"\xff")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert data["default_model"] == "gpt-3.5-turbo"
    assert data["providers"] == []
    assert data["client_keys"] == []


def test_dashboard_config_treats_empty_default_model_as_default(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(json.dumps({"providers": [], "default_model": "  "}), encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["default_model"] == "gpt-3.5-turbo"


def test_load_state_treats_invalid_state_json_as_empty(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    assert main.load_state() == {}


def test_load_state_treats_invalid_state_encoding_as_empty(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b"\xff")
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    assert main.load_state() == {}


def test_append_request_log_replaces_malformed_request_log(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"_requests": {"bad": "shape"}}), encoding="utf-8")
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "_state_cache", None, raising=False)
    monkeypatch.setattr(main, "_state_cache_path", None, raising=False)

    main.append_request_log({"provider": "official", "status": 200, "latency_ms": 12})

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["_requests"] == [{"provider": "official", "status": 200, "latency_ms": 12}]
    assert saved["_stats"]["total"]["attempts"] == 1


def test_routing_preview_treats_malformed_config_root_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/routing/preview?target=chat")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "empty"
    assert data["selected_provider"] is None
    assert data["candidates"] == []


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


def test_malformed_provider_model_override_is_ignored(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0)
    provider["model"] = ["bad-model-shape"]
    write_config(config_path, [provider])
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
    assert seen_body["model"] == "caller-model"


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
    assert provider["protocol"] == "openai"
    assert "api_key" not in provider
    assert "secret-key" not in response.text


def test_protocol_catalog_endpoint_returns_four_groups(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/protocols")

    assert response.status_code == 200
    protocols = {item["name"]: item for item in response.json()["protocols"]}
    assert set(protocols) == {"openai", "domestic", "claude", "gemini"}
    assert protocols["openai"]["count"] == 1
    assert protocols["claude"]["native_endpoint"] == "/v1/messages"


def test_provider_presets_endpoint_returns_common_templates(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/provider-presets")

    assert response.status_code == 200
    presets = {preset["id"]: preset for preset in response.json()["presets"]}
    assert "openrouter" in presets
    assert "moonshot" in presets
    assert presets["openrouter"]["protocol"] == "domestic"
    assert presets["moonshot"]["base_url"] == "https://api.moonshot.cn/v1"


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


def test_dashboard_config_can_clear_api_key_env(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "official",
                        "base_url": "https://official.example/v1",
                        "priority": 0,
                        "api_key_env": "OFFICIAL_API_KEY",
                    }
                ],
                "default_model": "default-model",
            }
        ),
        encoding="utf-8",
    )
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
                    "priority": 0,
                    "api_key": "",
                    "api_keys": [],
                    "api_key_env": "",
                }
            ],
        },
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "api_key_env" not in saved["providers"][0]


def test_dashboard_config_can_disable_use_curl_while_preserving_key(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0, api_key="secret-key")
    provider["use_curl"] = True
    write_config(config_path, [provider])
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
                    "priority": 0,
                    "api_key": "",
                    "api_keys": [],
                    "use_curl": False,
                }
            ],
        },
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "secret-key"
    assert saved["providers"][0].get("use_curl") is not True


def test_dashboard_config_rejects_non_object_payload(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.post("/api/config", json=["bad-payload"])

    assert response.status_code == 400
    assert response.json()["detail"] == "config payload must be an object"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "secret-key"


def test_dashboard_config_rejects_invalid_utf8_json_body(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.post("/api/config", content=b"\xff", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON request body"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "secret-key"


def test_dashboard_config_masks_and_preserves_client_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [make_provider("official", 0, api_key="secret-key")],
                "client_keys": [
                    {
                        "id": "key-1",
                        "label": "ChatBox",
                        "key": "local-secret",
                        "enabled": True,
                        "allowed_models": ["gpt-4o"],
                        "excluded_models": ["gpt-image-*"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    client_key = response.json()["client_keys"][0]
    assert client_key["key"] == ""
    assert client_key["has_key"] is True
    assert "local-secret" not in response.text

    saved_response = client.post(
        "/api/config",
        json={
            "default_model": "default-model",
            "providers": [
                {
                    "name": "official",
                    "base_url": "https://official.example/v1",
                    "priority": 0,
                    "api_key": "",
                }
            ],
            "client_keys": [
                {
                    "id": "key-1",
                    "label": "ChatBox renamed",
                    "key": "",
                    "enabled": True,
                    "allowed_models": ["gpt-4o", "gpt-4o-mini"],
                    "excluded_models": ["gpt-image-*"],
                }
            ],
        },
    )

    assert saved_response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["client_keys"][0]["key"] == "local-secret"
    assert saved["client_keys"][0]["label"] == "ChatBox renamed"
    assert saved["client_keys"][0]["allowed_models"] == ["gpt-4o", "gpt-4o-mini"]


def test_dashboard_config_ignores_malformed_client_key_secret(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [],
                "client_keys": [
                    {
                        "id": "bad-key",
                        "label": "Bad Key",
                        "key": ["client-secret"],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert data["security"]["enabled_client_key_count"] == 0
    assert data["security"]["v1_auth_mode"] == "open"
    assert data["client_keys"][0]["has_key"] is False


def test_dashboard_config_preserves_client_key_when_id_changes(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [make_provider("official", 0, api_key="secret-key")],
                "client_keys": [
                    {
                        "id": "key-1",
                        "label": "ChatBox",
                        "key": "local-secret",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
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
                    "priority": 0,
                    "api_key": "",
                }
            ],
            "client_keys": [
                {
                    "id": "renamed-key",
                    "saved_id": "key-1",
                    "label": "ChatBox",
                    "key": "",
                    "enabled": True,
                }
            ],
        },
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["client_keys"][0]["id"] == "renamed-key"
    assert saved["client_keys"][0]["key"] == "local-secret"


def test_dashboard_config_treats_malformed_client_keys_container_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [make_provider("official", 0, api_key="secret-key")],
                "client_keys": {"bad": "shape"},
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "", raising=False)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert data["client_keys"] == []
    assert data["security"]["v1_auth_mode"] == "open"
    assert data["security"]["enabled_client_key_count"] == 0


def test_dashboard_config_ignores_malformed_client_key_entries(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [make_provider("official", 0, api_key="secret-key")],
                "client_keys": [
                    "broken-entry",
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "", raising=False)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert len(data["client_keys"]) == 1
    assert data["client_keys"][0]["id"] == "chatbox"
    assert data["client_keys"][0]["has_key"] is True
    assert data["security"]["v1_auth_mode"] == "client_keys"
    assert data["security"]["enabled_client_key_count"] == 1
    assert "client-secret" not in response.text


def test_dashboard_config_normalizes_hand_edited_client_key_model_rules(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [make_provider("official", 0, api_key="secret-key")],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                        "allowed_models": "gpt-4o, gpt-4.1\nclaude-*",
                        "excluded_models": {"bad": "shape"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    client_key = response.json()["client_keys"][0]
    assert client_key["allowed_models"] == ["gpt-4o", "gpt-4.1", "claude-*"]
    assert client_key["excluded_models"] == []
    assert "client-secret" not in response.text


def test_dashboard_config_reports_auth_modes_without_exposing_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [make_provider("official", 0, api_key="secret-key")],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                    },
                    {
                        "id": "disabled",
                        "label": "Disabled",
                        "key": "disabled-secret",
                        "enabled": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "", raising=False)

    client = TestClient(main.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    security = response.json()["security"]
    assert security["management_auth_mode"] == "open"
    assert security["v1_auth_mode"] == "client_keys"
    assert security["enabled_client_key_count"] == 1
    assert "client-secret" not in response.text
    assert "disabled-secret" not in response.text


def test_dashboard_config_reports_combined_proxy_and_client_key_auth_modes(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [make_provider("official", 0, api_key="secret-key")],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "local-secret", raising=False)

    client = TestClient(main.app)
    response = client.get("/api/config", headers={"Authorization": "Bearer local-secret"})

    assert response.status_code == 200
    security = response.json()["security"]
    assert security["management_auth_mode"] == "proxy_token"
    assert security["v1_auth_mode"] == "proxy_token_or_client_keys"
    assert security["enabled_client_key_count"] == 1


def test_dashboard_can_delete_provider_and_state(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0, api_key="official-key"),
            make_provider("free", 1, api_key="free-key"),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "official": {"calls": 3, "last_remaining": 4},
                "free": {"calls": 5, "last_remaining": 1},
                "_requests": [{"provider": "free", "status": 200}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.delete("/api/providers/free")

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert [provider["name"] for provider in saved["providers"]] == ["official"]
    assert "free" not in state
    assert state["_requests"] == [{"provider": "free", "status": 200}]
    assert [provider["name"] for provider in response.json()["providers"]] == ["official"]


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


def test_provider_check_returns_structured_error_when_check_raises(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_check(provider, default_model):
        raise RuntimeError("connection setup failed")

    monkeypatch.setattr(main, "check_provider", fake_check)

    client = TestClient(main.app)
    response = client.post("/api/providers/official/check")

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "official"
    assert data["ok"] is False
    assert data["status"] == "check_error"
    assert data["detail"] == "connection setup failed"


def test_provider_models_returns_no_api_key_when_key_missing(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [{"name": "free-1", "base_url": "https://free.example/v1", "priority": 1}])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers/free-1/models")

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "free-1"
    assert data["ok"] is False
    assert data["status"] == "no_api_key"


def test_provider_models_returns_structured_error_when_fetch_fails(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, model="configured-model")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_fetch(provider):
        raise RuntimeError("model endpoint unavailable")

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.get("/api/providers/official/models")

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "official"
    assert data["ok"] is False
    assert data["status"] == "model_fetch_error"
    assert data["detail"] == "model endpoint unavailable"
    assert data["fallback_used"] is True
    assert data["models"]["data"] == [{"id": "configured-model", "object": "model"}]


def test_provider_models_uses_configured_model_when_fetch_returns_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, model="configured-model")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_fetch(provider):
        return []

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.get("/api/providers/official/models")

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "official"
    assert data["ok"] is True
    assert data["status"] == 200
    assert data["fallback_used"] is True
    assert data["models"]["data"] == [{"id": "configured-model", "object": "model"}]


def test_provider_models_normalizes_name_only_and_skips_malformed_models(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("gemini", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_fetch(provider):
        return [
            {"name": "models/gemini-2.0-flash"},
            ["bad-model"],
            {"id": ""},
            {"id": "gpt-4o"},
        ]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.get("/api/providers/gemini/models")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["models"]["data"] == [
        {"name": "models/gemini-2.0-flash", "id": "gemini-2.0-flash", "object": "model"},
        {"id": "gpt-4o", "object": "model"},
    ]


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


def test_provider_check_all_continues_when_provider_check_raises(tmp_path, monkeypatch):
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
        raise RuntimeError("connection setup failed")

    monkeypatch.setattr(main, "check_provider", fake_check)

    client = TestClient(main.app)
    response = client.post("/api/providers/check-all")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["ok"] == 1
    assert data["failed"] == 1
    assert data["results"][1]["provider"] == "backup"
    assert data["results"][1]["ok"] is False
    assert data["results"][1]["status"] == "check_error"
    assert data["results"][1]["detail"] == "connection setup failed"


def test_provider_models_sync_returns_provider_summary(tmp_path, monkeypatch):
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

    async def fake_fetch(provider):
        if provider["name"] == "official":
            return [{"id": "gpt-4o"}, {"id": "shared-model"}]
        return [{"id": "shared-model"}, {"id": "mimo-v2.5"}]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.post("/api/providers/models/sync")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["ok"] == 2
    assert data["unique_model_count"] == 3
    assert [result["count"] for result in data["results"]] == [2, 2]
    assert [model["id"] for model in data["models"]] == ["gpt-4o", "shared-model", "mimo-v2.5"]


def test_provider_models_sync_normalizes_name_only_and_skips_malformed_models(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("gemini", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_fetch(provider):
        return [
            {"name": "models/gemini-2.0-flash"},
            ["bad-model"],
            {"id": ""},
            {"id": "gpt-4o"},
        ]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.post("/api/providers/models/sync")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] == 1
    assert data["unique_model_count"] == 2
    assert [model["id"] for model in data["models"]] == ["gemini-2.0-flash", "gpt-4o"]
    assert data["results"][0]["count"] == 2
    assert data["results"][0]["models"] == [
        {"name": "models/gemini-2.0-flash", "id": "gemini-2.0-flash", "object": "model"},
        {"id": "gpt-4o", "object": "model"},
    ]


def test_provider_models_sync_keeps_configured_model_when_fetch_raises(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("broken", 0, model="configured-fallback"),
            make_provider("working", 1),
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_fetch(provider):
        if provider["name"] == "broken":
            raise RuntimeError("model endpoint unavailable")
        return [{"id": "gpt-4o"}]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.post("/api/providers/models/sync")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["ok"] == 1
    assert data["failed"] == 1
    assert data["unique_model_count"] == 2
    assert [model["id"] for model in data["models"]] == ["configured-fallback", "gpt-4o"]
    broken = data["results"][0]
    assert broken["provider"] == "broken"
    assert broken["ok"] is False
    assert broken["status"] == "model_fetch_error"
    assert broken["fallback_used"] is True
    assert broken["count"] == 1
    assert broken["models"] == [{"id": "configured-fallback", "object": "model"}]


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


def test_malformed_model_aliases_do_not_break_proxy_request(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0, model="provider-model")
    provider["model_aliases"] = ["gpt-4o"]
    write_config(config_path, [provider])
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
    assert seen_body["model"] == "provider-model"


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


def test_string_false_use_curl_uses_http_client_path(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0)
    provider["use_curl"] = "false"
    write_config(config_path, [provider])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    calls = []
    curl_called = False

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def fake_curl_forward_to_provider(*args, **kwargs):
        nonlocal curl_called
        curl_called = True
        return 200, {"curl": True}

    monkeypatch.setattr(main, "curl_forward_to_provider", fake_curl_forward_to_provider)
    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    assert curl_called is False
    assert calls == ["https://official.example/v1/chat/completions"]


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


def test_request_log_records_route_decision_after_fallback(tmp_path, monkeypatch):
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
        if "official.example" in str(request.url):
            return httpx.Response(429, json={"error": "quota"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 200

    entries = client.get("/api/requests").json()["requests"]
    success_entry = entries[0]
    failed_entry = entries[1]

    assert success_entry["provider"] == "free"
    assert success_entry["route_decision"]["reason"] == "fallback"
    assert success_entry["route_decision"]["routing_reason"] == "fallback"
    assert success_entry["route_decision"]["attempt"] == 2
    assert "official" in success_entry["route_decision"]["message"]
    assert failed_entry["provider"] == "official"
    assert failed_entry["route_decision"]["reason"] == "primary"
    assert failed_entry["route_decision"]["routing_reason"] == "primary"


def test_smart_routing_avoids_provider_when_all_keys_are_cooling(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0, api_key="hot-key"),
            make_provider("backup", 1, api_key="backup-key"),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "official": {
                    "key_cooldowns": {
                        main.key_fingerprint("hot-key"): main.time.time() + 60,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
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
    assert len(calls) == 1
    assert "backup.example" in calls[0]
    entry = client.get("/api/requests").json()["requests"][0]
    assert entry["provider"] == "backup"
    assert entry["route_decision"]["reason"] == "cooldown_avoided"
    assert entry["route_decision"]["routing_reason"] == "cooldown_avoided"
    assert "official" in entry["route_decision"]["message"]


def test_smart_routing_prefers_healthier_provider_at_same_priority(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("shaky", 0),
            make_provider("steady", 0),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "_stats": {
                    "providers": {
                        "shaky": {
                            "attempts": 4,
                            "success": 1,
                            "failed": 3,
                            "streamed": 0,
                            "fallbacks": 3,
                            "latency_ms_total": 800,
                        },
                        "steady": {
                            "attempts": 4,
                            "success": 4,
                            "failed": 0,
                            "streamed": 0,
                            "fallbacks": 0,
                            "latency_ms_total": 400,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
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
    assert len(calls) == 1
    assert "steady.example" in calls[0]
    entry = client.get("/api/requests").json()["requests"][0]
    assert entry["provider"] == "steady"
    assert entry["route_decision"]["reason"] == "health_preferred"
    assert entry["route_decision"]["routing_reason"] == "health_preferred"
    assert "shaky" in entry["route_decision"]["message"]


def test_smart_routing_keeps_healthy_higher_priority_provider_first(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0),
            make_provider("backup", 1),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "_stats": {
                    "providers": {
                        "official": {
                            "attempts": 3,
                            "success": 3,
                            "failed": 0,
                            "streamed": 0,
                            "fallbacks": 0,
                            "latency_ms_total": 300,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
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
    assert len(calls) == 1
    assert "official.example" in calls[0]
    entry = client.get("/api/requests").json()["requests"][0]
    assert entry["provider"] == "official"
    assert entry["route_decision"]["reason"] == "primary"


def test_smart_routing_does_not_treat_missing_history_as_zero_latency(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("known-good", 0),
            make_provider("newcomer", 0),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "_stats": {
                    "providers": {
                        "known-good": {
                            "attempts": 5,
                            "success": 5,
                            "failed": 0,
                            "streamed": 0,
                            "fallbacks": 0,
                            "latency_ms_total": 750,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
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
    assert len(calls) == 1
    assert "known-good.example" in calls[0]
    entry = client.get("/api/requests").json()["requests"][0]
    assert entry["provider"] == "known-good"
    assert entry["route_decision"]["reason"] == "primary"


def test_routing_preview_reports_selected_provider_without_calling_upstream(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0, api_key="hot-key"),
            make_provider("backup", 1, api_key="backup-key"),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "official": {
                    "key_cooldowns": {
                        main.key_fingerprint("hot-key"): main.time.time() + 60,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def fail_if_called():
        raise AssertionError("routing preview must not create an upstream HTTP client")

    monkeypatch.setattr(main, "create_http_client", fail_if_called)

    client = TestClient(main.app)
    response = client.get("/api/routing/preview?target=chat")

    assert response.status_code == 200
    data = response.json()
    assert data["target"] == "chat"
    assert "优先尝试" in data["message"]
    assert data["selected_provider"] == "backup"
    assert data["candidates"][0]["name"] == "backup"
    assert data["candidates"][0]["reason"] == "cooldown_avoided"
    assert "official" in data["candidates"][0]["message"]
    assert data["candidates"][1]["name"] == "official"
    assert data["candidates"][1]["all_keys_cooling"] is True


def test_routing_preview_uses_chinese_message_for_primary_route(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/routing/preview?target=chat")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["selected_provider"] == "official"
    assert "下一次" in data["message"]
    assert "official" in data["message"]


def test_routing_preview_reports_skipped_provider_reasons(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    disabled = make_provider("disabled-openai", 1)
    disabled["enabled"] = False
    missing_key = make_provider("missing-key-openai", 2, api_key="")
    claude = make_provider("claude-only", 3)
    claude["protocol"] = "claude"
    write_config(
        config_path,
        [
            make_provider("official", 0),
            disabled,
            missing_key,
            claude,
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/routing/preview?target=chat")

    assert response.status_code == 200
    data = response.json()
    assert data["selected_provider"] == "official"
    assert [item["name"] for item in data["candidates"]] == ["official"]
    skipped = {item["name"]: item for item in data["skipped_providers"]}
    assert skipped["disabled-openai"]["reason"] == "disabled"
    assert skipped["missing-key-openai"]["reason"] == "missing_key"
    assert skipped["claude-only"]["reason"] == "protocol_mismatch"
    assert skipped["claude-only"]["protocol"] == "claude"
    assert all("api_key" not in item for item in data["skipped_providers"])


def test_routing_preview_treats_skipped_malformed_priority_as_default(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    disabled = make_provider("disabled-bad-priority", "not-a-number")
    disabled["enabled"] = False
    write_config(
        config_path,
        [
            disabled,
            make_provider("official", 0),
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/routing/preview?target=chat")

    assert response.status_code == 200
    data = response.json()
    assert data["selected_provider"] == "official"
    assert data["skipped_providers"][0]["name"] == "disabled-bad-priority"
    assert data["skipped_providers"][0]["priority"] == 1000


def test_routing_preview_rejects_unknown_target(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/routing/preview?target=unknown")

    assert response.status_code == 400
    assert "Unknown routing preview target" in response.json()["detail"]


def test_routing_preview_reports_empty_target_without_error(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/routing/preview?target=claude")

    assert response.status_code == 200
    data = response.json()
    assert data["target"] == "claude"
    assert data["status"] == "empty"
    assert data["selected_provider"] is None
    assert data["candidates"] == []
    assert "暂无可用" in data["message"]


def test_stats_endpoint_aggregates_recent_attempts(tmp_path, monkeypatch):
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
    assert client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": []},
    ).status_code == 200

    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["total"]["attempts"] == 1
    assert data["total"]["success"] == 1
    assert data["providers"][0]["name"] == "official"
    assert data["providers"][0]["attempts"] == 1
    assert data["models"][0]["name"] == "gpt-4o"
    assert data["models"][0]["success"] == 1


def test_observability_delete_clears_logs_and_stats_but_keeps_provider_state(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text(
        json.dumps(
            {
                "official": {"calls": 3, "last_remaining": 7, "key_index": 1},
                "_requests": [{"provider": "official", "status": 200}],
                "_stats": {
                    "total": {
                        "attempts": 1,
                        "success": 1,
                        "failed": 0,
                        "streamed": 0,
                        "fallbacks": 0,
                        "latency_ms_total": 10,
                    },
                    "providers": {"official": {"attempts": 1, "success": 1}},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.delete("/api/observability")

    assert response.status_code == 200
    data = response.json()
    assert data["cleared"]["requests"] == 1
    assert data["cleared"]["stats"] is True
    assert data["requests"] == []
    assert data["stats"]["total"]["attempts"] == 0
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["official"] == {"calls": 3, "last_remaining": 7, "key_index": 1}
    assert saved["_requests"] == []
    assert "_stats" not in saved


def test_provider_status_includes_health_metrics_and_cooldown(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0, api_key="quota-key")])
    state_path.write_text(
        json.dumps(
            {
                "free": {
                    "calls": 3,
                    "last_remaining": 0,
                    "key_cooldowns": {
                        main.key_fingerprint("quota-key"): main.time.time() + 60,
                    },
                },
                "_stats": {
                    "providers": {
                        "free": {
                            "attempts": 4,
                            "success": 3,
                            "failed": 1,
                            "streamed": 1,
                            "fallbacks": 2,
                            "latency_ms_total": 500,
                        }
                    }
                },
                "_requests": [
                    {
                        "provider": "free",
                        "status": 429,
                        "error": "quota",
                        "time": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    provider = client.get("/api/providers").json()["providers"][0]

    assert provider["health"]["status"] == "cooldown"
    assert provider["health"]["success_rate"] == 75.0
    assert provider["health"]["avg_latency_ms"] == 125.0
    assert provider["health"]["recent_status"] == 429
    assert provider["health"]["cooldown_key_count"] == 1
    assert provider["health"]["cooldown_seconds"] > 0


def test_provider_status_treats_malformed_health_stats_as_unknown(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0, api_key="quota-key")])
    state_path.write_text(
        json.dumps(
            {
                "free": {
                    "calls": 0,
                    "last_remaining": None,
                },
                "_stats": {
                    "providers": {
                        "free": {
                            "attempts": "not-a-number",
                            "success": "bad",
                            "failed": "also-bad",
                            "latency_ms_total": "bad-latency",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["health"]["status"] == "unknown"
    assert provider["health"]["attempts"] == 0
    assert provider["health"]["success_rate"] is None
    assert provider["health"]["avg_latency_ms"] == 0


def test_provider_status_treats_malformed_stats_container_as_unknown(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0, api_key="quota-key")])
    state_path.write_text(
        json.dumps(
            {
                "free": {"calls": 1},
                "_stats": ["bad-stats"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["calls"] == 1
    assert provider["health"]["status"] == "unknown"
    assert provider["health"]["attempts"] == 0


def test_provider_status_treats_malformed_stats_provider_group_as_unknown(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0, api_key="quota-key")])
    state_path.write_text(
        json.dumps(
            {
                "free": {"calls": 1},
                "_stats": {"providers": ["bad-provider-group"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["calls"] == 1
    assert provider["health"]["status"] == "unknown"
    assert provider["health"]["attempts"] == 0


def test_provider_status_ignores_malformed_request_log_entries(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0, api_key="quota-key")])
    state_path.write_text(
        json.dumps(
            {
                "free": {"calls": 1},
                "_requests": ["broken", {"provider": "free", "status": 200, "time": "now"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["health"]["recent_status"] == 200


def test_provider_status_treats_malformed_state_root_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0, api_key="quota-key")])
    state_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["calls"] == 0
    assert provider["health"]["status"] == "unknown"


def test_provider_status_treats_invalid_state_json_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0, api_key="quota-key")])
    state_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["calls"] == 0
    assert provider["health"]["status"] == "unknown"


def test_recent_requests_treats_malformed_request_log_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0)])
    state_path.write_text(json.dumps({"_requests": {"bad": "shape"}}), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/requests")

    assert response.status_code == 200
    assert response.json()["requests"] == []


def test_recent_requests_treats_malformed_state_root_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0)])
    state_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/requests")

    assert response.status_code == 200
    assert response.json()["requests"] == []


def test_stats_treats_malformed_state_root_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0)])
    state_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["total"]["attempts"] == 0
    assert data["providers"] == []


def test_provider_status_treats_malformed_provider_state_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("free", 0, api_key="quota-key")])
    state_path.write_text(json.dumps({"free": []}), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["calls"] == 0
    assert provider["last_remaining"] is None
    assert provider["health"]["status"] == "unknown"


def test_proxy_request_replaces_malformed_provider_state(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text(json.dumps({"official": ["bad"]}), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            headers={"x-ratelimit-remaining": "3"},
        )

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["official"]["calls"] == 1
    assert saved["official"]["last_remaining"] == 3


def test_proxy_request_replaces_malformed_state_root(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            headers={"x-ratelimit-remaining": "5"},
        )

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(saved, dict)
    assert saved["official"]["calls"] == 1
    assert saved["official"]["last_remaining"] == 5


def test_proxy_request_recovers_malformed_provider_state_fields(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "official",
                        "base_url": "https://official.example/v1",
                        "priority": 0,
                        "api_keys": ["key-1", "key-2"],
                    }
                ],
                "default_model": "default-model",
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps(
            {
                "official": {
                    "calls": "bad-calls",
                    "last_remaining": None,
                    "key_index": "bad-index",
                    "key_cooldowns": ["bad-cooldowns"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    seen_auth = []

    def handler(request):
        seen_auth.append(request.headers["authorization"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            headers={"x-ratelimit-remaining": "3"},
        )

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    assert seen_auth == ["Bearer key-1"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["official"]["calls"] == 1
    assert saved["official"]["key_index"] == 1
    assert saved["official"]["key_cooldowns"] == {}
    assert saved["official"]["last_remaining"] == 3


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


def test_curl_streaming_logs_error_after_iterator_failure(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "_state_cache", None, raising=False)
    monkeypatch.setattr(main, "_state_cache_path", None, raising=False)

    async def failing_stream():
        yield b"data: partial\n\n"
        raise RuntimeError("curl stream failed")

    response = main._stream_callback(
        failing_stream(),
        "official",
        main.time.perf_counter(),
        0,
        [(0, "test-key")],
        0,
        None,
        True,
        "/v1/chat/completions",
        "gpt-4o",
        "",
        None,
    )

    async def consume_stream():
        chunks = []
        try:
            async for chunk in response.body_iterator:
                chunks.append(chunk)
        except RuntimeError as exc:
            assert str(exc) == "curl stream failed"
            return chunks
        raise AssertionError("expected curl stream failure")

    chunks = main.asyncio.run(consume_stream())

    assert chunks == [b"data: partial\n\n"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["_requests"][0]["stream_status"] == "stream_error"
    assert saved["_requests"][0]["error"] == "curl stream failed"


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


def test_configured_client_key_protects_v1_and_enforces_model_policy(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "gpt-4o",
                "providers": [make_provider("official", 0)],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                        "allowed_models": ["gpt-4o"],
                        "excluded_models": ["gpt-image-*"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "", raising=False)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    denied = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
    allowed = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer client-secret"},
        json={"model": "gpt-4o", "messages": []},
    )
    blocked_model = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer client-secret"},
        json={"model": "gpt-image-2", "messages": []},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert blocked_model.status_code == 403


def test_string_false_client_key_enabled_is_not_authorized(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "gpt-4o",
                "providers": [make_provider("official", 0)],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": "false",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "local-secret", raising=False)

    client = TestClient(main.app)
    denied = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer client-secret"},
        json={"model": "gpt-4o", "messages": []},
    )

    assert denied.status_code == 401


def test_configured_client_key_protects_v1_models_endpoint(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "gpt-4o",
                "providers": [make_provider("official", 0)],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "", raising=False)

    async def fake_fetch(provider):
        return [{"id": "gpt-4o"}]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    denied = client.get("/v1/models")
    allowed = client.get("/v1/models", headers={"Authorization": "Bearer client-secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["data"][0]["id"] == "gpt-4o"


def test_v1_models_filters_models_for_client_key_policy(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "gpt-4o",
                "providers": [make_provider("official", 0)],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                        "allowed_models": ["gpt-*"],
                        "excluded_models": ["gpt-image-*"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "", raising=False)

    async def fake_fetch(provider):
        return [
            {"id": "gpt-4o"},
            {"id": "gpt-image-2"},
            {"id": "claude-sonnet-4"},
        ]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.get("/v1/models", headers={"Authorization": "Bearer client-secret"})

    assert response.status_code == 200
    assert [model["id"] for model in response.json()["data"]] == ["gpt-4o"]


def test_v1_client_key_model_rules_accept_hand_edited_strings(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "gpt-4o",
                "providers": [make_provider("official", 0)],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                        "allowed_models": "gpt-*",
                        "excluded_models": "gpt-image-*",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "", raising=False)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    allowed = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer client-secret"},
        json={"model": "gpt-4o", "messages": []},
    )
    blocked = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer client-secret"},
        json={"model": "gpt-image-2", "messages": []},
    )

    assert allowed.status_code == 200
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Model 'gpt-image-2' is excluded for this local client key"


def test_v1_auth_ignores_malformed_client_key_entries(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "gpt-4o",
                "providers": [make_provider("official", 0)],
                "client_keys": [
                    "broken-entry",
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "", raising=False)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer client-secret"},
        json={"model": "gpt-4o", "messages": []},
    )

    assert response.status_code == 200


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


def test_v1_rate_limit_normalizes_token_identity(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "local-secret", raising=False)
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
    first = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer local-secret"},
        json={"messages": []},
    )
    second = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "bearer local-secret "},
        json={"messages": []},
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert all("local-secret" not in key for key in main.RATE_LIMIT_BUCKETS)


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


def test_v1_endpoints_reject_non_object_json_body(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [
                    {
                        "name": "openai",
                        "protocol": "openai",
                        "base_url": "https://openai.example/v1",
                        "priority": 0,
                        "api_keys": ["openai-key"],
                    },
                    {
                        "name": "claude",
                        "protocol": "claude",
                        "base_url": "https://claude.example/v1",
                        "priority": 1,
                        "api_keys": ["claude-key"],
                    },
                    {
                        "name": "gemini",
                        "protocol": "gemini",
                        "base_url": "https://gemini.example/v1beta",
                        "priority": 2,
                        "api_keys": ["gemini-key"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def fail_if_called():
        raise AssertionError("invalid v1 request body must not create an upstream HTTP client")

    monkeypatch.setattr(main, "create_http_client", fail_if_called)

    client = TestClient(main.app)
    paths = [
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages",
        "/v1beta/models/gemini-2.0-flash:generateContent",
    ]
    for path in paths:
        response = client.post(path, json=["bad-body"])
        assert response.status_code == 400
        assert response.json()["detail"] == "JSON request body must be an object"


def test_v1_endpoints_reject_non_string_model_field(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [
                    {
                        "name": "openai",
                        "protocol": "openai",
                        "base_url": "https://openai.example/v1",
                        "priority": 0,
                        "api_keys": ["openai-key"],
                    },
                    {
                        "name": "claude",
                        "protocol": "claude",
                        "base_url": "https://claude.example/v1",
                        "priority": 1,
                        "api_keys": ["claude-key"],
                    },
                    {
                        "name": "gemini",
                        "protocol": "gemini",
                        "base_url": "https://gemini.example/v1beta",
                        "priority": 2,
                        "api_keys": ["gemini-key"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def fail_if_called():
        raise AssertionError("invalid model field must not create an upstream HTTP client")

    monkeypatch.setattr(main, "create_http_client", fail_if_called)

    client = TestClient(main.app, raise_server_exceptions=False)
    paths = [
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages",
        "/v1beta/models/gemini-2.0-flash:generateContent",
    ]
    for path in paths:
        response = client.post(path, json={"model": ["bad-model"], "messages": []})
        assert response.status_code == 400
        assert response.json()["detail"] == "model must be a string"


def test_v1_endpoint_rejects_invalid_utf8_json_body(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    def fail_if_called():
        raise AssertionError("invalid v1 request body must not create an upstream HTTP client")

    monkeypatch.setattr(main, "create_http_client", fail_if_called)

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        content=b"\xff",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON request body"


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


def test_models_endpoint_continues_when_provider_model_fetch_fails(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("broken", 0, model="configured-fallback"),
            make_provider("working", 1),
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_fetch(provider):
        if provider["name"] == "broken":
            raise RuntimeError("model endpoint unavailable")
        return [{"id": "gpt-4o"}]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.get("/v1/models")

    assert response.status_code == 200
    assert [model["id"] for model in response.json()["data"]] == ["configured-fallback", "gpt-4o"]
    assert response.json()["data"][0]["owned_by"] == "broken"


def test_models_endpoint_normalizes_name_only_and_skips_malformed_models(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("gemini", 0)])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_fetch(provider):
        return [
            {"name": "models/gemini-2.0-flash"},
            ["bad-model"],
            {"id": ""},
            {"id": "gpt-4o"},
        ]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.get("/v1/models")

    assert response.status_code == 200
    data = response.json()["data"]
    assert [model["id"] for model in data] == ["gemini-2.0-flash", "gpt-4o"]
    assert data[0]["object"] == "model"
    assert data[0]["owned_by"] == "gemini"


def test_model_coverage_endpoint_groups_models_by_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0),
            make_provider("free", 1, model="fallback-model"),
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    async def fake_fetch(provider):
        if provider["name"] == "official":
            return [{"id": "gpt-4o"}, {"id": "shared-model"}]
        return [{"id": "shared-model"}, {"id": "mimo-v2.5"}]

    monkeypatch.setattr(main, "fetch_provider_models", fake_fetch)

    client = TestClient(main.app)
    response = client.get("/api/model-coverage")

    assert response.status_code == 200
    data = response.json()
    assert data["unique_model_count"] == 3
    shared = next(model for model in data["models"] if model["id"] == "shared-model")
    assert shared["providers"] == ["official", "free"]
    assert data["providers"][0]["name"] == "official"
    assert data["providers"][0]["model_count"] == 2


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


def test_key_rotation_uses_total_key_count_when_some_keys_are_cooled(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            {
                "name": "free",
                "base_url": "https://free.example/v1",
                "priority": 0,
                "api_keys": ["key-1", "key-2", "key-3"],
            }
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "free": {
                    "calls": 0,
                    "key_index": 1,
                    "key_cooldowns": {
                        main.key_fingerprint("key-2"): 9999999999,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
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

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert seen_auth == ["Bearer key-3"]
    assert state["free"]["key_index"] == 0


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


def test_config_import_rejects_non_object_payload(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.post("/api/config/import", json=["bad-payload"])

    assert response.status_code == 400
    assert response.json()["detail"] == "config payload must be an object"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "secret-key"


def test_config_import_rejects_invalid_utf8_json_body(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.post("/api/config/import", content=b"\xff", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON request body"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "secret-key"


def test_dashboard_config_rejects_request_body_over_size_limit(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 32, raising=False)

    payload = json.dumps({"default_model": "x" * 80, "providers": []}).encode("utf-8")

    client = TestClient(main.app)
    response = client.post("/api/config", content=payload, headers={"content-type": "application/json"})

    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "secret-key"


def test_config_import_rejects_request_body_over_size_limit(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 32, raising=False)

    payload = json.dumps({"default_model": "x" * 80, "providers": []}).encode("utf-8")

    client = TestClient(main.app)
    response = client.post("/api/config/import", content=payload, headers={"content-type": "application/json"})

    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "secret-key"


def test_config_export_redacted_masks_provider_and_client_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "gpt-4o",
                "providers": [
                    {
                        "name": "official",
                        "base_url": "https://official.example/v1",
                        "priority": 0,
                        "api_keys": ["provider-secret-1", "provider-secret-2"],
                        "api_key_env": "OFFICIAL_KEY",
                        "model_aliases": {"gpt-4o-mini": "gpt-4o-mini"},
                    }
                ],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                        "allowed_models": ["gpt-4o"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config/export?redacted=true")

    assert response.status_code == 200
    assert "provider-secret" not in response.text
    assert "client-secret" not in response.text
    data = response.json()
    assert data["redacted"] is True
    assert data["providers"][0]["api_key"] == ""
    assert data["providers"][0]["api_keys"] == []
    assert data["providers"][0]["has_api_key"] is True
    assert data["providers"][0]["key_count"] == 2
    assert data["providers"][0]["api_key_env"] == "OFFICIAL_KEY"
    assert data["providers"][0]["model_aliases"] == {"gpt-4o-mini": "gpt-4o-mini"}
    assert data["client_keys"][0]["key"] == ""
    assert data["client_keys"][0]["has_key"] is True
    assert data["client_keys"][0]["allowed_models"] == ["gpt-4o"]


def test_config_import_rejects_redacted_export_without_erasing_secrets(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "gpt-4o",
                "providers": [
                    {
                        "name": "official",
                        "base_url": "https://official.example/v1",
                        "priority": 0,
                        "api_key": "secret-key",
                    }
                ],
                "client_keys": [
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    redacted_export = client.get("/api/config/export?redacted=true").json()
    response = client.post("/api/config/import", json=redacted_export)

    assert response.status_code == 400
    assert response.json()["detail"] == "Redacted config exports cannot be imported because secrets are omitted"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "secret-key"
    assert saved["client_keys"][0]["key"] == "client-secret"


def test_config_export_redacted_treats_malformed_priority_as_default(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("bad-priority", "not-a-number"),
            make_provider("official", 0),
        ],
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config/export?redacted=true")

    assert response.status_code == 200
    providers = response.json()["providers"]
    assert [provider["name"] for provider in providers] == ["official", "bad-priority"]
    assert providers[1]["priority"] == 1000


def test_config_export_redacted_treats_malformed_model_aliases_as_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0)
    provider["model_aliases"] = ["bad-shape"]
    write_config(config_path, [provider])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config/export?redacted=true")

    assert response.status_code == 200
    assert response.json()["providers"][0]["model_aliases"] == {}


def test_config_export_redacted_sanitizes_malformed_provider_strings(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    provider = make_provider("official", 0)
    provider["name"] = ["official"]
    provider["base_url"] = ["https://official.example/v1"]
    provider["model"] = ["bad-model"]
    write_config(config_path, [provider])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config/export?redacted=true")

    assert response.status_code == 200
    exported_provider = response.json()["providers"][0]
    assert exported_provider["name"] == ""
    assert exported_provider["base_url"] == ""
    assert exported_provider["model"] == "default-model"
    assert "bad-model" not in response.text


def test_config_export_redacted_ignores_malformed_client_key_entries(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "default_model": "default-model",
                "providers": [make_provider("official", 0)],
                "client_keys": [
                    "broken-entry",
                    {
                        "id": "chatbox",
                        "label": "ChatBox",
                        "key": "client-secret",
                        "enabled": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    client = TestClient(main.app)
    response = client.get("/api/config/export?redacted=true")

    assert response.status_code == 200
    assert "client-secret" not in response.text
    client_keys = response.json()["client_keys"]
    assert len(client_keys) == 1
    assert client_keys[0]["id"] == "chatbox"
    assert client_keys[0]["key"] == ""
    assert client_keys[0]["has_key"] is True


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


def test_management_proxy_access_token_uses_shared_token_parser(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(config_path, [make_provider("official", 0, api_key="secret-key")])
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "PROXY_ACCESS_TOKEN", "local-secret", raising=False)

    client = TestClient(main.app)

    bearer = client.get("/api/config/export", headers={"Authorization": "bearer local-secret "})
    x_api_key = client.get("/api/config/export", headers={"x-api-key": " local-secret "})

    assert bearer.status_code == 200
    assert x_api_key.status_code == 200


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


def test_fetch_claude_models_uses_native_models_endpoint(monkeypatch):
    provider = {
        "name": "claude",
        "protocol": "claude",
        "base_url": "https://api.anthropic.com/v1",
        "api_keys": ["claude-test-key"],
    }
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-20250514"}]})

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)

    models = main.asyncio.run(main.fetch_provider_models(provider))

    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert seen["headers"]["x-api-key"] == "claude-test-key"
    assert "authorization" not in seen["headers"]
    assert models == [{"id": "claude-sonnet-4-20250514", "object": "model"}]


def test_fetch_gemini_models_normalizes_model_names(monkeypatch):
    provider = {
        "name": "gemini",
        "protocol": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_keys": ["gemini-test-key"],
    }
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"models": [{"name": "models/gemini-2.0-flash"}]})

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)

    models = main.asyncio.run(main.fetch_provider_models(provider))

    assert seen["url"] == "https://generativelanguage.googleapis.com/v1beta/models"
    assert seen["headers"]["x-goog-api-key"] == "gemini-test-key"
    assert "authorization" not in seen["headers"]
    assert models == [{"id": "gemini-2.0-flash", "object": "model", "owned_by": "gemini"}]
