"""Read-only, bounded graph-first projection of verified run claims.

Never infers physical topology or changes assertion status. The claim ledger is authoritative.
BAS software containment is not a mechanical edge. This projection is a presentation only.
"""
from collections import Counter, defaultdict

LIMIT = 64
MAX_LIMIT = 100
MAX_EDGES = 256
MAX_CLAIMS = 100_000
KINDS = {"system", "equipment", "point"}
STATUSES = {"verified", "supported_inference", "needs_review", "contradicted", "rejected"}
RELATIONS = {"hasPoint", "hasPart", "feeds", "connectedTo", "includes", "isPartOf", "isPointOf"}
ACCEPTED = {"verified", "supported_inference"}


def _kind(identifier):
    return identifier.split("/", 1)[0] if "/" in identifier else "other"


def _title(identifier):
    return identifier.split("/", 1)[-1].replace("_", " ")


def project(claims, mode="systems", query="", status="", focus="", offset=0, limit=LIMIT):
    """Return a bounded, deterministic node/edge projection and accurate total counts.

    The default overview contains no inferred edges between unconnected entities.
    A focused equipment view includes its actual point and equipment relationships.
    """
    if not isinstance(claims, list) or len(claims) > MAX_CLAIMS:
        raise ValueError("EXPLORER_LIMIT")
    if mode not in {"systems", "equipment", "points"}:
        raise ValueError("EXPLORER_MODE")
    if status and status not in STATUSES:
        raise ValueError("EXPLORER_STATUS")
    if not isinstance(query, str) or len(query) > 256:
        raise ValueError("EXPLORER_QUERY")
    if not isinstance(focus, str) or len(focus) > 256:
        raise ValueError("EXPLORER_FOCUS")
    if type(offset) is not int or offset < 0 or offset > MAX_CLAIMS:
        raise ValueError("EXPLORER_OFFSET")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("EXPLORER_PAGE")
    typed = defaultdict(list)
    attached = defaultdict(list)
    relations = []
    for c in claims:
        if not isinstance(c, dict) or not {"claim_uid", "subject_id", "predicate", "object_id", "status"} <= c.keys():
            raise ValueError("EXPLORER_CLAIM")
        if not all(isinstance(c[k], str) for k in ("claim_uid", "subject_id", "predicate", "object_id", "status")):
            raise ValueError("EXPLORER_CLAIM")
        if c["predicate"] == "type":
            typed[c["subject_id"]].append(c)
        elif c["predicate"] in RELATIONS:
            relations.append(c)
            attached[c["subject_id"]].append(c)
            attached[c["object_id"]].append(c)
    kinds = {identifier: _kind(identifier) for identifier in typed}
    for c in relations:
        kinds.setdefault(c["subject_id"], _kind(c["subject_id"]))
        kinds.setdefault(c["object_id"], _kind(c["object_id"]))
    def is_system(identifier):
        return kinds.get(identifier) == "system" or (
            kinds.get(identifier) == "equipment"
            and any(t["object_id"].endswith("System") for t in typed[identifier])
        )
    def matching(identifier):
        if query and query.casefold() not in (identifier + " " + " ".join(t["object_id"] for t in typed[identifier])).casefold():
            return False
        if status and not any(t["status"] == status for t in typed[identifier]) and not any(
            c["status"] == status for c in attached[identifier]
        ):
            return False
        return True
    if focus and focus not in kinds:
        raise ValueError("EXPLORER_FOCUS")
    if focus:
        neighbors = {focus}
        for c in attached[focus]:
            if not status or c["status"] == status:
                neighbors.update((c["subject_id"], c["object_id"]))
        if mode == "systems":
            visible = [n for n in neighbors if is_system(n) or kinds[n] == "equipment"]
        elif mode == "equipment":
            visible = [n for n in neighbors if kinds[n] in {"equipment", "system", "point"}]
        else:
            visible = [n for n in neighbors if kinds[n] in KINDS]
    elif mode == "systems":
        systems = [n for n in kinds if is_system(n) and matching(n)]
        # Expand only explicitly documented system relationships. Never attach
        # equipment by a matching name or BAS folder proximity.
        visible = list(systems)
        if systems:
            for system in systems:
                for rel in attached[system]:
                    other = rel["object_id"] if rel["subject_id"] == system else rel["subject_id"]
                    if kinds.get(other) == "equipment" and (not status or rel["status"] == status):
                        visible.append(other)
        else:
            visible = [n for n in kinds if kinds[n] == "equipment" and matching(n)]
    elif mode == "equipment":
        visible = [n for n in kinds if kinds[n] in {"equipment", "system"} and matching(n)]
    else:
        visible = [n for n in kinds if kinds[n] == "point" and matching(n)]
    visible = sorted(set(visible))
    if focus and focus in visible:
        visible.remove(focus)
        visible.insert(0, focus)
    total = len(visible)
    visible = visible[offset:offset+limit]
    ids = set(visible)
    nodes = []
    for ident in visible:
        classes = typed[ident]
        preferred = next((c for c in classes if c["status"] == "verified"), None)
        if preferred is None:
            preferred = next((c for c in classes if c["status"] == "supported_inference"), None)
        if preferred is None and classes:
            preferred = sorted(classes, key=lambda x: x["claim_uid"])[0]
        nodes.append({
            "id": ident, "label": _title(ident), "kind": kinds[ident],
            "class": preferred["object_id"] if preferred else "Unclassified",
            "status": preferred["status"] if preferred else "needs_review",
            "claim_uid": preferred["claim_uid"] if preferred else None,
            "relation_count": len(attached[ident]),
            "is_system": is_system(ident)
        })
    edges = [{
        "id": c["claim_uid"], "source": c["subject_id"], "target": c["object_id"],
        "predicate": c["predicate"], "status": c["status"],
        "directed": c["predicate"] == "feeds",
        "accepted": c["status"] in ACCEPTED
    } for c in sorted(relations, key=lambda x: x["claim_uid"])
        if c["subject_id"] in ids and c["object_id"] in ids
        and (not status or c["status"] == status)]
    truncated_edges = max(0, len(edges) - MAX_EDGES)
    edges = edges[:MAX_EDGES]
    counts = Counter(c["status"] for c in claims)
    return {"mode": mode, "nodes": nodes, "edges": edges, "total": total,
            "truncated_edges": truncated_edges,
            "offset": offset, "next_offset": offset+limit if offset+limit < total else None,
            "stats": {"claims": len(claims), "statuses": dict(counts),
                      "systems": sum(is_system(n) for n in kinds),
                      "equipment": sum(k == "equipment" and not is_system(n) for n, k in kinds.items()),
                      "points": sum(k == "point" for k in kinds.values())},
            "limitations": {
                "graph_is_projection": True,
                "system_fallback": mode == "systems" and not any(is_system(n) for n in kinds),
                "physical_direction_from_feeds_only": True,
                "status_in_graph_not_standalone_ttl": True
            }}
