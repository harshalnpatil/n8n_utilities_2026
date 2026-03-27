---
name: n8n-workflow-review
description: Use this skill when reviewing n8n workflow JSON, explaining workflow behavior, comparing versions, or proposing improvements using local and vendored n8n best-practice references.
---

# n8n Workflow Review

## When to use
- User asks to review one or more n8n `workflow.json` files.
- User asks questions about workflow behavior, quality, risks, or improvement ideas.
- User asks to compare two workflow versions and explain regressions.
- User reports validation failures or suspicious node configuration.
- User asks about expression syntax or Code node behavior while reviewing a workflow.

## Inputs expected
- One or more workflow JSON file paths.
- Optional explicit user question.

## Workflow
1. Run `python scripts/review_workflow.py --workflow <path> [--workflow <path2>] [--question "..."]`.
2. Read `.n8n_sync/review_context.json` and `.n8n_sync/review_report.md`.
3. If needed, read [references/n8n_ai_workflow_builder_best_practices.md](references/n8n_ai_workflow_builder_best_practices.md) first for local baseline guidance.
4. Route deeper analysis to vendored references in `references/`:
   - Workflow architecture/pattern fit:
     [references/n8n_skills__n8n-workflow-patterns__README.md](references/n8n_skills__n8n-workflow-patterns__README.md),
     [references/n8n_skills__n8n-workflow-patterns__webhook_processing.md](references/n8n_skills__n8n-workflow-patterns__webhook_processing.md),
     [references/n8n_skills__n8n-workflow-patterns__http_api_integration.md](references/n8n_skills__n8n-workflow-patterns__http_api_integration.md),
     [references/n8n_skills__n8n-workflow-patterns__database_operations.md](references/n8n_skills__n8n-workflow-patterns__database_operations.md),
     [references/n8n_skills__n8n-workflow-patterns__ai_agent_workflow.md](references/n8n_skills__n8n-workflow-patterns__ai_agent_workflow.md),
     [references/n8n_skills__n8n-workflow-patterns__scheduled_tasks.md](references/n8n_skills__n8n-workflow-patterns__scheduled_tasks.md).
   - Validation errors and false positives:
     [references/n8n_skills__n8n-validation-expert__SKILL.md](references/n8n_skills__n8n-validation-expert__SKILL.md),
     [references/n8n_skills__n8n-validation-expert__ERROR_CATALOG.md](references/n8n_skills__n8n-validation-expert__ERROR_CATALOG.md),
     [references/n8n_skills__n8n-validation-expert__FALSE_POSITIVES.md](references/n8n_skills__n8n-validation-expert__FALSE_POSITIVES.md).
   - Node-level configuration/dependencies:
     [references/n8n_skills__n8n-node-configuration__SKILL.md](references/n8n_skills__n8n-node-configuration__SKILL.md),
     [references/n8n_skills__n8n-node-configuration__DEPENDENCIES.md](references/n8n_skills__n8n-node-configuration__DEPENDENCIES.md),
     [references/n8n_skills__n8n-node-configuration__OPERATION_PATTERNS.md](references/n8n_skills__n8n-node-configuration__OPERATION_PATTERNS.md).
   - Expression issues:
     [references/n8n_skills__n8n-expression-syntax__SKILL.md](references/n8n_skills__n8n-expression-syntax__SKILL.md),
     [references/n8n_skills__n8n-expression-syntax__COMMON_MISTAKES.md](references/n8n_skills__n8n-expression-syntax__COMMON_MISTAKES.md),
     [references/n8n_skills__n8n-expression-syntax__EXAMPLES.md](references/n8n_skills__n8n-expression-syntax__EXAMPLES.md).
   - Code node logic risks:
     [references/n8n_skills__n8n-code-javascript__SKILL.md](references/n8n_skills__n8n-code-javascript__SKILL.md),
     [references/n8n_skills__n8n-code-javascript__ERROR_PATTERNS.md](references/n8n_skills__n8n-code-javascript__ERROR_PATTERNS.md),
     [references/n8n_skills__n8n-code-javascript__COMMON_PATTERNS.md](references/n8n_skills__n8n-code-javascript__COMMON_PATTERNS.md),
     [references/n8n_skills__n8n-code-javascript__DATA_ACCESS.md](references/n8n_skills__n8n-code-javascript__DATA_ACCESS.md),
     [references/n8n_skills__n8n-code-python__SKILL.md](references/n8n_skills__n8n-code-python__SKILL.md),
     [references/n8n_skills__n8n-code-python__ERROR_PATTERNS.md](references/n8n_skills__n8n-code-python__ERROR_PATTERNS.md),
     [references/n8n_skills__n8n-code-python__COMMON_PATTERNS.md](references/n8n_skills__n8n-code-python__COMMON_PATTERNS.md),
     [references/n8n_skills__n8n-code-python__DATA_ACCESS.md](references/n8n_skills__n8n-code-python__DATA_ACCESS.md),
     [references/n8n_skills__n8n-code-python__STANDARD_LIBRARY.md](references/n8n_skills__n8n-code-python__STANDARD_LIBRARY.md).
   - MCP tooling and validation workflow context:
     [references/n8n_skills__n8n-mcp-tools-expert__SKILL.md](references/n8n_skills__n8n-mcp-tools-expert__SKILL.md),
     [references/n8n_skills__n8n-mcp-tools-expert__SEARCH_GUIDE.md](references/n8n_skills__n8n-mcp-tools-expert__SEARCH_GUIDE.md),
     [references/n8n_skills__n8n-mcp-tools-expert__VALIDATION_GUIDE.md](references/n8n_skills__n8n-mcp-tools-expert__VALIDATION_GUIDE.md),
     [references/n8n_skills__n8n-mcp-tools-expert__WORKFLOW_GUIDE.md](references/n8n_skills__n8n-mcp-tools-expert__WORKFLOW_GUIDE.md).
5. Answer with prioritized findings first, then concrete remediations and test steps.

## Review rubric
- Reliability and failure handling: retries, timeout strategy, explicit error branches.
- Data contracts: clear input/output assumptions, schema consistency, null/empty handling.
- Security and credentials: no hardcoded secrets, safe external calls, principle of least privilege.
- Maintainability: node naming clarity, modularity, code node complexity, prompt clarity.
- Operational quality: observability, idempotency, and safe re-runs.
- Architecture quality: correct workflow pattern choice and clean data flow.
- Validation hygiene: classify hard errors vs warnings vs false positives.
- Node-configuration correctness: operation-aware required fields and dependency chains.
- Expression correctness: `{{ }}` usage, `$json` pathing, and node references.
- Code node correctness: return format, mode choice (all-items vs each-item), data access patterns.

## Output format
- Start with high-severity risks (if any), with node names.
- Provide concrete improvement actions.
- State unknowns explicitly when data is missing.
- If relevant, include a short "Reference basis" line naming which vendored reference files informed the recommendation.

## Source provenance
- Vendored references in this skill were imported from `https://github.com/czlonkowski/n8n-skills`.
- Pinned source commit: `d9c287202999481777868c4ce7441ced847350b3`.
- See [references/2026_03_18_n8n_skills_provenance.md](references/2026_03_18_n8n_skills_provenance.md) for imported file inventory.
