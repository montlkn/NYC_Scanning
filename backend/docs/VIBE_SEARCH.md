# Vibe search

Status as of 2026-09-26. Read this before spending money on venue cards.

## The problem

Searches like "punk bar", "sceney LES bars" and "cutty bars" describe a scene.
A venue row holds only a name, a Foursquare category and a host building, so
those searches had nothing to match and returned the nearest bars. The query
rewrite's `picks` (the model naming bars from memory) was thin and partly
wrong. It never named Clockwork or Clandestino, though both are in our data.

## How it works now

1. **Venue cards** (`scripts/build_venue_cards.py` → `cards/venue_cards.jsonl`).
   Each venue gets 2 Brave searches: one on the place, one on reviews and
   press. A model writes 2-3 sentences from the snippets only, plus:
   - `kind`: what the place really is. Foursquare categories are often
     wrong: Upstairs Bar is filed as a Chinese restaurant, Le Dive as a
     French one.
   - `match`: false when the results are about some other place.
   - `closed`: true when a result says it closed.

   The file is committed and loaded at startup. No database change.
2. **Editor notes** (`cards/venue_notes.jsonl`). Local knowledge the web
   lacks, one line per venue, and it wins over the card. It is shown first
   and marked to the judge as first-hand. Seeded with Upstairs Bar and
   Clandestino (the user's call: all of Dimes Square counts as sceney and
   cutty).
3. **Query rewrite** returns `vibe` (the scene words, copied from the query).
   `INTERP_VERSION` is 10.
4. **Judge** (`services/vibe_judge.py`) runs only when `vibe` is set. Steps:
   - Candidates are the asked-for kind of place: category match, or a card
     `kind` match.
   - A named neighborhood resolves to its venue centroid plus a 1.2km
     radius, not an NTA boundary. NTA puts Canal/Division under Chinatown.
   - A model picks up to 8 and gives a reason.
   - It only runs with 15 or more carded candidates. Otherwise the old
     recalled picks run: without that gate, Midtown "chic bars" got worse.
5. **Display.** A venue's result row shows its card instead of "1900 store".
   The venue page shows the full card under ABOUT (iOS fdc697d).

No vibe vocabulary exists anywhere. Slang is understood by the judge.

## Coverage

| Area | Venues carded | Scope |
|---|---|---|
| Dimes Square (Canal / Division / Ludlow / Orchard / Essex) | 323 | bars, restaurants, cafes; cards v3 |
| Rest of LES + East Village | ~250 | nightlife only; cards v2 (1 query, no `kind`) |
| Everywhere else | 0 | old recalled-picks path |

## Cost

Brave is $5 per 1,000 queries, and cards use 2 queries per venue, so about
**$0.01 per venue**. LLM cost per card is small next to that, but it has not
been measured. Measure it on the next run.

| Scope | Venues | Brave at 2 queries |
|---|---|---|
| Rest of the LES/EV pilot, all food and drink | ~4,650 | ~$47 |
| Citywide nightlife only | ~15,500 | ~$155 |
| Citywide food + drink + nightlife | ~88,000 | ~$880 |
| Every searchable POI | ~200,000 | ~$2,000 |

From the Dimes Square run: about 25% of Foursquare venues were closed and
about 30% did not match anything real. Part of every full run is spent
discovering that a row is junk.

**Brave budget:** the user sets a monthly spend cap in the Brave dashboard.
The same key also powers production lore lookups, so a card run can starve
lore. Ask before any run over ~$5, and do a small test first.

## Strategy (decided 2026-09-26: hold the $47, do not pre-card the city)

One card serves every vibe: the judge reads the same card for "punk",
"cutty" and "date spot". So cost scales with the number of venues, not the
number of vibes. The plan, most accurate first:

1. **Curators and user lists are the ground truth.** A public list such as
   "cutty Dimes Square bars" is a batch of first-hand notes: the list title
   is the vibe, and every member gets a note. The mechanism already exists
   (`venue_notes.jsonl`); it needs a feed from the app's Lists and community
   contributions, moderated, with a few trusted curators per neighborhood.
2. **Editorial guides for the well-known places.** One "best dive bars East
   Village" page names about 20 places. A few hundred guide pages cover most
   searched venues for a few dollars. Caveat: snippets name few places, so
   pages must be fetched, and some block it. Use them as a ranking signal,
   never republished.
3. **Paid cards only where people search** (lazy carding, below).

## Cheaper ways to scale

In rough order of value:

1. **Lazy carding.** When a vibe search lands in an uncarded area, card that
   area's candidates in the background, with a daily cap. Money follows real
   searches, not the map.
