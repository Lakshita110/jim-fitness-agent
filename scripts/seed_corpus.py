"""Seed/refresh the research corpus (M4) from data/corpus/*.md.

Each markdown file is one source document: the first `# heading` is the title,
optional `tags:` line right under it becomes tags. Chunks along paragraphs,
embeds via OpenRouter, and replaces that source's rows (idempotent per file).

    python scripts/seed_corpus.py
"""

import logging
import sys
from pathlib import Path

from jim.db import connect, migrate
from jim.tools.research import EMBEDDING_MODEL, chunk_text

log = logging.getLogger(__name__)

CORPUS_DIR = Path(__file__).resolve().parent.parent / "data" / "corpus"


def parse_doc(path: Path) -> tuple[str, list[str], str]:
    """Returns (title, tags, body) for a corpus markdown file."""
    lines = path.read_text().splitlines()
    title = path.stem
    tags: list[str] = []
    body_start = 0
    for i, line in enumerate(lines[:5]):
        if line.startswith("# "):
            title = line[2:].strip()
            body_start = i + 1
        elif line.lower().startswith("tags:"):
            tags = [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()]
            body_start = i + 1
    return title, tags, "\n".join(lines[body_start:]).strip()


def embed_batch(texts: list[str]) -> list[list[float]]:
    from openai import OpenAI

    from jim.config import OPENROUTER_BASE_URL, settings

    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=settings().openrouter_api_key)
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in resp.data]


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    docs = sorted(CORPUS_DIR.glob("*.md"))
    docs = [d for d in docs if d.name != "README.md" and "TEMPLATE" not in d.name]
    if not docs:
        log.warning("no corpus documents in %s — nothing to seed", CORPUS_DIR)
        return 1

    with connect() as conn:
        migrate(conn)
        for doc in docs:
            title, tags, body = parse_doc(doc)
            chunks = chunk_text(body)
            if not chunks:
                log.warning("skipping empty document %s", doc.name)
                continue
            embeddings = embed_batch(chunks)
            conn.execute("DELETE FROM research_corpus WHERE source = %s", (doc.name,))
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                conn.execute(
                    "INSERT INTO research_corpus (source, title, chunk_text, embedding, tags)"
                    " VALUES (%s, %s, %s, %s::vector, %s)",
                    (doc.name, title, chunk, str(embedding), tags),
                )
            conn.commit()
            log.info("seeded %s: %d chunks (tags: %s)", doc.name, len(chunks), tags)
    return 0


if __name__ == "__main__":
    sys.exit(main())
