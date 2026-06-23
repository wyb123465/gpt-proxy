import base64
import hashlib
import json
import os
import sys
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


def _object_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def provider_entries(config: Any) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    return _object_entries(config.get("providers"))


def client_key_entries(config: Any) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    return _object_entries(config.get("client_keys"))


def decrypt_config(config: Any) -> dict[str, Any]:
    decrypted = dict(config) if isinstance(config, dict) else {}
    providers = []
    for provider in provider_entries(config):
        item = dict(provider)
        if isinstance(item.get("api_key"), str) and item["api_key"]:
            item["api_key"] = decrypt_secret_value(item["api_key"])
        elif "api_key" in item and not isinstance(item.get("api_key"), str):
            item["api_key"] = ""
        if isinstance(item.get("api_keys"), list):
            item["api_keys"] = [
                decrypt_secret_value(key)
                for key in item["api_keys"]
                if isinstance(key, str) and key.strip()
            ]
        providers.append(item)
    decrypted["providers"] = providers
    client_keys = []
    for client_key in client_key_entries(config):
        item = dict(client_key)
        if isinstance(item.get("key"), str) and item["key"]:
            item["key"] = decrypt_secret_value(item["key"])
        elif "key" in item and not isinstance(item.get("key"), str):
            item["key"] = ""
        client_keys.append(item)
    decrypted["client_keys"] = client_keys
    return decrypted


def encrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    if not encryption_fernet():
        return config
    encrypted = dict(config)
    providers = []
    for provider in provider_entries(config):
        item = dict(provider)
        if isinstance(item.get("api_key"), str) and item["api_key"]:
            item["api_key"] = encrypt_secret_value(item["api_key"])
        elif "api_key" in item and not isinstance(item.get("api_key"), str):
            item["api_key"] = ""
        if isinstance(item.get("api_keys"), list):
            item["api_keys"] = [
                encrypt_secret_value(key)
                for key in item["api_keys"]
                if isinstance(key, str) and key.strip()
            ]
        providers.append(item)
    encrypted["providers"] = providers
    client_keys = []
    for client_key in client_key_entries(config):
        item = dict(client_key)
        if isinstance(item.get("key"), str) and item["key"]:
            item["key"] = encrypt_secret_value(item["key"])
        elif "key" in item and not isinstance(item.get("key"), str):
            item["key"] = ""
        client_keys.append(item)
    encrypted["client_keys"] = client_keys
    return encrypted


def safe_secret_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def provider_api_keys(provider: dict[str, Any]) -> list[str]:
    keys = []
    api_keys = provider.get("api_keys")
    if isinstance(api_keys, list):
        keys.extend(key for item in api_keys if (key := safe_secret_value(item)))
    api_key = safe_secret_value(provider.get("api_key", ""))
    if api_key and api_key not in keys:
        keys.append(api_key)
    return keys


def safe_model_aliases(provider: dict[str, Any]) -> dict[str, str]:
    aliases = provider.get("model_aliases")
    if not isinstance(aliases, dict):
        return {}
    cleaned: dict[str, str] = {}
    for source, target in aliases.items():
        source_name = str(source).strip()
        target_name = str(target).strip()
        if source_name and target_name:
            cleaned[source_name] = target_name
    return cleaned


