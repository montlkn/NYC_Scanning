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


# --- the second-pass fact checker ---------------------------------------
import asyncio
import json

from services import kit_judge

CAND = "A bomb meant for Russell Sage killed four people here, including the bomber."


def verdict(*issues):
    return json.dumps({"issues": list(issues)})


def test_major_issue_with_a_real_quote_rejects():
    out = verdict({"severity": "major", "candidate_quote": "killed four people here",
                   "problem": "the bombing was at the earlier building"})
    r = kit_judge.parse_verdict(out, CAND)
    assert r.startswith("judge:") and "earlier building" in r


def test_objection_to_words_the_candidate_never_wrote_is_discarded():
    out = verdict({"severity": "major", "candidate_quote": "the bombing occurred in 1891",
                   "problem": "year not in source"})
    assert kit_judge.parse_verdict(out, CAND) is None


def test_minor_issues_do_not_reject():
    out = verdict({"severity": "minor", "candidate_quote": "killed four people",
                   "problem": "reworded"})
    assert kit_judge.parse_verdict(out, CAND) is None


def test_clean_and_markdown_wrapped_json():
    assert kit_judge.parse_verdict('{"issues": []}', CAND) is None
    assert kit_judge.parse_verdict('```json\n{"issues": []}\n```', CAND) is None


def test_judge_fails_closed():
    assert kit_judge.parse_verdict(None, CAND) == "judge unavailable"
    assert kit_judge.parse_verdict("looks fine!", CAND).startswith("judge unparseable")
    assert kit_judge.parse_verdict("{not json}", CAND).startswith("judge unparseable")


def test_quote_matching_ignores_markdown_and_curly_quotes():
    cand = "**AT&T** said the tower \u201ctakes charge of the street\u201d."
    out = verdict({"severity": "major", "candidate_quote": 'takes charge of the street', "problem": "x"})
    assert kit_judge.parse_verdict(out, cand) is not None


# --- the retry loop -------------------------------------------------------
import scripts.rewrite_narratives_voice as rw

ORIGINAL = (
    "**Harold Pike**, owner of **The Alder Building**, was arrested in 1932 for running an "
    "illegal card game on the fourth floor. **Mara Quill** designed it in 1911 for the "
    "**Alder Hat Company**, with a terracotta facade and ornate cornices."
)
ATTEMPT1 = (
    "In 1932 **Harold Pike** got arrested for an illegal card game on the fourth floor of "
    "**The Alder Building**, a forgotten hat company tower. **Mara Quill** designed it in "
    "1911 for the **Alder Hat Company**: terracotta facade, ornate cornices."
)
ATTEMPT2 = (
    "Fourth floor, 1932, one illegal card game, and **Harold Pike**, owner of **The Alder "
    "Building**, got arrested for it. **Mara Quill** designed the place in 1911 for the "
    "**Alder Hat Company**: terracotta facade, ornate cornices."
)


def test_rejection_is_fed_back_and_the_second_attempt_wins(monkeypatch):
    calls = []

    async def fake_text(**kw):
        calls.append(kw["user"])
        return ATTEMPT1 if len(calls) == 1 else ATTEMPT2

    async def fake_judge(source, candidate, label="story"):
        return 'judge: "a forgotten hat company tower": invented' if "forgotten" in candidate else None

    monkeypatch.setattr(rw, "openai_text", fake_text)
    monkeypatch.setattr(rw, "judge", fake_judge)
    bin_, new, reason = asyncio.run(rw.rewrite_one(asyncio.Semaphore(1), "9", ORIGINAL + "\n\nSOURCES:\n- a"))
    assert reason is None and "Fourth floor" in new and new.endswith("SOURCES:\n- a")
    assert len(calls) == 2 and "forgotten hat company tower" in calls[1] and "ORIGINAL STORY" in calls[1]


def test_two_rejections_keep_the_old_story(monkeypatch):
    async def fake_text(**kw):
        return ATTEMPT1

    async def always_no(source, candidate, label="story"):
        return 'judge: "x": invented'

    monkeypatch.setattr(rw, "openai_text", fake_text)
    monkeypatch.setattr(rw, "judge", always_no)
    bin_, new, reason = asyncio.run(rw.rewrite_one(asyncio.Semaphore(1), "9", ORIGINAL))
    assert new is None and reason.startswith("judge:")


