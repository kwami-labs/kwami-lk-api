"""`_infer_node_type` and `_extract_user_display_name`.

Both are pure keyword heuristics over Zep's output, and both are user-visible:
the inferred type drives the graph view's node colours and icons, and the display
name is what the UI calls the person. They are long if-chains, so the tests are
a table per branch rather than prose.
"""

from __future__ import annotations

import pytest

from src.api.routes.memory import _extract_user_display_name, _infer_node_type
from src.core.security import AuthUser


class TestInferNodeTypeFromLabels:
    def test_a_specific_label_wins(self):
        assert _infer_node_type("Anything", "any summary", ["Preference"]) == "preference"

    def test_the_label_is_lowercased(self):
        assert _infer_node_type("X", None, ["PERSON"]) == "person"

    @pytest.mark.parametrize("label", ["entity", "node", "unknown", "", "Entity", "NODE"])
    def test_a_generic_label_falls_through_to_inference(self, label):
        """Zep's free tier returns these, which is why the heuristics exist."""
        assert _infer_node_type("my dog Rex", None, [label]) == "pet"

    def test_only_the_first_label_is_considered(self):
        assert _infer_node_type("X", None, ["Skill", "Person"]) == "skill"


class TestInferNodeTypeHeuristics:
    @pytest.mark.parametrize("name", ["kwami_abc", "user", "User", "KWAMI_xyz"])
    def test_the_user_themselves(self, name):
        assert _infer_node_type(name, None, []) == "user"

    def test_an_identifies_as_summary_is_the_user(self):
        assert _infer_node_type("Daniel", "identifies_as the account holder", []) == "user"

    @pytest.mark.parametrize("name", ["assistant", "ai", "bot", "My Assistant"])
    def test_the_assistant(self, name):
        assert _infer_node_type(name, None, []) == "assistant"

    @pytest.mark.parametrize(
        "text",
        [
            "my friend Bob",
            "her sister",
            "his boss",
            "a colleague",
            "a family",
            "my mother",
            "my father",
            "his wife",
            "her husband",
            "my brother",
            "a manager",
            "a person",
        ],
    )
    def test_people(self, text):
        assert _infer_node_type(text, None, []) == "person"

    @pytest.mark.parametrize(
        "text",
        [
            "my dog",
            "a cat",
            "a puppy",
            "her kitten",
            "a labrador",
            "golden retriever",
            "german shepherd",
            "a poodle",
            "a bulldog",
            "my pet",
            "a bird",
            "a fish",
        ],
    )
    def test_pets(self, text):
        assert _infer_node_type(text, None, []) == "pet"

    @pytest.mark.parametrize(
        "text",
        [
            "Barcelona",
            "Madrid",
            "London",
            "Paris",
            "New York",
            "Tokyo",
            "a city",
            "a country",
            "lives in Spain",
            "born in Peru",
            "a neighborhood",
            "a district",
            "a region",
            "Main street",
            "an address",
        ],
    )
    def test_locations(self, text):
        assert _infer_node_type(text, None, []) == "location"

    @pytest.mark.parametrize(
        "text",
        [
            "a park",
            "my home",
            "a house",
            "an apartment",
            "an office",
            "a restaurant",
            "a café",
            "a cafe",
            "a bar",
        ],
    )
    def test_places(self, text):
        assert _infer_node_type(text, None, []) == "place"

    @pytest.mark.parametrize(
        "text",
        [
            "likes jazz",
            "loves running",
            "enjoys films",
            "prefers tea",
            "a favorite",
            "a favourite",
            "a preference",
            "interested in art",
            "passionate about it",
        ],
    )
    def test_preferences(self, text):
        assert _infer_node_type(text, None, []) == "preference"

    @pytest.mark.parametrize(
        "text",
        [
            "a developer",
            "an engineer",
            "a designer",
            "a musician",
            "a programmer",
            "software",
            "works as a chef",
            "a profession",
            "a job",
            "a skill",
            "expertise",
            "experience in Go",
        ],
    )
    def test_skills(self, text):
        assert _infer_node_type(text, None, []) == "skill"

    @pytest.mark.parametrize(
        "text",
        [
            "an event",
            "a meeting",
            "an appointment",
            "a birthday",
            "an anniversary",
            "a conference",
            "a wedding",
            "a trip",
        ],
    )
    def test_events(self, text):
        assert _infer_node_type(text, None, []) == "event"

    @pytest.mark.parametrize(
        "text",
        [
            "music",
            "sports",
            "art",
            "technology",
            "science",
            "cooking",
            "gaming",
            "reading",
            "travel",
            "photography",
            "genre",
        ],
    )
    def test_topics(self, text):
        assert _infer_node_type(text, None, []) == "topic"

    def test_project(self):
        assert _infer_node_type("a redesign project", None, []) == "project"
        assert _infer_node_type("X", "working on a thing", []) == "project"

    def test_product(self):
        assert _infer_node_type("a new app", None, []) == "product"

    @pytest.mark.parametrize(
        "text",
        [
            "a company",
            "an organization",
            "our team",
            "a group",
            "a corporation",
            "a business",
            "a firm",
            "an agency",
        ],
    )
    def test_organizations(self, text):
        assert _infer_node_type(text, None, []) == "organization"

    @pytest.mark.parametrize(
        "text", ["a color", "a colour", "brown fur", "black", "white", "red", "blue"]
    )
    def test_colour_attributes(self, text):
        assert _infer_node_type(text, None, []) == "attribute"

    @pytest.mark.parametrize("text", ["30 years old", "his age", "her height", "a weight"])
    def test_measurement_attributes(self, text):
        assert _infer_node_type(text, None, []) == "attribute"

    def test_nothing_matches_is_an_entity(self):
        assert _infer_node_type("Zzzqqx", None, []) == "entity"

    def test_the_summary_is_searched_too(self):
        assert _infer_node_type("Rex", "a golden retriever", []) == "pet"

    def test_a_none_summary_is_tolerated(self):
        assert _infer_node_type("Zzzqqx", None, []) == "entity"

    def test_the_match_is_case_insensitive(self):
        assert _infer_node_type("MY DOG", None, []) == "pet"

    def test_earlier_categories_win(self):
        """The chain is ordered, so a pet in a city is a pet."""
        assert _infer_node_type("my dog in Barcelona", None, []) == "pet"


