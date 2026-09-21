-- L2-normalize building_search_index.profile.
--
-- apply_nudges() adds `W_PERSONALIZATION * dot(profile, user_vector)` and
-- sizes W_PERSONALIZATION (0.02) in rank-steps on the assumption that the dot
-- product is a cosine in [-1, 1]. It was not. Measured before this migration:
--
--   min |profile| = 0.000   mean = 65.180   max = 87.994
--
-- So the nudge reached ~1.76 against a rank step of ~0.016 -- about 110 rank
-- steps. Personal taste silently became the primary sort key ahead of the
-- entire RRF fusion, which is the "oculus -> Odyssey House #1, The Oculus #5"
-- failure the iOS client disabled user_vector over (sendUserVector = false).
--
-- The query side is normalized in routers/search.py; this is the corpus side.
-- Direction is all that personalization should contribute -- magnitude here is
-- just how many aesthetic events a building accumulated.
--
-- Run: psql "$SEARCH_DB_URL" -f migrations/20260920_normalize_profile_vectors.sql

UPDATE building_search_index
   SET profile = (
         SELECT CAST('[' || string_agg(to_char(v / n, 'FM9990.000000'), ',' ORDER BY i) || ']' AS vector)
           FROM unnest(profile::real[]) WITH ORDINALITY AS e(v, i),
                LATERAL (SELECT sqrt((profile <#> profile) * -1)) AS m(n)
       )
 WHERE profile IS NOT NULL
   AND sqrt((profile <#> profile) * -1) > 0;
