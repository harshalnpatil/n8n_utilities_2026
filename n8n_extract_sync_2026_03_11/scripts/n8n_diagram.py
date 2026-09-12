#!/usr/bin/env python3
"""Generate simplified Mermaid flowchart diagrams from n8n workflow JSON.

Two modes:
  1. Deterministic (default): aggressive rule-based simplification
  2. LLM-assisted (--use-llm): deterministic pass + GPT further compression

Deterministic simplification (no AI/LLM calls):
  - Remove non-functional nodes (stickyNote, timeSaved, noOp, executionData).
  - Detach AI sub-components (model, memory, tools) into agent subgraphs.
  - Keep only "functional" nodes: triggers, AI agents, integrations,
    responses, executeWorkflow.  Everything else (IF, Switch, filter,
    set, code, merge, loop, wait) is bypassed.
  - Preserve branching via edge labels (Yes/No, Rule N/Fallback).
  - Group triggers and error nodes into functional subgraphs.

LLM-assisted (--use-llm):
  - Run deterministic simplification first.
  - Send result to gpt-5.6-luna (high effort) for further compression.
  - LLM merges related nodes, creates functional groupings, improves labels.
  - Uses OPENAI_API_KEY env var.

Outputs:
  <context_dir>/diagram.mmd  – Mermaid source (editable canonical version)
  <context_dir>/diagram.svg  – rendered SVG (shareable with non-technical people)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from n8n_common import (
    SyncError,
    find_state_record,
    load_state,
    resolve_workspace_root,
)

# ── Node type sets ────────────────────────────────────────────────────────

SKIP_TYPES: Set[str] = {
    "stickyNote", "timeSaved", "noOp", "executionData",
}

TRIGGER_TYPES: Set[str] = {
    "scheduleTrigger", "webhook", "telegramTrigger", "gmailTrigger",
    "executeWorkflowTrigger", "manualTrigger", "formTrigger", "chatTrigger",
    "errorTrigger", "googleSheetsTrigger", "notionTrigger",
    "googleCalendarTrigger", "googleDriveTrigger", "whatsAppTrigger",
    "evaluationTrigger", "mcpTrigger",
}

AI_AGENT_TYPES: Set[str] = {"agent", "chainLlm", "chat"}
RESPONSE_TYPES: Set[str] = {"respondToWebhook"}

AI_SUB_TYPES: Set[str] = {
    "lmChatOpenAi", "lmChatAnthropic", "lmChatGoogleGemini", "lmChatOpenRouter",
    "memoryBufferWindow", "memoryPostgresChat", "mcpClientTool", "toolWorkflow",
    "toolVectorStore", "toolSerpApi", "toolCalculator", "toolCode", "openAi",
    "outputParserStructured", "embeddingsOpenAi", "vectorStorePinecone",
    "documentDefaultDataLoader", "textSplitterRecursiveCharacterTextSplitter",
}

TOOL_TYPES: Set[str] = {
    "httpRequestTool", "trelloTool", "gmailTool", "googleSheetsTool",
    "googleCalendarTool", "googleDriveTool", "supabaseTool", "perplexityTool",
    "tavilyTool",
}

# Nodes that are always kept as individual nodes in the simplified diagram.
# Everything else is a "connector" that gets bypassed.
KEEP_TYPES: Set[str] = (
    TRIGGER_TYPES
    | AI_AGENT_TYPES
    | RESPONSE_TYPES
    | {
        # Integrations – external service calls
        "telegram", "gmail", "googleDrive", "googleCalendar", "googleSheets",
        "supabase", "notion", "trello", "httpRequest", "rssFeedRead",
        "googleCloudStorage", "googleTasks", "quickChart", "perplexity",
        "dataTable", "n8n", "tavily", "form", "evaluation",
        # Workflow control
        "executeWorkflow", "stopAndError",
    }
)


# ── Categorisation ────────────────────────────────────────────────────────

def get_base_type(node_type: str) -> str:
    """Extract the simple type name from the full n8n type string."""
    return node_type.rsplit(".", 1)[-1] if "." in node_type else node_type


def categorize(base_type: str) -> str:
    if base_type in SKIP_TYPES:
        return "skip"
    if base_type in TRIGGER_TYPES:
        return "trigger"
    if base_type in AI_AGENT_TYPES:
        return "ai_agent"
    if base_type in AI_SUB_TYPES:
        return "ai_sub"
    if base_type in RESPONSE_TYPES:
        return "response"
    if base_type in KEEP_TYPES:
        return "integration"
    if base_type in TOOL_TYPES:
        return "tool"
    return "connector"  # IF, Switch, filter, set, code, merge, loop, wait, etc.


# ── Label helpers ─────────────────────────────────────────────────────────

def sanitize_label(text: str, max_len: int = 50) -> str:
    """Sanitise a label so it is safe inside Mermaid syntax."""
    label = text.strip().replace('"', "'")
    label = re.sub(r'[\[\]\{\}()<>|]', "", label)
    if len(label) > max_len:
        label = label[: max_len - 3] + "..."
    return label


def ai_conn_short(conn_type: str) -> str:
    mapping = {
        "ai_languageModel": "model",
        "ai_memory": "memory",
        "ai_tool": "tool",
        "ai_outputParser": "parser",
        "ai_embedder": "embedder",
        "ai_vectorStore": "vector store",
        "ai_documentStore": "doc store",
        "ai_textSplitter": "splitter",
        "ai_dataLoader": "data loader",
    }
    return mapping.get(conn_type, conn_type.replace("ai_", ""))


def get_switch_label(parameters: dict, output_index: int, num_outputs: int) -> Optional[str]:
    """Try to extract a meaningful label from Switch node rules."""
    if num_outputs <= 1:
        return None
    if output_index >= num_outputs - 1:
        return "Fallback"
    rules = parameters.get("rules", {})
    values = rules.get("values", [])
    if output_index < len(values):
        rule = values[output_index]
        conditions = rule.get("conditions", {})
        condition_values = conditions.get("values", [])
        if condition_values:
            cond = condition_values[0]
            right = cond.get("rightValue", "")
            if right and isinstance(right, str) and len(right) < 30:
                return sanitize_label(str(right), 30)
            left = cond.get("leftValue", "")
            if left and isinstance(left, str) and len(left) < 30:
                # Try to use just the last segment after dot
                short = str(left).split(".")[-1].strip("} ")
                if short and len(short) < 30:
                    return sanitize_label(short, 30)
    return f"Rule {output_index + 1}"


def get_edge_label(node: dict, base_type: str, output_index: int, num_outputs: int) -> Optional[str]:
    """Get the edge label for a branching output."""
    if base_type == "if":
        return "Yes" if output_index == 0 else "No"
    if base_type == "splitInBatches":
        return "Loop" if output_index == 0 else "Done"
    if base_type == "wait":
        return "Resume" if output_index == 0 else "Error"
    if base_type == "switch":
        return get_switch_label(node.get("parameters", {}), output_index, num_outputs)
    return None


# ── Graph building ────────────────────────────────────────────────────────

def build_node_map(nodes: list) -> Dict[str, dict]:
    """name → node dict from the nodes array."""
    result: Dict[str, dict] = {}
    for node in nodes:
        name = node.get("name", "")
        if name:
            result[name] = node
    return result


def identify_ai_subs(
    connections: dict,
) -> Tuple[Dict[str, str], Dict[str, List[Tuple[str, str]]]]:
    """Identify AI sub-components from ai_* connection types.

    Returns:
        sub_to_agent   – {sub_name: agent_name}
        agent_to_subs  – {agent_name: [(sub_name, conn_type), …]}
    """
    sub_to_agent: Dict[str, str] = {}
    agent_to_subs: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    for source, source_conn in connections.items():
        for conn_type, outputs in source_conn.items():
            if not conn_type.startswith("ai_"):
                continue
            for output_list in outputs:
                for target in output_list:
                    agent = target.get("node", "")
                    if agent:
                        sub_to_agent[source] = agent
                        agent_to_subs[agent].append((source, conn_type))

    return sub_to_agent, dict(agent_to_subs)


def bypass_skip_and_build_main(
    connections: dict,
    skip_nodes: set,
    ai_sub_nodes: set,
) -> Dict[str, List[List[str]]]:
    """Build the main flow graph with skip nodes bypassed and AI subs removed.

    Returns: {source_name: [out0_targets, out1_targets, …]}
    """
    def resolve_targets(node_name: str, visited: Optional[set] = None) -> List[str]:
        """Follow through skip nodes to get real targets."""
        if visited is None:
            visited = set()
        if node_name in visited:
            return []
        visited.add(node_name)

        if node_name not in skip_nodes:
            return [node_name]

        result: List[str] = []
        conn = connections.get(node_name, {})
        for output_list in conn.get("main", []):
            for t in output_list:
                result.extend(resolve_targets(t.get("node", ""), visited))
        return result

    graph: Dict[str, List[List[str]]] = {}
    for source, source_conn in connections.items():
        if source in skip_nodes or source in ai_sub_nodes:
            continue

        main_outputs = source_conn.get("main", [])
        outputs: List[List[str]] = []
        for output_list in main_outputs:
            resolved: List[str] = []
            for t in output_list:
                target = t.get("node", "")
                if target in ai_sub_nodes:
                    continue
                resolved.extend(resolve_targets(target))
            # Deduplicate, preserving order; also drop skip/ai_sub residues
            seen: Set[str] = set()
            deduped: List[str] = []
            for r in resolved:
                if r not in seen and r not in skip_nodes and r not in ai_sub_nodes:
                    seen.add(r)
                    deduped.append(r)
            outputs.append(deduped)

        if any(out for out in outputs):
            graph[source] = outputs

    return graph


# ── Aggressive connector bypass ───────────────────────────────────────────

def bypass_connectors(
    main_graph: Dict[str, List[List[str]]],
    keep_set: Set[str],
    connector_set: Set[str],
    node_map: Dict[str, dict],
) -> Dict[str, List[Tuple[str, Optional[str]]]]:
    """Bypass connector nodes, keeping only keep nodes.

    For each keep node, BFS through connectors to find reachable keep nodes.
    Tracks branch labels from IF/Switch/wait/loop nodes.

    Returns: {source_keep: [(target_keep, branch_label), ...]}
    """
    result: Dict[str, List[Tuple[str, Optional[str]]]] = defaultdict(list)
    result_seen: Dict[str, Set[Tuple[str, Optional[str]]]] = defaultdict(set)

    for source in keep_set:
        if source not in main_graph:
            continue

        visited_connectors: Set[str] = set()
        queue: List[Tuple[str, Optional[str]]] = []

        # Start with source's direct outputs
        outputs = main_graph.get(source, [])
        source_node = node_map.get(source, {})
        source_base = get_base_type(source_node.get("type", ""))
        for out_idx, out_list in enumerate(outputs):
            label = get_edge_label(source_node, source_base, out_idx, len(outputs))
            for target in out_list:
                queue.append((target, label))

        while queue:
            node, label = queue.pop(0)

            if node in keep_set:
                key = (node, label)
                if key not in result_seen[source]:
                    result_seen[source].add(key)
                    result[source].append((node, label))
                continue  # Don't traverse past keep nodes

            if node in connector_set:
                if node in visited_connectors:
                    continue  # Prevent cycles
                visited_connectors.add(node)

                conn_outputs = main_graph.get(node, [])
                conn_node = node_map.get(node, {})
                conn_base = get_base_type(conn_node.get("type", ""))
                for out_idx, out_list in enumerate(conn_outputs):
                    conn_label = get_edge_label(conn_node, conn_base, out_idx, len(conn_outputs))
                    effective_label = label or conn_label  # keep first label, don't override
                    for target in out_list:
                        queue.append((target, effective_label))

    return dict(result)


# ── Functional grouping ───────────────────────────────────────────────────

_ERROR_KEYWORDS = {"error", "refuse", "fail", "invalid", "unauthorized", "alert"}


def functional_group(
    name: str,
    base_type: str,
    category: str,
    is_terminal: bool,
) -> Optional[str]:
    """Classify a keep node into a functional group for subgraph grouping."""
    if category == "trigger":
        return "triggers"
    if category == "ai_agent":
        return None  # AI agents get their own subgraphs
    name_lower = name.lower()
    if any(w in name_lower for w in _ERROR_KEYWORDS):
        return "error_handling"
    if is_terminal and category in ("integration", "response"):
        return "output"
    return None


# ── Mermaid generation ────────────────────────────────────────────────────

def _sort_key(name: str, node_map: dict) -> tuple:
    cat = categorize(get_base_type(node_map.get(name, {}).get("type", "")))
    cat_order = {
        "trigger": 0,
        "ai_agent": 2,
        "integration": 3,
        "response": 4,
    }.get(cat, 8)
    return (cat_order, name)


def generate_mermaid(
    simplified_graph: dict,
    node_map: dict,
    ai_subs: dict,
    workflow_name: str,
) -> str:
    """Generate Mermaid flowchart syntax (string)."""
    lines: List[str] = []
    lines.append(f"%% Simplified diagram for: {workflow_name}")
    lines.append("%% Generated by n8n diagram command")
    lines.append("flowchart TD")
    lines.append("")

    # Collect all nodes
    all_nodes: Set[str] = set(simplified_graph.keys())
    for targets in simplified_graph.values():
        for target, _ in targets:
            all_nodes.add(target)

    # Determine terminal nodes (no outgoing edges in simplified graph)
    terminal_nodes = all_nodes - set(simplified_graph.keys())

    # Assign IDs
    id_map: Dict[str, str] = {}
    counter = 1
    for name in sorted(all_nodes, key=lambda n: _sort_key(n, node_map)):
        id_map[name] = f"n{counter}"
        counter += 1

    # IDs for AI sub-components
    sub_id_map: Dict[str, str] = {}
    for agent, subs in ai_subs.items():
        for sub_name, _ in subs:
            if sub_name not in sub_id_map and sub_name not in id_map:
                sub_id_map[sub_name] = f"s{counter}"
                counter += 1

    agent_names = set(ai_subs.keys())

    # Classify functional groups
    group_map: Dict[str, str] = {}
    group_counts: Dict[str, int] = defaultdict(int)
    for name in all_nodes:
        node = node_map.get(name, {})
        base_type = get_base_type(node.get("type", ""))
        cat = categorize(base_type)
        is_terminal = name in terminal_nodes
        group = functional_group(name, base_type, cat, is_terminal)
        if group and name not in agent_names:
            group_map[name] = group
            group_counts[group] += 1

    group_labels = {
        "triggers": "Triggers",
        "error_handling": "Error Handling",
        "output": "Output",
    }

    # ── Node declarations ──────────────────────────────────────────────
    grouped_nodes: Dict[str, List[str]] = defaultdict(list)
    ungrouped: List[str] = []

    for name in sorted(all_nodes, key=lambda n: _sort_key(n, node_map)):
        if name in agent_names:
            ungrouped.append(name)  # AI agents declared in their own subgraphs
            continue
        group = group_map.get(name)
        if group and group_counts[group] >= 2:
            grouped_nodes[group].append(name)
        else:
            ungrouped.append(name)

    # Declare ungrouped nodes (including AI agents that will be in subgraphs)
    for name in ungrouped:
        if name in agent_names:
            continue  # declared inside agent subgraph
        nid = id_map[name]
        node = node_map.get(name, {})
        cat = categorize(get_base_type(node.get("type", "")))
        label = sanitize_label(node.get("name", name))
        lines.append(f"    {nid}[{label}]")

    lines.append("")

    # ── Edge declarations ──────────────────────────────────────────────
    for source in sorted(simplified_graph.keys(), key=lambda n: id_map.get(n, "z")):
        sid = id_map.get(source)
        if not sid:
            continue
        for target, label in simplified_graph[source]:
            tid = id_map.get(target)
            if not tid or tid == sid:
                continue
            if label:
                lines.append(f"    {sid} -->|{label}| {tid}")
            else:
                lines.append(f"    {sid} --> {tid}")

    lines.append("")

    # ── Functional group subgraphs ─────────────────────────────────────
    for group in ["triggers", "error_handling", "output"]:
        if group_counts[group] < 2:
            continue
        members = grouped_nodes.get(group, [])
        if not members:
            continue
        sg_id = f"sg_{group}"
        lines.append(f"    subgraph {sg_id} [{group_labels[group]}]")
        for name in members:
            nid = id_map[name]
            node = node_map.get(name, {})
            cat = categorize(get_base_type(node.get("type", "")))
            label = sanitize_label(node.get("name", name))
            if cat == "trigger":
                lines.append(f"        {nid}([{label}])")
            else:
                lines.append(f"        {nid}[{label}]")
        lines.append("    end")
        lines.append("")

    # ── AI agent subgraphs ─────────────────────────────────────────────
    for agent in sorted(agent_names, key=lambda n: id_map.get(n, "z")):
        if agent not in id_map:
            continue
        aid = id_map[agent]
        agent_node = node_map.get(agent, {})
        agent_label = sanitize_label(agent_node.get("name", agent))
        subs = ai_subs.get(agent, [])

        lines.append(f"    subgraph sg_{aid} [{agent_label}]")
        lines.append(f"        {aid}[[{agent_label}]]")
        for sub_name, conn_type in subs:
            sid = sub_id_map.get(sub_name, id_map.get(sub_name))
            if not sid:
                continue
            sub_node = node_map.get(sub_name, {})
            sub_label = sanitize_label(sub_node.get("name", sub_name))
            conn_short = ai_conn_short(conn_type)
            lines.append(
                f'        {sid}["{sub_label}<br/><small>{conn_short}</small>"]'
            )
            lines.append(f"        {sid} -.-> {aid}")
        lines.append("    end")
        lines.append("")

    return "\n".join(lines)


# ── LLM compression ───────────────────────────────────────────────────────

def compress_with_llm(
    mmd_content: str,
    original_count: int,
    simplified_count: int,
    workflow_name: str,
) -> str:
    """Send the deterministic MMD to gpt-5.6-luna for further compression."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise SyncError("OPENAI_API_KEY not set in environment")

    prompt = (
        f"You are simplifying an n8n workflow diagram for non-technical people "
        f"who are curious about AI.\n\n"
        f"Workflow name: {workflow_name}\n"
        f"Original node count: {original_count}\n"
        f"Current simplified node count: {simplified_count}\n\n"
        f"Here is the current simplified Mermaid flowchart:\n"
        f"```mermaid\n{mmd_content}\n```\n\n"
        f"Please further compress this to at most 20 nodes by:\n"
        f"1. Merging consecutive nodes that perform related functions into a "
        f"single node with a clear, functional label\n"
        f"2. Grouping nodes into functional subgraphs (e.g. 'Input', "
        f"'Validation', 'AI Processing', 'Output', 'Error Handling')\n"
        f"3. Using plain-English labels that describe WHAT each step does, "
        f"not technical node names\n"
        f"4. Preserving the main flow direction and key branching points "
        f"(Yes/No, error paths)\n"
        f"5. Showing error/exception paths as dashed arrows (-.-->)\n"
        f"6. Keeping AI agent subgraphs with their tools/model/memory annotations\n"
        f"7. Using flowchart TD format\n\n"
        f"Output ONLY valid Mermaid flowchart syntax. No explanations, "
        f"no markdown fences."
    )

    body = json.dumps({
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"].strip()
            # Strip markdown fences if present
            content = re.sub(r"^```(?:mermaid)?\s*\n?", "", content)
            content = re.sub(r"\n?```\s*$", "", content)
            return content.strip()
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:300]
        raise SyncError(f"OpenAI API error {exc.code}: {body_text}") from exc
    except Exception as exc:
        raise SyncError(f"LLM compression failed: {exc}") from exc


# ── SVG rendering ─────────────────────────────────────────────────────────

def render_svg(mmd_path: Path, svg_path: Path) -> bool:
    """Render SVG from Mermaid via mmdc. Returns True on success."""
    mmdc = shutil.which("mmdc")
    if mmdc:
        cmd = [mmdc, "-i", str(mmd_path), "-o", str(svg_path), "-b", "transparent"]
    else:
        npx = shutil.which("npx")
        if npx:
            cmd = [npx, "mmdc", "-i", str(mmd_path), "-o", str(svg_path),
                   "-b", "transparent"]
        else:
            print("  Warning: mmdc not found, skipping SVG", file=sys.stderr)
            return False

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=90)
        return True
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "")[:300]
        print(f"  Warning: mmdc failed: {stderr}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("  Warning: mmdc timed out, skipping SVG", file=sys.stderr)
        return False


# ── Path resolution ───────────────────────────────────────────────────────

def resolve_workflow_path(
    args: argparse.Namespace, workspace_root: Path
) -> Path:
    if args.local_path:
        path = Path(args.local_path)
        if not path.is_absolute():
            path = (workspace_root / path).resolve()
        return path

    state = load_state(workspace_root)
    records = state.get("records", {})
    record = find_state_record(records, args.instance, str(args.workflow_id))
    if not record:
        raise SyncError(
            f"Workflow ID {args.workflow_id!r} not found in state.json "
            f"for instance {args.instance!r}."
        )
    local_path = record.get("localPath")
    if not local_path:
        raise SyncError(f"State record for {args.workflow_id!r} has no localPath.")
    return (workspace_root / local_path).resolve()


def resolve_context_dir(
    workspace_root: Path,
    instance: str,
    workflow_path: Path,
    output_dir: Optional[str],
) -> Path:
    if output_dir:
        return Path(output_dir).resolve()
    slug = workflow_path.parent.name
    return workspace_root / "workflow_context" / instance / slug


def find_all_workflows(workspace_root: Path, instance: str) -> List[Path]:
    base = workspace_root / "workflows" / instance
    if not base.exists():
        return []
    return sorted(base.glob("*/workflow.json"))


# ── Main processing ───────────────────────────────────────────────────────

def process_workflow(
    workflow_path: Path,
    workspace_root: Path,
    instance: str,
    no_svg: bool,
    output_dir: Optional[str],
    use_llm: bool = False,
) -> Tuple[bool, str]:
    """Process a single workflow. Returns (success, message)."""
    try:
        payload = json.loads(workflow_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Failed to load JSON: {exc}"

    name = payload.get("name", workflow_path.parent.name)
    nodes = payload.get("nodes", [])
    connections = payload.get("connections", {})

    if not nodes:
        return False, "No nodes found"
    if not connections:
        return False, "No connections found"

    node_map = build_node_map(nodes)

    # Identify AI sub-components
    sub_to_agent, agent_to_subs = identify_ai_subs(connections)
    ai_sub_nodes = set(sub_to_agent.keys())

    # Also catch typed AI sub-nodes that have no main connections
    for node in nodes:
        node_name = node.get("name", "")
        base_type = get_base_type(node.get("type", ""))
        if base_type in AI_SUB_TYPES and node_name not in ai_sub_nodes:
            has_main = node_name in connections and "main" in connections[node_name]
            if not has_main:
                ai_sub_nodes.add(node_name)

    # Identify skip nodes
    skip_nodes: Set[str] = set()
    for node in nodes:
        if get_base_type(node.get("type", "")) in SKIP_TYPES:
            skip_nodes.add(node.get("name", ""))

    # Build main graph (skip bypassed, AI subs removed)
    main_graph = bypass_skip_and_build_main(connections, skip_nodes, ai_sub_nodes)
    if not main_graph:
        return False, "No main connections after simplification"

    # Classify keep vs connector
    keep_set: Set[str] = set()
    connector_set: Set[str] = set()
    for node_name in main_graph:
        node = node_map.get(node_name, {})
        base_type = get_base_type(node.get("type", ""))
        cat = categorize(base_type)
        if cat in ("trigger", "ai_agent", "integration", "response") or base_type in KEEP_TYPES:
            keep_set.add(node_name)
        else:
            connector_set.add(node_name)

    # Also check targets
    for outputs in main_graph.values():
        for out_list in outputs:
            for target in out_list:
                if target in skip_nodes or target in ai_sub_nodes:
                    continue
                node = node_map.get(target, {})
                base_type = get_base_type(node.get("type", ""))
                cat = categorize(base_type)
                if cat in ("trigger", "ai_agent", "integration", "response") or base_type in KEEP_TYPES:
                    keep_set.add(target)
                else:
                    connector_set.add(target)

    # Bypass connectors to build simplified graph
    simplified_graph = bypass_connectors(main_graph, keep_set, connector_set, node_map)

    if not simplified_graph:
        # Fallback: if no keep nodes found, use the raw main graph
        simplified_graph = {s: [(t, None) for out in outs for t in out] for s, outs in main_graph.items() if any(outs)}

    # Generate deterministic Mermaid
    mmd_content = generate_mermaid(simplified_graph, node_map, agent_to_subs, name)

    # LLM compression if requested
    llm_used = False
    simplified_count = len(set(simplified_graph.keys()) | {t for targets in simplified_graph.values() for t, _ in targets})
    if use_llm:
        try:
            print("  Compressing with gpt-5.6-luna (high effort)...", file=sys.stderr)
            compressed = compress_with_llm(mmd_content, len(nodes), simplified_count, name)
            if compressed and "flowchart" in compressed:
                mmd_content = compressed
                llm_used = True
            else:
                print("  Warning: LLM returned empty/invalid output, using deterministic version", file=sys.stderr)
        except SyncError as exc:
            print(f"  Warning: LLM compression failed ({exc}), using deterministic version", file=sys.stderr)

    # Resolve output directory
    context_dir = resolve_context_dir(workspace_root, instance, workflow_path, output_dir)
    context_dir.mkdir(parents=True, exist_ok=True)

    mmd_path = context_dir / "diagram.mmd"
    svg_path = context_dir / "diagram.svg"

    mmd_path.write_text(mmd_content + "\n", encoding="utf-8")

    # Count final nodes
    final_count = simplified_count
    if llm_used:
        # Count nodes in the LLM output
        final_count = len(re.findall(r"\bn\d+\b|\bs\d+\b", mmd_content.split("\n\n")[-1]))

    # Render SVG
    svg_ok = False
    if not no_svg:
        svg_ok = render_svg(mmd_path, svg_path)

    try:
        mmd_rel = mmd_path.relative_to(workspace_root)
    except ValueError:
        mmd_rel = mmd_path
    try:
        svg_rel = svg_path.relative_to(workspace_root)
    except ValueError:
        svg_rel = svg_path

    msg = f"  nodes: {len(nodes)} -> {simplified_count}"
    if llm_used:
        msg += f" -> ~{final_count} (LLM)"
    msg += f"  | mmd: {mmd_rel}"
    if no_svg:
        msg += "  | svg: skipped"
    elif svg_ok:
        msg += f"  | svg: {svg_rel}"
    else:
        msg += "  | svg: failed"

    return True, msg


# ── CLI ───────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate simplified Mermaid diagram from n8n workflow JSON."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-wid", "--workflow-id", help="Workflow ID tracked in state.json")
    group.add_argument("--local-path", help="Path to workflow.json")
    group.add_argument("--all", action="store_true", help="Generate for all workflows")
    parser.add_argument("--instance", default="primary", help="Instance alias (default: primary)")
    parser.add_argument("--workspace-root", help="Explicit workspace root")
    parser.add_argument("--dotenv", default="./secrets/.env.n8n", help=argparse.SUPPRESS)
    parser.add_argument("--no-svg", action="store_true", help="Skip SVG rendering")
    parser.add_argument("--output-dir", help="Custom output directory")
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Further compress the deterministic diagram with gpt-5.6-luna (high effort)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    workspace_root = resolve_workspace_root(args.workspace_root, Path(__file__))

    if args.all and args.output_dir:
        print("Warning: --output-dir ignored with --all", file=sys.stderr)
        args.output_dir = None

    if args.all:
        workflows = find_all_workflows(workspace_root, args.instance)
        if not workflows:
            print(f"No workflows found in workflows/{args.instance}/")
            return 1

        success_count = 0
        fail_count = 0
        for wf_path in workflows:
            wf_name = wf_path.parent.name
            ok, msg = process_workflow(
                wf_path, workspace_root, args.instance,
                args.no_svg, args.output_dir, args.use_llm,
            )
            if ok:
                success_count += 1
                print(f"{wf_name}:")
                print(msg)
            else:
                fail_count += 1
                print(f"{wf_name}: SKIP ({msg})")

        print(f"\nDone: {success_count} generated, {fail_count} skipped.")
        return 0

    # Single workflow
    workflow_path = resolve_workflow_path(args, workspace_root)
    if not workflow_path.exists():
        print(f"Error: Workflow file not found: {workflow_path}", file=sys.stderr)
        return 1

    workflow_name = workflow_path.parent.name
    ok, msg = process_workflow(
        workflow_path, workspace_root, args.instance,
        args.no_svg, args.output_dir, args.use_llm,
    )
    if ok:
        print(f"{workflow_name}:")
        print(msg)
        return 0
    else:
        print(f"Error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