class TestInferNodeTypeTheBug:
    """`"he "` is in `person_indicators`, and `"he "` is a substring of `"the "`.

    So every node whose name or summary contains the ordinary English word
    "the" is classified as a person. Zep summaries are natural prose, so in
    practice that is most of the graph -- the memory view renders nearly every
    node with the person colour and icon.

    Pinned as it ships. The fix is a word-boundary match (`\bhe\b`) rather than
    a substring, for this and the other pronoun indicators; these tests should be
    inverted at the same time.
    """

    @pytest.mark.parametrize(
        ("name", "summary", "should_be"),
        [
            ("Barcelona", "The city where the user lives", "location"),
            ("Acme Corp", "The company the user works at", "organization"),
            ("Redesign", "The project the user is building", "project"),
            ("Rex", "The dog the user owns", "pet"),
            ("Python", "The language the user knows", "topic"),
        ],
    )
    def test_any_summary_containing_the_word_the_is_called_a_person(self, name, summary, should_be):
        assert _infer_node_type(name, summary, []) == "person"
        assert should_be != "person", "none of these are people"

    def test_it_is_specifically_the_word_the(self):
        """`"he "` needs the space, so only "the " (and "she ", which is intended)."""
        assert _infer_node_type("the thing", None, []) == "person"
        assert _infer_node_type("other thing", None, []) == "entity"
        assert _infer_node_type("whether thing", None, []) == "entity"

    def test_cat_inside_location_makes_a_location_a_pet(self):
        """`"cat"` is a pet indicator and a substring of "loCATion"."""
        assert _infer_node_type("a location", None, []) == "pet"
        assert _infer_node_type("X", "the allocation of resources", []) == "person", (
            "and 'the' gets there first"
        )

    def test_art_inside_startup_makes_a_startup_a_topic(self):
        """`"art"` is a topic indicator and a substring of "stARTup"."""
        assert _infer_node_type("a startup", None, []) == "topic"

    def test_art_inside_party_makes_a_party_a_topic(self):
        """Same `"art"`, this time inside "pARTy" -- a party is an event."""
        assert _infer_node_type("a party", None, []) == "topic"

    def test_cat_inside_vacation_makes_a_vacation_a_pet(self):
        """Same `"cat"` as loCATion, this time inside "vaCATion" -- an event."""
        assert _infer_node_type("a vacation", None, []) == "pet"


