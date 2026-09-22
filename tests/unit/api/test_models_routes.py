"""`/models` — the LLM/STT/TTS/realtime catalogs the app's model picker reads.

Two sources feed it: `config/livekit_inference_*.yaml` for hosted Inference
models (with pricing), and the installed LiveKit plugins' `Literal` type stubs
for bring-your-own-key models. Both degrade to empty rather than failing, which
is what most of these assert — a plugin that stops exporting its type must not
take the catalog down with it.
"""

from __future__ import annotations

import builtins
from typing import Literal

import pytest

from src.api.routes import models as mod

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_yaml_cache():
    """`_INFERENCE_DATA` is a module-level cache; a leak makes these order-dependent."""
    mod._INFERENCE_DATA.update(dict.fromkeys(mod._INFERENCE_DATA))
    yield
    mod._INFERENCE_DATA.update(dict.fromkeys(mod._INFERENCE_DATA))


class TestLoadYamlConfig:
    def test_a_shipped_file_loads(self):
        assert mod._load_yaml_config("livekit_inference_llm.yaml")

    def test_a_missing_file_is_an_empty_dict(self, caplog):
        with caplog.at_level("ERROR", logger="kwami-api.models"):
            assert mod._load_yaml_config("no_such_file.yaml") == {}
        assert "Failed to load" in caplog.text

    def test_an_empty_file_is_an_empty_dict(self, monkeypatch):
        monkeypatch.setattr(mod.yaml, "safe_load", lambda f: None)
        assert mod._load_yaml_config("livekit_inference_llm.yaml") == {}


class TestInferenceDataCache:
    def test_the_cache_and_the_filenames_are_keyed_the_same(self):
        """The invariant the old if/elif chain restated by hand."""
        assert set(mod._INFERENCE_DATA) == set(mod._INFERENCE_FILES)

    @pytest.mark.parametrize("model_type", ["llm", "stt", "tts"])
    def test_each_type_loads_and_then_caches(self, monkeypatch, model_type):
        loads: list[str] = []
        real = mod._load_yaml_config
        monkeypatch.setattr(mod, "_load_yaml_config", lambda name: loads.append(name) or real(name))
        first = mod._get_inference_data(model_type)
        second = mod._get_inference_data(model_type)
        assert first == second
        assert len(loads) == 1, "the second call is served from the cache"

    @pytest.mark.parametrize(
        ("getter", "model_type"),
        [
            (mod.get_inference_llm_models, "llm"),
            (mod.get_inference_stt_models, "stt"),
            (mod.get_inference_tts_models, "tts"),
        ],
    )
    def test_each_getter_returns_a_model_list(self, getter, model_type):
        models = getter()
        assert isinstance(models, list)
        assert models, f"{model_type} config ships with models"

    @pytest.mark.parametrize("model_type", ["llm", "stt", "tts"])
    def test_the_last_updated_date_is_read(self, model_type):
        assert mod.get_last_updated(model_type) != "unknown"

    def test_an_absent_last_updated_reads_as_unknown(self, monkeypatch):
        monkeypatch.setattr(mod, "_load_yaml_config", lambda name: {"models": []})
        assert mod.get_last_updated("llm") == "unknown"

    def test_a_config_without_models_yields_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(mod, "_load_yaml_config", lambda name: {"last_updated": "x"})
        assert mod.get_inference_llm_models() == []


class TestExtractLiteralValues:
    def test_a_literal(self):
        assert mod._extract_literal_values(Literal["a", "b"]) == ["a", "b"]

    def test_a_union_of_literals_is_flattened(self):
        """`X | Y` and `Union[X, Y]` are the same object here, so one test covers both.

        `typing` normalises `Literal["a"] | Literal["b"]` to
        `Union[Literal["a"], Literal["b"]]`, and `get_origin` reports
        `typing.Union` for either spelling -- it does not become a
        `types.UnionType` the way `int | str` does.
        """
        hint = Literal["a"] | Literal["b"]
        assert mod.get_origin(hint) is mod.Union
        assert sorted(mod._extract_literal_values(hint)) == ["a", "b"]

    def test_none_yields_nothing(self):
        assert mod._extract_literal_values(None) == []

    def test_a_plain_type_yields_nothing(self):
        assert mod._extract_literal_values(str) == []

    def test_a_dunder_args_fallback_keeps_only_strings(self):
        hint = type("H", (), {"__args__": ("a", 1, "b", None)})()
        assert mod._extract_literal_values(hint) == ["a", "b"]

    def test_non_string_literal_members_are_stringified(self):
        assert mod._extract_literal_values(Literal[1, 2]) == ["1", "2"]