def safe_provider_model(provider: dict[str, Any]) -> str:
    value = provider.get("model", "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def apply_env_overrides(provider: dict[str, Any]) -> dict[str, Any]:
    provider = dict(provider)
    env_key_name = safe_api_key_env(provider)
    if env_key_name:
        provider["api_key"] = os.getenv(env_key_name, provider.get("api_key", ""))
    return provider


def safe_api_key_env(provider: dict[str, Any]) -> str:
    value = provider.get("api_key_env", "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def safe_provider_name(provider: dict[str, Any]) -> str:
    value = provider.get("name", "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def safe_provider_base_url(provider: dict[str, Any]) -> str:
    value = provider.get("base_url", "")
    if not isinstance(value, str):
        return ""
    base_url = value.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        return ""
    return base_url


def safe_provider_protocol(provider: dict[str, Any], default: str = "openai") -> str:
    value = provider.get("protocol", default)
    if not isinstance(value, str):
        return default
    protocol = value.strip().lower() or default
    return protocol if protocol in {"openai", "domestic", "claude", "gemini"} else default


def safe_provider_priority(provider: dict[str, Any], default: int = 1000) -> int:
    try:
        return int(provider.get("priority", default))
    except (TypeError, ValueError):
        return default


def provider_with_safe_priority(provider: dict[str, Any], default: int = 1000) -> dict[str, Any]:
    normalized = dict(provider)
    normalized["priority"] = safe_provider_priority(provider, default)
    return normalized


def safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        if not normalized:
            return default
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def normalize_provider(provider: dict[str, Any], existing_provider: dict[str, Any] | None = None) -> dict[str, Any]:
    name = safe_provider_name(provider)
    base_url = safe_provider_base_url(provider)
    model = safe_provider_model(provider)
    api_key = safe_secret_value(provider.get("api_key", ""))
    api_key_env_present = "api_key_env" in provider
    api_key_env = safe_api_key_env(provider)
    use_curl_present = "use_curl" in provider
    use_curl = safe_bool(provider.get("use_curl"), False)
    enabled = safe_bool(provider.get("enabled"), True)
    raw_protocol = provider.get("protocol", "openai")
    protocol = safe_provider_protocol(provider)
    raw_protocol_name = raw_protocol.strip().lower() if isinstance(raw_protocol, str) else ""
    if raw_protocol_name and raw_protocol_name != protocol:
        raise ValueError(f"Provider '{name}' protocol must be one of openai/domestic/claude/gemini")
    model_aliases = safe_model_aliases(provider)

    if not name:
        raise ValueError("Provider name is required")
    if not base_url:
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

    if protocol != "openai":
        normalized["protocol"] = protocol

    keys = []
    api_keys = provider.get("api_keys")
    if isinstance(api_keys, list):
        keys = [key for item in api_keys if (key := safe_secret_value(item))]
    if api_key:
        keys = [api_key]
    elif not keys and existing_provider:
        keys = provider_api_keys(existing_provider)
    if keys:
        normalized["api_keys"] = keys
        normalized["api_key"] = keys[0]

    if api_key_env:
        normalized["api_key_env"] = api_key_env
    elif existing_provider and existing_provider.get("api_key_env") and not keys and not api_key_env_present:
        normalized["api_key_env"] = existing_provider["api_key_env"]

    if use_curl or (
        existing_provider
        and safe_bool(existing_provider.get("use_curl"), False)
        and not use_curl_present
    ):
        normalized["use_curl"] = True

    if model_aliases:
        normalized["model_aliases"] = model_aliases

    return normalized


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]
    return []


def client_key_model_rules(client_key: dict[str, Any], field: str) -> list[str]:
    return _string_list(client_key.get(field))


def normalize_client_key(
    client_key: dict[str, Any],
    existing_client_key: dict[str, Any] | None = None,
    fallback_id: str = "client-key",
) -> dict[str, Any]:
    key_id = str(client_key.get("id", "")).strip() or fallback_id
    label = str(client_key.get("label", "")).strip() or key_id
    key = safe_secret_value(client_key.get("key", ""))
    enabled = safe_bool(client_key.get("enabled"), True)

    normalized: dict[str, Any] = {
        "id": key_id,
        "label": label,
        "enabled": enabled,
        "allowed_models": client_key_model_rules(client_key, "allowed_models"),
        "excluded_models": client_key_model_rules(client_key, "excluded_models"),
    }
    if key:
        normalized["key"] = key
    elif existing_client_key and safe_secret_value(existing_client_key.get("key", "")):
        normalized["key"] = safe_secret_value(existing_client_key.get("key", ""))
    return normalized


def normalize_config_payload(payload: Any, existing_config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("config payload must be an object")
    existing_config = existing_config or {"providers": []}
    existing_by_name = {
        provider.get("name"): provider
        for provider in provider_entries(existing_config)
        if provider.get("name")
    }
    default_model = str(payload.get("default_model", "gpt-3.5-turbo")).strip() or "gpt-3.5-turbo"
    raw_providers = payload.get("providers", [])
    if not isinstance(raw_providers, list):
        raise ValueError("providers must be a list")

    providers = []
    seen_names = set()
    for provider in raw_providers:
        if not isinstance(provider, dict):
            raise ValueError("providers must contain objects")
        normalized = normalize_provider(provider, existing_by_name.get(provider.get("name")))
        if normalized["name"] in seen_names:
            raise ValueError(f"Provider '{normalized['name']}' is duplicated")
        seen_names.add(normalized["name"])
        providers.append(normalized)

    existing_client_keys = {
        client_key.get("id"): client_key
        for client_key in client_key_entries(existing_config)
        if client_key.get("id")
    }
    raw_client_keys = payload.get("client_keys", existing_config.get("client_keys", []))
    if not isinstance(raw_client_keys, list):
        raise ValueError("client_keys must be a list")

    client_keys = []
    seen_client_key_ids = set()
    for index, client_key in enumerate(raw_client_keys):
        if not isinstance(client_key, dict):
            raise ValueError("client_keys must contain objects")
        saved_client_key_id = str(
            client_key.get("saved_id")
            or client_key.get("_saved_id")
            or client_key.get("_savedId")
            or ""
        ).strip()
        normalized = normalize_client_key(
            client_key,
            existing_client_keys.get(client_key.get("id")) or existing_client_keys.get(saved_client_key_id),
            fallback_id=f"client-key-{index + 1}",
        )
        if normalized["id"] in seen_client_key_ids:
            raise ValueError(f"Client key '{normalized['id']}' is duplicated")
        seen_client_key_ids.add(normalized["id"])
        client_keys.append(normalized)

    return {
        "providers": sorted(providers, key=safe_provider_priority),
        "default_model": default_model,
        "client_keys": client_keys,
    }


def editable_provider(provider: dict[str, Any], state: dict[str, Any], default_model: str) -> dict[str, Any]:
    name = safe_provider_name(provider)
    base_url = safe_provider_base_url(provider)
    provider_state = state.get(name, {})
    resolved = apply_env_overrides(provider)
    keys = provider_api_keys(resolved)
    return {
        "name": name,
        "base_url": base_url,
        "model": safe_provider_model(provider) or default_model,
        "priority": safe_provider_priority(provider),
        "protocol": safe_provider_protocol(provider),
        "api_key": "",
        "api_keys": [],
        "api_key_env": safe_api_key_env(provider),
        "has_api_key": bool(keys),
        "key_count": len(keys),
        "enabled": safe_bool(provider.get("enabled"), True),
        "use_curl": safe_bool(provider.get("use_curl"), False),
        "model_aliases": safe_model_aliases(provider),
        "calls": provider_state.get("calls", 0),
        "last_remaining": provider_state.get("last_remaining"),
    }


def editable_client_key(client_key: dict[str, Any]) -> dict[str, Any]:
    key = safe_secret_value(client_key.get("key", ""))
    return {
        "id": client_key.get("id", ""),
        "label": client_key.get("label", ""),
        "key": "",
        "has_key": bool(key),
        "enabled": safe_bool(client_key.get("enabled"), True),
        "allowed_models": client_key_model_rules(client_key, "allowed_models"),
        "excluded_models": client_key_model_rules(client_key, "excluded_models"),
    }
