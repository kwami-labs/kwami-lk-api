"""`src.services.languages` — the STT/TTS/Realtime language catalog.

Same hybrid shape as the voice catalog: codes come from the LiveKit plugin type
stubs where they exist and from `config/livekit_languages.yaml` where they do
not, and every code is decorated through `get_language_info`. The interesting
arms are the fallbacks: an unknown code, a non-string code (YAML turns a bare
`no` into a boolean), and a plugin that has stopped exporting its `Literal`.
"""

from __future__ import annotations

import builtins
import types

import pytest
import yaml

from src.services import languages as lang_mod
from src.services.languages import (
    LANGUAGE_NAMES,
    Language,
    ProviderLanguages,
    _extract_cartesia_stt_languages,
    _extract_cartesia_tts_languages,
    _extract_deepgram_stt_languages,
    _extract_google_stt_languages,
    _get_yaml_languages,
    _load_languages_yaml,
    get_all_languages,
    get_language_info,
    get_realtime_languages,
    get_realtime_languages_by_provider,
    get_stt_languages,
    get_stt_languages_by_provider,
    get_tts_languages,
    get_tts_languages_by_provider,
    reload_languages_yaml,
)


@pytest.fixture(autouse=True)
def _reset_yaml_cache():
    lang_mod._LANGUAGES_YAML = None
    yield
    lang_mod._LANGUAGES_YAML = None


def _block_import(monkeypatch, prefix: str):
    real = builtins.__import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith(prefix):
            raise ImportError(f"No module named {name!r}")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake)


class TestModels:
    def test_a_language_needs_only_a_code_and_a_name(self):
        lang = Language(code="en", name="English")
        assert lang.native_name is None and lang.region is None

    def test_a_provider_defaults_to_the_yaml_source(self):
        assert ProviderLanguages(provider="p", languages=[]).source == "yaml"


class TestGetLanguageInfo:
    def test_a_known_code_is_decorated_from_the_table(self):
        lang = get_language_info("sw")
        assert lang.code == "sw"
        assert lang.name == "Swahili"
        assert lang.native_name == "Kiswahili"

    def test_an_unknown_code_falls_back_to_its_uppercase_self(self):
        lang = get_language_info("zz")
        assert lang.code == "zz"
        assert lang.name == "ZZ"
        assert lang.native_name is None

    def test_a_non_string_code_is_coerced(self):
        """YAML parses a bare `no` as the boolean False -- that is the Norwegian code."""
        lang = get_language_info(False)
        assert lang.code == "False"
        assert lang.name == "FALSE"

    def test_an_integer_code_is_coerced_too(self):
        assert get_language_info(1).code == "1"

    def test_the_region_is_carried_when_the_table_has_one(self):
        with_region = [c for c, v in LANGUAGE_NAMES.items() if "region" in v]
        if not with_region:
            pytest.skip("no regional entries in the table")
        assert get_language_info(with_region[0]).region is not None

    def test_the_multi_language_sentinel_is_known(self):
        assert get_language_info("multi").name == "Multi-language"


class TestSdkExtraction:
    @pytest.mark.parametrize(
        ("extractor", "blocked_prefix", "warning"),
        [
            (
                _extract_deepgram_stt_languages,
                "livekit.plugins.deepgram",
                "Could not import Deepgram languages",
            ),
            (
                _extract_cartesia_stt_languages,
                "livekit.plugins.cartesia",
                "Could not import Cartesia STT languages",
            ),
            (
                _extract_cartesia_tts_languages,
                "livekit.plugins.cartesia",
                "Could not import Cartesia TTS languages",
            ),
            (
                _extract_google_stt_languages,
                "livekit.plugins.google",
                "Could not import Google STT languages",
            ),
        ],
    )
    def test_a_missing_plugin_degrades_to_empty(
        self, monkeypatch, caplog, extractor, blocked_prefix, warning
    ):
        _block_import(monkeypatch, blocked_prefix)
        with caplog.at_level("WARNING", logger="kwami-api.languages"):
            assert extractor() == []
        assert warning in caplog.text

    @pytest.mark.parametrize(
        "extractor",
        [
            _extract_deepgram_stt_languages,
            _extract_cartesia_stt_languages,
            _extract_cartesia_tts_languages,
            _extract_google_stt_languages,
        ],
    )
    def test_each_installed_plugin_yields_decorated_languages(self, extractor):
        langs = extractor()
        assert langs, "these plugins are hard dependencies"
        assert all(isinstance(lang, Language) for lang in langs)
        assert all(lang.name for lang in langs)


