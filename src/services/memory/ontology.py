"""The shipped ontology, and the Zep SDK models built from it."""

from __future__ import annotations

import logging

logger = logging.getLogger("kwami-api.memory")

# Default ontology for Kwami agents
DEFAULT_ENTITY_TYPES = [
    {"name": "Preference", "description": "User preferences, choices, opinions, or selections."},
    {"name": "Procedure", "description": "Multi-step instructions or workflows."},
    {"name": "Person", "description": "People mentioned in conversation."},
    {"name": "Organization", "description": "Companies, institutions, teams, or groups."},
    {"name": "Location", "description": "Physical places, cities, countries, venues."},
    {"name": "Event", "description": "Scheduled or past events, meetings, appointments."},
    {"name": "Project", "description": "Work projects, personal initiatives, creative endeavors."},
    {"name": "Topic", "description": "Subjects of interest or discussion themes."},
    {"name": "Product", "description": "Products, services, software, or tools."},
    {"name": "Skill", "description": "User skills, expertise, or competencies."},
    {"name": "Goal", "description": "User goals, objectives, or aspirations."},
]


DEFAULT_EDGE_TYPES = [
    {"name": "KNOWS", "description": "The user knows a person."},
    {"name": "WORKS_AT", "description": "Employment relationship with an organization."},
    {"name": "LIVES_IN", "description": "The user's residence or location."},
    {"name": "INTERESTED_IN", "description": "Topics or things the user is interested in."},
    {"name": "WORKING_ON", "description": "Projects the user is actively working on."},
    {"name": "HAS_SKILL", "description": "Skills the user possesses."},
    {"name": "WANTS_TO", "description": "Goals or desires the user has expressed."},
    {"name": "ATTENDED", "description": "Events the user has attended or will attend."},
    {"name": "USES", "description": "Products or tools the user uses."},
    {"name": "PREFERS", "description": "Preferences the user has expressed."},
]


def _build_ontology_models(
    entity_types: list[dict],
    edge_types: list[dict],
) -> tuple[dict, dict]:
    """Build Zep v3 SDK ontology models from dict definitions.

    Converts simple {name, description} dicts into EntityModel/EdgeModel classes
    required by the Zep v3 SDK's set_ontology method.
    """
    from pydantic import Field
    from zep_cloud import EntityEdgeSourceTarget
    from zep_cloud.external_clients.ontology import EdgeModel, EntityModel, EntityText

    entities = {}
    for et in entity_types:
        name = et["name"]
        desc = et.get("description", name)
        model_cls = type(
            name,
            (EntityModel,),
            {
                "__doc__": desc,
                "__annotations__": {"detail": EntityText},
                "detail": Field(description=desc, default=None),
            },
        )
        entities[name] = model_cls

    edges = {}
    for edge in edge_types:
        name = edge["name"]
        desc = edge.get("description", name)
        model_cls = type(
            name,
            (EdgeModel,),
            {
                "__doc__": desc,
                "__annotations__": {"detail": EntityText},
                "detail": Field(description=desc, default=None),
            },
        )
        edges[name] = (
            model_cls,
            [EntityEdgeSourceTarget(source="User")],
        )

    return entities, edges
