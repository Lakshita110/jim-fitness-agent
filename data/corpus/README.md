# Research corpus sources

Drop curated markdown files here and run `python scripts/seed_corpus.py`.
This corpus is the ONLY thing `research_training` searches besides a
domain-restricted Tavily web search — keep it vetted; the agent never
free-roams the open web.

This corpus is **shared across every athlete Jim coaches** — it holds
general training science, not any one athlete's personal protocol. An
athlete's own knee/ankle limits, standing rules, and goals belong in their
`constraints` doc instead (`get_constraints`/`set_constraints`), which
Claude combines with whatever this corpus turns up at reasoning time. Don't
add a file here that only makes sense for one person.

Format per file:

```markdown
# Human-readable title
tags: knee, isometrics, tendinopathy

Body text… (split on blank lines; ~1500-char chunks)
```

## Seeded so far

- [x] `pain_monitoring_model.md` — traffic-light + next-morning check for
      training through irritable tissue.
- [x] `patellofemoral_pain_load_management.md` — CPG-style guidance for
      knee/patellofemoral pain.
- [x] `isometric_loading_tendon_pain.md` — isometric-first approach to
      irritable tendon pain.

## Still to curate

- [ ] Ankle stability / return-to-load progressions after sprain.
- [ ] General strength-progression and weekly-volume principles (load
      progression, deload cadence, ACWR-style injury-risk guidance).

Add sources you trust, written as general guidance rather than any one
athlete's case — delete items here as they land. These three docs are
original summaries of well-established, publicly discussed training-science
concepts, not verbatim reproductions of any copyrighted guideline text —
swap in the primary sources themselves (or fuller notes on them) when you
have time to curate properly.
