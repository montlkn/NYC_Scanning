-- Phase 1 Scan Tables

CREATE TABLE buildings (
    id SERIAL PRIMARY KEY,
    bbl VARCHAR(10) UNIQUE NOT NULL,
    des_addres TEXT,
    build_nme TEXT,
    style_prim TEXT,
    num_floors INT,
    final_score FLOAT,
    geom GEOMETRY(Point, 4326),
    center GEOMETRY(Point, 4326),
    tier INT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE reference_embeddings (
    id SERIAL PRIMARY KEY,
    building_id INT REFERENCES buildings(id),
    angle INT,
    pitch INT,
    embedding vector(512),
    image_key TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_buildings_geom ON buildings USING GIST(geom);
CREATE INDEX idx_buildings_center ON buildings USING GIST(center);
CREATE INDEX idx_buildings_tier ON buildings(tier);
CREATE INDEX idx_buildings_bbl ON buildings(bbl);
CREATE INDEX idx_embeddings_building ON reference_embeddings(building_id);
CREATE INDEX idx_embeddings_hnsw ON reference_embeddings 
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Success message
SELECT 'Phase 1 tables created successfully!' as status;
