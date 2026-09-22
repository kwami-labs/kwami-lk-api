"""The extraction schema: ontology, fact rating, and custom instructions."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from zep_cloud.client import AsyncZep

from src.api.deps import require_auth
from src.api.routes.memory.schemas import (
    CustomInstructionsBody,
    FactRatingRequest,
    OntologySchema,
)
from src.core.security import AuthUser
from src.services.memory import (
    DEFAULT_EDGE_TYPES,
    DEFAULT_ENTITY_TYPES,
    _build_ontology_models,
    get_zep_client,
    verify_user_access,
)

logger = logging.getLogger("kwami-api.memory")
router = APIRouter()

# How many records are pulled from Zep before slicing a "page" in Python.
# Endpoints fetch this many regardless of the requested page size, so `total`
# saturates here and `has_more` is wrong beyond it.
ZEP_FETCH_LIMIT = 1000


@router.get("/{user_id}/ontology")
async def get_user_ontology(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Get the ontology (entity/edge type schema) for a user's knowledge graph.

    Returns the configured entity types and edge types that Zep uses
    for extracting information from conversations.
    """
    verify_user_access(user, user_id)
    logger.info("📋 Fetching ontology for user: %s", user_id)

    try:
        ontology = await client.graph.get_ontology(user_id=user_id)

        if ontology:
            return {
                "user_id": user_id,
                "entity_types": [
                    {"name": e.name, "description": e.description}
                    for e in (ontology.entity_types or [])
                ],
                "edge_types": [
                    {"name": e.name, "description": e.description}
                    for e in (ontology.edge_types or [])
                ],
            }
        # Return defaults if no ontology configured
        return {
            "user_id": user_id,
            "entity_types": DEFAULT_ENTITY_TYPES,
            "edge_types": DEFAULT_EDGE_TYPES,
            "is_default": True,
        }

    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg:
            # No ontology configured, return defaults
            return {
                "user_id": user_id,
                "entity_types": DEFAULT_ENTITY_TYPES,
                "edge_types": DEFAULT_EDGE_TYPES,
                "is_default": True,
            }
        logger.exception("Failed to fetch ontology")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/{user_id}/ontology")
