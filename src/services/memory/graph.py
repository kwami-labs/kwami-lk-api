"""Graph shaping: node typing, the user's display name, and the raw fetch.

This is the part of the memory surface that is not HTTP at all -- ~350 lines of
classification and extraction that lived in the route module, which is why that
file reached 2,900 lines and why none of this could be exercised without going
through a router.
"""

from __future__ import annotations

import logging

from zep_cloud.client import AsyncZep

from src.core.security import AuthUser

logger = logging.getLogger("kwami-api.memory")


def _infer_node_type(name: str, summary: str | None, labels: list[str]) -> str:
    """Infer node type from Zep labels or fall back to keyword-based inference.

    Zep's free tier often returns generic labels, so we use smart inference.
    """
    # First, check if Zep provided a meaningful label
    if labels:
        label = labels[0].lower()
        # If it's a specific type (not generic), use it
        if label not in ("entity", "node", "unknown", ""):
            return label

    # Fall back to keyword-based inference
    name_lower = name.lower()
    text = f"{name_lower} {(summary or '').lower()}"

    # User/Assistant detection (highest priority)
    if "kwami_" in name_lower or name_lower == "user" or "identifies_as" in text:
        return "user"
    if name_lower in ("assistant", "ai", "bot") or "assistant" in name_lower:
        return "assistant"

    # Person detection
    person_indicators = [
        "person",
        "he ",
        "she ",
        "they ",
        "friend",
        "family",
        "brother",
        "sister",
        "mother",
        "father",
        "wife",
        "husband",
        "colleague",
        "boss",
        "manager",
    ]
    if any(p in text for p in person_indicators):
        return "person"

    # Pet/Animal detection
    pet_indicators = [
        "dog",
        "cat",
        "pet",
        "puppy",
        "kitten",
        "bird",
        "fish",
        "labrador",
        "retriever",
        "shepherd",
        "poodle",
        "bulldog",
    ]
    if any(p in text for p in pet_indicators):
        return "pet"

    # Location detection
    location_indicators = [
        "city",
        "country",
        "location",
        "lives in",
        "from",
        "born in",
        "street",
        "address",
        "neighborhood",
        "district",
        "region",
        "barcelona",
        "madrid",
        "london",
        "paris",
        "new york",
        "tokyo",
    ]
    if any(loc in text for loc in location_indicators):
        return "location"

    # Place/Venue detection (more specific than location)
    place_indicators = [
        "park",
        "home",
        "house",
        "apartment",
        "office",
        "restaurant",
        "café",
        "cafe",
        "bar",
        "gym",
        "school",
        "university",
        "hospital",
        "store",
        "shop",
        "mall",
        "airport",
        "station",
    ]
    if any(p in text for p in place_indicators):
        return "place"

    # Preference detection
    preference_indicators = [
        "likes",
        "loves",
        "enjoys",
        "prefers",
        "favorite",
        "favourite",
        "preference",
        "interested in",
        "passionate",
    ]
    if any(p in text for p in preference_indicators):
        return "preference"

    # Skill/Profession detection
    skill_indicators = [
        "developer",
        "engineer",
        "designer",
        "artist",
        "musician",
        "programmer",
        "software",
        "works as",
        "profession",
        "job",
        "skill",
        "expertise",
        "experience in",
    ]
    if any(s in text for s in skill_indicators):
        return "skill"

    # Topic/Interest detection
    topic_indicators = [
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
    ]
    if any(t in text for t in topic_indicators):
        return "topic"

    # Event detection
    event_indicators = [
        "event",
        "meeting",
        "appointment",
        "birthday",
        "anniversary",
        "conference",
        "party",
        "wedding",
        "trip",
        "vacation",
    ]
    if any(e in text for e in event_indicators):
        return "event"

    # Project detection
    project_indicators = ["project", "working on", "building", "developing", "creating"]
    if any(p in text for p in project_indicators):
        return "project"

    # Product detection
    product_indicators = ["product", "app", "application", "tool", "service", "device"]
    if any(p in text for p in product_indicators):
        return "product"

    # Organization detection
    org_indicators = [
        "company",
        "organization",
        "team",
        "group",
        "corporation",
        "startup",
        "business",
        "firm",
        "agency",
    ]
    if any(o in text for o in org_indicators):
        return "organization"

    # Attribute/Property detection (colors, ages, etc.)
    if any(c in text for c in ["color", "colour", "brown", "black", "white", "red", "blue"]):
        return "attribute"
    if any(a in text for a in ["years old", "age", "height", "weight"]):
        return "attribute"

    # Default
    return "entity"


