"""The CPU-bound passes over a graph: duplicate pairs and their ranking.

The duplicates route, the reorganizer preview and the one-click reorganizer each
carried their own copy of the pair loop. It lives here once, as a pure function,
so the routes can run it on a worker thread: it compares every pair of names,
and on a whole graph -- which the routes now read, instead of Zep's first 50
nodes -- that is long enough to stall every other request on the event loop.
"""

from __future__ import annotations

import unicodedata
from collections import Counter

from thefuzz import fuzz

from src.services.memory.graph import is_user_node

#: Labels that say nothing about what an entity is.
_GENERIC_LABELS = {"entity", "node", ""}


def normalize_name(name: str) -> str:
    """Case, accents and spacing removed: "José  Pérez" and "jose perez" match."""
    decomposed = unicodedata.normalize("NFKD", name)
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(bare.casefold().split())


def edge_counts(edges_list: list[dict]) -> Counter:
    counts: Counter = Counter()
    for e in edges_list:
        for end in (e["source_node_uuid"], e["target_node_uuid"]):
            if end:
                counts[end] += 1
    return counts


def labels_compatible(a: dict, b: dict) -> bool:
    """False when both nodes have a specific type and the types differ.

    "Apple" the company and "Apple" the fruit look identical by name; their
    labels are what tell them apart, so they are never merged automatically.
    """
    specific_a = {label.lower() for label in a.get("labels") or []} - _GENERIC_LABELS
    specific_b = {label.lower() for label in b.get("labels") or []} - _GENERIC_LABELS
    return not specific_a or not specific_b or bool(specific_a & specific_b)


def _side(node: dict, counts: Counter) -> dict:
    return {
        "uuid": node["uuid"],
        "name": node["name"],
        "summary": node["summary"],
        "labels": node.get("labels") or [],
        "edge_count": counts.get(node["uuid"], 0),
    }


def find_duplicate_pairs(
    nodes_list: list[dict], edges_list: list[dict], threshold: int
) -> list[dict]:
    """Pairs of nodes whose names look alike, best match first.

    The node with more edges is the one to keep. The user's own node is never
    the one removed: a pair of two user nodes is skipped, and a user node paired
    with anything else is always kept.
    """
    counts = edge_counts(edges_list)
    # One entry per uuid: a node is not its own duplicate however often it is listed.
    by_uuid = {n["uuid"]: n for n in nodes_list if n["uuid"] and n["name"]}
    named = [(n, normalize_name(n["name"])) for n in by_uuid.values()]
    pairs: list[dict] = []
    for i, (a, a_name) in enumerate(named):
        for b, b_name in named[i + 1 :]:
            if a_name == b_name:
                score = 100
            else:
                score = max(fuzz.ratio(a_name, b_name), fuzz.token_sort_ratio(a_name, b_name))
            if score < threshold:
                continue
            a_user, b_user = is_user_node(a), is_user_node(b)
            if a_user and b_user:
                continue
            if a_user or (not b_user and counts.get(a["uuid"], 0) >= counts.get(b["uuid"], 0)):
                keep, remove = a, b
            else:
                keep, remove = b, a
            pairs.append(
                {
                    "score": score,
                    "compatible": labels_compatible(a, b),
                    "keep": _side(keep, counts),
                    "remove": _side(remove, counts),
                }
            )
    pairs.sort(key=lambda p: -int(p["score"]))
    return pairs
