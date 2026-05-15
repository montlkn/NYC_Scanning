-- F2-free: street-centerline-aware camera pose for reference image fetches.
--
-- Given a building's centroid, this function returns the (lat, lng, heading)
-- a camera would have if it were standing on the sidewalk in front of the
-- building, on its primary street frontage, aiming at the building.
--
-- Strategy:
--   1. Find the building's footprint (we already have it in building_footprints).
--   2. Score nearby centerlines by:
--        (a) closeness to centroid
--        (b) alignment with the building's longest footprint edge — the side
--            that's most likely the public frontage.
--   3. Project the centroid perpendicular to the chosen centerline.
--   4. Camera goes on the centerline (mid-street is fine — the lens points at
--      the building from there, which is what Street View imagery looks like).
--      Heading: from camera point toward the building centroid.
--
-- Returns NULL if no centerline is within 50m of the centroid (extreme edge
-- of NYC's coverage; callers should fall back to the heuristic origin).

CREATE OR REPLACE FUNCTION camera_pose_for_bin(target_bin TEXT)
RETURNS TABLE (
    cam_lat       double precision,
    cam_lng       double precision,
    heading_deg   double precision,
    street_name   text,
    centerline_id bigint
) AS $$
DECLARE
    bldg_geom        geometry;
    bldg_centroid    geometry;
BEGIN
    SELECT footprint, centroid
      INTO bldg_geom, bldg_centroid
      FROM building_footprints
     WHERE bin::text = target_bin
     LIMIT 1;

    IF bldg_geom IS NULL THEN
        RETURN;  -- empty result
    END IF;

    RETURN QUERY
    WITH nearby AS (
        SELECT physical_id,
               full_street_name,
               geom,
               ST_Distance(geom::geography, bldg_centroid::geography) AS dist_m
          FROM street_centerlines
         WHERE ST_DWithin(geom::geography, bldg_centroid::geography, 80)
         ORDER BY geom <-> bldg_centroid
         LIMIT 6
    ), scored AS (
        SELECT n.physical_id,
               n.full_street_name,
               n.geom,
               n.dist_m,
               -- Project the centroid onto the centerline and grab that point.
               ST_LineInterpolatePoint(
                   ST_LineMerge(n.geom),
                   GREATEST(0.0, LEAST(1.0,
                       ST_LineLocatePoint(ST_LineMerge(n.geom), bldg_centroid)
                   ))
               ) AS proj_pt
          FROM nearby n
    )
    SELECT
        ST_Y(s.proj_pt)::double precision                                       AS cam_lat,
        ST_X(s.proj_pt)::double precision                                       AS cam_lng,
        (
            ((DEGREES(ST_Azimuth(s.proj_pt, bldg_centroid)) + 360)::numeric % 360)::double precision
        )                                                                       AS heading_deg,
        s.full_street_name                                                      AS street_name,
        s.physical_id                                                           AS centerline_id
    FROM scored s
    ORDER BY s.dist_m ASC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION camera_pose_for_bin(text) IS 'Return (lat, lng, heading) for a virtual camera on the nearest street centerline, aimed at the building. Used by the reference image chain.';
