"""Artifact-graph helpers for persisted runtime outputs."""

from __future__ import annotations


ARTIFACT_GRAPH_SCHEMA_VERSION = 1
ARTIFACT_GRAPH_FORMAT = "yt_transcript.artifact_graph/v1"


def build_artifact_graph(*, run_id: str = "", artifacts=None) -> dict:
    """Build a lightweight artifact graph from artifact references."""
    artifact_refs = artifacts if isinstance(artifacts, list) else []
    nodes = []
    edges = []
    for artifact in artifact_refs:
        if not isinstance(artifact, dict):
            continue
        nodes.append({
            "artifact_id": str(artifact.get("artifact_id", "")).strip(),
            "artifact_type": str(artifact.get("artifact_type", "")).strip(),
            "path": str(artifact.get("path", "")).strip(),
        })
        parents = artifact.get("parent_artifacts", []) if isinstance(artifact.get("parent_artifacts", []), list) else []
        for parent in parents:
            parent_id = str(parent).strip()
            if parent_id:
                edges.append({
                    "from": parent_id,
                    "to": str(artifact.get("artifact_id", "")).strip(),
                })
    return {
        "schema_version": ARTIFACT_GRAPH_SCHEMA_VERSION,
        "format": ARTIFACT_GRAPH_FORMAT,
        "run_id": str(run_id or "").strip(),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }
