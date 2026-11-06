"""The pure graph passes behind the duplicates and reorganize routes."""

from __future__ import annotations

from src.services.memory.analysis import (
    find_duplicate_pairs,
    labels_compatible,
    normalize_name,
)
from src.services.memory.graph import find_orphans, is_user_node


def n(uuid, name, labels=None, degree=None, summary=None):
    return {
        "uuid": uuid,
        "name": name,
        "labels": labels or [],
        "degree": degree,
        "summary": summary,
    }


def e(source, target):
    return {"source_node_uuid": source, "target_node_uuid": target}


class TestNormalizeName:
    def test_case_accents_and_spacing_are_ignored(self):
        assert normalize_name("  José   PÉREZ ") == "jose perez"

    def test_a_plain_name_is_unchanged_but_lowercased(self):
        assert normalize_name("Barcelona") == "barcelona"


class TestLabelsCompatible:
    def test_different_specific_types_are_not(self):
        assert not labels_compatible(n("a", "Apple", ["Organization"]), n("b", "Apple", ["Object"]))

    def test_a_shared_type_is(self):
        assert labels_compatible(
            n("a", "Ada", ["Person", "Entity"]), n("b", "Ada", ["Entity", "Person"])
        )

    def test_a_generic_side_is(self):
        assert labels_compatible(n("a", "Ada", ["Entity"]), n("b", "Ada", ["Person"]))
        assert labels_compatible(n("a", "Ada"), n("b", "Ada", ["Person"]))


class TestFindDuplicatePairs:
    def test_the_better_connected_node_is_kept(self):
        pairs = find_duplicate_pairs(
            [n("a", "Barcelona"), n("b", "Barcelona")], [e("b", "x"), e("b", "y")], 90
        )
        assert pairs[0]["keep"]["uuid"] == "b"
        assert pairs[0]["keep"]["edge_count"] == 2
        assert pairs[0]["remove"]["uuid"] == "a"

    def test_identical_normalised_names_score_100(self):
        pairs = find_duplicate_pairs([n("a", "José"), n("b", "jose")], [], 99)
        assert pairs[0]["score"] == 100

    def test_the_user_node_is_never_the_one_removed(self):
        """Even when the other node has more edges."""
        pairs = find_duplicate_pairs(
            [n("u", "Daniel", ["User"]), n("p", "Daniel", ["Person"])],
            [e("p", "x"), e("p", "y")],
            90,
        )
        assert pairs[0]["keep"]["uuid"] == "u"
        pairs = find_duplicate_pairs(
            [n("p", "Daniel", ["Person"]), n("u", "Daniel", ["User"])],
            [e("p", "x")],
            90,
        )
        assert pairs[0]["keep"]["uuid"] == "u"

    def test_two_user_nodes_are_not_a_pair(self):
        assert find_duplicate_pairs([n("a", "kwami_1"), n("b", "kwami_1")], [], 90) == []

    def test_nodes_without_uuid_or_name_are_ignored(self):
        assert find_duplicate_pairs([n(None, "X"), n("b", "X"), n("c", "")], [], 50) == []

    def test_pairs_are_ordered_best_first(self):
        pairs = find_duplicate_pairs(
            [n("a", "Barcelona"), n("b", "Barcelonaa"), n("c", "Barcelona")], [], 80
        )
        assert [p["score"] for p in pairs] == sorted((p["score"] for p in pairs), reverse=True)
        assert pairs[0]["score"] == 100


class TestFindOrphans:
    def test_a_node_no_edge_touches_and_zep_counts_none_for(self):
        assert [o["uuid"] for o in find_orphans([n("a", "A", degree=0)], [])] == ["a"]

    def test_zep_degree_alone_saves_a_node(self):
        """An edge the listing missed still counts if Zep says the node has one."""
        assert find_orphans([n("a", "A", degree=2)], []) == []

    def test_an_edge_in_the_listing_saves_a_node(self):
        assert find_orphans([n("a", "A", degree=0)], [e("a", "b")]) == []

    def test_the_user_node_is_never_one(self):
        assert find_orphans([n("u", "Daniel", ["User"])], []) == []


class TestIsUserNode:
    def test_by_name_or_label(self):
        assert is_user_node(n("a", "kwami_123"))
        assert is_user_node(n("a", "User"))
        assert is_user_node(n("a", "Daniel", ["User"]))
        assert not is_user_node(n("a", "Daniel", ["Person"]))


def test_a_uuid_listed_twice_is_not_paired_with_itself():
    assert find_duplicate_pairs([n("a", "Barcelona"), n("a", "Barcelona")], [], 90) == []
