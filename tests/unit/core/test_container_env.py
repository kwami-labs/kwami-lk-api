"""The Worker hands the container only the settings it names.

A setting left off that list does nothing in production, and nothing says so: the value
sits in the Worker and the API runs on its default. Prices were like that.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices

from src.core.config import Settings

ROOT = Path(__file__).resolve().parents[3]
#: Read by the code, and left off on purpose.
NOT_FORWARDED = {
    "ELEVENLABS_API_KEY": "another name for ELEVEN_API_KEY, which is forwarded",
    "KWAMI_ENV_FILE": "a container takes its settings from the environment, not a file",
    "PORT": "another name for API_PORT, which is forwarded",
}


def _forwarded() -> set[str]:
    source = (ROOT / "infra/src/container-env.ts").read_text(encoding="utf-8")
    return set(re.findall(r'^\s*"([A-Z][A-Z0-9_]+)",\s*$', source, re.MULTILINE))


def _names_of_each_setting() -> list[set[str]]:
    names = []
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias or field.alias or name.upper()
        names.append(set(map(str, alias.choices)) if isinstance(alias, AliasChoices) else {alias})
    source = (ROOT / "src/core/config.py").read_text(encoding="utf-8")
    names.extend({read} for read in re.findall(r'os\.environ(?:\.get\(|\[)"([A-Z0-9_]+)"', source))
    return names


def test_every_setting_reaches_the_container() -> None:
    forwarded = _forwarded() | set(NOT_FORWARDED)
    lost = [sorted(names) for names in _names_of_each_setting() if not names & forwarded]
    assert not lost, f"add these to infra/src/container-env.ts, or say why not: {lost}"


def test_what_is_left_off_is_still_read_by_the_code() -> None:
    read = set().union(*_names_of_each_setting())
    source = (ROOT / "src/core/config.py").read_text(encoding="utf-8")
    stale = [name for name in NOT_FORWARDED if name not in read and f'"{name}"' not in source]
    assert not stale, f"these are gone from the code: {stale}"
