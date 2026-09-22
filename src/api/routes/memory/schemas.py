"""Request bodies for the memory endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class EntityTypeDefinition(BaseModel):
    """Definition of an entity type for the knowledge graph."""

    name: str
    description: str


class EdgeTypeDefinition(BaseModel):
    """Definition of an edge (relationship) type for the knowledge graph."""

    name: str
    description: str


class OntologySchema(BaseModel):
    """Full ontology schema with entity and edge types."""

    entity_types: list[EntityTypeDefinition]
    edge_types: list[EdgeTypeDefinition]


class UpdateEdgeRequest(BaseModel):
    """Request body for updating an edge (fact/relationship)."""

    fact: str | None = None
    name: str | None = None
    source_node_uuid: str | None = None
    target_node_uuid: str | None = None
    valid_at: str | None = None
    invalid_at: str | None = None


class UpdateNodeRequest(BaseModel):
    """Request body for updating a node (entity)."""

    name: str | None = None
    summary: str | None = None
    labels: list[str] | None = None


class FactRatingExamplesRequest(BaseModel):
    """Examples for fact rating (high/medium/low rated facts)."""

    high: str
    medium: str
    low: str


class FactRatingRequest(BaseModel):
    """Request body for setting fact rating instructions."""

    instruction: str
    examples: FactRatingExamplesRequest


class CustomInstructionRequest(BaseModel):
    """A single custom instruction."""

    name: str
    text: str


class CustomInstructionsBody(BaseModel):
    """Request body for adding custom instructions."""

    instructions: list[CustomInstructionRequest]


class DeleteInstructionsBody(BaseModel):
    """Request body for deleting custom instructions."""

    instruction_names: list[str] | None = None


class IngestRequest(BaseModel):
    """Request body for re-ingesting data into the graph."""

    data: str
    type: str = "text"
    source_description: str | None = None


class MergeNodesRequest(BaseModel):
    """Request body for merging two duplicate nodes."""

    keep_uuid: str
    remove_uuid: str


class ConnectNodesRequest(BaseModel):
    """Request body for connecting two nodes with a new edge."""

    source_node_uuid: str
    target_node_uuid: str
    relation: str
    fact: str | None = None


class MergePair(BaseModel):
    """A pair of nodes to merge."""

    keep_uuid: str
    remove_uuid: str


class ReorganizeApplyRequest(BaseModel):
    """Request body to apply selected reorganization actions."""

    orphan_uuids: list[str] = []
    merge_pairs: list[MergePair] = []
