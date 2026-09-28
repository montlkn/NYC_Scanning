"""Public-list notes as the vibe judge reads them (services/list_notes.py)."""

from services import list_notes


def test_format_orders_descriptors_by_agreement():
    note = {
        "list_count": 4,
        "lists": [{"name": "Dimes bars", "mood": "late"}, {"name": "cutty spots"}],
        "descriptors": {"cozy": 1, "cutty": 4, "sceney": 3},
        "comments": ["stairs are the point", "  two\nlines  "],
    }
    text = list_notes.format_note(note)
    assert text.startswith('on 4 public lists: "Dimes bars (late)", "cutty spots"')
    assert "cutty (4 lists), sceney (3 lists), cozy (1 list)" in text
    assert '"stairs are the point" / "two lines"' in text


def test_popularity_only_row_gives_no_text():
    list_notes._NOTES = {"abc": {"venue_id": "abc", "list_count": 0, "save_count": 7}}
    try:
        assert list_notes.text_for("abc") is None
        assert list_notes.save_count("abc") == 7
        assert list_notes.list_count("abc") == 0
    finally:
        list_notes._NOTES = {}


def test_key_for_venues_and_buildings():
    assert list_notes.key_for({"type": "venue", "id": "fsq1"}) == "fsq1"
    assert list_notes.key_for({"type": "building", "id": "b", "bin": "1001234.0"}) == "bin:1001234"
    assert list_notes.key_for({"type": "lore", "id": "x"}) is None


def test_descriptor_bonus_needs_every_word_and_caps():
    list_notes._NOTES = {"bin:1": {"descriptors": {"cast iron": 2, "moody": 1, "loud": 9}}}
    try:
        assert list_notes.descriptor_bonus("cast iron soho", "bin:1") == 2 * list_notes.W_LIST_DESCRIPTOR
        assert list_notes.descriptor_bonus("iron works", "bin:1") == 0.0
        assert list_notes.descriptor_bonus("loud moody bar", "bin:1") == list_notes.W_LIST_DESCRIPTOR_MAX
        assert list_notes.descriptor_bonus("cast iron", "bin:2") == 0.0
    finally:
        list_notes._NOTES = {}


def test_match_finds_venues_by_descriptor_most_lists_first():
    list_notes._NOTES = {
        "a": {"list_count": 1, "descriptors": {"cutty": 1}},
        "b": {"list_count": 3, "descriptors": {"cutty": 3, "dim lit": 1}},
        "c": {"list_count": 2, "descriptors": {"loud": 2}},
        "bin:1": {"list_count": 5, "descriptors": {"cutty": 5}},
    }
    try:
        assert list_notes.match("cutty bars") == ["b", "a"]
        assert list_notes.match("dim lit spots") == ["b"]
        assert list_notes.match("dim") == []
        s = list_notes.status()
        assert (s["venues"], s["buildings"]) == (3, 1)
    finally:
        list_notes._NOTES = {}


def test_similar_ranks_by_meaning_and_skips_buildings(monkeypatch):
    import asyncio
    import sys
    import types
    # Stand-in model: 2-d vectors, "vibe" words along x, "quiet" words along y.
    fake = types.ModuleType("services.text_embeddings")
    fake.embed_texts = lambda texts: [[1.0, 0.1] if "cutty" in t or "hip" in t else [0.1, 1.0] for t in texts]
    monkeypatch.setitem(sys.modules, "services.text_embeddings", fake)
    list_notes._NOTES = {
        "a": {"list_count": 1, "descriptors": {"cutty": 1, "hip": 1}},
        "b": {"list_count": 1, "descriptors": {"quiet": 1}},
        "bin:1": {"list_count": 1, "descriptors": {"cutty": 1}},
    }
    list_notes._generation += 1
    try:
        ids = asyncio.run(list_notes.similar([1.0, 0.0], floor=0.9))
        assert ids == ["a"]
    finally:
        list_notes._NOTES = {}
        list_notes._VECS.clear()
        list_notes._VEC_TEXT.clear()
