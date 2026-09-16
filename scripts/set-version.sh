#!/usr/bin/env bash
#
# Writes one version number into every place this repository states it.
#
# semantic-release calls this from `prepareCmd` in .releaserc.json, between deciding the version
# and committing it; the files it touches are exactly the `assets` that commit carries. It is a
# script rather than a wall of sed inside a JSON string so that it can be read, and run by hand:
#
#     ./scripts/set-version.sh 1.4.2
#
# The version is stated in four places, and the awkward one is uv.lock. pyproject.toml's version
# is mirrored into the lockfile's own entry for this project, so bumping one without the other
# makes `uv lock --check` fail — which is a required status check, so the next run on main would
# go red on the release commit itself. That is the whole reason this file exists.
set -euo pipefail

cd "$(dirname "$0")/.."

version="${1:-}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]]; then
  echo "usage: $0 <semver>   (got '${version}')" >&2
  exit 1
fi

project="$(sed -nE 's/^name[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' pyproject.toml | head -1)"
[[ -n "$project" ]] || { echo "could not read the project name from pyproject.toml" >&2; exit 1; }

# pyproject.toml — only the `version` in [project], which is the first one in the file.
python3 - "$version" <<'PY'
import re, sys
version = sys.argv[1]
text = open("pyproject.toml").read()
new, n = re.subn(r'(?m)^version\s*=\s*"[^"]*"', f'version = "{version}"', text, count=1)
if n != 1:
    raise SystemExit("no [project] version line in pyproject.toml")
open("pyproject.toml", "w").write(new)
PY

# uv.lock — the `version` line inside this project's own [[package]] block, and nothing else.
# Every dependency has the same two keys, so the edit has to be anchored on the name above it.
python3 - "$version" "$project" <<'PY'
import re, sys
version, project = sys.argv[1], sys.argv[2]
text = open("uv.lock").read()
pattern = re.compile(
    r'(?m)^(name\s*=\s*"' + re.escape(project) + r'"\nversion\s*=\s*)"[^"]*"'
)
new, n = pattern.subn(lambda m: m.group(1) + f'"{version}"', text, count=1)
if n != 1:
    raise SystemExit(f"no [[package]] entry for {project} in uv.lock")
open("uv.lock", "w").write(new)
PY

# src/__init__.py — what the running process reports: the FastAPI `version`, /docs, and the
# startup log line all read it from here.
python3 - "$version" <<'PY'
import re, sys
version = sys.argv[1]
text = open("src/__init__.py").read()
new, n = re.subn(r'(?m)^__version__\s*=\s*"[^"]*"', f'__version__ = "{version}"', text, count=1)
if n != 1:
    raise SystemExit("no __version__ in src/__init__.py")
open("src/__init__.py", "w").write(new)
PY

# README badge. Absent is not an error: the badge is decoration, not a source of truth.
sed -i -E "s|badge/release-v[0-9][0-9A-Za-z.+-]*|badge/release-v${version}|" README.md

echo "version set to ${version} in pyproject.toml, uv.lock, src/__init__.py and README.md"
