import json
import re
import tomllib

import main


def test_config_example_is_safe_to_copy_before_editing():
    config = json.loads((main.BASE_DIR / "config.example.json").read_text(encoding="utf-8"))

    for provider in config["providers"]:
        assert provider["enabled"] is False
        assert main.provider_api_keys(provider) == []

    for client_key in config["client_keys"]:
        assert client_key["enabled"] is False
        assert client_key.get("key", "") == ""


def test_requirements_match_project_runtime_dependencies():
    pyproject = tomllib.loads((main.BASE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    project_dependencies = {
        dependency.strip().lower()
        for dependency in pyproject["project"]["dependencies"]
    }
    requirements = {
        line.strip().lower()
        for line in (main.BASE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    assert requirements == project_dependencies


def test_env_example_variables_are_documented():
    env_example = (main.BASE_DIR / ".env.example").read_text(encoding="utf-8")
    readme = (main.BASE_DIR / "README.md").read_text(encoding="utf-8")
    variables = {
        match.group(1)
        for line in env_example.splitlines()
        if (match := re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", line))
    }

    assert variables
    missing = sorted(
        variable
        for variable in variables
        if not re.search(rf"(?<![A-Z0-9_]){re.escape(variable)}(?![A-Z0-9_])", readme)
    )
    assert missing == []
