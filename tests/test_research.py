from vesper.tools.research import chunk_text


def test_short_doc_is_one_chunk():
    assert chunk_text("hello world\n\nsecond para") == ["hello world\n\nsecond para"]


def test_packs_paragraphs_up_to_cap():
    paras = [f"para {i} " + "x" * 500 for i in range(6)]
    chunks = chunk_text("\n\n".join(paras), max_chars=1500)
    assert all(len(c) <= 1500 for c in chunks)
    # nothing lost
    assert sum(c.count("para") for c in chunks) == 6


def test_oversized_paragraph_hard_split():
    chunks = chunk_text("y" * 4000, max_chars=1500)
    assert [len(c) for c in chunks] == [1500, 1500, 1000]


def test_tiny_trailing_chunk_merges():
    text = "a" * 1400 + "\n\n" + "tail"
    chunks = chunk_text(text, max_chars=1500)
    # "tail" alone is under CHUNK_MIN_CHARS, so it merges into the previous chunk
    assert len(chunks) == 1
    assert chunks[0].endswith("tail")


def test_empty_doc():
    assert chunk_text("") == []
    assert chunk_text("\n\n\n") == []
