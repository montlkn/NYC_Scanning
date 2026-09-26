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