async def set_user_ontology(
    user_id: str,
    ontology: OntologySchema,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Set the ontology (entity/edge type schema) for a user's knowledge graph.

    This configures what types of entities and relationships Zep will
    extract from conversations. Changes apply to future extractions.
    """
    verify_user_access(user, user_id)
    logger.info("📋 Setting ontology for user: %s", user_id)

    try:
        entities, edges = _build_ontology_models(
            [e.model_dump() for e in ontology.entity_types],
            [e.model_dump() for e in ontology.edge_types],
        )

        await client.graph.set_ontology(
            entities=entities,
            edges=edges,
            user_ids=[user_id],
        )

        logger.info(
            "📋 Ontology set: %s entity types, %s edge types",
            len(ontology.entity_types),
            len(ontology.edge_types),
        )

        return {
            "success": True,
            "user_id": user_id,
            "entity_types_count": len(ontology.entity_types),
            "edge_types_count": len(ontology.edge_types),
        }

    except Exception as e:
        logger.exception("Failed to set ontology")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{user_id}/ontology/reset")
async def reset_user_ontology(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Reset the ontology to default Kwami entity/edge types."""
    verify_user_access(user, user_id)
    logger.info("📋 Resetting ontology to defaults for user: %s", user_id)

    try:
        entities, edges = _build_ontology_models(
            DEFAULT_ENTITY_TYPES,
            DEFAULT_EDGE_TYPES,
        )

        await client.graph.set_ontology(
            entities=entities,
            edges=edges,
            user_ids=[user_id],
        )

        return {
            "success": True,
            "user_id": user_id,
            "message": "Ontology reset to defaults",
            "entity_types_count": len(DEFAULT_ENTITY_TYPES),
            "edge_types_count": len(DEFAULT_EDGE_TYPES),
        }

    except Exception as e:
        logger.exception("Failed to reset ontology")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{user_id}/fact-rating")
async def get_fact_rating(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Get the current fact rating instruction for a user's graph.

    Fact rating lets you configure how Zep rates the importance of facts,
    so you can filter out low-value memories for your use case.
    """
    verify_user_access(user, user_id)
    logger.info("⭐ Fetching fact rating for user: %s", user_id)

    try:
        # User graphs are identified by user_id as graph_id in Zep
        # Try to get the graph info to read fact_rating_instruction
        try:
            # Search with min_fact_rating to see if rating is configured
            # There's no direct "get graph by user" - try listing
            graphs_response = await client.graph.list_all(page_size=100)
            user_graph = None
            if graphs_response and hasattr(graphs_response, "graphs"):
                for g in graphs_response.graphs or []:
                    gid = getattr(g, "graph_id", None) or getattr(g, "uuid", None)
                    if gid and user_id in str(gid):
                        user_graph = g
                        break

            if (
                user_graph
                and hasattr(user_graph, "fact_rating_instruction")
                and user_graph.fact_rating_instruction
            ):
                fri = user_graph.fact_rating_instruction
                result = {
                    "configured": True,
                    "instruction": getattr(fri, "instruction", None),
                    "examples": None,
                }
                if hasattr(fri, "examples") and fri.examples:
                    result["examples"] = {
                        "high": getattr(fri.examples, "high", None),
                        "medium": getattr(fri.examples, "medium", None),
                        "low": getattr(fri.examples, "low", None),
                    }
                return result
        except Exception as e:
            logger.warning("⭐ Could not read graph info: %s", e)

        return {"configured": False, "instruction": None, "examples": None}

    except Exception as e:
        logger.exception("Failed to get fact rating")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/{user_id}/fact-rating")
async def set_fact_rating(
    user_id: str,
    body: FactRatingRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Set fact rating instructions for a user's graph.

    This configures how Zep rates the importance of extracted facts.
    You provide an instruction string and three examples (high/medium/low).
    """
    verify_user_access(user, user_id)
    logger.info("⭐ Setting fact rating for user: %s", user_id)

    try:
        from zep_cloud import FactRatingExamples, FactRatingInstruction

        fri = FactRatingInstruction(
            instruction=body.instruction,
            examples=FactRatingExamples(
                high=body.examples.high,
                medium=body.examples.medium,
                low=body.examples.low,
            ),
        )

        # Update the user's graph with the fact rating instruction
        # User graphs use user_id as the graph_id
        await client.graph.update(
            graph_id=user_id,
            fact_rating_instruction=fri,
        )

        logger.info("⭐ Fact rating set for user: %s", user_id)
        return {
            "success": True,
            "user_id": user_id,
            "instruction": body.instruction,
        }

    except Exception as e:
        logger.exception("Failed to set fact rating")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{user_id}/instructions")
async def get_custom_instructions(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Get all custom instructions for a user's graph.

    Custom instructions guide how Zep extracts and organizes knowledge
    from conversations for this specific user.
    """
    verify_user_access(user, user_id)
    logger.info("📝 Fetching custom instructions for user: %s", user_id)

    try:
        response = await client.graph.list_custom_instructions(user_id=user_id)

        instructions = []
        if response and hasattr(response, "instructions") and response.instructions:
            for inst in response.instructions:
                instructions.append(
                    {
                        "name": getattr(inst, "name", ""),
                        "text": getattr(inst, "text", ""),
                    }
                )

        return {"instructions": instructions, "count": len(instructions)}

    except Exception as e:
        logger.exception("Failed to get custom instructions")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{user_id}/instructions")
async def add_custom_instructions(
    user_id: str,
    body: CustomInstructionsBody,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Add custom instructions for a user's graph.

    Instructions guide how Zep processes and extracts knowledge.
    Each instruction has a unique name and a text body.
    """
    verify_user_access(user, user_id)
    logger.info("📝 Adding %s custom instructions for user: %s", len(body.instructions), user_id)

    try:
        from zep_cloud import CustomInstruction

        zep_instructions = [
            CustomInstruction(name=inst.name, text=inst.text) for inst in body.instructions
        ]

        await client.graph.add_custom_instructions(
            instructions=zep_instructions,
            user_ids=[user_id],
        )

        logger.info("📝 Added %s instructions for user: %s", len(body.instructions), user_id)
        return {
            "success": True,
            "added": len(body.instructions),
        }

    except Exception as e:
        logger.exception("Failed to add custom instructions")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete("/{user_id}/instructions")
async def delete_custom_instructions(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    names: str | None = Query(
        None, description="Comma-separated instruction names to delete. If empty, deletes all."
    ),
):
    """Delete custom instructions for a user's graph.

    Pass instruction names as a comma-separated query param to delete specific ones,
    or omit to delete all instructions for the user.
    """
    verify_user_access(user, user_id)

    instruction_names = None
    if names:
        instruction_names = [n.strip() for n in names.split(",") if n.strip()]

    logger.info("📝 Deleting instructions for user: %s (names=%s)", user_id, instruction_names)

    try:
        await client.graph.delete_custom_instructions(
            user_ids=[user_id],
            instruction_names=instruction_names,
        )

        return {"success": True, "deleted": instruction_names or "all"}

    except Exception as e:
        logger.exception("Failed to delete custom instructions")
        raise HTTPException(status_code=500, detail=str(e)) from e
