# Research corpus sources (PLAN.md §12 Q4)

Drop curated markdown files here and run `python scripts/seed_corpus.py`.
This corpus is the ONLY thing `research_training` searches besides Tavily —
keep it vetted; the agent never free-roams the open web.

Format per file:

```markdown
# Human-readable title
tags: knee, isometrics, tendinopathy

Body text… (split on blank lines; ~1500-char chunks)
```

## Seed list (to curate — nothing here is ingested yet)

- [ ] **PT protocol** — the actual prescribed knee/ankle protocol from PT.
      Highest priority: fill in `pt_protocol_TEMPLATE.md` and rename it to
      `pt_protocol.md`. (Checked Notion: only PT *expense* entries exist,
      no written protocol.)
- [ ] Patellofemoral pain load-management guidance (e.g. the JOSPT
      patellofemoral pain clinical practice guideline).
- [ ] Isometric protocols for tendon/knee pain (e.g. Rio et al. style
      isometric loading summaries).
- [ ] Strength progression principles around pain (pain-monitoring model,
      e.g. Silbernagel).
- [ ] Ankle stability / return-to-load progressions.

Add sources you trust; delete this checklist as items land.