class TestSafeImportAndExtract:
    def test_a_real_plugin_type(self):
        values = mod._safe_import_and_extract("livekit.plugins.openai.models", "ChatModels")
        assert values, "the openai plugin is a hard dependency"

    def test_a_missing_module_is_empty(self, caplog):
        with caplog.at_level("DEBUG", logger="kwami-api.models"):
            assert mod._safe_import_and_extract("no.such.module", "Thing") == []
        assert "Could not import" in caplog.text

    def test_a_missing_attribute_is_empty(self):
        assert mod._safe_import_and_extract("livekit.plugins.openai.models", "NoSuchType") == []

    def test_an_import_error_from_inside_the_module_is_swallowed(self, monkeypatch):
        def boom(name):
            raise ImportError("transitive failure")

        import importlib

        monkeypatch.setattr(importlib, "import_module", boom)
        assert mod._safe_import_and_extract("livekit.plugins.openai.models", "ChatModels") == []


class TestPluginExtraction:
    def test_llm_plugins_are_found(self):
        plugins = mod._get_llm_plugin_models()
        assert "openai" in plugins
        assert "anthropic" in plugins

    def test_a_provider_with_no_models_is_omitted(self, monkeypatch):
        monkeypatch.setattr(mod, "_safe_import_and_extract", lambda m, t: [])
        assert mod._get_llm_plugin_models() == {}

    def test_stt_models_come_from_the_sdk(self):
        result = mod._get_stt_models()
        assert result["source"] == "sdk"
        assert result["inference"]
        assert "deepgram" in result["plugins"]

    def test_stt_falls_back_when_the_inference_module_is_missing(self, monkeypatch):
        _block_import(monkeypatch, "livekit.agents.inference.stt")
        result = mod._get_stt_models()
        assert result["source"] == "fallback"
        assert result["inference"] == []

    def test_tts_models_come_from_the_sdk(self):
        result = mod._get_tts_models()
        assert result["source"] == "sdk"
        assert result["inference"]

    def test_tts_falls_back_when_the_inference_module_is_missing(self, monkeypatch):
        _block_import(monkeypatch, "livekit.agents.inference.tts")
        result = mod._get_tts_models()
        assert result["source"] == "fallback"
        assert result["inference"] == []

    def test_an_stt_provider_with_no_models_is_omitted(self, monkeypatch):
        monkeypatch.setattr(mod, "_safe_import_and_extract", lambda m, t: [])
        assert mod._get_stt_models()["plugins"] == {}

    def test_a_tts_provider_with_no_models_is_omitted(self, monkeypatch):
        monkeypatch.setattr(mod, "_safe_import_and_extract", lambda m, t: [])
        assert mod._get_tts_models()["plugins"] == {}

    def test_realtime_models(self):
        result = mod._get_realtime_models()
        assert result["source"] == "sdk"
        assert "openai" in result["plugins"]
        assert "aws" in result["plugins"]

    def test_aws_realtime_has_a_hardcoded_fallback(self, monkeypatch):
        """The AWS plugin is optional, so the model list is inlined."""
        monkeypatch.setattr(mod, "_safe_import_and_extract", lambda m, t: [])
        plugins = mod._get_realtime_models()["plugins"]
        assert plugins["aws"] == ["amazon.nova-sonic-v1:0", "amazon.nova-2-sonic-v1:0"]
        assert "openai" not in plugins
        assert "google" not in plugins

    def test_an_installed_aws_plugin_wins_over_the_fallback(self, monkeypatch):
        monkeypatch.setattr(
            mod,
            "_safe_import_and_extract",
            lambda m, t: ["amazon.custom"] if "aws" in m else [],
        )
        assert mod._get_realtime_models()["plugins"]["aws"] == ["amazon.custom"]


def _block_import(monkeypatch, prefix: str):
    real = builtins.__import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith(prefix):
            raise ImportError(f"No module named {name!r}")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake)


# -- routes ------------------------------------------------------------------


