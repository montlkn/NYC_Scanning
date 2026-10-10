"""The checks that stop an unattended voice rewrite from writing a wrong fact."""
from services.kit_voice import check_hook, check_rewrite, split_tail

OLD = (
    "**550 Madison Avenue** was one of the ten most disliked structures in New York City "
    "in a 1987 poll of more than 100 prominent New Yorkers. Built for **AT&T** between 1978 "
    "and 1984, it was designed by **Philip Johnson** and **John Burgee** with a broken "
    "pediment critics mocked as a Chippendale highboy. It cost $200 million."
)


def test_split_tail_keeps_machine_block():
    prose, tail = split_tail(OLD + "\n\nSOURCES:\n- a\n\nFACTS:\n- b")
    assert prose.endswith("$200 million.")
    assert tail.startswith("\n\nSOURCES:") and "FACTS:" in tail


def test_split_tail_without_tail():
    prose, tail = split_tail(OLD)
    assert prose == OLD and tail == ""


def test_good_rewrite_passes():
    new = (
        "Fun fact: **550 Madison Avenue** made the 1987 list of the ten most disliked "
        "structures in New York City, ranked by more than 100 prominent New Yorkers. "
        "**AT&T** paid $200 million, and **Philip Johnson** and **John Burgee** gave it a "
        "broken pediment critics called a Chippendale highboy. Built 1978 to 1984. "
        "Trust me, the basement stayed quiet."
    )
    assert check_rewrite(OLD, new) is None


def test_invented_number_rejected():
    new = OLD.replace("$200 million", "$300 million")
    assert "unsupported" in (check_rewrite(OLD, new) or "")


def test_invented_name_rejected():
    new = OLD.replace("John Burgee", "Robert Stern")
    assert "unsupported" in (check_rewrite(OLD, new) or "")


def test_rat_puns_and_length_rejected():
    assert check_rewrite(OLD, OLD.replace("It cost", "Squeak, it cost")) is not None
    assert check_rewrite(OLD, "Short.") is not None


def test_cut_off_rejected():
    assert check_rewrite(OLD, OLD[:-12]) is not None


def test_research_narration_rejected():
    assert check_rewrite(OLD, OLD + " My searches returned no results.") is not None


FACTS = "BUILDING: Cherokee Apartments\nA 1915 tenement block built by Henry Phipps for working families."


def test_good_hook_passes():
    assert check_hook("Built by Henry Phipps so working families could breathe.", FACTS) is None


def test_hook_with_invented_fact_rejected():
    assert "unsupported" in (check_hook("Built by Andrew Carnegie so families could breathe.", FACTS) or "")
    assert "unsupported" in (check_hook("A 1925 block built so working families could breathe.", FACTS) or "")


def test_hook_none_and_shape():
    assert check_hook("NONE", FACTS) == "none"
    assert check_hook("Too short.", FACTS) is not None
    assert check_hook("An iconic stunning building that boasts a lot of working families.", FACTS) is not None
    assert check_hook("Built by Henry Phipps so working families could breathe in", FACTS) is not None


def test_possessives_are_not_new_proper_nouns():
    old = "**Trump Tower** is New York's best-known glass lobby and the Brothers' old site."
    new = "New York's best-known glass lobby sits on the Brothers' old site, at **Trump Tower**."
    assert check_rewrite(old, new) is None or "unsupported" not in check_rewrite(old, new)
    assert check_hook("New York's loudest lobby belongs to Trump Tower.", old) is None


REAL = (
    "**Pepsi-Cola Building** was designed by **Natalie de Blois**, one of the very few women "
    "architects working at that level in mid-century corporate America, with **Gordon Bunshaft** "
    "guiding **Skidmore, Owings & Merrill**'s New York office. Completed in 1958, it was a glass box."
)


def test_copyedit_is_rejected_as_too_close():
    copy = REAL.replace("was designed by", "was designed by")  # unchanged
    assert "too close" in (check_rewrite(REAL, copy) or "")
    light = REAL.replace("Completed in 1958", "Finished in 1958")
    assert "too close" in (check_rewrite(REAL, light) or "")


def test_real_restructure_passes():
    new = (
        "A glass box in 1958, and the person who drew it was **Natalie de Blois**, one of the very "
        "few women architects working at that level in mid-century corporate America. **Gordon "
        "Bunshaft** guided **Skidmore, Owings & Merrill**'s New York office. She did the work. "
        "Trust me, I have seen worse glass."
    )
    assert check_rewrite(REAL, new) is None
