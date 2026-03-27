#!/usr/bin/env python3
"""Manual workflow review context and baseline report generator."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from n8n_common import load_json, repo_root_from_script, utc_now_iso, write_json


RISKY_NODE_TYPES = {
    "n8n-nodes-base.code": "Code node can hide logic and runtime failures; validate error handling and input assumptions.",
    "n8n-nodes-base.httpRequest": "HTTP request nodes should define timeouts, retries, and explicit error behavior.",
    "n8n-nodes-base.executeCommand": "Execute Command can create host-level risk; limit scope and sanitize inputs.",
}

DEFAULT_BEST_PRACTICES_REL = "skills/n8n-workflow-review/references/n8n_ai_workflow_builder_best_practices.md"
EXTERNAL_BEST_PRACTICES_WSL = (
    "/mnt/c/Users/harsh/Documents/n8n_workflows_2026_01_25/skills/"
    "n8n-workflow-review/references/n8n_ai_workflow_builder_best_practices.md"
)
EXTERNAL_BEST_PRACTICES_WINDOWS = (
    r"C:\Users\harsh\Documents\n8n_workflows_2026_01_25\skills"
    r"\n8n-workflow-review\references\n8n_ai_workflow_builder_best_practices.md"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate manual review context for n8n workflow files.")
    parser.add_argument("--workflow", action="append", required=True, help="Path to workflow.json (repeatable)")
    parser.add_argument("--question", default="", help="Optional user question to include in context")
    parser.add_argument(
        "--best-practices",
        default=DEFAULT_BEST_PRACTICES_REL,
        help="Best-practices reference path. If the repo-local file is missing, the script also checks the private external skills workspace.",
    )
    parser.add_argument(
        "--output-json",
        default=".n8n_sync/review_context.json",
        help="Output JSON path relative to repo root",
    )
    parser.add_argument(
        "--output-md",
        default=".n8n_sync/review_report.md",
        help="Output markdown report path relative to repo root",
    )
    return parser.parse_args()


def resolve_best_practices_path(repo_root: Path, best_practices: str) -> Path:
    candidates: List[Path] = []

    direct = Path(best_practices)
    if direct.is_absolute():
        candidates.append(direct)
    else:
        candidates.append((repo_root / direct).resolve())

    if best_practices == DEFAULT_BEST_PRACTICES_REL:
        candidates.append(Path(EXTERNAL_BEST_PRACTICES_WSL))
        if os.name == "nt":
            candidates.append(Path(EXTERNAL_BEST_PRACTICES_WINDOWS))

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def summarize_workflow(path: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    nodes = payload.get("nodes", []) if isinstance(payload.get("nodes"), list) else []
    connections = payload.get("connections", {}) if isinstance(payload.get("connections"), dict) else {}

    type_counts: Dict[str, int] = {}
    warnings: List[str] = []

    for node in nodes:
        node_type = str(node.get("type", "unknown"))
        type_counts[node_type] = type_counts.get(node_type, 0) + 1
        for risky, message in RISKY_NODE_TYPES.items():
            if node_type.startswith(risky):
                warnings.append(f"{node.get('name', 'Unnamed node')}: {message}")

    orphan_nodes = [n.get("name", "Unnamed") for n in nodes if n.get("name") not in connections]

    return {
        "path": str(path),
        "workflowId": payload.get("id"),
        "name": payload.get("name"),
        "active": payload.get("active"),
        "nodeCount": len(nodes),
        "connectionRoots": len(connections),
        "nodeTypeCounts": type_counts,
        "orphanNodeNames": orphan_nodes,
        "warnings": warnings,
    }


def build_markdown(question: str, summaries: List[Dict[str, Any]], best_practices_path: str) -> str:
    lines: List[str] = []
    lines.append("# n8n Workflow Review Report")
    lines.append("")
    lines.append(f"Generated at: `{utc_now_iso()}`")
    lines.append(f"Best-practices reference: `{best_practices_path}`")
    if question:
        lines.append(f"Question: {question}")
    lines.append("")

    for summary in summaries:
        lines.append(f"## {summary.get('name') or summary.get('path')}")
        lines.append(f"- Path: `{summary['path']}`")
        lines.append(f"- Workflow ID: `{summary.get('workflowId')}`")
        lines.append(f"- Active: `{summary.get('active')}`")
        lines.append(f"- Node count: `{summary.get('nodeCount')}`")
        lines.append(f"- Connection roots: `{summary.get('connectionRoots')}`")

        node_types = summary.get("nodeTypeCounts", {})
        if node_types:
            compact = ", ".join(f"`{k}`={v}" for k, v in sorted(node_types.items(), key=lambda x: x[0]))
            lines.append(f"- Node types: {compact}")

        orphan_nodes = summary.get("orphanNodeNames", [])
        if orphan_nodes:
            lines.append("- Potential orphan nodes: " + ", ".join(f"`{name}`" for name in orphan_nodes[:10]))

        warnings = summary.get("warnings", [])
        if warnings:
            lines.append("- Risk signals:")
            for warning in warnings[:10]:
                lines.append(f"  - {warning}")

        lines.append("")

    lines.append("## Suggested Next Prompt")
    lines.append(
        "Use the `n8n-workflow-review` skill and produce node-specific improvements prioritized by reliability, "
        "error handling, data contracts, and maintainability."
    )
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    repo_root = repo_root_from_script(Path(__file__))

    summaries: List[Dict[str, Any]] = []
    workflows: List[Dict[str, Any]] = []

    for workflow_arg in args.workflow:
        wf_path = Path(workflow_arg)
        if not wf_path.is_absolute():
            wf_path = (repo_root / wf_path).resolve()

        payload = load_json(wf_path, fallback={})
        if not payload or not isinstance(payload, dict):
            raise SystemExit(f"Invalid workflow JSON: {wf_path}")

        workflows.append({"path": str(wf_path), "payload": payload})
        summaries.append(summarize_workflow(wf_path, payload))

    best_practices = args.best_practices
    bp_path = resolve_best_practices_path(repo_root, best_practices)
    bp_excerpt = ""
    if bp_path.exists():
        raw_text = bp_path.read_text(encoding="utf-8")
        bp_excerpt = re.sub(r"\s+", " ", raw_text).strip()[:3500]

    context = {
        "generatedAtUtc": utc_now_iso(),
        "question": args.question,
        "bestPracticesPath": str(bp_path),
        "bestPracticesExcerpt": bp_excerpt,
        "workflows": workflows,
        "summaries": summaries,
    }

    out_json = Path(args.output_json)
    if not out_json.is_absolute():
        out_json = repo_root / out_json
    write_json(out_json, context)

    out_md = Path(args.output_md)
    if not out_md.is_absolute():
        out_md = repo_root / out_md
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(build_markdown(args.question, summaries, str(bp_path)), encoding="utf-8")

    print(f"wrote json: {out_json}")
    print(f"wrote report: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
