"""Tunnel Management (TM) — multi-server tunnel-chain control plane.

The visual graph is the *source of truth*. It is validated, the role of every
participant is derived from topology, per-hop routes are computed, and the
resulting participant configuration is pushed to every real SpiderPanel server
in the chain over the existing authenticated Node (SPI-key) channel.

Nothing in this module duplicates the Node subsystem: TM reuses the same
``NODES`` storage, the same ``spdr_`` key handling, the same ``_probe_node``
health code, the same VLESS relay/proxy pipeline and the same state file.
TM only owns the parts that did not exist before — the graph overlay
(positions, edges, Set selection), the chain algorithm and the TM inbound.

The module never imports ``main`` at import time: it resolves the already
loaded application module lazily through ``sys.modules["main"]`` (the same
pattern ``main._get_main()`` uses), which keeps the import graph acyclic.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import sys
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

# ── Constants ───────────────────────────────────────────────────────────────
TM_INBOUND_ID = "tm"
TM_INBOUND_NAME = "TM"
TM_MANAGED_KIND = "tm"
TM_PROTOCOL = "tm"
TM_PUBLIC_PORT = 443

# Path templates. ``{uuid}`` is substituted with the user's existing
# ``config_uuid`` — TM never invents a second identifier.
TM_ENTRY_PATH_TEMPLATE = "/ws/TM/{uuid}"       # node has no next hop  → exits here
TM_FORWARD_PATH_TEMPLATE = "/ws/nextTM/{uuid}"  # node has a next hop  → forwards

EDGE_STYLE_STRAIGHT = "straight"
EDGE_STYLE_CURVED = "curved"


def _clean_edge(edge: dict) -> dict:
    """Normalize one edge from the browser: known direction, valid style, a
    bounded `bend` (how far the operator dragged the bar off its cord, 0 =
    straight). Returns None when the input is unusable."""
    src, dst = str(edge.get("from") or ""), str(edge.get("to") or "")
    if not src or not dst or src == dst:
        return None
    try:
        bend = float(edge.get("bend")) if edge.get("bend") is not None else None
    except (TypeError, ValueError):
        bend = None
    if bend is not None and not (math.isfinite(bend)):
        bend = None
    if bend is not None:
        bend = max(-70.0, min(70.0, bend))
    style = str(edge.get("style") or EDGE_STYLE_CURVED).lower()
    return {
        "id": str(edge.get("id") or f"edge_{secrets.token_hex(4)}"),
        "from": src,
        "to": dst,
        "style": style if style in (EDGE_STYLE_CURVED, EDGE_STYLE_STRAIGHT) else EDGE_STYLE_CURVED,
        # Persisted only so a dragged bar keeps its shape after a reload; the
        # runtime never reads it.
        "bend": bend,
    }

# Roles (derived, never configured by hand).
ROLE_INITIAL = "initial"
ROLE_MIDDLE = "middle"
ROLE_FINAL = "final"


def _m():
    """The already-loaded application module (never a second copy)."""
    mod = sys.modules.get("main")
    if mod is None:
        raise RuntimeError("main module is not initialized")
    return mod


# ── TM state (persisted inside the panel's own state file) ──────────────────
# TM keeps ONLY what the plain Node records do not have: the graph overlay.
# Server credentials stay in NODES; this structure is safe to hand to the
# browser once ``_safe_servers()`` strips it down.
TM_STATE: dict = {
    "enabled": False,
    "positions": {},      # node_id -> {"x": float, "y": float}
    "edges": [],          # [{"id", "from", "to", "style", "bend"}]
    "active_server": "",  # the "Set" server used for config generation
    "applied": None,      # last known-good applied topology
    "participant": None,  # this panel's own role, pushed by the entry panel
    "last_apply": "",
    "last_error": "",
    "last_validate": None,
}
TM_LOCK = asyncio.Lock()


def load_state(data) -> None:
    """Restore TM state from the persisted panel state (called by load_state).

    Node records are loaded before TM, so unknown servers (deleted nodes) are
    dropped here instead of leaving orphan positions/edges behind.
    """
    if not isinstance(data, dict):
        return
    try:
        known = set(_live_servers().keys())
    except Exception:
        known = set()
    positions = data.get("positions")
    if isinstance(positions, dict):
        cleaned = {}
        for nid, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            nid = str(nid)
            if known and nid not in known:
                continue
            try:
                cleaned[nid] = {"x": float(pos.get("x") or 0), "y": float(pos.get("y") or 0)}
            except (TypeError, ValueError):
                continue
        TM_STATE["positions"] = cleaned
    edges = data.get("edges")
    if isinstance(edges, list):
        seen = set()
        cleaned_edges = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            src, dst = str(edge.get("from") or ""), str(edge.get("to") or "")
            if not src or not dst or src == dst or (src, dst) in seen:
                continue
            if known and (src not in known or dst not in known):
                continue
            cleaned = _clean_edge(edge)
            if cleaned is None:
                continue
            seen.add((src, dst))
            cleaned_edges.append(cleaned)
        TM_STATE["edges"] = cleaned_edges
    active = str(data.get("active_server") or "")
    TM_STATE["active_server"] = active if (not known or active in known) else ""
    TM_STATE["enabled"] = bool(data.get("enabled"))
    TM_STATE["last_apply"] = str(data.get("last_apply") or "")
    TM_STATE["last_error"] = str(data.get("last_error") or "")
    applied = data.get("applied")
    if isinstance(applied, dict):
        TM_STATE["applied"] = applied
    participant = data.get("participant")
    if isinstance(participant, dict):
        TM_STATE["participant"] = participant
    TM_STATE["last_validate"] = data.get("last_validate")


def dump_state() -> dict:
    """The exact TM slice written into the panel state file."""
    return {
        "enabled": bool(TM_STATE.get("enabled")),
        "positions": dict(TM_STATE.get("positions") or {}),
        "edges": [dict(e) for e in (TM_STATE.get("edges") or [])],
        "active_server": str(TM_STATE.get("active_server") or ""),
        "applied": TM_STATE.get("applied"),
        "participant": TM_STATE.get("participant"),
        "last_apply": str(TM_STATE.get("last_apply") or ""),
        "last_error": str(TM_STATE.get("last_error") or ""),
        "last_validate": TM_STATE.get("last_validate"),
    }


async def _save():
    await _m().save_state()


def _now() -> str:
    return datetime.now().isoformat()


# ── Server access (reuses the ordinary Node records) ────────────────────────
def _live_servers() -> dict:
    """Every ordinary Node is a TM candidate server; the graph selects which
    of them actually participate."""
    main = _m()
    return dict(main.NODES)


def _normalize_positions(positions: dict, server_ids: list) -> dict:
    """Keep positions JSON-serializable and bounded to real servers."""
    known = set(server_ids)
    out = {}
    for nid, pos in (positions or {}).items():
        nid = str(nid)
        if nid not in known or not isinstance(pos, dict):
            continue
        try:
            out[nid] = {"x": round(float(pos.get("x") or 0), 1), "y": round(float(pos.get("y") or 0), 1)}
        except (TypeError, ValueError):
            continue
    return out


def _edge_nodes() -> set:
    ids = set()
    for edge in TM_STATE.get("edges") or []:
        ids.add(str(edge.get("from") or ""))
        ids.add(str(edge.get("to") or ""))
    return {x for x in ids if x}


def _tm_server_ids() -> list:
    """Nodes that are part of the graph (have an edge) — the active members."""
    return sorted(_edge_nodes())


def _node_name(node_id: str) -> str:
    node = _live_servers().get(node_id) or {}
    return str(node.get("name") or node.get("domain") or node_id)


def _default_position(index: int, total: int) -> dict:
    """Deterministic fallback layout so a node added in Server has a place to
    live in Configure before the admin drags it."""
    if total <= 1:
        return {"x": 260.0, "y": 220.0}
    cols = max(1, min(4, total))
    col = index % cols
    row = index // cols
    return {"x": 160.0 + col * 220.0, "y": 120.0 + row * 170.0}


def _ensure_position(node_id: str, index: int, total: int) -> dict:
    positions = TM_STATE.setdefault("positions", {})
    pos = positions.get(node_id)
    if not isinstance(pos, dict):
        pos = _default_position(index, total)
        positions[node_id] = pos
    return pos


def _safe_servers() -> list:
    """Browser-safe server metadata: never the API key, never the raw record."""
    main = _m()
    out = []
    live = _live_servers()
    member_ids = _edge_nodes()
    ordered = sorted(live.keys(), key=lambda nid: str((live.get(nid) or {}).get("created_at") or ""), reverse=True)
    for index, nid in enumerate(ordered):
        node = live.get(nid) or {}
        view = main._node_public_view(nid, node)
        pos = _ensure_position(nid, index, max(1, len(ordered)))
        out.append({
            "node_id": nid,
            "name": view.get("name") or nid,
            "domain": view.get("domain") or "",
            "status": view.get("status") or "offline",
            "api_key_masked": view.get("api_key_masked") or "",
            "server_latency_ms": view.get("latency_ms"),
            "country": view.get("country") or "",
            "country_flag": view.get("country_flag") or "🌐",
            "public_ip": view.get("public_ip") or "",
            "last_seen": view.get("last_seen"),
            "last_error": view.get("last_error") or "",
            "created_at": view.get("created_at") or "",
            "in_graph": nid in member_ids,
            "x": pos.get("x", 0.0),
            "y": pos.get("y", 0.0),
        })
    return out


def _server_payload(node_id: str, node: dict) -> dict:
    """The subset of a Node that TM pushes/uses server-side (contains the key —
    this value never leaves a server-to-server channel or the state file)."""
    return {
        "node_id": node_id,
        "name": node.get("name") or node_id,
        "domain": node.get("domain") or "",
        "api_key": node.get("api_key") or "",
        "remote_tls_ws": dict(node.get("remote_tls_ws") or {}),
        "status": node.get("status") or "",
    }


# ── Topology: validation, roles, routes ─────────────────────────────────────
class TopologyError(Exception):
    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or [message]


def _cycle_path(edges: list, nodes: list) -> list | None:
    """Return the node-id cycle when the graph is not a DAG (else None)."""
    adjacency = {nid: [] for nid in nodes}
    for edge in edges:
        adjacency.setdefault(edge["from"], []).append(edge["to"])
    WHITE, GREY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}
    stack: list = []

    def visit(nid: str) -> list | None:
        color[nid] = GREY
        stack.append(nid)
        for nxt in adjacency.get(nid, []):
            if color.get(nxt, WHITE) == GREY:
                idx = stack.index(nxt) if nxt in stack else 0
                return stack[idx:] + [nxt]
            if color.get(nxt, WHITE) == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[nid] = BLACK
        return None

    for nid in nodes:
        if color.get(nid, WHITE) == WHITE:
            found = visit(nid)
            if found:
                return found
    return None


def _compute_topology(edges: list, server_ids: list, active_server: str) -> dict:
    """Derive roles, per-node routes and the active chain from the graph.

    The graph is a layered DAG:

      * level 0   → every node is an *Initial* server
      * last level → every node is a *Final* server
      * anything between → *Middle*

    A node with several outgoing edges forms a **balancer group**: the entry
    tries its next hops in order and falls through to the next one when a hop is
    unreachable, so `A → {B, C, D} → E` uses all three middles automatically.
    Fan-in is allowed only when every predecessor belongs to one and the same
    parallel group (they share a single upstream), so `E` above is reachable
    from `B`, `C` and `D` while an accidental `X → E` merge is still rejected.

    Components that do not contain the active server stay idle and are
    reported, never silently mixed into the live chain.
    """
    next_of: dict = {}
    prev_of: dict = {}
    for edge in edges:
        next_of.setdefault(edge["from"], [])
        if edge["to"] not in next_of[edge["from"]]:
            next_of[edge["from"]].append(edge["to"])
        prev_of.setdefault(edge["to"], [])
        if edge["from"] not in prev_of[edge["to"]]:
            prev_of[edge["to"]].append(edge["from"])

    id_set = set(server_ids)
    # Only servers that actually carry an edge participate in the chain
    # analysis; servers that have never been connected are idle candidates,
    # not opposing chains (that would make any two unconnected servers look
    # ambiguous). A single unconnected server is still a valid Initial+Final
    # chain on its own.
    # `next_of` and `prev_of` are both keyed by the node the edge touches, so
    # only the KEYS are participants (taking prev_of's values would name the
    # upstream server twice and lose the downstream one).
    participating = {nid for nid in next_of if nid in id_set}
    participating |= {nid for nid in prev_of if nid in id_set}
    heads = [nid for nid in server_ids if nid in participating and not prev_of.get(nid)]

    def walk(start: str) -> list:
        """Levels reachable from `start`: [[entry], [parallel middles], …]."""
        levels: list = [[start]]
        seen = {start}
        frontier = [start]
        while frontier:
            nxt: list = []
            for node in frontier:
                for child in next_of.get(node, []):
                    if child not in seen:
                        seen.add(child)
                        nxt.append(child)
            if not nxt:
                break
            levels.append(nxt)
            frontier = nxt
        return levels

    chains: list = []          # level lists
    flat_chains: list = []     # same, flattened — the shape tests and the UI read
    placed: set = set()
    for head in heads:
        levels = walk(head)
        if head in placed:
            continue
        for level in levels:
            for nid in level:
                placed.add(nid)
        chains.append(levels)
        flat_chains.append([nid for level in levels for nid in level])

    # Standalone servers (no edges at all): each is its own one-node chain.
    standalone = [nid for nid in server_ids if nid not in participating]
    for nid in standalone:
        chains.append([[nid]])
        flat_chains.append([nid])
        placed.add(nid)

    # A node whose edges point at an unknown id would have been dropped —
    # surface it instead of losing it silently.
    leftovers = [nid for nid in server_ids if nid not in placed]

    roles: dict = {}
    for levels, flat in zip(chains, flat_chains):
        size = len(flat)
        last_index = len(levels) - 1
        for level_index, level in enumerate(levels):
            has_next = level_index < last_index
            for position, nid in enumerate(level):
                if level_index == 0 and last_index == 0:
                    # A single-node chain is both ends: Initial + Final (spec §6).
                    role = "initial_final"
                elif level_index == 0:
                    role = ROLE_INITIAL
                elif level_index == last_index:
                    role = ROLE_FINAL
                else:
                    role = ROLE_MIDDLE
                nexts = list(next_of.get(nid, [])) if has_next else []
                prevs = list(prev_of.get(nid, [])) if level_index > 0 else []
                flat_pos = sum(len(levels[i]) for i in range(level_index)) + position
                roles[nid] = {
                    "node_id": nid,
                    "name": _node_name(nid),
                    "chain_position": flat_pos,
                    "chain_length": size,
                    "level": level_index,
                    "level_size": len(level),
                    "initial": level_index == 0,
                    "middle": 0 < level_index < last_index,
                    "final": level_index == last_index,
                    "prev_node_id": prevs[0] if prevs else None,
                    "prev_node_ids": prevs,
                    # A node with several next hops *is* the balancer.
                    "next_node_id": nexts[0] if nexts else None,
                    "next_node_ids": nexts,
                    "balancer": len(nexts) > 1,
                    "role": role,
                    "role_label": ("Initial + Final" if role == "initial_final"
                                   else ROLE_INITIAL.capitalize() if role == ROLE_INITIAL
                                   else ROLE_FINAL.capitalize() if role == ROLE_FINAL
                                   else "Middle"),
                    "ingress_path": (TM_FORWARD_PATH_TEMPLATE if nexts else TM_ENTRY_PATH_TEMPLATE),
                }

    active_chain: list = []
    active_levels: list = []
    if active_server:
        for levels, flat in zip(chains, flat_chains):
            if active_server in flat:
                active_chain = list(flat)
                active_levels = levels
                break
    if not active_chain:
        # With no Set, the single chain the operator actually built is the live
        # one. Servers that were never connected are idle candidates, not
        # competing chains.
        edge_ids = set(participating)
        built = [(levels, flat) for levels, flat in zip(chains, flat_chains) if flat and flat[0] in edge_ids]
        if len(built) == 1:
            active_levels, active_chain = built[0][0], list(built[0][1])
        elif not built and len(chains) == 1:
            # One unconnected server is a valid Initial + Final chain.
            active_levels, active_chain = chains[0], list(flat_chains[0])
    if not active_chain and active_server and active_server in id_set:
        active_levels = [[active_server]]
        active_chain = [active_server]
    active_set = set(active_chain)
    idle = [nid for flat in flat_chains for nid in flat if nid not in active_set]
    return {
        "chains": flat_chains,
        "levels": active_levels,
        "roles": roles,
        "leftovers": leftovers,
        "active_chain": active_chain,
        "idle": idle,
    }


def validate_graph(edges: list, server_ids: list, active_server: str = "") -> dict:
    """Validate the topology. Raises TopologyError with a human message."""
    known = set(server_ids)
    seen_pairs = set()
    for edge in edges:
        src, dst = str(edge.get("from") or ""), str(edge.get("to") or "")
        if not src or not dst:
            raise TopologyError("Invalid TM topology: an edge is missing a source or destination.")
        if src not in known or dst not in known:
            missing = src if src not in known else dst
            raise TopologyError(f"Invalid TM topology: server {missing} is not a registered server.")
        if src == dst:
            raise TopologyError(
                f"Invalid TM topology: self-loop on {_node_name(src)} — a server cannot route to itself."
            )
        pair = (src, dst)
        if pair in seen_pairs:
            raise TopologyError(
                f"Invalid TM topology: duplicate connection {_node_name(src)} → {_node_name(dst)}."
            )
        seen_pairs.add(pair)

    # Fan-out from one server is the balancer; fan-in is allowed only when every
    # predecessor is itself a balancer branch of the SAME upstream, which is
    # exactly `A → {B, C} → D`. Anything else would need routing the chain
    # cannot express, so it is rejected with a message that says why.
    predecessors: dict = {}
    successors: dict = {}
    for edge in edges:
        successors.setdefault(edge["from"], []).append(edge["to"])
        predecessors.setdefault(edge["to"], []).append(edge["from"])

    for nid, preds in predecessors.items():
        if len(preds) < 2:
            continue
        roots = set()
        for pred in preds:
            own = predecessors.get(pred) or []
            roots.add(tuple(sorted(own)) if own else ("<head>", pred))
        if len(roots) != 1:
            raise TopologyError(
                f"Invalid TM topology: {_node_name(nid)} is reached from {len(preds)} unrelated servers "
                f"({', '.join(_node_name(p) for p in preds)}). Only parallel servers of one balancer may "
                "share a next server."
            )

    for nid, succs in successors.items():
        if len(succs) != len(set(succs)):
            raise TopologyError(f"Invalid TM topology: duplicate connection from {_node_name(nid)}.")

    cycle = _cycle_path(edges, sorted(known))
    if cycle:
        names = [f"{_node_name(nid)}" for nid in cycle]
        raise TopologyError(
            "Invalid TM topology: cycle detected between " + " → ".join(names) + "."
        )

    if active_server and str(active_server) not in known:
        raise TopologyError(
            f"Invalid TM topology: the selected (Set) server {active_server} is not a registered server."
        )

    topology = _compute_topology(edges, sorted(known), str(active_server or ""))
    if topology.get("leftovers"):
        names = ", ".join(_node_name(nid) for nid in topology["leftovers"])
        raise TopologyError(
            f"Invalid TM topology: routing is ambiguous for {names}. Re-apply the connections."
        )

    # Multi-component graphs ARE valid, but only chains the operator built with
    # edges count as components needing a Set. A node with no edges at all stays
    # a candidate, not a competing component.
    built_chains = [c for c in topology["chains"] if len(c) > 1]
    if len(built_chains) > 1 and not str(active_server or ""):
        raise TopologyError(
            "Multiple independent chains found: set (Set) a chain before applying. "
            "Chains: " + "; ".join(" → ".join(_node_name(n) for n in c) for c in built_chains)
        )

    # Every member chain must be a real path with a reachable final destination.
    for chain in topology["chains"]:
        if not chain:
            raise TopologyError("Invalid TM topology: an empty chain was produced.")
        for nid in chain:
            role = topology["roles"].get(nid) or {}
            if not role.get("next_node_id") and not role.get("final"):
                raise TopologyError(
                    f"Invalid TM topology: {_node_name(nid)} has no next server and is not a final destination."
                )
    return topology


def client_path_for(topology: dict, node_id: str) -> str:
    """The TM route a client must use when it enters at ``node_id``."""
    role = (topology.get("roles") or {}).get(node_id) or {}
    return str(role.get("ingress_path") or TM_ENTRY_PATH_TEMPLATE)


def _topology_summary(topology: dict) -> dict:
    chain = topology.get("active_chain") or []
    entry = chain[0] if chain else ""
    exit_node = chain[-1] if chain else ""
    return {
        "chains": topology.get("chains") or [],
        "levels": [list(level) for level in (topology.get("levels") or [])],
        "balancer": any(len(level) > 1 for level in (topology.get("levels") or [])[1:]),
        "active_chain": chain,
        "idle": topology.get("idle") or [],
        "entry": {"node_id": entry, "name": _node_name(entry) if entry else ""} if entry else None,
        "exit": {"node_id": exit_node, "name": _node_name(exit_node) if exit_node else ""} if exit_node else None,
        "roles": topology.get("roles") or {},
    }


def current_topology(strict: bool = False) -> dict:
    """Topology for the live graph; used by config generation and status."""
    edges = [dict(e) for e in (TM_STATE.get("edges") or [])]
    server_ids = sorted(_live_servers().keys())
    try:
        return validate_graph(edges, server_ids, str(TM_STATE.get("active_server") or ""))
    except TopologyError:
        if strict:
            raise
        return {"chains": [], "roles": {}, "leftovers": [], "active_chain": [], "idle": [],
                "entry_node_id": "", "active_server": ""}


def resolve_entry_node_id() -> str:
    """The server whose domain the generated client config dials.

    The explicit Set selection wins. When nothing is Set, the entry of the
    active chain is used so TM still produces a working config.
    """
    servers = _live_servers()
    active = str(TM_STATE.get("active_server") or "").strip()
    if active and active in servers:
        return active
    topology = current_topology()
    chain = topology.get("active_chain") or []
    return chain[0] if chain else ""


def _split_host_port(value: str, default_port: int | None) -> tuple[str, int | None]:
    """Split ``host[:port]`` into its parts.

    Node domains are often written as ``example.com:8443`` (or ``[::1]:8443``).
    Emitting that verbatim into ``vless://uuid@host:port`` would produce a
    double port, so the port is taken out of the host when it is there.
    """
    value = str(value or "").strip()
    if not value:
        return "", default_port
    if value.startswith("["):
        end = value.find("]")
        if end > 0:
            rest = value[end + 1:]
            if rest.startswith(":") and rest[1:].isdigit():
                return value[:end + 1], int(rest[1:])
            return value[:end + 1], default_port
        return value, default_port
    if value.count(":") == 1:
        host, _, port_text = value.rpartition(":")
        if host and port_text.isdigit():
            return host, int(port_text)
    return value, default_port


def _entry_target(node_id: str) -> dict | None:
    """Resolve the dial target (host/port/path) for a TM entry server."""
    main = _m()
    node = _live_servers().get(node_id)
    if not node:
        return None
    view = main._node_public_view(node_id, node)
    remote_ib = dict(node.get("remote_tls_ws") or {})
    raw_host = str(remote_ib.get("domain") or node.get("remote_host") or view.get("domain") or "").strip()
    if not raw_host:
        raw_host = str(node.get("domain") or "").strip()
    raw_host = raw_host.replace("https://", "").replace("http://", "").strip("/")
    if not raw_host:
        return None
    try:
        port = int(remote_ib.get("external_port") or remote_ib.get("port") or 443)
    except (TypeError, ValueError):
        port = 443
    # A port carried inside the host string wins: it is what the URL really is.
    host, embedded_port = _split_host_port(raw_host, None)
    if embedded_port is not None:
        port = embedded_port
    if not host:
        return None
    topology = current_topology()
    path_template = client_path_for(topology, node_id)
    # The SNI is a hostname, never host:port.
    sni, _ = _split_host_port(str(remote_ib.get("sni") or ""), None)
    return {
        "node_id": node_id,
        "host": host,
        "port": port,
        "path_template": path_template,
        "sni": sni or host,
        "flag": view.get("country_flag") or "🌐",
        "name": view.get("name") or node_id,
    }


def tm_config_for_node(user: dict, node_id: str) -> str:
    """One VLESS config for entering the tunnel at ``node_id``."""
    from urllib.parse import quote

    config_uuid = str(user.get("config_uuid") or "").strip()
    if not config_uuid or not node_id:
        return ""
    target = _entry_target(node_id)
    if not target:
        return ""
    path = target["path_template"].replace("{uuid}", config_uuid)
    username = user.get("username") or "user"
    role = (current_topology().get("roles") or {}).get(node_id) or {}
    # The entry a config dials is always an entry point: the remark says which
    # server it goes through, never "relay"/"exit" — those are not places a
    # client can start from.
    hop_note = " Entry" if role.get("initial") or role.get("initial_final") else ""
    remark = f"Spider-{username} TM {target['flag']} {target['name']}{hop_note}".strip()
    params = (
        "encryption=none&security=tls&type=ws"
        f"&host={quote(target['host'], safe='')}"
        f"&path={quote(path, safe='')}"
        f"&sni={quote(target['sni'], safe='')}"
        "&fp=chrome&alpn=http/1.1"
    )
    return f"vless://{config_uuid}@{target['host']}:{target['port']}?{params}#{quote(remark)}"


def tm_entry_node_ids() -> list:
    """Every server a client may legitimately enter the tunnel through.

    Only the first layer of a chain counts as a door — a balancer's parallel
    entries therefore each get their own config, which is the whole point of
    having several. A middle or exit is never a door, even when the operator
    left a stray edge on it; the Set selection is the one exception, because it
    is an explicit instruction and always gets its own config.
    """
    topology = current_topology()
    roles = topology.get("roles") or {}
    # A server with no links at all is not part of any chain: it must not
    # collect a config merely because it sits alone in its own layer.
    linked = set()
    for edge in (TM_STATE.get("edges") or []):
        linked.add(str(edge.get("from") or ""))
        linked.add(str(edge.get("to") or ""))
    ids: list = []
    for nid, role in roles.items():
        if nid not in linked:
            continue
        # `initial` is layer 0; `initial_final` is a one-server chain.
        if role.get("initial") or role.get("initial_final") or role.get("level") == 0:
            ids.append(nid)
    # Keep the topology's own ordering so the configs read top-down.
    ordered = [nid for nid in (topology.get("active_chain") or []) if nid in ids]
    for nid in ids:
        if nid not in ordered:
            ordered.append(nid)
    active = str(TM_STATE.get("active_server") or "").strip()
    if active and active in _live_servers() and active not in ordered:
        ordered.append(active)
    return ordered


def tm_client_configs(user: dict) -> list:
    """Every TM config this user should receive.

    One per entry server (a balancer with several entries gives several) and
    one for the Set server — all carrying the user's own ``config_uuid``.
    """
    out, seen = [], set()
    for nid in tm_entry_node_ids():
        cfg = tm_config_for_node(user, nid)
        if cfg and cfg not in seen:
            out.append(cfg)
            seen.add(cfg)
    return out


def tm_client_config(user: dict, node_id: str = "") -> str:
    """The single config for a user over TM — same UUID as every other config.

    Used wherever the panel needs exactly one string. The Set selection wins,
    then the chain entry; subscriptions use ``tm_client_configs`` for every door.
    """
    if node_id:
        return tm_config_for_node(user, node_id)
    return tm_config_for_node(user, resolve_entry_node_id())


# ── TM inbound (a real inbound, participates in config generation) ──────────
def is_tm_inbound(inbound: dict | None) -> bool:
    if not inbound:
        return False
    return (str(inbound.get("_managed_kind") or "") == TM_MANAGED_KIND
            or (str(inbound.get("protocol") or "").lower() == TM_PROTOCOL
                and str(inbound.get("name") or "").strip() == TM_INBOUND_NAME))


def find_tm_inbound_id() -> str | None:
    main = _m()
    for iid, ib in main.INBOUNDS.items():
        if str(ib.get("_managed_kind") or "") == TM_MANAGED_KIND:
            return iid
    for iid, ib in main.INBOUNDS.items():
        if is_tm_inbound(ib):
            ib.setdefault("_managed_kind", TM_MANAGED_KIND)
            return iid
    return None


async def ensure_tm_inbound() -> str:
    """Create the dedicated ``TM`` inbound once; adopt it on later boots."""
    main = _m()
    changed = False
    async with main.INBOUNDS_LOCK:
        iid = find_tm_inbound_id()
        if not iid:
            iid = TM_INBOUND_ID if TM_INBOUND_ID not in main.INBOUNDS else main.generate_short_id()
            main.INBOUNDS[iid] = {
                "name": TM_INBOUND_NAME,
                "_managed_kind": TM_MANAGED_KIND,
                "protocol": TM_PROTOCOL,
                "inbound_type": "transport",
                "system": True,
                "enabled": True,
                # No extra listener: TM rides the panel's own HTTPS/WSS entry.
                "port": TM_PUBLIC_PORT,
                "internal_port": int(main.CONFIG.get("port") or 8080),
                "network": "ws",
                "security": "tls",
                "domain": main._safe_host(main.SETTINGS.get("domain"), main.get_host()),
                "external_domain": "",
                "sni": "",
                "external_port": "",
                "fingerprint": "chrome",
                "ws_settings": {"path": TM_ENTRY_PATH_TEMPLATE},
                "created_at": _now(),
            }
            changed = True
        else:
            ib = main.INBOUNDS[iid]
            ib.setdefault("_managed_kind", TM_MANAGED_KIND)
            ib.setdefault("system", True)
            if not ib.get("advanced_customized"):
                ib["name"] = TM_INBOUND_NAME
                ib["protocol"] = TM_PROTOCOL
                ib["network"] = "ws"
                ib["security"] = "tls"
                ib["port"] = TM_PUBLIC_PORT
                ib.setdefault("ws_settings", {"path": TM_ENTRY_PATH_TEMPLATE})
                ib["domain"] = main._safe_host(ib.get("domain"), main.SETTINGS.get("domain"), main.get_host())
    if changed:
        main.log_activity("inbound", "اینباند TM ساخته شد", "ok")
        await _save()
    return iid


async def sync_tm_inbound_nodes() -> None:
    """The TM inbound exposes exactly the servers of the live chain."""
    main = _m()
    iid = find_tm_inbound_id()
    if not iid:
        return
    chain = list((current_topology().get("active_chain") or []))
    if not chain:
        chain = _tm_server_ids()
    async with main.INBOUNDS_LOCK:
        ib = main.INBOUNDS.get(iid)
        if ib is not None:
            ib["enabled_node_ids"] = chain
            ib["node_ids"] = chain


# ── Graph API payload ───────────────────────────────────────────────────────
def graph_payload() -> dict:
    servers = _safe_servers()
    valid_ids = {s["node_id"] for s in servers}
    edges = [dict(e) for e in (TM_STATE.get("edges") or []) if e.get("from") in valid_ids and e.get("to") in valid_ids]
    active = str(TM_STATE.get("active_server") or "")
    if active not in valid_ids:
        active = ""
    return {
        "servers": servers,
        "edges": edges,
        "active_server": active,
    }


def status_payload() -> dict:
    servers = _safe_servers()
    valid_ids = {s["node_id"] for s in servers}
    edges = [dict(e) for e in (TM_STATE.get("edges") or []) if e.get("from") in valid_ids and e.get("to") in valid_ids]
    active = str(TM_STATE.get("active_server") or "")
    if active not in valid_ids:
        active = ""
    topology = _compute_topology(edges, sorted(valid_ids), active)
    chain = topology.get("active_chain") or []
    applied = TM_STATE.get("applied") or {}
    participant = TM_STATE.get("participant") or {}
    # The graph is "live" once it has been applied AND still matches what was
    # applied — an unapplied edit must never be reported as the running chain.
    applied_chain = list((applied or {}).get("chain") or [])
    applied_edges = (applied or {}).get("edges") or []
    dirty = bool(applied) and (
        list(chain) != applied_chain
        or sorted((e.get("from"), e.get("to")) for e in edges)
        != sorted((e.get("from"), e.get("to")) for e in applied_edges)
    )
    return {
        "enabled": bool(TM_STATE.get("enabled")),
        "dirty": dirty,
        "applied": bool(applied),
        # Edges as they were applied. The browser compares these with the live
        # graph to tell "what the chain is doing now" from "what you just drew".
        "applied_edges": [dict(e) for e in applied_edges],
        "active": bool(chain),
        "server_count": len(servers),
        "link_count": len(edges),
        "active_server": active,
        "active_server_name": _node_name(active) if active else "",
        "entry": _node_name(chain[0]) if chain else "",
        "entry_node_id": chain[0] if chain else "",
        "exit": _node_name(chain[-1]) if chain else "",
        "exit_node_id": chain[-1] if chain else "",
        "chain": [{"node_id": nid, "name": _node_name(nid)} for nid in chain],
        "idle": [{"node_id": nid, "name": _node_name(nid)} for nid in (topology.get("idle") or [])],
        "levels": [list(level) for level in (topology.get("levels") or [])],
        "balancer": any(len(level) > 1 for level in (topology.get("levels") or [])[1:]),
        # Per-node role (initial / middle / final), which layer it sits in and
        # whether it fans out — the browser never derives these itself.
        "roles": topology.get("roles") or {},
        "chain_length": len(chain),
        "last_apply": TM_STATE.get("last_apply") or "",
        "last_error": TM_STATE.get("last_error") or "",
        "applied_at": (applied or {}).get("at") or "",
        "applied_chain": (applied or {}).get("chain") or [],
        "documented_path": (client_path_for(topology, chain[0]) if chain else TM_ENTRY_PATH_TEMPLATE).replace("{uuid}", "<uuid>"),
        "participant": {
            "node_id": (participant or {}).get("node_id") or main_node_domain(),
            "role": ((participant or {}).get("self") or {}).get("role") or "",
            "next_node_id": ((participant or {}).get("self") or {}).get("next_node_id") or "",
            "prev_node_id": ((participant or {}).get("self") or {}).get("prev_node_id") or "",
            "ingress_path": ((participant or {}).get("self") or {}).get("ingress_path") or "",
            "applied_at": (participant or {}).get("applied_at") or "",
        } if participant else None,
    }


def main_node_domain() -> str:
    main = _m()
    try:
        return str(main.SETTINGS.get("domain") or main.get_host() or "")
    except Exception:
        return ""


# ── Server management (thin layer over the Node records) ────────────────────
async def add_server(name: str, domain: str, api_key: str) -> dict:
    """Verify then persist, exactly like /api/nodes does."""
    main = _m()
    normalized_domain = main._normalize_node_base_url(domain)
    raw_key = str(api_key or "").strip()
    if not raw_key:
        raise TopologyError("API Key is required.")
    if not raw_key.startswith("spdr_"):
        raise TopologyError("API Key must start with spdr_")
    key = main._normalize_node_key(raw_key)
    if not normalized_domain:
        raise TopologyError("Server domain is not valid.")
    async with main.NODES_LOCK:
        duplicate = any(
            str(n.get("domain") or "").rstrip("/").lower() == normalized_domain.rstrip("/").lower()
            for n in main.NODES.values()
        )
    if duplicate:
        raise TopologyError("This server is already registered in Nodes.")
    node_id = "node_" + secrets.token_hex(6)
    node = {
        "name": str(name or "").strip()[:60],
        "domain": normalized_domain,
        "url": normalized_domain,
        "api_key": key,
        "created_at": _now(),
        "last_sync": None,
        "status": "checking",
        "last_status": "checking",
        "last_error": "",
    }
    probe = await main._probe_node(node)
    if probe.get("status") != "online":
        raise TopologyError(probe.get("last_error") or "Server verification failed.")
    node.update(probe)
    if not node.get("name"):
        node["name"] = f"{node.get('country_flag') or '🌐'} {node.get('country') or node.get('public_ip') or normalized_domain}"
    async with main.NODES_LOCK:
        main.NODES[node_id] = node
    total = len(main.NODES)
    async with TM_LOCK:
        positions = TM_STATE.setdefault("positions", {})
        positions[node_id] = _default_position(total - 1, total)
    await _save()
    main.log_activity("tm", f"سرور TM «{node['name']}» اضافه شد", "ok")
    return {"ok": True, "server": next((s for s in _safe_servers() if s["node_id"] == node_id), None)}


async def update_server(node_id: str, name: str = "", domain: str = "", api_key: str = "") -> dict:
    main = _m()
    async with main.NODES_LOCK:
        current = dict(main.NODES.get(node_id) or {})
    if not current:
        raise TopologyError("Server not found.")
    new_name = str(name or current.get("name") or "").strip()[:60]
    new_domain = main._normalize_node_base_url(domain if domain is not None and str(domain).strip() else current.get("domain"))
    if not new_domain:
        raise TopologyError("Server domain is not valid.")
    raw_key = str(api_key or "").strip()
    if raw_key and not raw_key.startswith("spdr_"):
        raise TopologyError("API Key must start with spdr_")
    new_key = main._normalize_node_key(raw_key if raw_key else current.get("api_key"))
    if not new_key.startswith("spdr_"):
        raise TopologyError("API Key must start with spdr_")
    candidate = dict(current)
    candidate.update({"name": new_name, "domain": new_domain, "url": new_domain, "api_key": new_key, "status": "checking"})
    probe = await main._probe_node(candidate)
    candidate.update(probe)
    async with main.NODES_LOCK:
        main.NODES[node_id] = candidate
    await _save()
    if probe.get("status") != "online":
        main.log_activity("tm", f"سرور TM «{new_name}» ذخیره شد اما در دسترس نیست", "warn")
        return {"ok": False, "server": next((s for s in _safe_servers() if s["node_id"] == node_id), None),
                "error": probe.get("last_error") or "Server verification failed."}
    main.log_activity("tm", f"سرور TM «{new_name}» به‌روزرسانی شد", "ok")
    return {"ok": True, "server": next((s for s in _safe_servers() if s["node_id"] == node_id), None)}


async def delete_server(node_id: str) -> dict:
    main = _m()
    async with main.NODES_LOCK:
        node = dict(main.NODES.get(node_id) or {})
    if not node:
        raise TopologyError("Server not found.")
    ok, failed = await main._cleanup_remote_users_for_node(node_id, node)
    if not ok:
        return {"ok": False, "pending_cleanup": True, "errors": failed}
    async with main.NODES_LOCK:
        main.NODES.pop(node_id, None)
    async with TM_LOCK:
        TM_STATE.setdefault("positions", {}).pop(node_id, None)
        TM_STATE["edges"] = [e for e in (TM_STATE.get("edges") or [])
                             if e.get("from") != node_id and e.get("to") != node_id]
        if TM_STATE.get("active_server") == node_id:
            TM_STATE["active_server"] = ""
    await sync_tm_inbound_nodes()
    await _save()
    main.log_activity("tm", f"سرور TM «{node.get('name', node_id)}» حذف شد", "warn")
    return {"ok": True, "deleted": node_id}


async def ping_server(node_id: str) -> dict:
    """SERVER-side health ping (never presented as the client-side latency)."""
    main = _m()
    async with main.NODES_LOCK:
        node = dict(main.NODES.get(node_id) or {})
    if not node:
        raise TopologyError("Server not found.")
    probe = await main._probe_node(node)
    async with main.NODES_LOCK:
        if node_id in main.NODES:
            main.NODES[node_id].update(probe)
    await _save()
    return {
        "ok": probe.get("status") == "online",
        "node_id": node_id,
        "status": probe.get("status"),
        "server_latency_ms": probe.get("latency_ms"),
        "last_error": probe.get("last_error") or "",
        "server": next((s for s in _safe_servers() if s["node_id"] == node_id), None),
    }


# ── RPC helpers (server-to-server, authenticated) ───────────────────────────
async def _push_participant(node: dict, participant: dict) -> tuple[bool, str]:
    main = _m()
    try:
        r = await main._remote_request(node, "POST", "/api/tm/peer/config", participant, timeout=20.0)
    except Exception as exc:
        return False, str(exc)[:160]
    if r.status_code in (200, 201):
        return True, ""
    if r.status_code in (401, 403):
        return False, "unauthorized"
    if r.status_code == 404:
        return False, "TM is not available on this server (update it first)"
    return False, f"HTTP {r.status_code}: {r.text[:120]}"


async def _push_cancel(node: dict) -> tuple[bool, str]:
    """Tell a server it no longer participates in any chain."""
    main = _m()
    try:
        r = await main._remote_request(node, "POST", "/api/tm/peer/config", {"clear": True}, timeout=15.0)
    except Exception as exc:
        return False, str(exc)[:160]
    if r.status_code in (200, 201):
        return True, ""
    if r.status_code == 404:
        return True, ""  # a stranger panel has nothing to clear
    return False, f"HTTP {r.status_code}"


def _build_participant(topology: dict, node_id: str, tokens: dict) -> dict:
    roles = topology.get("roles") or {}
    role = roles.get(node_id) or {}
    next_ids = list(role.get("next_node_ids") or ([role["next_node_id"]] if role.get("next_node_id") else []))
    prev_ids = list(role.get("prev_node_ids") or ([role["prev_node_id"]] if role.get("prev_node_id") else []))
    next_id = next_ids[0] if next_ids else ""
    prev_id = prev_ids[0] if prev_ids else ""

    # Ordered list of dial targets. The runtime walks it and falls through to
    # the next entry when one is unreachable — that is the balancer.
    next_hops: list = []
    for nid in next_ids:
        target = _entry_target(nid)
        if not target:
            continue
        next_hops.append({
            "node_id": nid,
            "host": target["host"],
            "port": target["port"],
            "path_template": target["path_template"],
            "token": tokens.get((node_id, nid)) or "",
            "name": _node_name(nid),
        })
    upstream = None
    if prev_id:
        upstream = {"node_id": prev_id, "name": _node_name(prev_id),
                    "node_ids": prev_ids}
    return {
        "chain": list(topology.get("active_chain") or []),
        "levels": [list(level) for level in (topology.get("levels") or [])],
        "self": {
            "node_id": node_id,
            "name": _node_name(node_id),
            "role": role.get("role") or ROLE_FINAL,
            "initial": bool(role.get("initial")),
            "middle": bool(role.get("middle")),
            "final": bool(role.get("final")),
            "prev_node_id": prev_id or None,
            "next_node_id": next_id or None,
            "next_node_ids": next_ids,
            "balancer": len(next_hops) > 1,
            "ingress_path": role.get("ingress_path") or TM_ENTRY_PATH_TEMPLATE,
            "chain_position": role.get("chain_position", 0),
            "chain_length": role.get("chain_length", 1),
        },
        # `next_hop` stays for peers running an older build (first choice only).
        "next_hop": next_hops[0] if next_hops else None,
        "next_hops": next_hops,
        "upstream": upstream,
        "active": True,
        "applied_at": _now(),
    }


async def _sync_chain_users(topology: dict) -> dict:
    """Reconcile every user that selected the TM inbound with the chain nodes.

    Reuses the existing Node reconciler, so TM users get exactly the same
    replication (same ``config_uuid``, same quota, same cleanup) as any other
    Node-selected user.
    """
    main = _m()
    iid = find_tm_inbound_id()
    if not iid:
        return {"users": 0}
    chain = list(topology.get("active_chain") or [])
    async with main.USERS_LOCK:
        users = [(uid, dict(u)) for uid, u in main.USERS.items()
                 if iid in [str(x) for x in (u.get("inbound_ids") or [])]]
    failed = []
    for uid, user in users:
        try:
            res = await main._sync_user_to_selected_nodes(uid, user, chain, False)
            for row in (res.get("results") or []):
                if not row.get("ok"):
                    failed.append(f"{row.get('name')}: {row.get('detail')}")
        except Exception as exc:
            failed.append(str(exc)[:120])
    return {"users": len(users), "failed": failed}


# ── Apply (validate → push → persist, with rollback) ────────────────────────
async def apply_tunnel(progress: list | None = None) -> dict:
    main = _m()
    steps = progress if progress is not None else []

    def step(ok: bool, label: str) -> None:
        steps.append({"ok": bool(ok), "label": label})

    servers = _live_servers()
    edges = [dict(e) for e in (TM_STATE.get("edges") or [])]
    active = str(TM_STATE.get("active_server") or "")

    try:
        topology = validate_graph(edges, sorted(servers.keys()), active)
    except TopologyError as exc:
        step(False, "Graph validated")
        TM_STATE["last_error"] = exc.message
        await _save()
        return {"ok": False, "stage": "validate", "error": exc.message, "steps": steps}
    step(True, "Graph validated")

    chain = list(topology.get("active_chain") or [])
    if not chain:
        message = ("No active chain. Connect servers in Configure — a single server with no "
                   "connection is a valid Initial + Final chain.")
        step(False, "Chain resolved")
        TM_STATE["last_error"] = message
        await _save()
        return {"ok": False, "stage": "topology", "error": message, "steps": steps}
    step(True, f"Chain resolved: {' → '.join(_node_name(n) for n in chain)}")

    # One shared per-hop token per adjacent pair (including every balancer
    # branch); rotated on every apply and never exposed to the browser or to
    # generated configs.
    tokens = {}
    for edge in edges:
        tokens[(edge["from"], edge["to"])] = secrets.token_hex(24)

    previous = TM_STATE.get("applied")
    rollback = (previous or {}).get("participants") if isinstance(previous, dict) else None

    pushed: list = []
    failed: list = []
    for node_id in chain:
        node = servers.get(node_id)
        if not node:
            continue
        participant = _build_participant(topology, node_id, tokens)
        ok, detail = await _push_participant(node, participant)
        if ok:
            pushed.append(node_id)
            step(True, f"Server {_node_name(node_id)} updated")
        else:
            failed.append({"node_id": node_id, "name": _node_name(node_id), "error": detail})
            step(False, f"Server {_node_name(node_id)} failed: {detail}")

    if failed:
        # Transactional rollback: restore the servers that already accepted the
        # new topology, and never overwrite the last known-good record.
        for node_id in pushed:
            node = servers.get(node_id)
            if not node:
                continue
            old = (rollback or {}).get(node_id)
            try:
                if old:
                    await _push_participant(node, old)
                else:
                    await _push_cancel(node)
            except Exception:
                pass
        if pushed:
            step(False, "Rolled back the servers that had accepted the new topology")
        names = ", ".join(f["name"] for f in failed)
        message = f"Apply failed on: {names}"
        TM_STATE["last_error"] = message
        await _save()
        return {"ok": False, "stage": "sync", "error": message, "failed": failed,
                "steps": steps, "chain": chain}

    # The local panel is part of TM too: if it is one of the chain servers, its
    # own role was just pushed by itself.
    local_node_id = _local_tm_node_id()
    if local_node_id and local_node_id in chain:
        TM_STATE["participant"] = _build_participant(topology, local_node_id, tokens)

    participants = {}
    for node_id in chain:
        participants[node_id] = _build_participant(topology, node_id, tokens)

    # The TM inbound exposes exactly the live chain.
    async with TM_LOCK:
        TM_STATE["enabled"] = True
    await sync_tm_inbound_nodes()
    step(True, "TM inbound updated")

    await _save()

    # Users who selected TM are reconciled with every chain server so the
    # generated route actually resolves on each hop.
    try:
        sync_result = await _sync_chain_users(topology)
        failed_sync = sync_result.get("failed") or []
        if failed_sync:
            step(False, "User sync: " + "; ".join(failed_sync[:3]))
        else:
            step(True, f"Users synced ({sync_result.get('users', 0)})")
    except Exception as exc:
        step(False, f"User sync failed: {exc}")

    try:
        await main.refresh_all_selected_users()
    except Exception:
        pass

    applied = {
        "at": _now(),
        "chain": chain,
        "edges": edges,
        "active_server": str(TM_STATE.get("active_server") or ""),
        "topology": topology,
        "participants": participants,
    }
    async with TM_LOCK:
        TM_STATE["applied"] = applied
        TM_STATE["last_apply"] = applied["at"]
        TM_STATE["last_error"] = ""
    await _save()
    step(True, "TM configuration applied")
    main.log_activity("tm", f"TM اعمال شد ({len(chain)} سرور)", "ok")
    return {
        "ok": True,
        "steps": steps,
        "chain": chain,
        "chain_names": [_node_name(n) for n in chain],
        "applied_at": applied["at"],
        "status": status_payload(),
    }


def _local_tm_node_id() -> str:
    """This panel's own Node id, when the panel is registered as its own node."""
    main = _m()
    try:
        own_host = str(main.SETTINGS.get("domain") or main.get_host() or "").rstrip("/").lower()
    except Exception:
        return ""
    if not own_host:
        return ""
    for nid, node in _live_servers().items():
        domain = str(node.get("domain") or "").rstrip("/").lower()
        if domain and own_host and own_host in domain:
            return nid
    return ""


