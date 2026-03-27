# n8n AI Workflow Review Best Practices

Source target for deeper alignment:
- https://github.com/n8n-io/n8n/tree/master/packages/%40n8n/ai-workflow-builder.ee

This local reference is a concise rubric used by `review_workflow.py` and the `n8n-workflow-review` skill.

## Reliability
- Prefer explicit failure paths over silent drops.
- Configure retries and backoff for transient external calls.
- Set explicit timeouts on HTTP-like operations.
- Avoid hidden side effects in code nodes.

## Data Contracts
- Keep stable, explicit JSON shapes between nodes.
- Validate required fields before downstream use.
- Normalize null/empty/default behavior.
- Keep transformation logic deterministic.

## Security
- Use n8n credentials, never embed secrets in node params.
- Limit external endpoints and payload scope.
- Avoid command execution unless unavoidable.

## Maintainability
- Use clear, intention-revealing node names.
- Split long workflows into coherent segments.
- Minimize custom code; document unavoidable code nodes.
- Keep prompts specific, constrained, and testable.

## Operational Readiness
- Add logging or telemetry points at critical boundaries.
- Design for idempotent re-runs where possible.
- Include guardrails for high-cost LLM/tool operations.

## How to update this reference
- Periodically review the upstream `ai-workflow-builder.ee` code and docs.
- Refresh this rubric with concrete patterns seen in upstream updates.