class TestLlmRoutes:
    async def test_the_inference_catalog(self, client):
        r = await client.get("/models/llm")
        assert r.status_code == 200
        body = r.json()
        assert body["last_updated"]
        assert body["models"]
        first = body["models"][0]
        assert first["model_id"] and first["provider"]
        assert isinstance(first["providers"], dict)

    async def test_a_model_with_minimal_yaml_gets_defaults(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "get_inference_llm_models",
            lambda: [
                {"model_id": "m", "display_name": "M", "provider": "p", "context_window": 1000}
            ],
        )
        model = (await client.get("/models/llm")).json()["models"][0]
        assert model["max_output"] is None
        assert model["capabilities"] == []
        assert model["speed"] == "standard"
        assert model["tier"] == "standard"
        assert model["description"] is None
        assert model["providers"] == {}

    async def test_provider_pricing_is_mapped(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "get_inference_llm_models",
            lambda: [
                {
                    "model_id": "m",
                    "display_name": "M",
                    "provider": "p",
                    "context_window": 1,
                    "providers": {
                        "openai": {"input_per_1m": 1.0, "output_per_1m": 2.0, "cached_per_1m": 0.5}
                    },
                }
            ],
        )
        pricing = (await client.get("/models/llm")).json()["models"][0]["providers"]["openai"]
        assert pricing == {"input_per_1m": 1.0, "output_per_1m": 2.0, "cached_per_1m": 0.5}

    async def test_an_absent_cached_price_is_null(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "get_inference_llm_models",
            lambda: [
                {
                    "model_id": "m",
                    "display_name": "M",
                    "provider": "p",
                    "context_window": 1,
                    "providers": {"openai": {"input_per_1m": 1.0, "output_per_1m": 2.0}},
                }
            ],
        )
        pricing = (await client.get("/models/llm")).json()["models"][0]["providers"]["openai"]
        assert pricing["cached_per_1m"] is None

    async def test_an_empty_catalog_is_an_empty_list(self, monkeypatch, client):
        monkeypatch.setattr(mod, "get_inference_llm_models", list)
        assert (await client.get("/models/llm")).json()["models"] == []

    async def test_the_plugin_catalog(self, client):
        r = await client.get("/models/llm/plugins")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "sdk"
        assert "openai" in body["providers"]
        model = body["providers"]["openai"][0]
        assert model["model_id"] and model["display_name"]

    async def test_a_plugin_model_without_curated_metadata_gets_a_default(
        self, monkeypatch, client
    ):
        monkeypatch.setattr(mod, "_get_llm_plugin_models", lambda: {"newprov": ["brand-new-model"]})
        model = (await client.get("/models/llm/plugins")).json()["providers"]["newprov"][0]
        assert model["display_name"] == "Brand New Model"
        assert model["provider"] == "newprov"

    async def test_an_empty_plugin_set(self, monkeypatch, client):
        monkeypatch.setattr(mod, "_get_llm_plugin_models", dict)
        assert (await client.get("/models/llm/plugins")).json()["providers"] == {}


class TestSttRoutes:
    async def test_the_inference_catalog(self, client):
        r = await client.get("/models/stt")
        assert r.status_code == 200
        body = r.json()
        assert body["models"]
        assert "pricing" in body["models"][0]

    async def test_a_model_with_minimal_yaml_gets_defaults(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "get_inference_stt_models",
            lambda: [{"model_id": "m", "display_name": "M", "provider": "p"}],
        )
        model = (await client.get("/models/stt")).json()["models"][0]
        assert model["languages"] == []
        assert model["features"] == []
        assert model["speed"] == "standard"
        assert model["pricing"] == {"build_ship_per_min": 0, "scale_per_min": 0}

    async def test_an_empty_catalog(self, monkeypatch, client):
        monkeypatch.setattr(mod, "get_inference_stt_models", list)
        assert (await client.get("/models/stt")).json()["models"] == []

    async def test_the_plugin_catalog_uses_provider_defaults(self, client):
        body = (await client.get("/models/stt/plugins")).json()
        assert body["source"] == "sdk"
        deepgram = body["providers"]["deepgram"][0]
        assert deepgram["features"] == ["streaming", "punctuation", "smart_format"]
        assert deepgram["speed"] == "fast"

    async def test_an_unknown_provider_falls_back_to_generic_defaults(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "_get_stt_models",
            lambda: {"inference": [], "source": "sdk", "plugins": {"newprov": ["some_model-v2"]}},
        )
        model = (await client.get("/models/stt/plugins")).json()["providers"]["newprov"][0]
        assert model["languages"] == ["en"]
        assert model["features"] == ["streaming"]
        assert model["display_name"] == "Some Model V2"

    async def test_a_provider_with_no_models_is_omitted(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "_get_stt_models",
            lambda: {"inference": [], "source": "sdk", "plugins": {"empty": []}},
        )
        assert (await client.get("/models/stt/plugins")).json()["providers"] == {}

    async def test_the_fallback_source_is_reported(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "_get_stt_models",
            lambda: {"inference": [], "source": "fallback", "plugins": {}},
        )
        assert (await client.get("/models/stt/plugins")).json()["source"] == "fallback"