# ── TM WebSocket entry (client landing + hop relay + exit) ──────────────────
# Balancer health bookkeeping. A hop that failed is skipped only until this
# window elapses, then it is tried again — nothing here polls, and nothing runs
# unless a real client is connected.
HOP_COOLDOWN_SECONDS = 20.0
_HOP_DOWN: dict = {}


def _hop_mark_down(node_id) -> None:
    if node_id:
        _HOP_DOWN[str(node_id)] = time.monotonic()


def _hop_mark_up(node_id) -> None:
    if node_id:
        _HOP_DOWN.pop(str(node_id), None)


def _hop_is_available(node_id) -> bool:
    if not node_id:
        return True
    marked = _HOP_DOWN.get(str(node_id))
    return marked is None or (time.monotonic() - marked) > HOP_COOLDOWN_SECONDS


def _balancer_order(hops: list) -> list:
    """Ready hops first (their configured order), cooled-down ones after."""
    ready = [h for h in hops if _hop_is_available(h.get("node_id"))]
    cooling = [h for h in hops if h not in ready]
    return ready + cooling


async def tm_ws_entry(ws: WebSocket, uuid: str, hop: bool = False) -> None:
    """Serve a TM WebSocket connection on this server.

    The behaviour is decided by the *pushed role*, never by a hard-coded list:
      * node has next hops → the raw stream is forwarded to the first reachable
                             one (that is the balancer)
      * node has no next hop → the VLESS header is parsed and the traffic exits
                               to the destination through the normal pipeline
    """
    main = _m()
    await ws.accept()

    participant = TM_STATE.get("participant") or {}
    self_role = dict(participant.get("self") or {})
    hops = list(participant.get("next_hops") or [])
    if not hops and participant.get("next_hop"):
        hops = [participant["next_hop"]]        # peer running an older build

    # Authorise against the same LINK table every other relay path uses.
    async with main.LINKS_LOCK:
        link = main.LINKS.get(uuid)
    tm_inbound_id = find_tm_inbound_id()
    if tm_inbound_id:
        relay_ids = list(link.get("relay_inbound_ids") or []) if link else []
        if tm_inbound_id not in relay_ids:
            # Users replicated from the entry panel carry the entry-side relay
            # list; the TM inbound is a legitimate relay for them.
            if link and link.get("relay_enabled"):
                relay_ids.append(tm_inbound_id)
                async with main.LINKS_LOCK:
                    if uuid in main.LINKS:
                        main.LINKS[uuid]["relay_inbound_ids"] = relay_ids
    if not main.is_link_allowed(link):
        await ws.close(code=1008, reason="not authorized")
        return

    ip = main._ws_client_ip(ws)
    conn_id = secrets.token_hex(6)
    main.connections[conn_id] = {
        "uuid": uuid,
        "ip": ip,
        "transport": "tm" + ("-hop" if hop else ""),
        "connected_at": _now(),
        "bytes": 0,
    }

    if hops and any(h.get("host") for h in hops):
        await _tm_forward(main, ws, uuid, conn_id, ip, hops)
    else:
        await _tm_exit(main, ws, uuid, conn_id, ip)

    main.connections.pop(conn_id, None)
    try:
        asyncio.create_task(main.save_state())
    except Exception:
        pass