def _extract_user_display_name(
    user_entity: dict,
    graph_edges_raw: list[dict],
    all_nodes: list[dict],
    auth_user: AuthUser,
) -> str:
    """Extract the user's actual name from the knowledge graph data.

    Strategy (in order of priority):
    1. Check the user node's summary for a name (e.g. "Daniel is a...")
    2. Check edge facts for name-related info (e.g. "User's name is Daniel")
    3. Fall back to the auth user's email local part
    4. Fall back to "User"
    """
    import re

    # 1. Try to extract name from the user node summary
    summary = user_entity.get("summary") or ""
    if summary:
        # Common patterns: "X is a...", "X, also known as...", "X enjoys..."
        # The summary typically starts with the person's name
        first_sentence = summary.split(".")[0].strip()

        # Pattern: "Name is a/an ..." or "Name enjoys/likes/works..."
        match = re.match(
            r"^([A-Z][a-zà-ÿ]+(?:\s[A-Z][a-zà-ÿ]+)*)\s+(?:is|enjoys|likes|works|lives|has|was|prefers|loves|wants)",
            first_sentence,
        )
        if match:
            name = match.group(1).strip()
            # Sanity check: not a generic word
            if name.lower() not in (
                "the",
                "this",
                "user",
                "person",
                "someone",
                "he",
                "she",
                "they",
            ):
                return name

        # Pattern: "The user, Name, ..." or "The user Name ..."
        match = re.match(
            r"(?:The\s+)?user[,\s]+([A-Z][a-zà-ÿ]+(?:\s[A-Z][a-zà-ÿ]+)*)",
            first_sentence,
            re.IGNORECASE,
        )
        if match:
            name = match.group(1).strip().rstrip(",")
            if name.lower() not in ("is", "has", "was", "the"):
                return name

    # 2. Check edge facts for name mentions
    user_uuid = user_entity.get("uuid")
    if user_uuid:
        for edge in graph_edges_raw:
            if edge.get("source_node") == user_uuid or edge.get("target_node") == user_uuid:
                # Look for "name is X", "called X", "named X", "identifies as X"
                for pattern in [
                    r"name\s+is\s+([A-Z][a-zà-ÿ]+)",
                    r"called\s+([A-Z][a-zà-ÿ]+)",
                    r"named\s+([A-Z][a-zà-ÿ]+)",
                    r"identifies\s+as\s+([A-Z][a-zà-ÿ]+)",
                ]:
                    match = re.search(pattern, edge.get("fact") or "", re.IGNORECASE)
                    if match:
                        return match.group(1).strip()

    # 3. Check if any connected "person" node matches the user
    # (sometimes the user's name exists as a separate Person node linked to the user node)
    if user_uuid:
        connected_person_uuids = set()
        for edge in graph_edges_raw:
            if edge.get("source_node") == user_uuid:
                connected_person_uuids.add(edge.get("target_node"))
            elif edge.get("target_node") == user_uuid:
                connected_person_uuids.add(edge.get("source_node"))

        for node in all_nodes:
            if (
                node.get("uuid") in connected_person_uuids
                and node.get("type") == "person"
                and node.get("name")
                and "kwami_" not in (node.get("name") or "").lower()
            ):
                # Check if the summary suggests this is the user themselves
                node_summary = (node.get("summary") or "").lower()
                if any(
                    kw in node_summary for kw in ["identifies as", "the user", "self", "themselves"]
                ):
                    return node["name"]

    # 4. Fall back to email-derived name
    if auth_user.email:
        return auth_user.email.split("@")[0].replace(".", " ").replace("_", " ").title()

    return "User"


async def _fetch_graph_raw(client: AsyncZep, user_id: str, limit: int = 200):
    """Helper: fetch raw nodes and edges from Zep for graph analysis."""
    nodes_list = []
    edges_list = []

    try:
        nodes_response = await client.graph.node.get_by_user_id(user_id=user_id, limit=limit)
        if nodes_response:
            for n in nodes_response:
                nodes_list.append(
                    {
                        "uuid": getattr(n, "uuid_", None) or getattr(n, "uuid", None),
                        "name": getattr(n, "name", "Unknown"),
                        "summary": getattr(n, "summary", None),
                        "labels": list(n.labels) if hasattr(n, "labels") and n.labels else [],
                    }
                )
    except Exception as e:
        logger.warning("Failed to fetch nodes for analysis: %s", e)

    try:
        edges_response = await client.graph.edge.get_by_user_id(user_id=user_id, limit=limit)
        if edges_response:
            for e in edges_response:
                edges_list.append(
                    {
                        "uuid": getattr(e, "uuid_", None) or getattr(e, "uuid", None),
                        "source_node_uuid": getattr(e, "source_node_uuid", None),
                        "target_node_uuid": getattr(e, "target_node_uuid", None),
                        "fact": getattr(e, "fact", None),
                        "name": getattr(e, "name", None),
                        "valid_at": str(e.valid_at)
                        if hasattr(e, "valid_at") and e.valid_at
                        else None,
                        "invalid_at": str(e.invalid_at)
                        if hasattr(e, "invalid_at") and e.invalid_at
                        else None,
                    }
                )
    except Exception as e:
        logger.warning("Failed to fetch edges for analysis: %s", e)

    return nodes_list, edges_list
