"""
actions/recall_graph.py  —  Knowledge Graph recall tool for ROSE
================================================================
Surfaces graph-memory results to the LLM as a tool call.
Follows AGENTS.md: returns a short string, never raises, no Qt touches.
"""

TOOL = {
    "name": "recall_graph",
    "description": (
        "Search the knowledge graph for entities, relationships, devices, projects, "
        "or people. Use this when the user asks about something specific you've learned "
        "about their world — a device, a project name, a person, a place. "
        "More powerful than recall_memory for structured / relational lookups."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Name, keyword or phrase to search for in the graph.",
            },
            "expand": {
                "type": "STRING",
                "description": (
                    "Optional: a node name to expand — returns its direct neighbours "
                    "and relationships instead of doing a text search."
                ),
            },
            "depth": {
                "type": "INTEGER",
                "description": "Hop depth for expand mode (default 1, max 3).",
            },
        },
        "required": [],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        from memory.graph_memory import search, neighbors, stats, format_for_prompt

        query  = str(parameters.get("query") or "").strip()
        expand = str(parameters.get("expand") or "").strip()
        depth  = min(int(parameters.get("depth") or 1), 3)

        # Expand mode: show direct graph neighbourhood
        if expand:
            nbrs = neighbors(expand, depth=depth)
            if not nbrs:
                return f"No graph nodes found matching '{expand}'."
            lines = [f"Graph neighbourhood of '{expand}' (depth={depth}):"]
            for nb in nbrs[:20]:
                props = nb.get("properties") or {}
                val   = props.get("value") or props.get("summary") or ""
                rel   = nb.get("predicate", "related")
                lines.append(f"  {rel} → [{nb['type']}] {nb['name']}" + (f": {val[:80]}" if val else ""))
            return "\n".join(lines)

        # Search mode
        if query:
            from memory.graph_memory import search as gsearch
            nodes = gsearch(query, limit=10)
            if not nodes:
                return f"Nothing in the knowledge graph about '{query}'."
            lines = [f"Knowledge graph results for '{query}':"]
            for n in nodes:
                props = n.get("properties") or {}
                val   = props.get("value") or props.get("summary") or str(props)[:80]
                lines.append(f"  [{n['type']}] {n['name']}: {val[:100]}")
                # Also show first-degree relations
                nbrs = neighbors(n["id"], depth=1, direction="out")
                for nb in nbrs[:3]:
                    lines.append(f"      └─ {nb['predicate']} → {nb['name']}")
            return "\n".join(lines)

        # Empty query: return stats + format_for_prompt summary
        s = stats()
        summary = f"Knowledge graph: {s['nodes']} nodes, {s['edges']} edges."
        by_type = ", ".join(f"{v} {k}" for k, v in s["by_type"].items())
        if by_type:
            summary += f" Types: {by_type}."
        return summary

    except Exception as e:
        return f"Graph recall error: {e}"
