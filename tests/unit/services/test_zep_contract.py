"""Contract test: every Zep method the memory code calls exists in the installed zep-cloud.

The memory tests stub the Zep client, so they pass whatever the SDK looks like. That is how
zep-cloud 4.1 reached `dev` while twelve of the methods called here -- `graph.search`,
`graph.add_fact_triple`, `thread.get_user_context`, `user.get_threads` and others -- no longer
existed in it: every test was green, and each of those routes would have failed with an
AttributeError once deployed. This test reads the calls from the source, so a new call is
covered without editing it, and fails on the SDK version that drops one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from zep_cloud.client import AsyncZep

SRC = Path(__file__).resolve().parents[3] / "src"
MEMORY_CODE = [
    *sorted((SRC / "services" / "memory").glob("*.py")),
    *sorted((SRC / "api" / "routes" / "memory").glob("*.py")),
]

# `client.graph.search(`, `client.thread.get(`, `client.graph.edge.update(` ... The receiver
# must be exactly `client`, so an attribute such as `self.client.x` or `http_client.get` is
# not read as a Zep call; `graph`, `thread` and `user` are Zep's namespaces.
ZEP_CALL = re.compile(r"(?<![\w.])client\.((?:graph|thread|user)(?:\.\w+)+)\s*\(")


def _calls() -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in MEMORY_CODE:
        code = "\n".join(line.split("#", 1)[0] for line in path.read_text("utf-8").splitlines())
        for match in ZEP_CALL.finditer(code):
            found.add((match.group(1), path.relative_to(SRC).as_posix()))
    return sorted(found)


CALLS = _calls()


def test_the_scan_finds_the_memory_codes_calls():
    """An empty or tiny list would make the test below pass by finding nothing."""
    methods = {method for method, _ in CALLS}
    assert len(methods) >= 20
    assert {"graph.search", "thread.get", "user.get", "graph.set_ontology"} <= methods


@pytest.mark.parametrize(("method", "caller"), CALLS)
def test_each_zep_method_called_exists(method: str, caller: str):
    target: object = AsyncZep(api_key="test-key-not-real")
    for part in method.split("."):
        assert hasattr(target, part), f"{caller} calls client.{method}, which zep-cloud lacks"
        target = getattr(target, part)
    assert callable(target)
