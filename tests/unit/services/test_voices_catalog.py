"""`src.services.voices` — the TTS/Realtime catalog the app's voice picker reads.

Hybrid by design: voice ids come from the LiveKit plugin type stubs where the
SDK has them, and from `config/livekit_voices.yaml` where it does not. Both
sources, and the arms where either is missing, are covered here — a plugin that
stops shipping its `Literal` must degrade to an empty provider, not a 500.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest
import yaml

from src.services import voices as voices_mod
from src.services.voices import (
    Voice,
    VoiceProvider,
    _extract_gemini_realtime_voices,
    _extract_openai_realtime_voices,
    _extract_openai_tts_voices,
    _extract_rime_voices,
    _get_yaml_voices,
    _load_voices_yaml,
    get_realtime_voices,
    get_realtime_voices_by_provider,
    get_tts_voices,
    get_tts_voices_by_provider,
    reload_voices_yaml,
)


@pytest.fixture(autouse=True)
def _reset_yaml_cache():
    """`_VOICES_YAML` is a module global; a leaked cache makes these order-dependent."""
    voices_mod._VOICES_YAML = None
    yield
    voices_mod._VOICES_YAML = None


def _import_stub(real, target: str, **attrs):
    """Serve `target` from a throwaway module carrying `attrs`."""
    import types

    module = types.ModuleType(target)
    for name, value in attrs.items():
        setattr(module, name, value)

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name == target:
            return module
        return real(name, globals, locals, fromlist, level)

    return fake


def _block_import(monkeypatch, prefix: str):
    """Make `import <prefix>...` raise ImportError, as a missing plugin would."""
    real = builtins.__import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith(prefix):
            raise ImportError(f"No module named {name!r}")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake)


class TestModels:
    def test_a_voice_needs_only_an_id_and_a_name(self):
        v = Voice(id="alloy", name="Alloy")
        assert v.category is None and v.gender is None and v.language is None

    def test_a_provider_defaults_to_the_yaml_source(self):
        assert VoiceProvider(provider="p", voices=[]).source == "yaml"

    @pytest.mark.parametrize("gender", ["male", "female", "neutral"])
    def test_the_gender_literal_accepts_its_three_values(self, gender):
        assert Voice(id="x", name="X", gender=gender).gender == gender

    def test_an_unknown_gender_is_rejected(self):
        with pytest.raises(ValueError):
            Voice(id="x", name="X", gender="other")


class TestSdkExtraction:
    def test_openai_tts_voices_come_back_with_metadata(self):
        vs = _extract_openai_tts_voices()
        assert vs, "the openai plugin is a hard dependency; this should not be empty"
        by_id = {v.id: v for v in vs}
        assert by_id["alloy"].name == "Alloy"
        assert by_id["alloy"].gender == "neutral"
        assert all(v.language == "en" for v in vs)

    def test_a_voice_the_metadata_table_does_not_know_is_titled(self, monkeypatch):
        """The SDK adds voices faster than the hand-written table does."""
        import livekit.plugins.openai.models as m

        monkeypatch.setattr(voices_mod, "get_args", lambda _t: ("brand_new",))
        vs = _extract_openai_tts_voices()
        assert [v.id for v in vs] == ["brand_new"]
        assert vs[0].name == "Brand_New"
        assert vs[0].category == "Unknown"
        assert vs[0].gender is None
        assert m is not None

    def test_a_missing_openai_plugin_degrades_to_empty(self, monkeypatch, caplog):
        _block_import(monkeypatch, "livekit.plugins.openai")
        with caplog.at_level("WARNING", logger="kwami-api.voices"):
            assert _extract_openai_tts_voices() == []
        assert "Could not import OpenAI TTS voices" in caplog.text

    def test_realtime_reuses_the_tts_voice_set(self):
        assert _extract_openai_realtime_voices() == _extract_openai_tts_voices()

    def test_gemini_voices_are_labelled_multilingual(self):
        vs = _extract_gemini_realtime_voices()
        assert vs
        assert all(v.category == "Gemini Live" for v in vs)
        assert all(v.language == "multilingual" for v in vs)
        assert all(v.name == v.id for v in vs)

    def test_a_missing_google_plugin_degrades_to_empty(self, monkeypatch, caplog):
        _block_import(monkeypatch, "livekit.plugins.google")
        with caplog.at_level("WARNING", logger="kwami-api.voices"):
            assert _extract_gemini_realtime_voices() == []
        assert "Could not import Gemini Live voices" in caplog.text

    def test_rime_currently_yields_nothing_known_bug(self):
        """`livekit.plugins.rime.models` no longer exports `ArcanaVoices`.

        The installed plugin exports `TTSModels`, `DefaultCodaVoice` and
        `DefaultMistVoice`. The import therefore raises, the `except ImportError`
        swallows it, and Rime is absent from the TTS catalog the app renders --
        silently, because a warning in the log is the only signal. Pinned as it
        ships; invert this test when the extractor is pointed at the new names.
        """
        assert _extract_rime_voices() == []

    def test_rime_voices_are_titled_and_labelled_arcana(self, monkeypatch):
        """The mapping itself, exercised through a stand-in for the missing type."""
        monkeypatch.setattr(voices_mod, "get_args", lambda _t: ("celeste", "orion"))
        monkeypatch.setattr(
            builtins,
            "__import__",
            _import_stub(builtins.__import__, "livekit.plugins.rime.models", ArcanaVoices=str),
        )
        vs = _extract_rime_voices()
        assert [v.id for v in vs] == ["celeste", "orion"]
        assert all(v.category == "Arcana" for v in vs)
        assert all(v.language == "en" for v in vs)
        assert all(v.name == v.id.title() for v in vs)

    def test_a_missing_rime_plugin_degrades_to_empty(self, monkeypatch, caplog):
        _block_import(monkeypatch, "livekit.plugins.rime")
        with caplog.at_level("WARNING", logger="kwami-api.voices"):
            assert _extract_rime_voices() == []
        assert "Could not import Rime voices" in caplog.text


class TestYamlLoading:
    def test_the_shipped_config_loads(self):
        data = _load_voices_yaml()
        assert isinstance(data, dict)
        assert (Path(__file__).parents[3] / "config" / "livekit_voices.yaml").exists()

    def test_it_is_cached_after_the_first_read(self, monkeypatch):
        first = _load_voices_yaml()

        def explode(*a, **k):
            raise AssertionError("the second call must not re-read the file")

        monkeypatch.setattr(builtins, "open", explode)
        assert _load_voices_yaml() is first

    def test_a_missing_file_is_an_empty_config(self, monkeypatch, caplog):
        def missing(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(builtins, "open", missing)
        with caplog.at_level("WARNING", logger="kwami-api.voices"):
            assert _load_voices_yaml() == {}
        assert "Voices YAML not found" in caplog.text

    def test_malformed_yaml_is_an_empty_config(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise yaml.YAMLError("bad indentation")

        monkeypatch.setattr(voices_mod.yaml, "safe_load", boom)
        with caplog.at_level("ERROR", logger="kwami-api.voices"):
            assert _load_voices_yaml() == {}
        assert "Failed to load voices YAML" in caplog.text

    def test_an_empty_file_is_an_empty_config(self, monkeypatch):
        monkeypatch.setattr(voices_mod.yaml, "safe_load", lambda f: None)
        assert _load_voices_yaml() == {}

    def test_reload_drops_the_cache(self, monkeypatch):
        _load_voices_yaml()
        monkeypatch.setattr(voices_mod.yaml, "safe_load", lambda f: {"tts": {"x": []}})
        reload_voices_yaml()
        assert _load_voices_yaml() == {"tts": {"x": []}}


class TestGetYamlVoices:
    def test_it_maps_every_optional_field(self, monkeypatch):
        monkeypatch.setattr(
            voices_mod,
            "_load_voices_yaml",
            lambda: {
                "tts": {
                    "cartesia": [
                        {
                            "id": "v1",
                            "name": "One",
                            "category": "Female",
                            "gender": "female",
                            "language": "en",
                            "description": "warm",
                        }
                    ]
                }
            },
        )
        (v,) = _get_yaml_voices("cartesia", "tts")
        assert (v.id, v.name, v.category, v.gender, v.language, v.description) == (
            "v1",
            "One",
            "Female",
            "female",
            "en",
            "warm",
        )

    def test_optional_fields_may_be_absent(self, monkeypatch):
        monkeypatch.setattr(
            voices_mod, "_load_voices_yaml", lambda: {"tts": {"p": [{"id": "v", "name": "V"}]}}
        )
        (v,) = _get_yaml_voices("p", "tts")
        assert v.category is None and v.description is None

    def test_an_unknown_provider_is_empty(self, monkeypatch):
        monkeypatch.setattr(voices_mod, "_load_voices_yaml", lambda: {"tts": {}})
        assert _get_yaml_voices("nope", "tts") == []

    def test_an_unknown_voice_type_is_empty(self, monkeypatch):
        monkeypatch.setattr(voices_mod, "_load_voices_yaml", lambda: {})
        assert _get_yaml_voices("p", "realtime") == []


class TestPublicTtsApi:
    def test_the_sdk_providers_are_marked_as_such(self):
        providers = get_tts_voices()
        assert providers["openai"].source == "sdk"
        assert "rime" not in providers, "see test_rime_currently_yields_nothing_known_bug"

    def test_rime_would_be_listed_as_an_sdk_provider_if_it_yielded_voices(self, monkeypatch):
        """The arm the broken Rime import currently makes unreachable in production."""
        monkeypatch.setattr(
            voices_mod,
            "_extract_rime_voices",
            lambda: [Voice(id="celeste", name="Celeste", category="Arcana", language="en")],
        )
        providers = get_tts_voices()
        assert providers["rime"].source == "sdk"
        assert [v.id for v in providers["rime"].voices] == ["celeste"]

    def test_yaml_providers_are_included_when_configured(self, monkeypatch):
        monkeypatch.setattr(
            voices_mod,
            "_get_yaml_voices",
            lambda provider, kind: (
                [Voice(id=f"{provider}-1", name="x")] if provider == "cartesia" else []
            ),
        )
        providers = get_tts_voices()
        assert providers["cartesia"].source == "yaml"
        assert "elevenlabs" not in providers, "an empty provider must not be listed"

    def test_an_empty_sdk_provider_is_omitted(self, monkeypatch):
        monkeypatch.setattr(voices_mod, "_extract_openai_tts_voices", list)
        monkeypatch.setattr(voices_mod, "_extract_rime_voices", list)
        monkeypatch.setattr(voices_mod, "_get_yaml_voices", lambda p, k: [])
        assert get_tts_voices() == {}

    def test_lookup_by_provider(self):
        assert get_tts_voices_by_provider("openai") is not None
        assert get_tts_voices_by_provider("no-such-provider") is None


class TestPublicRealtimeApi:
    def test_the_sdk_providers_are_marked_as_such(self):
        providers = get_realtime_voices()
        assert providers["openai"].source == "sdk"
        assert providers["gemini"].source == "sdk"

    def test_extra_yaml_realtime_providers_are_merged_in(self, monkeypatch):
        monkeypatch.setattr(
            voices_mod,
            "_load_voices_yaml",
            lambda: {"realtime": {"custom": [{"id": "c1", "name": "C1"}]}},
        )
        providers = get_realtime_voices()
        assert providers["custom"].source == "yaml"
        assert providers["custom"].voices[0].id == "c1"

    def test_yaml_never_overrides_an_sdk_provider(self, monkeypatch):
        """`if provider not in providers` — the SDK is the source of truth."""
        monkeypatch.setattr(
            voices_mod,
            "_load_voices_yaml",
            lambda: {"realtime": {"openai": [{"id": "fake", "name": "Fake"}]}},
        )
        providers = get_realtime_voices()
        assert providers["openai"].source == "sdk"
        assert "fake" not in {v.id for v in providers["openai"].voices}

    def test_an_empty_sdk_provider_is_omitted(self, monkeypatch):
        monkeypatch.setattr(voices_mod, "_extract_openai_realtime_voices", list)
        monkeypatch.setattr(voices_mod, "_extract_gemini_realtime_voices", list)
        monkeypatch.setattr(voices_mod, "_load_voices_yaml", dict)
        assert get_realtime_voices() == {}

    def test_lookup_by_provider(self):
        assert get_realtime_voices_by_provider("openai") is not None
        assert get_realtime_voices_by_provider("no-such-provider") is None
