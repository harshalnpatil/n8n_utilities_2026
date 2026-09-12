# 2026-09-12 — n8n diagram command

## Goal

Build a `n8n diagram` CLI command that generates a simplified Mermaid flowchart
(.mmd) and rendered SVG (.svg) for every n8n workflow. The diagram should explain
the big picture to a non-technical person who is curious about AI, not reproduce
the n8n canvas node-for-node.

## Design reference — hand-crafted examples

Two existing hand-crafted diagrams guided the target style:

1. **Newsletter-to-podcast article diagram** at
   `G:\My Drive\Documents\articles blogs\2026\2026_03_18_newsletter_to_podcast\newsletter_to_podcast_build_n8n_flowchart.mmd`
   — 155-node workflow compressed to 8 nodes:
   ```
   newsletter emails → Pre-process → AI extracts value → Text to Speech
                     → Store in cloud → Add to Podcast Feed
   ```
   With dashed side-paths for rejected blocks → database → review/improve,
   and TTS failure → email alert.

2. **VibeCon feedback loop diagram** at
   `G:\My Drive\Documents\business_startup_soleprop_SCT\marketing\PR Events\2026_04_07_VibeCon\working_assets\vibecon_feedback_loop.mmd`
   — 6 nodes with `classDef` styling, accept/reject branching, and a dashed
   feedback arrow for "next run".

Both examples share: very few nodes (6–8), functional labels (not technical
node names), clean linear flow with dashed lines for feedback/error paths,
and `flowchart LR` layout.

## Implementation

### v1 — conservative chain-collapsing (first pass)

- `scripts/n8n_diagram.py` with union-find chain collapsing.
- Only nodes with exactly 1 input and 1 output were chainable.
- Same-category consecutive chainable nodes were merged.
- Skip nodes (stickyNote, timeSaved, noOp, executionData) removed.
- AI sub-components (model, memory, tools) detached into agent subgraphs.

### v1 results (deterministic only)

| Workflow | Original nodes | v1 simplified |
|---|---|---|
| Read Google Tasks | 5 | 4 |
| Finances Agent | 24 | 14 |
| Safe Workout Research | 20 | 18 |
| Appreciation Bot | 48 | 37 |
| Newsletter to Podcast | 155 | 112 |
| Zoho CRM Agent | 117 | 66 |

v1 was too conservative — large workflows still had 40–112 nodes.

### v2 — aggressive keep/bypass (rewrite)

User feedback: "way way more aggressive … stuff more than 25 nodes are going
to be so hard to read."

Key change: classify every node as either **keep** or **connector**:
- **Keep**: triggers, AI agents, integrations (telegram, gmail, google*,
  supabase, notion, trello, HTTP, Tavily, etc.), responses, executeWorkflow,
  stopAndError.
- **Connector**: everything else — IF, Switch, filter, set, code, sort, limit,
  merge, aggregate, splitOut, splitInBatches, wait, editImage, html, markdown,
  summarize, convertToFile, extractFromFile.

Connectors are bypassed via BFS. The BFS tracks the **first** branching label
encountered (Yes/No from IF, Rule N/Fallback from Switch, Loop/Done from
splitInBatches, Resume/Error from wait) and propagates it to the edge between
the two keep nodes. Only the first IF's label is kept (not the last) to avoid
confusing multi-IF label chains.

Functional subgraph grouping added:
- Triggers (2+) → "Triggers" subgraph
- Error-related nodes → "Error Handling" subgraph
- Terminal output integrations → "Output" subgraph
- AI agents → individual subgraphs with model/memory/tools

Better Switch labels: extracts `rightValue` from rule conditions when available
(e.g. "email" instead of "Rule 1"), falls back to "Rule N"/"Fallback".

### v2 results (deterministic)

| Workflow | Original nodes | v1 | v2 |
|---|---|---|---|
| Read Google Tasks | 5 | 4 | **2** |
| Finances Agent | 24 | 14 | **7** |
| Safe Workout Research | 20 | 18 | **7** |
| Appreciation Bot | 48 | 37 | **15** |
| Newsletter to Podcast | 155 | 112 | **47** |
| Zoho CRM Agent | 117 | 66 | **33** |
| Build Google Drive MCP | 17 | 8 | **3** |
| Auto-Schedule Delivery | 21 | 10 | **5** |

Full batch run: 91 generated, 15 skipped (MCP tool-server workflows with only
`ai_tool` connections and no main flow).

### `--use-llm` mode

After the deterministic pass, sends the simplified MMD to `gpt-5.6-luna`
(high effort) via `OPENAI_API_KEY` for further compression. The prompt asks
the LLM to:
- Merge consecutive related nodes into single functional steps
- Create functional subgraphs (Input, Validation, AI Processing, Output, etc.)
- Use plain-English labels describing WHAT each step does
- Preserve branching and error paths (dashed arrows)
- Keep AI agent subgraphs with model/memory/tools
- Target ≤20 nodes

Falls back to the deterministic version if the API call fails or returns
invalid output.

### LLM mode test results

Tested on newsletter-to-podcast (155 nodes):

| Pass | Node count |
|---|---|
| Original | 155 |
| v1 deterministic | 112 |
| v2 deterministic | 47 |
| v2 + `--use-llm` | **~17** |

The LLM output organised the workflow into 6 functional subgraphs:
- Input: trigger → read webpage/download
- AI Processing: extract/validate/classify (with model + tool sub-components)
- Queue and Status: add to queue → check readiness
- Audio Creation: TTS → check success
- Output: upload transcript → publish → notify
- Error Handling: 4 error nodes for different failure modes

This is very close to the hand-crafted example (8 nodes, linear flow with
dashed error paths).

## Files changed

- `n8n_extract_sync_2026_03_11/scripts/n8n_diagram.py` — new script (~450 lines)
- `n8n_extract_sync_2026_03_11/n8n.ps1` — added `diagram` subcommand
- `n8n_extract_sync_2026_03_11/CHEATSHEET.md` — documented the command

## Usage

```powershell
n8n diagram --workflow-id <id>                    # one workflow, MMD + SVG
n8n diagram --local-path workflows/primary/<slug>/workflow.json
n8n diagram --all --no-svg                        # all 91 workflows, MMD only
n8n diagram --workflow-id <id> --use-llm          # deterministic + GPT compression
```

Output: `workflow_context/<instance>/<slug>/diagram.mmd` and `diagram.svg`.

## Verification

- Tested on 5 workflows of varying sizes (5, 20, 24, 48, 155 nodes).
- Full `--all` batch run: 91 generated, 15 skipped (MCP tool servers).
- LLM mode tested on newsletter-to-podcast: 155 → 47 → ~17 nodes.
- SVGs rendered successfully via `mmdc` (v11.12.0) for all test workflows.
- Edge label propagation fixed: first IF's label is preserved, not the last.

## Outcome

The deterministic mode produces clean diagrams for small-to-medium workflows
(2–15 nodes). For large workflows (40+ nodes after deterministic pass), the
`--use-llm` mode brings them down to ~15–20 nodes with functional groupings
and plain-English labels, matching the quality of the hand-crafted examples.
