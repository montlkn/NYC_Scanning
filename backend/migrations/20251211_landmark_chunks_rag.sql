-- RAG System for Similar Buildings
-- Stores text chunks from NYC Landmarks PDF reports with Gemini embeddings

-- Enable pgvector extension if not already enabled
CREATE EXTENSION IF NOT EXISTS vector;

-- Create landmark_chunks table
CREATE TABLE IF NOT EXISTS landmark_chunks (
    id SERIAL PRIMARY KEY,
    building_name TEXT,
    bin TEXT,
    bbl TEXT,
    address TEXT,
    chunk_text TEXT NOT NULL,
    chunk_index INT,
    embedding vector(768),  -- Gemini embedding-001 dimension
    source_file TEXT,
    page_number INT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Vector similarity index (IVFFlat for cosine distance)
CREATE INDEX IF NOT EXISTS landmark_chunks_embedding_idx
ON landmark_chunks
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);

-- Text search index for building names
CREATE INDEX IF NOT EXISTS landmark_chunks_building_name_idx
ON landmark_chunks
USING gin(to_tsvector('english', building_name));

-- Additional indexes for common queries
CREATE INDEX IF NOT EXISTS landmark_chunks_bin_idx ON landmark_chunks(bin);
CREATE INDEX IF NOT EXISTS landmark_chunks_bbl_idx ON landmark_chunks(bbl);
CREATE INDEX IF NOT EXISTS landmark_chunks_source_file_idx ON landmark_chunks(source_file);

-- Grant permissions
GRANT SELECT ON landmark_chunks TO authenticated;
GRANT SELECT ON landmark_chunks TO anon;

-- RPC function to search landmark chunks
CREATE OR REPLACE FUNCTION search_landmark_chunks(
    building_name_pattern TEXT,
    query_embedding vector(768) DEFAULT NULL,
    match_limit INT DEFAULT 3
)
RETURNS TABLE (
    id INT,
    building_name TEXT,
    bin TEXT,
    bbl TEXT,
    address TEXT,
    chunk_text TEXT,
    source_file TEXT,
    page_number INT,
    similarity FLOAT
) AS $$
BEGIN
    IF query_embedding IS NOT NULL THEN
        -- Semantic search with embedding
        RETURN QUERY
        SELECT
            lc.id,
            lc.building_name,
            lc.bin,
            lc.bbl,
            lc.address,
            lc.chunk_text,
            lc.source_file,
            lc.page_number,
            1 - (lc.embedding <=> query_embedding) as similarity
        FROM landmark_chunks lc
        WHERE lc.building_name ILIKE '%' || building_name_pattern || '%'
           OR lc.address ILIKE '%' || building_name_pattern || '%'
        ORDER BY lc.embedding <=> query_embedding
        LIMIT match_limit;
    ELSE
        -- Text-based search only (fallback)
        RETURN QUERY
        SELECT
            lc.id,
            lc.building_name,
            lc.bin,
            lc.bbl,
            lc.address,
            lc.chunk_text,
            lc.source_file,
            lc.page_number,
            0.5::FLOAT as similarity  -- Default similarity for text match
        FROM landmark_chunks lc
        WHERE lc.building_name ILIKE '%' || building_name_pattern || '%'
           OR lc.address ILIKE '%' || building_name_pattern || '%'
        ORDER BY lc.created_at DESC
        LIMIT match_limit;
    END IF;
END;
$$ LANGUAGE plpgsql STABLE;

-- Grant execute permission on RPC function
GRANT EXECUTE ON FUNCTION search_landmark_chunks TO authenticated;
GRANT EXECUTE ON FUNCTION search_landmark_chunks TO anon;

-- Add comment for documentation
COMMENT ON TABLE landmark_chunks IS 'Stores text chunks from NYC Landmarks Commission PDF reports with Gemini embeddings for RAG-enhanced building similarity explanations';
COMMENT ON FUNCTION search_landmark_chunks IS 'Searches landmark chunks by building name with optional semantic similarity using embeddings';
