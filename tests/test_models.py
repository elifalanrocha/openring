"""The model picker's list: each model with its cost a minute and its time to answer (GET /models)."""

from openring import routes, stats
from openring.providers import LLM_PROVIDERS

from test_api import AUTH, make_client


def test_models_need_a_key_and_a_known_provider():
    client = make_client()
    assert client.get("/models?provider=groq").status_code == 401
    assert client.get("/models?provider=nope", headers=AUTH).status_code == 400
    assert "ollama" in LLM_PROVIDERS


def test_each_model_has_its_cost_a_minute_and_a_time_only_when_measured(monkeypatch):
    monkeypatch.setattr(routes, "_speed", {})
    client = make_client()
    data = client.get("/models?provider=groq", headers=AUTH).json()
    by_id = {m["id"]: m for m in data["models"]}
    fast = by_id["openai/gpt-oss-20b"]
    assert fast["latencyMs"] == 750 and fast["latencySource"] == "OpenRing's tests"  # measured on test calls
    price = (fast["inputPerM"], fast["outputPerM"])
    assert fast["costPerMinute"] == stats.llm_per_minute(price) and fast["costPerMinute"] > 0
    others = [m for m in data["models"] if m["id"] != "openai/gpt-oss-20b"]
    assert others and all(m["latencyMs"] is None for m in others)  # unknown stays unknown, never guessed
    assert data["assumes"]["modelRequests"] == stats.ESTIMATE["modelRequests"]

    routes.record_speed("groq", "openai/gpt-oss-120b", 420)  # this server's own call beats the reference
    again = {m["id"]: m for m in client.get("/models?provider=groq", headers=AUTH).json()["models"]}
    assert again["openai/gpt-oss-120b"]["latencyMs"] == 420
    assert again["openai/gpt-oss-120b"]["latencySource"] == "this server"


def test_a_provider_without_list_prices_has_an_empty_list_and_a_local_one_is_free(monkeypatch):
    client = make_client()
    assert client.get("/models?provider=nebius", headers=AUTH).json()["models"] == []
    options = stats.model_options("ollama", ["llama3.2"])["models"]
    assert options[0]["id"] == "llama3.2" and options[0]["costPerMinute"] == 0.0  # runs on this computer


def test_measuring_a_model_times_it_and_remembers(monkeypatch):
    monkeypatch.setattr(routes, "_speed", {})
    client = make_client()
    seen = []

    async def probe(model, plan):
        seen.append((model, plan.llm_model))
        routes.record_speed(model["provider"], model["model"], 333)
        return {"firstTokenMs": 333}

    monkeypatch.setattr(routes, "probe", probe)
    ok = client.post("/models/measure", json={"provider": "groq", "model": "openai/gpt-oss-120b"}, headers=AUTH)
    assert ok.status_code == 200 and ok.json() == {"firstTokenMs": 333}
    assert seen == [({"provider": "groq", "model": "openai/gpt-oss-120b"}, "openai/gpt-oss-120b")]
    assert client.post("/models/measure", json={"provider": "nope", "model": "x"}, headers=AUTH).status_code == 400
    assert client.post("/models/measure", json={"provider": "groq", "model": "x"}).status_code == 401

    async def failing(model, plan):
        return {"error": "no key: add GROQ_API_KEY"}

    monkeypatch.setattr(routes, "probe", failing)
    refused = client.post("/models/measure", json={"provider": "groq", "model": "x"}, headers=AUTH)
    assert refused.status_code == 400 and "GROQ_API_KEY" in refused.json()["detail"]


def test_one_list_across_providers_says_which_have_a_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    client = make_client()
    data = client.get("/models", headers=AUTH).json()
    providers = {m["provider"] for m in data["models"]}
    assert {"groq", "openai", "anthropic", "openrouter"} <= providers  # every provider with a price list
    assert all({"provider", "id", "costPerMinute", "latencyMs", "keySet"} <= set(m) for m in data["models"])
    groq = [m for m in data["models"] if m["provider"] == "groq"]
    assert groq and all(m["keySet"] for m in groq)
    assert all(not m["keySet"] for m in data["models"] if m["provider"] == "mistral")
    one = client.get("/models?provider=groq", headers=AUTH).json()["models"]
    assert [m["id"] for m in one] == [m["id"] for m in groq]
