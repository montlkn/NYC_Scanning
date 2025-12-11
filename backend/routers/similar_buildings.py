"""
Similar Buildings Router
Finds visually similar buildings using CLIP embeddings from reference_embeddings table.
Uses SQLAlchemy for database queries (not Supabase client) for Modal compatibility.
"""

from fastapi import APIRouter, Form, Depends
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import httpx
import logging
import os
import google.generativeai as genai

from services.clip_matcher import encode_photo
from models.session import get_db
from models.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Gemini API key - read lazily since Modal secrets aren't available at import time
_gemini_configured = False

def get_gemini_api_key():
    """Get Gemini API key and configure if not already done."""
    global _gemini_configured
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key and not _gemini_configured:
        genai.configure(api_key=api_key)
        _gemini_configured = True
        logger.info(f"✅ GEMINI_API_KEY configured (starts with: {api_key[:8]}...)")
    elif not api_key:
        logger.warning("⚠️ GEMINI_API_KEY not found in environment!")
    return api_key

class SimilarBuilding(BaseModel):
    bin: str
    name: Optional[str] = None
    address: Optional[str] = None
    architect: Optional[str] = None
    style: Optional[str] = None
    materials: Optional[str] = None
    year: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    visual_similarity: float
    metadata_boost: float
    combined_score: float
    similarity_reason: Optional[str] = None  # AI-generated explanation


class SimilarBuildingsResponse(BaseModel):
    similar_buildings: List[SimilarBuilding]
    source_building_name: Optional[str] = None
    error: Optional[str] = None


async def generate_similarity_reasons(
    source_name: str,
    source_year: Optional[int],
    source_style: Optional[str],
    source_architect: Optional[str],
    buildings: List[SimilarBuilding]
) -> List[str]:
    """
    Generate one-sentence explanations for why each building is similar.
    Uses a single batched Gemini call for efficiency.
    """
    api_key = get_gemini_api_key()
    if not api_key or not buildings:
        return [None] * len(buildings)
    
    try:
        # Build the prompt
        building_list = "\n".join([
            f"{i+1}. {b.name or 'Unknown'} ({b.year or '?'}, {b.style or 'unknown style'}, {b.architect or 'unknown architect'})"
            for i, b in enumerate(buildings)
        ])
        
        prompt = f"""For each building below, write exactly ONE sentence (max 20 words) explaining why it's architecturally similar to {source_name} ({source_year or '?'}, {source_style or 'unknown style'}).

Buildings:
{building_list}

Output ONLY numbered sentences. No preamble, no explanation, no headers.
Example format:
1. Shares the same Art Deco setbacks and ornamental crown.
2. Built in the same era with similar limestone facade."""

        model = genai.GenerativeModel('gemini-2.0-flash-lite')
        response = model.generate_content(prompt)
        
        # Parse the response - only accept numbered lines
        lines = response.text.strip().split('\n')
        reasons = []
        for line in lines:
            cleaned = line.strip()
            # Skip any non-numbered lines (preambles, headers, etc)
            if not cleaned or not cleaned[0].isdigit():
                continue
            # Remove the number prefix
            cleaned = cleaned.lstrip('0123456789.)- ').strip()
            if cleaned:
                reasons.append(cleaned)
        
        # Pad with None if we got fewer responses
        while len(reasons) < len(buildings):
            reasons.append(None)
        
        logger.info(f"  ✅ Generated {len(reasons)} similarity reasons")
        return reasons[:len(buildings)]
        
    except Exception as e:
        logger.error(f"  Failed to generate similarity reasons: {e}")
        return [None] * len(buildings)