2. **Card by demand.** Rank neighborhoods by vibe queries in
   `search_query_log` and card the top ones first.
3. **One-query first pass.** Use only the "place" query ($0.005) to find
   junk and closed rows. Spend the second query only on venues that match.
4. **User notes.** Let users add a one-line note to a venue, feeding
   `venue_notes` (moderated, like community contributions). This is the only
   fix for places the web says nothing useful about, like Upstairs.
5. **Refresh.** Re-card monthly for closures, only where there are cards.

## Running it

```
cd backend
# free: count venues and estimate cost
python3 -m scripts.build_venue_cards --area "Dimes Square, Lower East Side" \
  --bbox 40.7132,-73.9940,40.7172,-73.9878 \
  --categories-regex 'bar|pub|lounge|club|...|restaurant|caf|coffee|...' \
  --exclude-regex 'store|service|...|juice|smoothie' --dry-run
# spend: same command without --dry-run, under `railway run` for the keys
```

The run is resumable and appends rows. It stops on a real Brave 402 and
records nothing for failed requests. The venue list comes from the public
`/venues/nearby` API, which allows 60 requests a minute and 2,000 a day per
IP; the list is cached in `cards/venues_*.json`, which is git-ignored.

To add a note, append a row to `cards/venue_notes.jsonl` and deploy:
`{"fsq_id", "name", "kind", "note", "by", "date"}`.

## Checks

`python3 -m scripts.search_eval` runs the cases in
`tests/search_eval_cases.json`. Run it before and after every change. As of
2026-09-26 it is 31/34 before the editor notes. Known failures:
- The Clandestino cases. The notes should fix them; re-run to confirm.
- "bars id like": a furniture store named "…Bars" ranks first.

## Open

- "bars id like" furniture store (a name-only match on the kind of place).
- "dimes square bars" has no `vibe`, so the judge does not run, and "Dimes
  Square" is not an NTA.
- The venue page only shows ABOUT when opened from a search result, not
  from a map pin.
- Cold vibe queries take 4-7s (the app gives up at 10s). Warm queries take
  about 0.1s.
- The ~250 older v2 cards in LES/EV have no `kind`. Re-card them with v3
  when there is budget (~$2.50).

## Curator lists (designed 2026-09-26, not built)

Real people are the most accurate source of vibe. Upstairs Bar proved it:
the web says "happy hour", and the regulars say cutty. Lists turn that
knowledge into search signal at no per-venue cost.

**Decisions (user):**
- Any public list is a curator. There is no curator role; trust comes from
  agreement between lists, and later from saves and follows.
- Publishing is opt-in. Lists are private by default.
- Private lists count only as anonymous save counts ("saved by 12 people"),
  and only once at least 5 different people have saved a place, so no one
  person's list can be worked out. A private list's title, descriptors and
  comments never leave it. This must be stated in settings or the privacy
  policy.

**Publish gate:**
- Each place needs 1-5 vibe descriptors: free text, 1-3 words each. 1 is
  the minimum; requiring 5 makes a long list a chore.
- The list title and mood apply to every place on it, so per-place
  descriptors only add what is specific.
- Descriptor suggestions come from what others already used for that place
  and across lists. They are data-derived; there is no hardcoded word list.
- Comments are optional, prompted with "What's it like?".
- Moderation mirrors community contributions: flag-to-hide, a length cap,
  and a rate limit.

**Into search:**
- A nightly job reads public lists on MAIN and builds per-venue notes on
  the search backend: lists joined, titles and moods, descriptors with
  counts, and comments.
- The judge sees them as local notes, e.g. "cutty (4 lists), sceney (3),
  stairs are the point".
- Weighting: agreement across lists beats one list; a comment beats a tag;
  a place's own descriptors beat the list title.
- Anonymous save counts are a popularity signal only, never vibe text.
- Web cards remain the fallback where no list covers a place.
- `venue_notes.jsonl` (the hand-written editor notes) keeps working
  alongside.

**Where lists are seen:**
- v1: "On N lists" on result rows, tappable; "On these lists" plus
  descriptors on the venue page; public lists on a profile.
- Later: browse lists nearby, follow a list or person, copy a list.

**Smallest first version:**
1. Lists can hold venues (today they hold only buildings and lore:
   `user_lists`, `user_list_buildings`, `user_list_lore`), with a public
   toggle.
2. The descriptor gate and suggestions.
3. The nightly lists → notes job.
4. "On N lists" in results and on the venue page.

**Cold start:** the user plus friends write about 10 LES/EV lists.
Editorial guides can come in as "editorial lists" for the famous places.
