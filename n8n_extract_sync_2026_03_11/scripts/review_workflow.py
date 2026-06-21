#!/usr/bin/env python3
"""Deterministic workflow review and quality gate for n8n workflow files."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from n8n_common import (
    SyncError,
    get_instances,
    get_workflow,
    load_config,
    load_json,
    resolve_workspace_root,
    utc_now_iso,
    write_json,
)
from n8n_executions import list_executions


DEFAULT_BEST_PRACTICES_REL = "skills/n8n-workflow-review/references/n8n_ai_workflow_builder_best_practices.md"
EXTERNAL_BEST_PRACTICES_WSL = (
    "/mnt/c/Users/harsh/Documents/n8n_workflows_2026_01_25/skills/"
    "n8n-workflow-review/references/n8n_ai_workflow_builder_best_practices.md"
)
EXTERNAL_BEST_PRACTICES_WINDOWS = (
    r"C:\Users\harsh\Documents\n8n_workflows_2026_01_25\skills"
    r"\n8n-workflow-review\references\n8n_ai_workflow_builder_best_practices.md"
)

CODE_PATTERN_RULES: Tuple[Tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "code-process-env",
        "error",
        re.compile(r"\bprocess\.env\b"),
        "Code node reads `process.env`, which is a known runtime-invalid pattern in this workspace.",
    ),
)

EXPRESSION_NODE_REF_PATTERN = re.compile(r"""\$\(\s*(['"])([^'"]+?)\1\s*\)""")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate deterministic review context for n8n workflow files.")
    parser.add_argument(
        "workflow_paths",
        nargs="*",
        help="Path(s) to workflow.json, relative to workspace root or absolute",
    )
    parser.add_argument(
        "-w",
        "--workflow",
        action="append",
        default=[],
        help="Path to workflow.json, relative to workspace root or absolute (repeatable)",
    )
    parser.add_argument("-q", "--question", default="", help="Optional user question to include in context")
    parser.add_argument(
        "--workspace-root",
        help="Explicit workspace root. Defaults to the nearest parent with workflows/ and .n8n_sync/.",
    )
    parser.add_argument(
        "-bp",
        "--best-practices",
        default=DEFAULT_BEST_PRACTICES_REL,
        help="Best-practices reference path. If the repo-local file is missing, the script also checks the private external skills workspace.",
    )
    parser.add_argument(
        "-oj",
        "--output-json",
        default=".n8n_sync/review_context.json",
        help="Output JSON path relative to workspace root",
    )
    parser.add_argument(
        "-om",
        "--output-md",
        default=".n8n_sync/review_report.md",
        help="Output markdown report path relative to workspace root",
    )
    parser.add_argument(
        "--quality-gate",
        action="store_true",
        help="Return a nonzero exit code when error-level review findings are present.",
    )
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="If live workflow context is available, scope node-level findings to changed nodes only.",
    )
    parser.add_argument(
        "--include-executions",
        action="store_true",
        help="Include recent execution summary for the workflow. Requires workflow ID and instance access.",
    )
    parser.add_argument("--workflow-id", help="Workflow ID for live comparison and execution lookup.")
    parser.add_argument("--instance", default="primary", help="n8n instance alias for live comparison (default: primary).")
    parser.add_argument("--dotenv", default="./secrets/.env.n8n", help="Path to .env.n8n file for live comparison.")
    parser.add_argument("--execution-limit", type=int, default=10, help="Recent execution count to inspect when --include-executions is set.")
    return parser.parse_args(argv)


def resolve_best_practices_path(workspace_root: Path, best_practices: str) -> Path:
    candidates: List[Path] = []

    direct = Path(best_practices)
    if direct.is_absolute():
        candidates.append(direct)
    else:
        candidates.append((workspace_root / direct).resolve())

    if best_practices == DEFAULT_BEST_PRACTICES_REL:
        candidates.append(Path(EXTERNAL_BEST_PRACTICES_WSL))
        if os.name == "nt":
            candidates.append(Path(EXTERNAL_BEST_PRACTICES_WINDOWS))

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def _node_name(node: Dict[str, Any]) -> str:
    return str(node.get("name") or "Unnamed node")


def _node_key(node: Dict[str, Any]) -> str:
    return str(node.get("id") or node.get("name") or "")


def _node_type(node: Dict[str, Any]) -> str:
    return str(node.get("type") or "unknown")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _make_finding(
    severity: str,
    code: str,
    message: str,
    *,
    node: Optional[Dict[str, Any]] = None,
    node_name: Optional[str] = None,
    node_type: Optional[str] = None,
    path: str = "",
) -> Dict[str, Any]:
    finding: Dict[str, Any] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if node is not None:
        finding["nodeName"] = _node_name(node)
        finding["nodeType"] = _node_type(node)
    elif node_name:
        finding["nodeName"] = node_name
        if node_type:
            finding["nodeType"] = node_type
    if path:
        finding["path"] = path
    return finding


def _severity_rank(severity: str) -> int:
    return {"error": 0, "info": 1}.get(severity, 9)


def _status_from_findings(findings: Iterable[Dict[str, Any]]) -> str:
    severities = {str(f.get("severity")) for f in findings}
    if "error" in severities:
        return "fail"
    return "pass"


def _collect_string_values(value: Any, path: str = "") -> List[Tuple[str, str]]:
    results: List[Tuple[str, str]] = []
    if isinstance(value, str):
        results.append((path or "/", value))
    elif isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}/{key}" if path else f"/{key}"
            results.extend(_collect_string_values(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}/{index}" if path else f"/{index}"
            results.extend(_collect_string_values(child, child_path))
    return results


def _strip_code_comments(source: str) -> str:
    without_block = re.sub(r"/\*[\s\S]*?\*/", "", source)
    without_line = re.sub(r"^\s*//.*$", "", without_block, flags=re.MULTILINE)
    without_hash = re.sub(r"^\s*#.*$", "", without_line, flags=re.MULTILINE)
    return without_hash


def _connection_entries(connections: Any) -> List[Tuple[str, str, str]]:
    entries: List[Tuple[str, str, str]] = []
    if not isinstance(connections, dict):
        return entries
    for source, by_type in connections.items():
        if not isinstance(by_type, dict):
            continue
        for connection_type, targets_by_index in by_type.items():
            if not isinstance(targets_by_index, list):
                continue
            for source_index, targets in enumerate(targets_by_index):
                if not isinstance(targets, list):
                    continue
                for target_index, target in enumerate(targets):
                    if not isinstance(target, dict):
                        continue
                    node_name = str(target.get("node") or "")
                    if node_name:
                        path = f"/connections/{source}/{connection_type}/{source_index}/{target_index}"
                        entries.append((str(source), node_name, path))
    return entries


def _connection_edges(connections: Any) -> List[Tuple[str, str]]:
    return [(source, target) for source, target, _ in _connection_entries(connections)]


def _build_graph(payload: Dict[str, Any]) -> Tuple[Set[str], Dict[str, Set[str]], Dict[str, Set[str]]]:
    node_names = {
        _node_name(node)
        for node in payload.get("nodes", [])
        if isinstance(node, dict)
    }
    outgoing = {name: set() for name in node_names}
    incoming = {name: set() for name in node_names}
    for source, target in _connection_edges(payload.get("connections")):
        if source not in node_names or target not in node_names:
            continue
        outgoing[source].add(target)
        incoming[target].add(source)
    return node_names, outgoing, incoming


def _find_broken_expression_refs(node: Dict[str, Any], declared_names: Set[str]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    parameters = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}
    string_fields = _collect_string_values(parameters)
    code_fields = {
        path
        for path, _text in _collect_string_values(parameters)
        if path.endswith("/jsCode") or path.endswith("/pythonCode") or path.endswith("/code")
    }

    for path, text in string_fields:
        if path in code_fields:
            continue
        for match in EXPRESSION_NODE_REF_PATTERN.finditer(text):
            ref_name = match.group(2).strip()
            if not ref_name or ref_name in declared_names:
                continue
            findings.append(
                _make_finding(
                    "error",
                    "stale-node-reference",
                    f"Expression references missing node `{ref_name}`.",
                    node=node,
                    path=path,
                )
            )
    return findings


def _find_stale_connection_findings(
    payload: Dict[str, Any],
    reviewable_node_names: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    declared_names = {
        _node_name(node)
        for node in payload.get("nodes", [])
        if isinstance(node, dict)
    }
    for source, target, path in _connection_entries(payload.get("connections")):
        if reviewable_node_names is not None and source not in reviewable_node_names and target not in reviewable_node_names:
            continue
        missing_parts = []
        if source not in declared_names:
            missing_parts.append(f"source node `{source}`")
        if target not in declared_names:
            missing_parts.append(f"target node `{target}`")
        if missing_parts:
            findings.append(
                _make_finding(
                    "error",
                    "stale-connection-node-reference",
                    "Connection references missing " + " and ".join(missing_parts) + ".",
                    path=path,
                )
            )
    return findings


def _find_code_findings(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    parameters = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}
    string_fields = _collect_string_values(parameters)
    code_fields = [(path, text) for path, text in string_fields if path.endswith("/jsCode") or path.endswith("/pythonCode") or path.endswith("/code")]
    if not code_fields:
        code_fields = string_fields

    for path, source in code_fields:
        normalized_source = _strip_code_comments(source)
        for code, severity, pattern, message in CODE_PATTERN_RULES:
            if pattern.search(normalized_source):
                findings.append(_make_finding(severity, code, message, node=node, path=path))
    return findings


def _remote_context(
    workspace_root: Path,
    args: argparse.Namespace,
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    explicit_requested = bool(args.workflow_id or args.changed_only or args.include_executions)
    workflow_id = str(args.workflow_id or payload.get("id") or "").strip()
    if not workflow_id:
        return None, None, None

    try:
        config = load_config(workspace_root, args.dotenv)
        instances = get_instances(config)
        instance = instances.get(args.instance)
        if not instance:
            if explicit_requested:
                return None, workflow_id, f"Instance `{args.instance}` is not configured."
            return None, workflow_id, None
        return get_workflow(instance, workflow_id), workflow_id, None
    except SyncError as exc:
        if explicit_requested:
            return None, workflow_id, str(exc)
        return None, workflow_id, None


def _changed_node_names(before: Dict[str, Any], after: Dict[str, Any]) -> Set[str]:
    def index_nodes(nodes: Any) -> Dict[str, Dict[str, Any]]:
        indexed: Dict[str, Dict[str, Any]] = {}
        if not isinstance(nodes, list):
            return indexed
        for node in nodes:
            if not isinstance(node, dict):
                continue
            key = _node_key(node)
            if key:
                indexed[key] = node
        return indexed

    before_nodes = index_nodes(before.get("nodes"))
    after_nodes = index_nodes(after.get("nodes"))
    changed: Set[str] = set()

    for key in sorted(set(before_nodes) | set(after_nodes)):
        if key not in before_nodes:
            changed.add(_node_name(after_nodes[key]))
        elif key not in after_nodes:
            changed.add(_node_name(before_nodes[key]))
        elif _canonical_json(before_nodes[key]) != _canonical_json(after_nodes[key]):
            changed.add(_node_name(after_nodes[key]))

    before_edges = set(_connection_edges(before.get("connections")))
    after_edges = set(_connection_edges(after.get("connections")))
    for source, target in before_edges ^ after_edges:
        changed.add(source)
        changed.add(target)

    return {name for name in changed if name}


def _trace_validation_findings(
    payload: Dict[str, Any],
    changed_names: Set[str],
) -> List[Dict[str, Any]]:
    return []


def _execution_summary(
    workspace_root: Path,
    args: argparse.Namespace,
    workflow_id: str,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    findings: List[Dict[str, Any]] = []
    try:
        config = load_config(workspace_root, args.dotenv)
        instances = get_instances(config)
        instance = instances.get(args.instance)
        if not instance:
            raise SyncError(f"Instance `{args.instance}` is not configured.")
        executions = list_executions(instance, workflow_id=workflow_id, limit=args.execution_limit)
    except SyncError as exc:
        findings.append(
            _make_finding(
                "info",
                "execution-summary-unavailable",
                f"Could not load recent executions: {exc}",
            )
        )
        return None, findings

    status_counts: Dict[str, int] = {}
    for execution in executions:
        status = str(execution.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    summary = {
        "count": len(executions),
        "statusCounts": status_counts,
        "latestExecutionIds": [str(item.get("id")) for item in executions[:5]],
    }
    return summary, findings


def summarize_workflow(
    path: Path,
    payload: Dict[str, Any],
    workspace_root: Path,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    nodes = payload.get("nodes", []) if isinstance(payload.get("nodes"), list) else []
    connections = payload.get("connections", {}) if isinstance(payload.get("connections"), dict) else {}

    type_counts: Dict[str, int] = {}
    findings: List[Dict[str, Any]] = []

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = _node_type(node)
        type_counts[node_type] = type_counts.get(node_type, 0) + 1

    remote_payload, effective_workflow_id, remote_error = _remote_context(workspace_root, args, payload)
    changed_names: Set[str] = set()
    scope_note = "full"
    if remote_payload is not None:
        changed_names = _changed_node_names(remote_payload, payload)
        if args.changed_only:
            scope_note = "changed-only"
    elif args.changed_only:
        findings.append(
            _make_finding(
                "info",
                "changed-only-unavailable",
                "Changed-only review requested, but live workflow comparison was unavailable. Reviewed the full workflow instead.",
            )
        )
    if remote_error:
        findings.append(
            _make_finding(
                "info",
                "live-comparison-unavailable",
                f"Live workflow comparison unavailable: {remote_error}",
            )
        )

    declared_node_names = {
        _node_name(node)
        for node in nodes
        if isinstance(node, dict)
    }
    reviewable_node_names = changed_names if scope_note == "changed-only" else declared_node_names

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_name = _node_name(node)
        if node_name not in reviewable_node_names:
            continue
        node_type = _node_type(node)
        if node_type == "n8n-nodes-base.code":
            findings.extend(_find_code_findings(node))
        findings.extend(_find_broken_expression_refs(node, declared_node_names))

    connection_scope = reviewable_node_names if scope_note == "changed-only" else None
    findings.extend(_find_stale_connection_findings(payload, connection_scope))

    if remote_payload is not None:
        findings.extend(_trace_validation_findings(payload, changed_names))

    execution_summary = None
    if args.include_executions:
        if not effective_workflow_id:
            findings.append(
                _make_finding(
                    "info",
                    "execution-summary-unavailable",
                    "Execution summary requested, but no workflow ID was available.",
                )
            )
        else:
            execution_summary, execution_findings = _execution_summary(workspace_root, args, effective_workflow_id)
            findings.extend(execution_findings)

    findings = sorted(
        findings,
        key=lambda item: (
            _severity_rank(str(item.get("severity"))),
            str(item.get("nodeName") or ""),
            str(item.get("code") or ""),
        ),
    )

    quality_gate_status = _status_from_findings(findings)
    severity_counts: Dict[str, int] = {"error": 0, "info": 0}
    for finding in findings:
        severity = str(finding.get("severity") or "info")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

    return {
        "path": str(path),
        "workflowId": effective_workflow_id or payload.get("id"),
        "name": payload.get("name"),
        "active": payload.get("active"),
        "nodeCount": len(nodes),
        "connectionRoots": len(connections),
        "nodeTypeCounts": type_counts,
        "orphanNodeNames": [],
        "findings": findings,
        "severityCounts": severity_counts,
        "qualityGateStatus": quality_gate_status,
        "reviewScope": scope_note,
        "changedNodeNames": sorted(changed_names),
        "executionSummary": execution_summary,
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

    blocking = [
        (summary, finding)
        for summary in summaries
        for finding in summary.get("findings", [])
        if finding.get("severity") == "error"
    ]
    lines.append("## Blocking Findings")
    if blocking:
        for summary, finding in blocking:
            node_part = f" node `{finding.get('nodeName')}`" if finding.get("nodeName") else ""
            lines.append(f"- `{summary.get('name') or summary.get('path')}`{node_part}: {finding.get('message')}")
    else:
        lines.append("- None.")
    lines.append("")

    advisory = [
        (summary, finding)
        for summary in summaries
        for finding in summary.get("findings", [])
        if finding.get("severity") != "error"
    ]
    lines.append("## Advisory Findings")
    if advisory:
        for summary, finding in advisory:
            node_part = f" node `{finding.get('nodeName')}`" if finding.get("nodeName") else ""
            lines.append(f"- `{summary.get('name') or summary.get('path')}`{node_part}: {finding.get('message')}")
    else:
        lines.append("- None.")
    lines.append("")

    for summary in summaries:
        lines.append(f"## {summary.get('name') or summary.get('path')}")
        lines.append(f"- Path: `{summary['path']}`")
        lines.append(f"- Workflow ID: `{summary.get('workflowId')}`")
        lines.append(f"- Active: `{summary.get('active')}`")
        lines.append(f"- Node count: `{summary.get('nodeCount')}`")
        lines.append(f"- Connection roots: `{summary.get('connectionRoots')}`")
        lines.append(f"- Quality gate status: `{summary.get('qualityGateStatus')}`")
        lines.append(f"- Review scope: `{summary.get('reviewScope')}`")

        severity_counts = summary.get("severityCounts", {})
        lines.append(
            "- Findings: "
            f"errors={severity_counts.get('error', 0)}, "
            f"info={severity_counts.get('info', 0)}"
        )

        node_types = summary.get("nodeTypeCounts", {})
        if node_types:
            compact = ", ".join(f"`{k}`={v}" for k, v in sorted(node_types.items(), key=lambda x: x[0]))
            lines.append(f"- Node types: {compact}")

        changed_node_names = summary.get("changedNodeNames", [])
        if changed_node_names:
            preview = ", ".join(f"`{name}`" for name in changed_node_names[:12])
            lines.append(f"- Changed nodes vs live: {preview}")

        execution_summary = summary.get("executionSummary")
        if execution_summary:
            compact = ", ".join(
                f"{status}={count}" for status, count in sorted(execution_summary.get("statusCounts", {}).items())
            )
            lines.append(f"- Recent executions: {execution_summary.get('count', 0)} reviewed ({compact})")

        findings = summary.get("findings", [])
        blocking_findings = [finding for finding in findings if finding.get("severity") == "error"]
        if blocking_findings:
            lines.append("- Blocking findings:")
            for finding in blocking_findings[:20]:
                node_label = f" `{finding.get('nodeName')}`" if finding.get("nodeName") else ""
                lines.append(f"  - [{finding.get('severity')}] {finding.get('code')}{node_label}: {finding.get('message')}")
        advisory_findings = [finding for finding in findings if finding.get("severity") != "error"]
        if advisory_findings:
            lines.append("- Advisory findings:")
            for finding in advisory_findings[:20]:
                node_label = f" `{finding.get('nodeName')}`" if finding.get("nodeName") else ""
                lines.append(f"  - [{finding.get('severity')}] {finding.get('code')}{node_label}: {finding.get('message')}")

        lines.append("")

    lines.append("## Suggested Next Prompt")
    lines.append(
        "Use the `n8n-workflow-review` skill and produce node-specific improvements prioritized by reliability, "
        "runtime safety, error handling, data contracts, and maintainability."
    )
    lines.append("")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    workspace_root = resolve_workspace_root(args.workspace_root, Path(__file__))

    workflow_args = list(args.workflow_paths) + list(args.workflow)
    if not workflow_args:
        raise SystemExit("At least one workflow path is required: use positional path, -w, or --workflow.")

    summaries: List[Dict[str, Any]] = []
    workflows: List[Dict[str, Any]] = []

    for workflow_arg in workflow_args:
        wf_path = Path(workflow_arg)
        if not wf_path.is_absolute():
            wf_path = (workspace_root / wf_path).resolve()

        if not wf_path.exists():
            raise SystemExit(f"Workflow file does not exist: {wf_path}")

        payload = load_json(wf_path, fallback={})
        if not payload or not isinstance(payload, dict):
            raise SystemExit(f"Invalid workflow JSON: {wf_path}")

        workflows.append({"path": str(wf_path), "payload": payload})
        summaries.append(summarize_workflow(wf_path, payload, workspace_root, args))

    best_practices = args.best_practices
    bp_path = resolve_best_practices_path(workspace_root, best_practices)
    bp_excerpt = ""
    if bp_path.exists():
        raw_text = bp_path.read_text(encoding="utf-8")
        bp_excerpt = re.sub(r"\s+", " ", raw_text).strip()[:3500]

    overall_quality_gate_status = _status_from_findings(
        finding
        for summary in summaries
        for finding in summary.get("findings", [])
    )

    context = {
        "generatedAtUtc": utc_now_iso(),
        "question": args.question,
        "bestPracticesPath": str(bp_path),
        "bestPracticesExcerpt": bp_excerpt,
        "qualityGateStatus": overall_quality_gate_status,
        "qualityGateEnabled": bool(args.quality_gate),
        "changedOnly": bool(args.changed_only),
        "includeExecutions": bool(args.include_executions),
        "workflows": workflows,
        "summaries": summaries,
    }

    out_json = Path(args.output_json)
    if not out_json.is_absolute():
        out_json = (workspace_root / out_json).resolve()
    write_json(out_json, context)

    out_md = Path(args.output_md)
    if not out_md.is_absolute():
        out_md = (workspace_root / out_md).resolve()
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(build_markdown(args.question, summaries, str(bp_path)), encoding="utf-8")

    print(f"wrote json: {out_json}")
    print(f"wrote report: {out_md}")

    if args.quality_gate and overall_quality_gate_status == "fail":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
