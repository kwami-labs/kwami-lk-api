"""Kwami LiveKit API.

`__version__` is the one place the running service states its version: `src/main.py` reads it
for the FastAPI `version` (and so for /docs and /openapi.json) and for the startup log line.

It is written by `scripts/set-version.sh`, which semantic-release runs while cutting a release —
along with pyproject.toml, uv.lock and the README badge. Do not edit it by hand; a version that
disagrees with the tag is worse than one that is a release behind.
"""

__version__ = "0.1.1"
