from fastapi import APIRouter, UploadFile, Depends, Form
from sqlalchemy.orm import Session
from sqlalchemy import text
import time
from models.scan_db import get_scan_db
from services.clip_matcher import encode_photo

router = APIRouter()

@router.post("/scan")
async def scan_building(
    photo: UploadFile,
    lat: float = Form(...),
    lng: float = Form(...),
    gps_accuracy: float = Form(...),
    bearing: float = Form(...),
    pitch: float = Form(...),
    db: Session = Depends(get_scan_db)
):
    start = time.time()
    
    # 1. Encode user photo
    user_embedding = await encode_photo(await photo.read())
    embedding_str = '[' + ','.join(map(str, user_embedding.tolist())) + ']'
    
    # 2. Radius query (100m)
    candidates = db.execute(text("""
        SELECT id, bbl, des_addres, build_nme, 
               ST_Y(center) as lat, ST_X(center) as lng
        FROM buildings
        WHERE ST_DWithin(center::geography, 
                        ST_MakePoint(:lng, :lat)::geography, 
                        100)
        AND tier = 1
    """), {'lng': lng, 'lat': lat}).fetchall()
    
    if not candidates:
        return {
            "error": "No buildings nearby",
            "latency_ms": (time.time() - start) * 1000
        }
    
    # 3. Match embeddings
    results = []
    for candidate in candidates:
        scores = db.execute(text("""
            SELECT 1 - (embedding <=> CAST(:user_emb AS vector)) as score
            FROM reference_embeddings
            WHERE building_id = :building_id
            ORDER BY embedding <=> CAST(:user_emb AS vector)
            LIMIT 4
        """), {
            'user_emb': embedding_str,
            'building_id': candidate.id
        }).fetchall()
        
        if scores:
            max_score = max([s.score for s in scores])
            results.append({
                'building_id': candidate.id,
                'bbl': candidate.bbl,
                'name': candidate.build_nme,
                'address': candidate.des_addres,
                'score': float(max_score),
                'lat': float(candidate.lat),
                'lng': float(candidate.lng)
            })
    
    # 4. Return top match
    results.sort(key=lambda x: x['score'], reverse=True)
    latency = (time.time() - start) * 1000
    
    if not results:
        return {
            "error": "No matches found",
            "latency_ms": latency
        }
    
    return {
        "building": results[0],
        "confidence": results[0]['score'],
        "candidates": results[:3],
        "latency_ms": latency,
        "num_candidates": len(candidates)
    }