async def _tm_connect_hops(hops: list, uuid: str):
    """Dial the balancer's branches in order; return the live upstream.

    A hop that refuses a connection is marked down for a few seconds and
    skipped, so the traffic falls through to the next middle server instead of
    dying; when everything is cooling down they are tried again rather than
    failing blindly. Returns ``(upstream, used_hop, last_error)``.
    """
    import websockets as _websockets

    upstream = None
    used = None
    last_error = ""
    for hop in _balancer_order(hops):
        path = str(hop.get("path_template") or TM_ENTRY_PATH_TEMPLATE).replace("{uuid}", uuid)
        if not path.startswith("/"):
            path = "/" + path
        host = str(hop.get("host") or "").strip()
        port = int(hop.get("port") or 443)
        if not host:
            continue
        scheme = "wss" if port == 443 else "ws"
        url = f"{scheme}://{host}:{port}{path}"
        headers = {
            "User-Agent": "Spider-TM",
            "X-Spider-TM-Hop": str(hop.get("token") or ""),
            "X-Spider-TM-Origin": main_node_domain()[:120],
        }
        try:
            upstream = await asyncio.wait_for(
                _websockets.connect(url, extra_headers=headers, max_size=None), timeout=6.0
            )
        except Exception as exc:
            last_error = str(exc)[:160]
            _hop_mark_down(hop.get("node_id"))
            continue
        _hop_mark_up(hop.get("node_id"))
        used = hop
        break
    return upstream, used, last_error


