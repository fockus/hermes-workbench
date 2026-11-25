---
name: research-knowledge-workflows
description: "Use when doing research and knowledge workflows: paper discovery, RSS/blog monitoring, LLM wiki construction, prediction-market queries, and academic paper writing."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [research, arxiv, rss, papers, literature, polymarket, knowledge-base]
    related_skills: []
---

# Research Knowledge Workflows

## Overview

This umbrella covers research discovery, monitoring, synthesis, domain reconnaissance, prediction-market data gathering, and academic paper-writing support.

## When to Use

- Search arXiv by keyword, author, category, or paper id.
- Monitor blogs or RSS/Atom feeds.
- Build/query a local LLM wiki or markdown knowledge base.
- Query Polymarket markets, orderbooks, prices, or history.
- Draft, revise, or prepare ML research papers for submission.

## Workflow

1. Define the research question and source class.
2. Fetch primary data with tools; avoid unsourced claims.
3. Normalize citations, URLs, IDs, and timestamps.
4. Synthesize findings into the requested format.
5. Preserve source handles for verification.

## Sub-workflows

### Paper discovery
Use arXiv IDs and metadata; read abstracts before summarizing claims.

### Monitoring
For blogs/RSS, distinguish new items from previously seen items and include feed/source URLs.

### Knowledge bases
1. Determine the wiki scope before searching or writing: use the explicitly bound project's wiki or the profile's general wiki for a confirmed non-project chat. Do not use the general wiki as a fallback when project identity is unknown.
2. When producing a reusable research result, document or report, save it to the established wiki for that scope without waiting for a separate save request. Preserve original files and cite them from the curated page. Give the user the canonical path; do not turn a read-only lookup into a write or copy project work to General. Memory Bank owns plans, decisions, executable workflow gates and resume checkpoints: link the artifact version there rather than maintaining another report. A saved report is not evidence that a pipeline stage passed.
   - Match the question to the source: a curated conclusion, a quoted passage, previous discussion or the next task need different evidence. Keep scope and original citations attached.
   - After an authorized update, distinguish a saved file, an accepted indexing job and a fresh query that returns the new version. Do not widen collection roots to get a hit.
3. Test the selected server and profile, not a similarly named connector. Confirm a known allowed file is found, an excluded file is absent and a paraphrase works using the advertised retrieval method. Filename or lexical matches do not verify vector search. Keep corpus, vector index and model cache separate from conversation memory. Before indexing sensitive documents, use synthetic markers to test both retrieval and automatic capture of tool results. If the two systems cannot be isolated, do not ingest the real documents. State any remaining transcript and provider exposure.

4. Separate local export from publication. Prepare only explicitly selected Markdown pages with a manifest of paths and checksums; inspect those pages for sensitive excerpts and broken private links. Leave raw imports, transcripts, and derived indexes out by default. Confirm the destination and review the diff before any GitHub/GitLab push; a local export is not a remote sync.

### Markets
For prediction markets, include market id, timestamp, price/orderbook source, and liquidity caveats.

### Paper writing
Treat writing as research engineering: claim, evidence, experiment, citation, and reviewer-risk loops.

## Common Pitfalls

1. Do not cite papers or market data that were not fetched.
2. Research summaries need dates; source state changes.
3. Paper writing advice should be tied to venue expectations and available evidence.

## Verification Checklist

- [ ] Sources were fetched and source handles retained.
- [ ] Dates/timestamps included for changing data.
- [ ] Output separates evidence from interpretation.