class TestExtractUserDisplayName:
    AUTH = AuthUser({"sub": "u1", "email": "ada.lovelace@example.com"})

    def _entity(self, **overrides):
        return {"uuid": "user-1", "summary": None, **overrides}

    def test_a_name_from_the_summary(self):
        entity = self._entity(summary="Daniel is a software engineer. He likes coffee.")
        assert _extract_user_display_name(entity, [], [], self.AUTH) == "Daniel"

    @pytest.mark.parametrize(
        "verb",
        ["is", "enjoys", "likes", "works", "lives", "has", "was", "prefers", "loves", "wants"],
    )
    def test_every_summary_verb(self, verb):
        entity = self._entity(summary=f"Daniel {verb} something")
        assert _extract_user_display_name(entity, [], [], self.AUTH) == "Daniel"

    def test_a_multi_word_name(self):
        entity = self._entity(summary="Ada Lovelace is a mathematician.")
        assert _extract_user_display_name(entity, [], [], self.AUTH) == "Ada Lovelace"

    def test_an_accented_name(self):
        entity = self._entity(summary="José is a chef.")
        assert _extract_user_display_name(entity, [], [], self.AUTH) == "José"

    @pytest.mark.parametrize(
        "word", ["The", "This", "User", "Person", "Someone", "He", "She", "They"]
    )
    def test_a_generic_word_is_not_a_name(self, word):
        entity = self._entity(summary=f"{word} is a person.")
        assert _extract_user_display_name(entity, [], [], self.AUTH) != word

    def test_the_user_comma_name_pattern(self):
        entity = self._entity(summary="The user, Daniel, enjoys hiking")
        assert _extract_user_display_name(entity, [], [], self.AUTH) == "Daniel"

    def test_the_user_name_pattern_without_a_comma(self):
        entity = self._entity(summary="user Daniel, does things")
        assert _extract_user_display_name(entity, [], [], self.AUTH) == "Daniel"

    def test_the_ignorecase_flag_defeats_the_capitalisation_rule_known_bug(self):
        r"""The pattern uses `[A-Z]` to find a proper noun, then passes IGNORECASE.

        With the flag, `[A-Z]` matches lowercase too, so the greedy
        `(?:\s[A-Z][a-zà-ÿ]+)*` swallows the rest of the clause as if it were
        more given names. A summary reading "user daniel does things" yields
        "daniel does things" as the display name. Pinned as it behaves; the fix
        is to drop `re.IGNORECASE` and match the literal "user"/"User" instead.
        """
        entity = self._entity(summary="user daniel does things")
        assert _extract_user_display_name(entity, [], [], self.AUTH) == "daniel does things"

    @pytest.mark.parametrize(
        ("fact", "expected"),
        [
            ("Their name is Daniel", "Daniel"),
            ("They are called Daniel", "Daniel"),
            ("A person named Daniel", "Daniel"),
            ("The user identifies as Daniel", "Daniel"),
        ],
    )
    def test_a_name_from_an_edge_fact(self, fact, expected):
        edges = [{"source_node": "user-1", "target_node": "n-2", "fact": fact}]
        assert _extract_user_display_name(self._entity(), edges, [], self.AUTH) == expected

    def test_an_edge_on_the_target_side_is_searched_too(self):
        edges = [{"source_node": "n-0", "target_node": "user-1", "fact": "name is Daniel"}]
        assert _extract_user_display_name(self._entity(), edges, [], self.AUTH) == "Daniel"

    def test_an_unconnected_edge_is_ignored(self):
        edges = [{"source_node": "n-7", "target_node": "n-8", "fact": "name is Daniel"}]
        assert _extract_user_display_name(self._entity(), edges, [], self.AUTH) != "Daniel"

    def test_an_edge_without_a_fact(self):
        edges = [{"source_node": "user-1", "target_node": "n-2", "fact": None}]
        assert _extract_user_display_name(self._entity(), edges, [], self.AUTH) == "Ada Lovelace"

    def test_a_connected_person_node_that_is_the_user(self):
        edges = [{"source_node": "user-1", "target_node": "p-1", "fact": "knows"}]
        nodes = [
            {
                "uuid": "p-1",
                "type": "person",
                "name": "Daniel",
                "summary": "identifies as the account holder",
            }
        ]
        assert _extract_user_display_name(self._entity(), edges, nodes, self.AUTH) == "Daniel"

    def test_a_connected_person_on_the_source_side(self):
        edges = [{"source_node": "p-1", "target_node": "user-1", "fact": "knows"}]
        nodes = [
            {
                "uuid": "p-1",
                "type": "person",
                "name": "Daniel",
                "summary": "this is the user themselves",
            }
        ]
        assert _extract_user_display_name(self._entity(), edges, nodes, self.AUTH) == "Daniel"

    def test_a_connected_person_who_is_not_the_user_is_skipped(self):
        edges = [{"source_node": "user-1", "target_node": "p-1", "fact": "knows"}]
        nodes = [{"uuid": "p-1", "type": "person", "name": "Bob", "summary": "a colleague"}]
        assert _extract_user_display_name(self._entity(), edges, nodes, self.AUTH) != "Bob"

    def test_a_kwami_named_node_is_skipped(self):
        edges = [{"source_node": "user-1", "target_node": "p-1", "fact": "knows"}]
        nodes = [
            {
                "uuid": "p-1",
                "type": "person",
                "name": "kwami_abc",
                "summary": "identifies as the user",
            }
        ]
        assert _extract_user_display_name(self._entity(), edges, nodes, self.AUTH) != "kwami_abc"

    def test_a_non_person_node_is_skipped(self):
        edges = [{"source_node": "user-1", "target_node": "p-1", "fact": "knows"}]
        nodes = [
            {"uuid": "p-1", "type": "place", "name": "Daniel", "summary": "identifies as the user"}
        ]
        assert _extract_user_display_name(self._entity(), edges, nodes, self.AUTH) != "Daniel"

    def test_the_email_local_part_is_the_fallback(self):
        assert _extract_user_display_name(self._entity(), [], [], self.AUTH) == "Ada Lovelace"

    def test_underscores_in_the_email_become_spaces(self):
        auth = AuthUser({"sub": "u", "email": "ada_lovelace@example.com"})
        assert _extract_user_display_name(self._entity(), [], [], auth) == "Ada Lovelace"

    def test_no_email_falls_back_to_user(self):
        auth = AuthUser({"sub": "u"})
        assert _extract_user_display_name(self._entity(), [], [], auth) == "User"

    def test_an_entity_without_a_uuid_skips_the_edge_search(self):
        entity = {"summary": None}
        edges = [{"source_node": None, "target_node": None, "fact": "name is Daniel"}]
        assert _extract_user_display_name(entity, edges, [], self.AUTH) == "Ada Lovelace"

    def test_a_summary_matching_nothing_falls_through(self):
        entity = self._entity(summary="nothing useful here at all")
        assert _extract_user_display_name(entity, [], [], self.AUTH) == "Ada Lovelace"