# --- the judge's objections are verified against the source ---------------
SRC = ("The Empire Building 1891 bombing: Isaac Liebman was injured when seven elevators "
       "fell in 1915. Olayan Group pursued the renovation with Gensler.")


def test_wrong_absent_claim_is_discarded_when_the_source_has_it():
    cand = "In 1915 seven elevators fell simultaneously, injuring passenger Isaac Liebman."
    out = verdict({"severity": "major", "candidate_quote": "injuring passenger Isaac Liebman",
                   "kind": "absent", "evidence": "Isaac Liebman", "problem": "not in source"})
    assert kit_judge.parse_verdict(out, cand, SRC) is None


def test_real_distortion_is_kept():
    cand = "Olayan Group hired Gensler for a major renovation."
    out = verdict({"severity": "major", "candidate_quote": "hired Gensler", "kind": "distorted",
                   "evidence": "pursued the renovation with Gensler", "problem": "source says pursued"})
    assert "hired Gensler" in kit_judge.parse_verdict(out, cand, SRC)


def test_real_insinuation_is_kept():
    cand = "She held the design leadership that too often gets filed under somebody else's name."
    out = verdict({"severity": "major", "candidate_quote": "too often gets filed under somebody else's name",
                   "kind": "absent", "evidence": "filed somebody else's name", "problem": "insinuation"})
    assert kit_judge.parse_verdict(out, cand, "Natalie de Blois designed the Pepsi-Cola Building.")


def test_distortion_citing_words_the_source_never_had_is_discarded():
    cand = "Olayan Group hired Gensler for a major renovation."
    out = verdict({"severity": "major", "candidate_quote": "hired Gensler", "kind": "distorted",
                   "evidence": "words the source never contained", "problem": "x"})
    assert kit_judge.parse_verdict(out, cand, SRC) is None


def test_too_close_retry_asks_for_a_restructure():
    msg = rw.retry_message("orig", "too close to original (voice did not change)")
    assert "EVERY sentence" in msg and "ORIGINAL STORY" in msg


def test_rewrite_must_keep_every_citation():
    from services.kit_voice import check_rewrite
    old = ("**The Alder Building** was finished in 1911 for the **Alder Hat Company** [[1]](https://a.org/x/y). "
           "In 1932 its owner **Harold Pike** was arrested for a card game on the fourth floor [[2]](https://b.org/p/q). "
           "The terracotta facade has ornate cornices and is a fine example of early commercial work.")
    dropped = ("**Harold Pike** got arrested in 1932 over a card game on the fourth floor of **The Alder Building**. "
               "It went up in 1911 for the **Alder Hat Company**, terracotta facade, ornate cornices, "
               "early commercial work at its most earnest. A hat company. A card game. Sure.")
    assert check_rewrite(old, dropped).startswith("citations changed")


def test_rewrite_rejects_meta_and_relationship_drift():
    from services.kit_voice import check_rewrite
    old = ("**Harold Pike** occupied **The Alder Building** in 1932, running a card game on the fourth floor "
           "while the **Alder Hat Company** sold hats downstairs to people who did not ask questions.")
    meta = ("The source says **Harold Pike** occupied **The Alder Building** in 1932 and ran a card game on the "
            "fourth floor, while the **Alder Hat Company** sold hats below to customers who kept quiet.")
    owned = ("**The Alder Building** belonged to **Harold Pike** in 1932, card game on the fourth floor, "
             "while the **Alder Hat Company** moved hats downstairs to customers who knew better than to ask.")
    assert check_rewrite(old, meta).startswith("meta phrase")
    assert check_rewrite(old, owned).startswith("changed relationship word")


def test_story_may_end_on_a_citation():
    from services.kit_voice import check_rewrite
    old = ("**Harold Pike** occupied **The Alder Building** in 1932 and ran a card game on the fourth floor "
           "while the **Alder Hat Company** sold hats downstairs [[1]](https://a.org/x/y).")
    new = ("A card game on the fourth floor. Hats downstairs. In 1932 **Harold Pike** occupied **The Alder "
           "Building** and the **Alder Hat Company** kept selling below him [[1]](https://a.org/x/y).")
    r = check_rewrite(old, new)
    assert r is None or not r.startswith("does not end cleanly"), r
