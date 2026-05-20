-- F2-free-3: facade-aware camera pose.
--
-- The previous camera_pose_for_bin picked the *nearest* street centerline,
-- which on corner lots ends up being the side street (e.g. 555 Park Avenue
-- is on the corner of Park + E 62; the nearest centerline by ~7m is E 62,
-- not Park Ave). That puts the virtual camera on the wrong street, framing
-- the side of the building instead of the front facade.
--
-- Fix: score each nearby centerline by how parallel it is to the building's
-- longest footprint edge. The longest edge is almost always the front facade
-- in Manhattan, so the most-parallel centerline is the street the building
-- faces. Tiebreak on distance.

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
    longest_edge     geometry;
    facade_azimuth   double precision;
BEGIN
    SELECT footprint, centroid
      INTO bldg_footprint, bldg_centroid
      FROM building_footprints
     WHERE bin::text = target_bin
     LIMIT 1;

    IF bldg_footprint IS NULL THEN
        RETURN;
    END IF;

    -- Find the longest edge of the footprint. ST_Boundary gives us the outline;
    -- we iterate over consecutive vertex pairs and keep the longest segment.
    WITH boundary AS (
        SELECT (ST_DumpPoints(ST_ExteriorRing(
            CASE
                WHEN ST_GeometryType(bldg_footprint) = 'ST_MultiPolygon'
                THEN ST_GeometryN(bldg_footprint, 1)
                ELSE bldg_footprint
            END
        ))).geom AS pt,
        (ST_DumpPoints(ST_ExteriorRing(
            CASE
                WHEN ST_GeometryType(bldg_footprint) = 'ST_MultiPolygon'
                THEN ST_GeometryN(bldg_footprint, 1)
                ELSE bldg_footprint
            END
        ))).path[1] AS idx
    ),
    edges AS (
        SELECT
            ST_MakeLine(b1.pt, b2.pt) AS edge_geom,
            ST_Length(ST_MakeLine(b1.pt, b2.pt)::geography) AS edge_len
          FROM boundary b1
          JOIN boundary b2 ON b2.idx = b1.idx + 1
    )
    SELECT edge_geom INTO longest_edge
      FROM edges
     ORDER BY edge_len DESC
     LIMIT 1;

    -- Azimuth of the facade edge (its compass direction).
    facade_azimuth := DEGREES(ST_Azimuth(
        ST_StartPoint(longest_edge),
        ST_EndPoint(longest_edge)
    ));

    -- Score each nearby centerline by:
    --   (1) parallelism with facade — score higher when angular difference
    --       between centerline direction and facade direction is small
    --       (modulo 180° since streets are bidirectional);
    --   (2) closeness to the centroid as tiebreaker.
    RETURN QUERY
    WITH nearby AS (
        SELECT physical_id,
               full_street_name,
               geom,
               ST_Distance(geom::geography, bldg_centroid::geography) AS dist_m,
               -- Estimate the centerline's azimuth using its endpoints.
               -- Most segments are short and roughly straight in NYC's grid.
               DEGREES(ST_Azimuth(
                   ST_StartPoint(ST_LineMerge(geom)),
                   ST_EndPoint(ST_LineMerge(geom))
               )) AS line_az
          FROM street_centerlines
         WHERE ST_DWithin(geom::geography, bldg_centroid::geography, 80)
    ),
    scored AS (
        SELECT n.physical_id,
               n.full_street_name,
               n.geom,
               n.dist_m,
               -- Angular difference between facade and line, normalized to [0,90]
               -- since streets are undirected.
               LEAST(
                   ABS((facade_azimuth - n.line_az + 540)::numeric % 360 - 180),
                   180 - ABS((facade_azimuth - n.line_az + 540)::numeric % 360 - 180)
               )::double precision AS angle_diff,
               ST_LineInterpolatePoint(
                   ST_LineMerge(n.geom),
                   GREATEST(0.0, LEAST(1.0,
                       ST_LineLocatePoint(ST_LineMerge(n.geom), bldg_centroid)
                   ))
               ) AS proj_pt
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
      -- Combined score: angle difference dominates (parallel facade > close).
      -- Distance breaks ties: 1° ≈ 2m of priority.
      ORDER BY (s.angle_diff + s.dist_m / 2.0) ASC
      LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION camera_pose_for_bin(text) IS 'Return (lat, lng, heading) for a virtual camera on the building''s facade-parallel street centerline, aimed at the building. Picks the avenue/street the building actually fronts rather than the literally-nearest centerline.';