class TestYamlLoading:
    def test_the_shipped_config_loads(self):
        assert isinstance(_load_languages_yaml(), dict)

    def test_it_is_cached_after_the_first_read(self, monkeypatch):
        first = _load_languages_yaml()

        def explode(*a, **k):
            raise AssertionError("the second call must not re-read the file")

        monkeypatch.setattr(builtins, "open", explode)
        assert _load_languages_yaml() is first

    def test_a_missing_file_is_an_empty_config(self, monkeypatch, caplog):
        def missing(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(builtins, "open", missing)
        with caplog.at_level("WARNING", logger="kwami-api.languages"):
            assert _load_languages_yaml() == {}
        assert "Languages YAML not found" in caplog.text

    def test_malformed_yaml_is_an_empty_config(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise yaml.YAMLError("bad indentation")

        monkeypatch.setattr(lang_mod.yaml, "safe_load", boom)
        with caplog.at_level("ERROR", logger="kwami-api.languages"):
            assert _load_languages_yaml() == {}
        assert "Failed to load languages YAML" in caplog.text

    def test_an_empty_file_is_an_empty_config(self, monkeypatch):
        monkeypatch.setattr(lang_mod.yaml, "safe_load", lambda f: None)
        assert _load_languages_yaml() == {}

    def test_reload_drops_the_cache(self, monkeypatch):
        _load_languages_yaml()
        monkeypatch.setattr(lang_mod.yaml, "safe_load", lambda f: {"stt": {"x": ["en"]}})
        reload_languages_yaml()
        assert _load_languages_yaml() == {"stt": {"x": ["en"]}}


class TestGetYamlLanguages:
    def test_codes_are_decorated(self, monkeypatch):
        monkeypatch.setattr(
            lang_mod, "_load_languages_yaml", lambda: {"stt": {"openai": ["en", "sw"]}}
        )
        langs = _get_yaml_languages("openai", "stt")
        assert [lang.code for lang in langs] == ["en", "sw"]
        assert langs[1].native_name == "Kiswahili"

    def test_an_unknown_provider_is_empty(self, monkeypatch):
        monkeypatch.setattr(lang_mod, "_load_languages_yaml", lambda: {"stt": {}})
        assert _get_yaml_languages("nope", "stt") == []

    def test_an_unknown_model_type_is_empty(self, monkeypatch):
        monkeypatch.setattr(lang_mod, "_load_languages_yaml", lambda: {})
        assert _get_yaml_languages("openai", "nonsense") == []


class TestSttApi:
    def test_the_sdk_providers_are_marked_as_such(self):
        providers = get_stt_languages()
        for name in ("deepgram", "cartesia", "google"):
            assert providers[name].source == "sdk"

    def test_yaml_providers_are_added_when_configured(self, monkeypatch):
        monkeypatch.setattr(
            lang_mod,
            "_get_yaml_languages",
            lambda p, t: [Language(code="en", name="English")] if p == "groq" else [],
        )
        providers = get_stt_languages()
        assert providers["groq"].source == "yaml"
        assert "assemblyai" not in providers

    def test_empty_providers_are_omitted(self, monkeypatch):
        for fn in (
            "_extract_deepgram_stt_languages",
            "_extract_cartesia_stt_languages",
            "_extract_google_stt_languages",
        ):
            monkeypatch.setattr(lang_mod, fn, list)
        monkeypatch.setattr(lang_mod, "_get_yaml_languages", lambda p, t: [])
        assert get_stt_languages() == {}

    def test_lookup_by_provider(self):
        assert get_stt_languages_by_provider("deepgram") is not None
        assert get_stt_languages_by_provider("nope") is None


class TestTtsApi:
    def test_cartesia_is_the_sdk_provider(self):
        assert get_tts_languages()["cartesia"].source == "sdk"

    def test_yaml_providers_are_added_when_configured(self, monkeypatch):
        monkeypatch.setattr(
            lang_mod,
            "_get_yaml_languages",
            lambda p, t: [Language(code="en", name="English")] if p == "rime" else [],
        )
        assert get_tts_languages()["rime"].source == "yaml"

    def test_empty_providers_are_omitted(self, monkeypatch):
        monkeypatch.setattr(lang_mod, "_extract_cartesia_tts_languages", list)
        monkeypatch.setattr(lang_mod, "_get_yaml_languages", lambda p, t: [])
        assert get_tts_languages() == {}

    def test_lookup_by_provider(self):
        assert get_tts_languages_by_provider("cartesia") is not None
        assert get_tts_languages_by_provider("nope") is None


class TestRealtimeApi:
    def test_it_is_yaml_only(self, monkeypatch):
        monkeypatch.setattr(
            lang_mod,
            "_get_yaml_languages",
            lambda p, t: [Language(code="en", name="English")] if p == "gemini" else [],
        )
        providers = get_realtime_languages()
        assert providers["gemini"].source == "yaml"
        assert "openai" not in providers

    def test_empty_providers_are_omitted(self, monkeypatch):
        monkeypatch.setattr(lang_mod, "_get_yaml_languages", lambda p, t: [])
        assert get_realtime_languages() == {}

    def test_lookup_by_provider(self, monkeypatch):
        monkeypatch.setattr(
            lang_mod, "_get_yaml_languages", lambda p, t: [Language(code="en", name="English")]
        )
        assert get_realtime_languages_by_provider("openai") is not None
        assert get_realtime_languages_by_provider("nope") is None


class TestGetAllLanguages:
    def test_it_returns_one_entry_per_known_code(self):
        langs = get_all_languages()
        assert len(langs) == len(LANGUAGE_NAMES)
        assert {lang.code for lang in langs} == set(LANGUAGE_NAMES)

    def test_every_entry_is_decorated(self):
        assert all(lang.name for lang in get_all_languages())


def test_the_extractors_use_get_args_on_the_plugin_literal(monkeypatch):
    """The whole mechanism: a plugin's `Literal` is the source of the code list."""
    real = builtins.__import__
    module = types.ModuleType("livekit.plugins.deepgram.models")
    module.DeepgramLanguages = str

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "livekit.plugins.deepgram.models":
            return module
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake)
    monkeypatch.setattr(lang_mod, "get_args", lambda _t: ("en", "zz"))
    langs = _extract_deepgram_stt_languages()
    assert [lang.code for lang in langs] == ["en", "zz"]
    assert langs[1].name == "ZZ", "an unknown code still gets an entry"