class TestTtsRoutes:
    async def test_the_inference_catalog(self, client):
        r = await client.get("/models/tts")
        assert r.status_code == 200
        assert r.json()["models"]

    async def test_a_model_with_minimal_yaml_gets_defaults(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "get_inference_tts_models",
            lambda: [{"model_id": "m", "display_name": "M", "provider": "p"}],
        )
        model = (await client.get("/models/tts")).json()["models"][0]
        assert model["speed"] == "standard"
        assert model["tier"] == "standard"

    async def test_an_empty_catalog(self, monkeypatch, client):
        monkeypatch.setattr(mod, "get_inference_tts_models", list)
        assert (await client.get("/models/tts")).json()["models"] == []

    async def test_the_plugin_catalog(self, client):
        body = (await client.get("/models/tts/plugins")).json()
        assert body["source"] == "sdk"
        assert body["providers"]

    async def test_an_unknown_provider_falls_back_to_generic_defaults(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "_get_tts_models",
            lambda: {"inference": [], "source": "sdk", "plugins": {"newprov": ["voice_one-x"]}},
        )
        model = (await client.get("/models/tts/plugins")).json()["providers"]["newprov"][0]
        assert model["display_name"] == "Voice One X"

    async def test_a_provider_with_no_models_is_omitted(self, monkeypatch, client):
        monkeypatch.setattr(
            mod,
            "_get_tts_models",
            lambda: {"inference": [], "source": "sdk", "plugins": {"empty": []}},
        )
        assert (await client.get("/models/tts/plugins")).json()["providers"] == {}


class TestOtherRoutes:
    async def test_realtime(self, client):
        r = await client.get("/models/realtime")
        assert r.status_code == 200
        assert r.json()["source"] == "sdk"
        assert "aws" in r.json()["plugins"]

    async def test_capabilities(self, client):
        r = await client.get("/models/capabilities")
        assert r.status_code == 200
        assert r.json()

    async def test_the_llm_metrics_schema(self, client):
        r = await client.get("/models/metrics/llm")
        assert r.status_code == 200
        assert r.json()


class TestEstimateCost:
    @pytest.fixture(autouse=True)
    def _catalog(self, monkeypatch):
        monkeypatch.setattr(
            mod,
            "get_inference_llm_models",
            lambda: [
                {
                    "model_id": "openai/test-model",
                    "display_name": "Test",
                    "provider": "openai",
                    "context_window": 128000,
                    "providers": {
                        "openai": {
                            "input_per_1m": 2.0,
                            "output_per_1m": 10.0,
                            "cached_per_1m": 1.0,
                        },
                        "azure": {"input_per_1m": 3.0, "output_per_1m": 12.0},
                    },
                }
            ],
        )

    def _params(self, **overrides):
        return {
            "model_id": "openai/test-model",
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            **overrides,
        }

    async def test_a_plain_estimate(self, client):
        r = await client.post("/models/estimate-cost", params=self._params())
        assert r.status_code == 200
        body = r.json()
        assert body["prompt_cost_usd"] == 2.0
        assert body["completion_cost_usd"] == 10.0
        assert body["total_cost_usd"] == 12.0
        assert body["cached_tokens"] is None

    async def test_cached_tokens_are_billed_at_the_cached_rate(self, client):
        r = await client.post("/models/estimate-cost", params=self._params(cached_tokens=500_000))
        body = r.json()
        # 500k at 2.0 + 500k cached at 1.0
        assert body["prompt_cost_usd"] == pytest.approx(1.0 + 0.5)
        assert body["cached_tokens"] == 500_000

    async def test_cached_tokens_without_a_cached_price_are_not_discounted(self, client):
        r = await client.post(
            "/models/estimate-cost",
            params=self._params(provider="azure", cached_tokens=500_000),
        )
        # azure has no cached_per_1m: only the non-cached half is charged
        assert r.json()["prompt_cost_usd"] == pytest.approx(1.5)

    async def test_a_different_provider_uses_its_own_pricing(self, client):
        r = await client.post("/models/estimate-cost", params=self._params(provider="azure"))
        assert r.json()["prompt_cost_usd"] == 3.0

    async def test_an_unknown_model_is_a_404(self, client):
        r = await client.post("/models/estimate-cost", params=self._params(model_id="nope/nothing"))
        assert r.status_code == 404
        assert "Model not found" in r.json()["detail"]

    async def test_an_unavailable_provider_is_a_400_listing_the_options(self, client):
        r = await client.post("/models/estimate-cost", params=self._params(provider="google"))
        assert r.status_code == 400
        assert "openai" in r.json()["detail"]
        assert "azure" in r.json()["detail"]

    @pytest.mark.parametrize(
        "params",
        [{"prompt_tokens": -1}, {"completion_tokens": -1}, {"cached_tokens": -1}],
    )
    async def test_negative_token_counts_are_a_422(self, client, params):
        r = await client.post("/models/estimate-cost", params=self._params(**params))
        assert r.status_code == 422

    async def test_zero_usage_is_free(self, client):
        r = await client.post(
            "/models/estimate-cost",
            params=self._params(prompt_tokens=0, completion_tokens=0),
        )
        assert r.json()["total_cost_usd"] == 0.0
