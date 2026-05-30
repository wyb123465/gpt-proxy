import base64
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON file: {path}") from exc


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    temp_path.replace(path)


def encryption_fernet() -> Fernet | None:
    secret = getattr(sys.modules["main"], "CONFIG_ENCRYPTION_SECRET", "")
    if not secret:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret_value(value: str) -> str:
    fernet = encryption_fernet()
    if not fernet or value.startswith("enc:"):
        return value
    return "enc:" + fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret_value(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("enc:"):
        return value
    fernet = encryption_fernet()
    if not fernet:
        raise RuntimeError("Encrypted config.json API key requires GPT_PROXY_CONFIG_SECRET")
    try:
        return fernet.decrypt(value[4:].encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Unable to decrypt config.json API key; check GPT_PROXY_CONFIG_SECRET") from exc


def decrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    decrypted = dict(config)
    providers = []
    for provider in config.get("providers", []):
        item = dict(provider)
        if item.get("api_key"):
            item["api_key"] = decrypt_secret_value(str(item["api_key"]))
        if isinstance(item.get("api_keys"), list):
            item["api_keys"] = [decrypt_secret_value(str(key)) for key in item["api_keys"]]
        providers.append(item)
    decrypted["providers"] = providers
    return decrypted


def encrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    if not encryption_fernet():
        return config
    encrypted = dict(config)
    providers = []
    for provider in config.get("providers", []):
        item = dict(provider)
        if item.get("api_key"):
            item["api_key"] = encrypt_secret_value(str(item["api_key"]))
        if isinstance(item.get("api_keys"), list):
            item["api_keys"] = [encrypt_secret_value(str(key)) for key in item["api_keys"]]
        providers.append(item)
    encrypted["providers"] = providers
    return encrypted


def provider_api_keys(provider: dict[str, Any]) -> list[str]:
    keys = []
    api_keys = provider.get("api_keys")
    if isinstance(api_keys, list):
        keys.extend(str(key).strip() for key in api_keys if str(key).strip())
    if provider.get("api_key"):
        api_key = str(provider["api_key"]).strip()
        if api_key and api_key not in keys:
            keys.append(api_key)
    return keys


def apply_env_overrides(provider: dict[str, Any]) -> dict[str, Any]:
    provider = dict(provider)
    env_key_name = provider.get("api_key_env")
    if env_key_name:
        provider["api_key"] = os.getenv(env_key_name, provider.get("api_key", ""))
    return provider


def normalize_provider(provider: dict[str, Any], existing_provider: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(provider.get("name", "")).strip()
    base_url = str(provider.get("base_url", "")).strip().rstrip("/")
    model = str(provider.get("model", "")).strip()
    api_key = str(provider.get("api_key", "")).strip()
    api_key_env = str(provider.get("api_key_env", "")).strip()
    use_curl = bool(provider.get("use_curl", False))
    enabled = bool(provider.get("enabled", True))
    model_aliases = provider.get("model_aliases") or {}
    if not isinstance(model_aliases, dict):
        model_aliases = {}

    if not name:
        raise ValueError("Provider name is required")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"Provider '{name}' base_url must start with http:// or https://")

    try:
        priority = int(provider.get("priority", 1000))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Provider '{name}' priority must be a number") from exc

    normalized: dict[str, Any] = {
        "name": name,
        "base_url": base_url,
        "model": model,
        "priority": priority,
        "enabled": enabled,
    }

    keys = []
    api_keys = provider.get("api_keys")
    if isinstance(api_keys, list):
        keys = [str(key).strip() for key in api_keys if str(key).strip()]
    if api_key:
        keys = [api_key]
    elif not keys and existing_provider:
        keys = provider_api_keys(existing_provider)
    if keys:
        normalized["api_keys"] = keys
        normalized["api_key"] = keys[0]

    if api_key_env:
        normalized["api_key_env"] = api_key_env
    elif existing_provider and existing_provider.get("api_key_env") and not keys:
        normalized["api_key_env"] = existing_provider["api_key_env"]

    if use_curl or (existing_provider and existing_provider.get("use_curl") and not api_key):
        normalized["use_curl"] = True

    if model_aliases:
        normalized["model_aliases"] = model_aliases

    return normalized


def normalize_config_payload(payload: dict[str, Any], existing_config: dict[str, Any] | None = None) -> dict[str, Any]:
    existing_config = existing_config or {"providers": []}
    existing_by_name = {
        provider.get("name"): provider
        for provider in existing_config.get("providers", [])
        if provider.get("name")
    }
    default_model = str(payload.get("default_model", "gpt-3.5-turbo")).strip() or "gpt-3.5-turbo"
    raw_providers = payload.get("providers", [])
    if not isinstance(raw_providers, list):
        raise ValueError("providers must be a list")

    providers = []
    seen_names = set()
    for provider in raw_providers:
        normalized = normalize_provider(provider, existing_by_name.get(provider.get("name")))
        if normalized["name"] in seen_names:
            raise ValueError(f"Provider '{normalized['name']}' is duplicated")
        seen_names.add(normalized["name"])
        providers.append(normalized)

    return {
        "providers": sorted(providers, key=lambda item: item.get("priority", 1000)),
        "default_model": default_model,
    }


def editable_provider(provider: dict[str, Any], state: dict[str, Any], default_model: str) -> dict[str, Any]:
    provider_state = state.get(provider.get("name", ""), {})
    resolved = apply_env_overrides(provider)
    keys = provider_api_keys(resolved)
    return {
        "name": provider.get("name", ""),
        "base_url": provider.get("base_url", ""),
        "model": provider.get("model", default_model),
        "priority": provider.get("priority", 1000),
        "api_key": "",
        "api_keys": [],
        "api_key_env": provider.get("api_key_env", ""),
        "has_api_key": bool(keys),
        "key_count": len(keys),
        "enabled": bool(provider.get("enabled", True)),
        "use_curl": bool(provider.get("use_curl", False)),
        "model_aliases": provider.get("model_aliases") or {},
        "calls": provider_state.get("calls", 0),
        "last_remaining": provider_state.get("last_remaining"),
    }


def response_text_from_chat(chat_data: dict[str, Any]) -> str:
    try:
        return chat_data.get("choices", [{}])[0].get("message", {}).get("content") or ""
    except (AttributeError, IndexError):
        return ""


def responses_input_to_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    input_value = payload.get("input", "")
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if isinstance(input_value, list):
        messages = []
        for item in input_value:
            if isinstance(item, dict) and item.get("role") and "content" in item:
                messages.append({"role": item["role"], "content": item["content"]})
            else:
                messages.append({"role": "user", "content": json.dumps(item, ensure_ascii=False)})
        return messages or [{"role": "user", "content": ""}]
    return [{"role": "user", "content": json.dumps(input_value, ensure_ascii=False)}]


def chat_to_responses_payload(chat_data: dict[str, Any], requested_model: str | None) -> dict[str, Any]:
    text = response_text_from_chat(chat_data)
    model = chat_data.get("model") or requested_model or ""
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "output_text": text,
        "usage": chat_data.get("usage"),
    }