@router.post("/similar-buildings", response_model=SimilarBuildingsResponse)
async def find_similar_buildings(
    bin: Optional[str] = Form(None),
    image_url: Optional[str] = Form(None),
    limit: int = Form(7),  # Reduced to 7 for cost optimization
    db: AsyncSession = Depends(get_db),
):
    """
    Find visually similar buildings using CLIP embeddings.
    Returns AI-generated explanations for why each building is similar.
    """
    # DEBUG: Check if Gemini API key is available (reads lazily from env)
    api_key = get_gemini_api_key()
    logger.info(f"  🔑 GEMINI_API_KEY present: {bool(api_key)}")
    
    logger.info(f"🔍 Finding similar buildings for bin={bin}, image_url={image_url}")
    
    target_embedding = None
    target_style = None
    target_architect = None
    target_materials = None
    source_name = None
    source_year = None
    
    # Option 1: Get embedding from existing building's reference_embeddings
    if bin:
        building_id = None
        try:
            bin_variants = [bin, f"{bin}.0"]
            
            for bin_variant in bin_variants:
                result = await db.execute(
                    text("""
                        SELECT id, building_name, style, architect, mat_prim, year_built 
                        FROM buildings_full_merge_scanning 
                        WHERE bin = :bin 
                        LIMIT 1
                    """),
                    {"bin": bin_variant}
                )
                row = result.fetchone()
                if row:
                    building_id = row[0]
                    source_name = row[1]
                    target_style = row[2]
                    target_architect = row[3]
                    target_materials = row[4]
                    try:
                        source_year = int(float(row[5])) if row[5] else None
                    except:
                        source_year = None
                    logger.info(f"  Found: {source_name} ({source_year}, {target_style})")
                    break
                    
            if not building_id:
                logger.warning(f"  No building found for bin={bin}")
        except Exception as e:
            logger.warning(f"  Could not fetch building metadata: {e}")
        
        if building_id:
            try:
                result = await db.execute(
                    text("""
                        SELECT embedding 
                        FROM reference_embeddings 
                        WHERE building_id = :building_id 
                        LIMIT 1
                    """),
                    {"building_id": building_id}
                )
                row = result.fetchone()
                
                if row and row[0]:
                    target_embedding = row[0]
                    logger.info(f"  Found embedding for building_id {building_id}")
                else:
                    settings = get_settings()
                    r2_base = getattr(settings, 'r2_public_url', 
                        "https://jink-building-captures.47cf6ebee78f6d8a95ed0a80b66ccdc7.r2.cloudflarestorage.com")
                    image_url = f"{r2_base}/buildings/{bin}/0deg_40pitch.jpg"
                    logger.info(f"  No embedding, trying R2 image: {image_url}")
            except Exception as e:
                logger.warning(f"  Could not fetch embedding: {e}")
    
    # Option 2: Compute embedding from image URL
    if not target_embedding and image_url:
        try:
            logger.info(f"  Computing embedding from image URL...")
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(image_url)
                if response.status_code == 200:
                    embedding = await encode_photo(response.content)
                    target_embedding = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)
                    logger.info(f"  ✅ Computed embedding from image")
                else:
                    logger.warning(f"  Image fetch failed: HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"  Failed to fetch/encode image: {e}")
            return SimilarBuildingsResponse(
                similar_buildings=[],
                error=f"Failed to fetch/encode image: {str(e)}"
            )
    
    if not target_embedding:
        logger.warning(f"  No embedding available for this building")
        return SimilarBuildingsResponse(
            similar_buildings=[],
            error="No embedding available for this building"
        )
    
    # Call the find_similar_buildings RPC
    try:
        logger.info(f"  Calling find_similar_buildings RPC...")
        
        if isinstance(target_embedding, list):
            embedding_str = "[" + ",".join(str(x) for x in target_embedding) + "]"
        else:
            embedding_str = target_embedding
        
        result = await db.execute(
            text("""
                SELECT * FROM find_similar_buildings(
                    CAST(:target_embedding AS vector(512)),
                    :target_style,
                    :target_architect,
                    :target_materials,
                    :match_count,
                    :exclude_bin
                )
            """),
            {
                "target_embedding": embedding_str,
                "target_style": target_style,
                "target_architect": target_architect,
                "target_materials": target_materials,
                "match_count": limit,
                "exclude_bin": f"{bin}.0" if bin and not bin.endswith('.0') else bin,
            }
        )
        
        rows = result.fetchall()
        
        buildings = []
        for row in rows:
            buildings.append(SimilarBuilding(
                bin=row[0],
                name=row[1],
                address=row[2],
                architect=row[3],
                style=row[4],
                materials=row[5],
                year=row[6],
                latitude=row[7],
                longitude=row[8],
                visual_similarity=row[9],
                metadata_boost=row[10],
                combined_score=row[11],
            ))
        
        logger.info(f"  ✅ Found {len(buildings)} similar buildings")
        
        # Generate AI explanations for why each is similar
        if buildings and source_name:
            reasons = await generate_similarity_reasons(
                source_name, source_year, target_style, target_architect, buildings
            )
            for i, reason in enumerate(reasons):
                if i < len(buildings):
                    buildings[i].similarity_reason = reason
        
        return SimilarBuildingsResponse(
            similar_buildings=buildings,
            source_building_name=source_name
        )
        
    except Exception as e:
        logger.error(f"  RPC failed: {e}")
        return SimilarBuildingsResponse(
            similar_buildings=[],
            error=str(e)
        )
