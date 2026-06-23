from main._routing import order_providers_for_request


def make_provider(name, priority=0, api_key="test-key"):
    return {
        "name": name,
        "base_url": f"https://{name}.example/v1",
        "api_key": api_key,
        "priority": priority,
        "enabled": True,
    }


def test_routing_treats_malformed_stats_as_missing_history():
    providers = [
        make_provider("official", 0),
        make_provider("backup", 1),
    ]
    state = {
        "_stats": {
            "providers": {
                "official": {
                    "attempts": "not-a-number",
                    "success": "also-bad",
                    "latency_ms_total": "bad-latency",
                }
            }
        }
    }

    candidates = order_providers_for_request(providers, state, now=1000)

    assert [candidate.provider["name"] for candidate in candidates] == ["official", "backup"]
    assert candidates[0].reason == "primary"


def test_routing_treats_string_false_enabled_as_disabled():
    providers = [
        {**make_provider("disabled", 0), "enabled": "false"},
        make_provider("backup", 1),
    ]

    candidates = order_providers_for_request(providers, {}, now=1000)

    assert [candidate.provider["name"] for candidate in candidates] == ["backup"]
