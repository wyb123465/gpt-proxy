from main._state import record_request_stats, summarize_request_stats


def test_record_request_stats_replaces_malformed_stats_container():
    state = {"_stats": []}
    entry = {
        "provider": "official",
        "status": 200,
        "latency_ms": 12.5,
        "fallback_count": 0,
        "path": "/v1/chat/completions",
        "model": "gpt-4o",
        "client_key": "ChatBox",
    }

    record_request_stats(state, entry)

    summary = summarize_request_stats(state)
    assert summary["total"]["attempts"] == 1
    assert summary["total"]["success"] == 1
    assert summary["providers"][0]["name"] == "official"
    assert summary["models"][0]["name"] == "gpt-4o"
    assert summary["client_keys"][0]["name"] == "ChatBox"


def test_summarize_request_stats_skips_malformed_group_counters():
    state = {
        "_stats": {
            "total": {"attempts": "bad"},
            "providers": {
                "official": [],
                "backup": {"attempts": 2, "success": 1, "failed": 1, "latency_ms_total": 40},
            },
            "models": [],
            "paths": {},
            "client_keys": {},
        }
    }

    summary = summarize_request_stats(state)

    assert summary["total"]["attempts"] == 0
    assert summary["providers"] == [
        {
            "name": "backup",
            "attempts": 2,
            "success": 1,
            "failed": 1,
            "streamed": 0,
            "fallbacks": 0,
            "avg_latency_ms": 20.0,
        }
    ]
    assert summary["models"] == []


def test_summarize_request_stats_treats_malformed_state_root_as_empty():
    summary = summarize_request_stats(["bad-root"])

    assert summary["total"]["attempts"] == 0
    assert summary["providers"] == []
    assert summary["models"] == []
    assert summary["paths"] == []
    assert summary["client_keys"] == []


def test_summarize_request_stats_treats_malformed_stats_container_as_empty():
    summary = summarize_request_stats({"_stats": ["bad-stats"]})

    assert summary["total"]["attempts"] == 0
    assert summary["providers"] == []
    assert summary["models"] == []
    assert summary["paths"] == []
    assert summary["client_keys"] == []
