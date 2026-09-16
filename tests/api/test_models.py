import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_get_llm_inference_models(client: AsyncClient):
    """Test retrieving LLM inference models."""
    response = await client.get("/models/llm")
    assert response.status_code == 200
    data = response.json()
    assert "models" in data
    assert len(data["models"]) > 0

    first_model = data["models"][0]
    assert "model_id" in first_model
    assert "provider" in first_model
    assert "context_window" in first_model
    assert "providers" in first_model


@pytest.mark.anyio
async def test_get_capabilities(client: AsyncClient):
    """Test retrieving capabilities."""
    response = await client.get("/models/capabilities")
    assert response.status_code == 200
    data = response.json()
    assert "llm" in data
    assert "stt" in data
    assert "vision" in data
    assert "features" in data["llm"]


@pytest.mark.anyio
async def test_estimate_cost(client: AsyncClient):
    """Test cost estimation endpoint."""
    # Find a valid model first
    models_res = await client.get("/models/llm")
    model_id = models_res.json()["models"][0]["model_id"]
    provider = list(models_res.json()["models"][0]["providers"].keys())[0]

    response = await client.post(
        "/models/estimate-cost",
        params={
            "model_id": model_id,
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "provider": provider,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["model_id"] == model_id
    assert data["total_cost_usd"] > 0
    assert "prompt_cost_usd" in data
    assert "completion_cost_usd" in data


@pytest.mark.anyio
async def test_estimate_cost_invalid_model(client: AsyncClient):
    """Test cost estimation with invalid model."""
    response = await client.post(
        "/models/estimate-cost",
        params={
            "model_id": "invalid-model-id-999",
            "prompt_tokens": 100,
            "completion_tokens": 100,
        },
    )
    assert response.status_code == 404
