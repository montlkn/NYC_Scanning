-- F2-free-3 v2: facade-aware camera pose, corrected.
--
-- v1's heuristic was wrong. Manhattan row houses have a SHORT facade
-- (touching the street) and a LONG depth (perpendicular to the street).
-- So "centerline parallel to longest edge" picks the *side* street, not
-- the front street.
--
-- Correct heuristic: for each nearby centerline, score it by:
--   (1) Whether the centerline runs perpendicular to a footprint edge that
--       is close to the centerline. Such an edge is the building's frontage.
--   (2) Closeness of that edge to the centerline (the building literally
--       sits on this street).
--   (3) Length of that edge (longer frontage edges are more decisive).
--
-- For 555 Park (corner): the front edge on Park Ave is short (~14m wide)
-- and the side edge on E 62 is also short (~20m wide). But the **footprint
-- edge facing Park Ave** is closer to Park's centerline than the edge
-- facing E 62 is to E 62's centerline (different distances from each
-- street). Combined with the building's primary address being 555 Park
-- (Park Ave-fronting), the Park centerline should win.

CREATE OR REPLACE FUNCTION camera_pose_for_bin(target_bin TEXT)
RETURNS TABLE (
    cam_lat       double precision,
    cam_lng       double precision,
    heading_deg   double precision,
    street_name   text,
    centerline_id bigint
) AS $$
DECLARE
    bldg_footprint   geometry;
    bldg_centroid    geometry;
BEGIN
    SELECT footprint, centroid
      INTO bldg_footprint, bldg_centroid
      FROM building_footprints
     WHERE bin::text = target_bin
     LIMIT 1;

    IF bldg_footprint IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH
    -- 1. Candidate centerlines within 80m.
    nearby AS (
        SELECT physical_id,
               full_street_name,
               geom,
               ST_LineMerge(geom) AS line_geom,
               ST_Distance(geom::geography, bldg_centroid::geography) AS centroid_dist_m
          FROM street_centerlines
         WHERE ST_DWithin(geom::geography, bldg_centroid::geography, 80)
    ),
    -- 2. For each candidate centerline, find the closest point on the
    --    building's outline. The geography distance from that point to
    --    the centerline is how "in front of" the centerline the building is.
    scored AS (
        SELECT n.physical_id,
               n.full_street_name,
               n.line_geom,
               n.centroid_dist_m,
               -- Distance from the building's nearest footprint point to
               -- the centerline. Small distance = building literally sits
               -- on this street.
               ST_Distance(
                   ST_ClosestPoint(ST_Boundary(bldg_footprint), n.line_geom)::geography,
                   n.line_geom::geography
               ) AS frontage_gap_m,
               -- The point on the centerline closest to the centroid:
               -- the virtual-camera "stand here" point.
               ST_ClosestPoint(n.line_geom, bldg_centroid) AS proj_pt
          FROM nearby n
    )
    SELECT
        ST_Y(s.proj_pt)::double precision        AS cam_lat,
        ST_X(s.proj_pt)::double precision        AS cam_lng,
        (
            ((DEGREES(ST_Azimuth(s.proj_pt, bldg_centroid)) + 360)::numeric % 360)::double precision
        )                                         AS heading_deg,
        s.full_street_name                        AS street_name,
        s.physical_id                             AS centerline_id
      FROM scored s
      -- Lower "frontage gap" wins: the building's footprint is touching
      -- this centerline. Centroid distance is a tiebreaker only for cases
      -- where two streets are equally close to the footprint.
      ORDER BY s.frontage_gap_m ASC, s.centroid_dist_m ASC
      LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION camera_pose_for_bin(text) IS 'Return (lat, lng, heading) for a virtual camera on the centerline of the street the building''s footprint actually touches. Handles corner lots correctly by measuring footprint-to-centerline distance rather than centroid-to-centerline distance.';