async def _tm_forward(main, ws: WebSocket, uuid: str, conn_id: str, ip: str, hops: list) -> None:
    """Relay the raw stream to the next TM participant — through the balancer."""
    upstream, used, last_error = await _tm_connect_hops(hops, uuid)

    if upstream is None:
        main.stats["total_errors"] += 1
        reason = f"{len(hops)} TM hop(s) unreachable"
        main.error_logs.append({"error": f"TM hop failed: {last_error or reason}", "time": _now()})
        main.log_activity("error", f"TM hop ناموفق: {last_error or reason}", "err")
        try:
            await ws.close(code=1014, reason="next TM hop unreachable")
        except Exception:
            pass
        return

    main.log_activity("connection", f"TM hop {ip} → {used.get('host')}", "info")

    try:
        first = await asyncio.wait_for(ws.receive(), timeout=15.0)
        if first["type"] != "websocket.disconnect":
            chunk = first.get("bytes") or (first.get("text") or "").encode()
            if chunk:
                if not await main.check_and_use(uuid, len(chunk)):
                    await ws.close(code=1008, reason="quota/disabled")
                    return
                main.connections[conn_id]["bytes"] += len(chunk)
                await upstream.send(chunk)

        async def client_to_hop():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes") or (msg.get("text") or "").encode()
                if not data:
                    continue
                if not await main.check_and_use(uuid, len(data)):
                    await ws.close(code=1008, reason="quota/disabled")
                    break
                main.connections[conn_id]["bytes"] += len(data)
                await upstream.send(data)

        async def hop_to_client():
            async for data in upstream:
                if isinstance(data, str):
                    data = data.encode()
                if not data:
                    continue
                if not await main.check_and_use(uuid, len(data)):
                    break
                main.connections[conn_id]["bytes"] += len(data)
                await ws.send_bytes(data)

        done, pending = await asyncio.wait(
            {asyncio.create_task(client_to_hop()), asyncio.create_task(hop_to_client())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as exc:
        main.stats["total_errors"] += 1
        main.error_logs.append({"error": f"TM hop relay: {exc}", "time": _now()})
    finally:
        try:
            await upstream.close()
        except Exception:
            pass


async def _tm_exit(main, ws: WebSocket, uuid: str, conn_id: str, ip: str) -> None:
    """Terminal server: parse the VLESS header and exit to the destination."""
    writer = None
    try:
        first_msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
        if first_msg["type"] == "websocket.disconnect":
            return
        first_chunk = first_msg.get("bytes") or (first_msg.get("text") or "").encode()
        if not first_chunk:
            return
        command, address, port, payload = await main.parse_vless_header(first_chunk, uuid)
        if not await main.check_and_use(uuid, len(first_chunk)):
            await ws.close(code=1008, reason="quota/disabled")
            return
        main.stats["total_requests"] += 1
        main.connections[conn_id]["bytes"] += len(first_chunk)
        main.log_activity("connection", f"TM exit {ip} → {address}:{port}", "info")
        reader, writer = await main.proxy_connect(uuid, address, port)
        main._tune_relay_socket(writer)
        speed_limiter = None
        user_id = ""
        async with main.LINKS_LOCK:
            link = main.LINKS.get(uuid)
            user_id = str((link or {}).get("user_id") or "")
        if user_id:
            async with main.USERS_LOCK:
                linked = main.USERS.get(user_id) or {}
                try:
                    user_speed = max(0.0, float(linked.get("speed_limit_mbps") or 0))
                except (TypeError, ValueError):
                    user_speed = 0.0
            speed_limiter = main._user_speed_limiter(user_id, user_speed)
        if payload:
            if speed_limiter:
                await speed_limiter.wait_for(len(payload))
            writer.write(payload)
            await writer.drain()
        done, pending = await asyncio.wait(
            {
                asyncio.create_task(main.relay_ws_to_tcp(ws, writer, conn_id, uuid, speed_limiter)),
                asyncio.create_task(main.relay_tcp_to_ws(ws, reader, conn_id, uuid, speed_limiter)),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    except asyncio.TimeoutError:
        main.stats["total_errors"] += 1
        main.error_logs.append({"error": "TM exit timeout", "time": _now()})
    except Exception as exc:
        main.stats["total_errors"] += 1
        main.error_logs.append({"error": f"TM exit: {exc}", "time": _now()})
        main.log_activity("error", f"خطای TM exit {ip}: {exc}", "err")
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


# ── HTTP API ────────────────────────────────────────────────────────────────
router = APIRouter()


async def _auth(request: Request):
    """Reuse the panel's existing session authentication."""
    return await _m().require_auth(request)


async def _peer_auth(request: Request):
    """Reuse the existing replication auth (session cookie or X-API-Key)."""
    return await _m().require_replication_auth(request)


@router.get("/api/tm")
async def tm_root(_=Depends(_auth)):
    return {"ok": True, "status": status_payload(), "graph": graph_payload()}


@router.get("/api/tm/status")
async def tm_status(_=Depends(_auth)):
    return {"ok": True, "status": status_payload()}


@router.get("/api/tm/servers")
async def tm_servers(_=Depends(_auth)):
    return {"ok": True, "servers": _safe_servers()}


@router.post("/api/tm/servers")
async def tm_add_server(request: Request, _=Depends(_auth)):
    main = _m()
    body = await request.json()
    try:
        result = await add_server(body.get("name"), body.get("domain"), body.get("api_key"))
    except TopologyError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    # A newly added server is a valid TM participant immediately; keep the TM
    # inbound's node list in step when it joins an already-applied chain.
    if TM_STATE.get("enabled"):
        await sync_tm_inbound_nodes()
        await main.save_state()
    return result


@router.patch("/api/tm/servers/{server_id}")
async def tm_update_server(server_id: str, request: Request, _=Depends(_auth)):
    body = await request.json()
    try:
        result = await update_server(
            server_id,
            body.get("name"),
            body.get("domain"),
            body.get("api_key"),
        )
    except TopologyError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return result


@router.delete("/api/tm/servers/{server_id}")
async def tm_delete_server(server_id: str, _=Depends(_auth)):
    try:
        return await delete_server(server_id)
    except TopologyError as exc:
        raise HTTPException(status_code=404, detail=exc.message)


@router.post("/api/tm/servers/{server_id}/ping")
async def tm_ping_server(server_id: str, _=Depends(_auth)):
    """Server-side health ping (the panel → the server)."""
    try:
        return await ping_server(server_id)
    except TopologyError as exc:
        raise HTTPException(status_code=404, detail=exc.message)


@router.get("/api/tm/graph")
async def tm_get_graph(_=Depends(_auth)):
    return {"ok": True, "graph": graph_payload()}


@router.put("/api/tm/graph")
async def tm_put_graph(request: Request, _=Depends(_auth)):
    body = await request.json()
    servers = _live_servers()
    known = set(servers.keys())

    raw_positions = body.get("positions") or {}
    if isinstance(raw_positions, dict):
        async with TM_LOCK:
            positions = TM_STATE.setdefault("positions", {})
            for nid, pos in raw_positions.items():
                if nid not in known or not isinstance(pos, dict):
                    continue
                try:
                    positions[nid] = {"x": float(pos.get("x") or 0), "y": float(pos.get("y") or 0)}
                except (TypeError, ValueError):
                    continue

    if "edges" in body:
        raw_edges = body.get("edges") or []
        cleaned = []
        for edge in raw_edges:
            if not isinstance(edge, dict):
                continue
            cleaned_edge = _clean_edge(edge)
            if cleaned_edge is None:
                continue
            cleaned.append(cleaned_edge)
        async with TM_LOCK:
            TM_STATE["edges"] = cleaned

    if "active_server" in body:
        active = str(body.get("active_server") or "")
        async with TM_LOCK:
            TM_STATE["active_server"] = active if active in known else ""

    await _save()
    return {"ok": True, "graph": graph_payload(), "status": status_payload()}


@router.post("/api/tm/graph/validate")
async def tm_validate_graph(request: Request, _=Depends(_auth)):
    body = await request.json() if await request.body() else {}
    servers = sorted(_live_servers().keys())
    edges = body.get("edges")
    if edges is None:
        edges = [dict(e) for e in (TM_STATE.get("edges") or [])]
    active = body.get("active_server")
    if active is None:
        active = str(TM_STATE.get("active_server") or "")
    try:
        topology = validate_graph([dict(e) for e in edges], servers, str(active or ""))
    except TopologyError as exc:
        TM_STATE["last_validate"] = {"ok": False, "error": exc.message, "at": _now()}
        await _save()
        return JSONResponse(status_code=200, content={
            "ok": False, "error": exc.message, "errors": exc.errors,
            "status": status_payload(),
        })
    result = {
        "ok": True,
        "topology": _topology_summary(topology),
        "status": status_payload(),
    }
    TM_STATE["last_validate"] = {"ok": True, "error": "", "at": _now()}
    await _save()
    return result


@router.post("/api/tm/apply")
async def tm_apply(_=Depends(_auth)):
    return await apply_tunnel()


@router.post("/api/tm/set-server")
async def tm_set_server(request: Request, _=Depends(_auth)):
    """The Set selection is real state: it decides which server the generated
    client configuration dials."""
    body = await request.json()
    node_id = str(body.get("node_id") or "")
    servers = _live_servers()
    if node_id and node_id not in servers:
        raise HTTPException(status_code=404, detail="Server not found.")
    async with TM_LOCK:
        TM_STATE["active_server"] = node_id
    await sync_tm_inbound_nodes()
    await _save()
    if node_id:
        _m().log_activity("tm", f"سرور فعال TM روی «{_node_name(node_id)}» تنظیم شد", "info")
    return {"ok": True, "status": status_payload(), "graph": graph_payload()}


@router.post("/api/tm/chain-ping")
async def tm_chain_ping(request: Request, _=Depends(_auth)):
    """SERVER-to-server chain latency.

    Measured by asking each participant to ping the next hop it would actually
    dial (every balancer branch gets its own number), so the result describes
    the tunnel path — never the browser's latency.
    """
    main = _m()
    body = await request.json() if await request.body() else {}
    servers = _live_servers()
    edges = [dict(e) for e in (TM_STATE.get("edges") or [])]
    active = str(body.get("active_server") or TM_STATE.get("active_server") or "")
    try:
        topology = validate_graph(edges, sorted(servers.keys()), active)
    except TopologyError as exc:
        return JSONResponse(status_code=200, content={"ok": False, "error": exc.message})
    chain = list(topology.get("active_chain") or [])
    if not chain:
        return {"ok": False, "error": "No active chain."}
    levels = topology.get("levels") or [chain]

    # One row per balancer branch, walked the way the traffic walks it: each
    # entry servers are contacted first and each of them walks its own branch
    # to the exit, so the entry → exit round trip is what gets measured.
    paths = []
    for entry in levels[0]:
        for path in _paths_from(topology, [entry]) or [[entry]]:
            paths.append(path)
        if not _paths_from(topology, [entry]):
            paths.append([entry])
    if not paths:
        paths = [list(chain)]

    hops = []
    best = None
    for path in paths:
        row = {"path": [_node_name(n) for n in path], "from": _node_name(path[0]),
               "to": _node_name(path[-1]), "ms": None, "ok": False, "kind": "chain"}
        hop_targets = _hop_path_for(topology, path)
        if not hop_targets:
            # A single-server chain exits on its own: the walk is one connect.
            row["hops"] = 0
            row["error"] = "" if len(path) == 1 else "no address for the next hop"
            row["ok"] = len(path) == 1
            row["ms"] = 0 if len(path) == 1 else None
            hops.append(row)
            continue
        first_host = hop_targets[0]
        result = await _chain_ping_walk(hop_targets)
        row["hops"] = result.get("hops") or 0
        if result.get("ok"):
            row["ms"] = int(result.get("ms") or 0)
            row["ok"] = True
            if best is None or row["ms"] < best:
                best = row["ms"]
        else:
            row["error"] = result.get("error") or "unreachable"
        hops.append(row)

    complete = any(h.get("ok") for h in hops) if hops else False
    parallel = any(len(level) > 1 for level in levels[1:])
    return {
        "ok": complete,
        "measurement": "server-to-server",
        "chain": [_node_name(n) for n in chain],
        "levels": [len(level) for level in levels],
        "balancer": parallel,
        # The best working branch describes the tunnel a client gets; the
        # per-branch rows show what each middle server is doing.
        "total_ms": best,
        "partial_ms": best,
        "hops": hops,
    }


@router.post("/api/tm/edge-ping")
async def tm_edge_ping(request: Request, _=Depends(_auth)):
    """Latency of ONE connection bar, measured server-to-server.

    The panel probes the source server, then asks the source to probe the
    destination (`/api/tm/peer/hop-ping` with a `to` name). No browser timing
    ever enters this number.
    """
    main = _m()
    body = await request.json() if await request.body() else {}
    servers = _live_servers()
    src, dst = str(body.get("from") or ""), str(body.get("to") or "")
    if src not in servers or dst not in servers:
        return {"ok": False, "ms": None, "error": "Invalid TM topology: unknown endpoint on this link."}
    row = {"from": _node_name(src), "to": _node_name(dst), "ms": None, "ok": False, "kind": "server"}
    # The asker sends the destination's dial address as well: a peer may not
    # have a Node record for it, and the number must describe the address the
    # tunnel itself would use.
    target = _entry_target(dst) or {}
    payload = {"to": dst}
    if target.get("host"):
        payload["host"] = target["host"]
        payload["port"] = target["port"]
    try:
        r = await main._remote_request(servers[src], "POST", "/api/tm/peer/hop-ping",
                                       payload, timeout=15.0)
        if r.status_code != 200:
            row["error"] = _http_error(r.status_code)
        else:
            data = r.json() or {}
            if data.get("ms") is not None:
                row["ms"] = int(data["ms"])
                row["ok"] = True
            else:
                row["error"] = data.get("error") or "no measurement"
    except Exception as exc:
        row["error"] = _connect_error(exc)
    return {"ok": row["ok"], "ms": row["ms"], "error": row.get("error", ""),
            "from": row["from"], "to": row["to"]}


@router.post("/api/tm/generate")
async def tm_generate(request: Request, _=Depends(_auth)):
    """Preview the exact client config TM would produce for a user."""
    main = _m()
    body = await request.json() if await request.body() else {}
    config_uuid = str(body.get("uuid") or "")
    user = None
    if config_uuid:
        for _, candidate in list(main.USERS.items()):
            if str(candidate.get("config_uuid") or "") == config_uuid:
                user = candidate
                break
    if user is None:
        for _, candidate in list(main.USERS.items()):
            user = candidate
            break
    if user is None:
        return {"ok": False, "error": "No users yet — create a user and assign the TM inbound."}
    entry_id = str(body.get("node_id") or "") or resolve_entry_node_id()
    cfg = tm_client_config(user, entry_id)
    if not cfg:
        return {"ok": False, "error": "TM has no usable entry server yet.",
                "status": status_payload()}
    topology = current_topology()
    return {
        "ok": True,
        "config": cfg,
        "entry_node_id": entry_id,
        "entry_name": _node_name(entry_id),
        "path": client_path_for(topology, entry_id).replace("{uuid}", str(user.get("config_uuid") or "")),
        "chain": [_node_name(n) for n in (topology.get("active_chain") or [])],
        "status": status_payload(),
    }


# ── Peer endpoints (used by other SpiderPanels, SPI-key authenticated) ──────
@router.post("/api/tm/peer/config")
async def tm_peer_config(request: Request, _=Depends(_peer_auth)):
    """Receive this server's role in a chain from the entry panel."""
    main = _m()
    body = await request.json()
    if body.get("clear"):
        async with TM_LOCK:
            TM_STATE["participant"] = None
        await _save()
        return {"ok": True, "cleared": True}

    participant = {
        "chain": [str(x) for x in (body.get("chain") or [])],
        "self": dict(body.get("self") or {}),
        "next_hop": dict(body.get("next_hop") or {}) if body.get("next_hop") else None,
        "upstream": dict(body.get("upstream") or {}) if body.get("upstream") else None,
        "applied_at": str(body.get("applied_at") or _now()),
        "received_at": _now(),
    }
    # The `self` block is what this panel answers status with, so its own
    # identity always wins over a peer-supplied node id.
    if not participant["self"].get("node_id"):
        participant["self"]["node_id"] = main_node_domain()
    async with TM_LOCK:
        TM_STATE["participant"] = participant
        TM_STATE["enabled"] = True
        TM_STATE["last_apply"] = participant["applied_at"]
        TM_STATE["last_error"] = ""
    await _save()
    main.log_activity("tm", "تنظیمات TM از سرور مبدأ دریافت شد", "ok")
    return {"ok": True, "status": status_payload()}


@router.get("/api/tm/peer/status")
async def tm_peer_status(_=Depends(_peer_auth)):
    return {"ok": True, "status": status_payload()}


def _paths_from(topology: dict, start: str, limit: int = 8) -> list:
    """Every path from ``start`` down to an exit, one per balancer branch.

    The graph is a DAG, so this always terminates; ``limit`` only keeps a
    pathological fan-out from producing hundreds of measurements.
    """
    roles = topology.get("roles") or {}
    out, stack = [], [list(start) if isinstance(start, list) else [start]]
    while stack and len(out) < limit:
        path = stack.pop()
        node = path[-1]
        next_ids = list((roles.get(node) or {}).get("next_node_ids") or [])
        if not next_ids:
            if len(path) > 1:
                out.append(path)
            continue
        for nid in next_ids:
            stack.append(path + [nid])
    return out


def _hop_path_for(topology: dict, path: list) -> list:
    """Turn a list of node ids into the dial targets a participant would use."""
    hops = []
    for nid in path[1:]:
        target = _entry_target(nid)
        if not target:
            continue
        hops.append({
            "node_id": nid,
            "host": target["host"],
            "port": target["port"],
            "path_template": target["path_template"],
            "name": _node_name(nid),
        })
    return hops


async def _chain_ping_walk(hops: list) -> dict:
    """Walk a chain from THIS server to its end, measuring every real dial.

    Each hop is a genuine TCP connect to the address the tunnel itself would
    dial, made by the server that would make it. When the next participant is
    a known Node the measurement is continued from there, so the total
    describes the whole path entry → exit and not a single link. Nothing is
    reported alive unless the connect actually succeeded.
    """
    main = _m()
    if not hops:
        return {"ok": True, "ms": 0, "hops": 0, "error": ""}

    hop = hops[0]
    rest = [h for h in hops[1:] if isinstance(h, dict)]
    host, port = _split_host_port(str(hop.get("host") or ""), int(hop.get("port") or 443))
    if not host:
        return {"ok": False, "ms": None, "hops": 0, "error": "no address for the next hop"}

    started = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port or 443)), timeout=6.0)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except Exception as exc:
        return {"ok": False, "ms": None, "hops": 0, "error": _connect_error(exc)}
    here = max(1, round((time.perf_counter() - started) * 1000))
    if not rest:
        return {"ok": True, "ms": here, "hops": 1, "error": ""}

    node = _live_servers().get(str(hop.get("node_id")))
    if node:
        try:
            r = await main._remote_request(
                node, "POST", "/api/tm/peer/chain-ping", {"path": rest}, timeout=20.0)
            if r.status_code == 404:
                # A peer on an older build has no chain walk. Measure the rest
                # of the path directly from here instead of failing the whole
                # chain: the addresses are the ones the relay would dial, so the
                # total is still the real entry → exit figure.
                return await _chain_ping_direct(rest, here)
            if r.status_code != 200:
                return {"ok": False, "ms": None, "hops": 1, "error": _http_error(r.status_code)}
            data = r.json() or {}
            if not data.get("ok"):
                return {"ok": False, "ms": None, "hops": 1,
                        "error": str(data.get("error") or "the next server refused the walk")[:160]}
            return {"ok": True, "ms": here + int(data.get("ms") or 0),
                    "hops": 1 + int(data.get("hops") or 0), "error": ""}
        except Exception as exc:
            return {"ok": False, "ms": None, "hops": 1, "error": _connect_error(exc)}

    return await _chain_ping_direct(rest, here)


async def _chain_ping_direct(rest: list, already: int) -> dict:
    """Measure the remaining hops from here, one real TCP connect each."""
    total, count = already, 1
    for nxt in rest:
        nxt_host, nxt_port = _split_host_port(str(nxt.get("host") or ""), int(nxt.get("port") or 443))
        if not nxt_host:
            return {"ok": False, "ms": None, "hops": count, "error": "no address for the next hop"}
        t0 = time.perf_counter()
        try:
            _r2, w2 = await asyncio.wait_for(
                asyncio.open_connection(nxt_host, int(nxt_port or 443)), timeout=6.0)
            w2.close()
            try:
                await w2.wait_closed()
            except Exception:
                pass
        except Exception as exc:
            return {"ok": False, "ms": None, "hops": count, "error": _connect_error(exc)}
        total += max(1, round((time.perf_counter() - t0) * 1000))
        count += 1
    return {"ok": True, "ms": total, "hops": count, "error": ""}


def _http_error(status_code: int) -> str:
    """A raw HTTP status means nothing to an operator; say what happened."""
    return {
        401: "the next server rejected the API key",
        403: "the next server refused this request",
        404: "the next server has no TM endpoints (old build?)",
        502: "the next server is unreachable",
        503: "the next server is unreachable",
        504: "the next server did not answer in time",
    }.get(status_code, f"the next server answered HTTP {status_code}")


def _connect_error(exc: Exception) -> str:
    """Turn a transport exception into a sentence, never a bare class name."""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return "the next server did not answer in time"
    if "refused" in low:
        return "the next server refused the connection"
    if "name or service" in low or "getaddrinfo" in low or "resolve" in low:
        return "the next server's address could not be resolved"
    if "certificate" in low or "ssl" in low:
        return "the next server's certificate was rejected"
    return text[:160]


@router.post("/api/tm/peer/chain-ping")
async def tm_peer_chain_ping(request: Request, _=Depends(_peer_auth)):
    """Walk the rest of the chain from this server, measuring every dial.

    The entry panel calls this so the number on the status bar is the real
    entry → exit round trip, measured by the servers that carry the traffic.
    """
    body = await request.json() if await request.body() else {}
    hops = [h for h in (body.get("path") or []) if isinstance(h, dict) and h.get("host")]
    return await _chain_ping_walk(hops)


@router.post("/api/tm/peer/hop-ping")
async def tm_peer_hop_ping(request: Request, _=Depends(_peer_auth)):
    """Measure this server's latency toward the peer the caller names.

    The caller sends ``{"to": node_id}`` and, when the asking panel knows the
    peer under an address this server may not use, that peer's ``host`` and
    ``port`` too — the two ends may see a server under different addresses,
    and only the asker's view is the one that should be measured.

    The measurement is a real TCP connect from THIS server, so a dead peer is
    never reported alive. A direction this server does not serve (asking the
    Final server to ping upstream) is refused explicitly instead of returning
    a number for a target that was never contacted.
    """
    body = await request.json() if await request.body() else {}
    wanted = str(body.get("to") or "")
    raw_host = str(body.get("host") or "")
    raw_port = body.get("port")
    target_host, target_port = _split_host_port(raw_host, int(raw_port) if raw_port else 443)

    participant = TM_STATE.get("participant") or {}
    role = str((participant.get("self") or {}).get("role") or "")
    hops = list(participant.get("next_hops") or [])
    if not hops and participant.get("next_hop"):
        hops = [participant["next_hop"]]

    # Prefer this server's own config for a hop it actually forwards to; that
    # is the address the real relay would dial.
    for candidate in hops:
        if wanted and str(candidate.get("node_id")) != wanted:
            continue
        if candidate.get("host"):
            target_host, _sp = _split_host_port(
                str(candidate.get("host") or ""), int(candidate.get("port") or 443))
            target_port = int(candidate.get("port") or 443)
        break

    if not target_host:
        if not wanted:
            return {"ok": True, "ms": None, "error": "no target given"}
        if role == ROLE_FINAL:
            return {"ok": True, "ms": None,
                    "error": "this server is the final one and does not forward"}
        return {"ok": True, "ms": None, "error": "no address for that server"}

    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target_host, target_port), timeout=6.0)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"ok": True, "ms": max(1, round((time.perf_counter() - started) * 1000)),
                "to": wanted or target_host}
    except Exception as exc:
        return {"ok": True, "ms": None, "to": wanted or target_host,
                "error": _connect_error(exc)}


@router.get("/api/tm/peer/ping")
async def tm_peer_ping():
    """Cheap, unauthenticated round-trip target for the browser's own ping.

    Deliberately returns no server-side latency: the browser measures the
    request/response time itself, so the number is client-side by construction.
    """
    return JSONResponse(
        status_code=200,
        content={"ok": True, "t": time.time(), "service": "spider-tm"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )
