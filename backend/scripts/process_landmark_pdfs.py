#!/usr/bin/env python3
"""
Process NYC Landmarks PDF reports into chunks with Gemini embeddings for RAG.

Usage:
    python scripts/process_landmark_pdfs.py /path/to/pdfs --limit 50
    python scripts/process_landmark_pdfs.py /path/to/pdfs --limit 50 --dry-run
"""

import os
import sys
import re
import argparse
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import execute_batch
from dotenv import load_dotenv

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from models.config import get_settings

load_dotenv()

# Config
CHUNK_SIZE = 500  # tokens (approx 4 chars per token)
CHUNK_OVERLAP = 50
BATCH_SIZE = 20  # Insert chunks in batches
PARALLEL_WORKERS = 5  # Parallel embedding requests


def extract_text_from_pdf(pdf_path: str) -> List[Dict]:
    """Extract text from PDF with page numbers using PyPDF2."""
    try:
        import PyPDF2
        pages = []
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append({"page": i + 1, "text": text})
        return pages
    except Exception as e:
        print(f"  ❌ Error reading {pdf_path}: {e}")
        return []


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks."""
    chars_per_chunk = chunk_size * 4  # ~4 chars per token
    overlap_chars = overlap * 4

    chunks = []
    start = 0
    while start < len(text):
        end = start + chars_per_chunk
        chunk = text[start:end]

        # Try to break at sentence boundary
        if end < len(text):
            last_period = chunk.rfind('. ')
            if last_period > chars_per_chunk * 0.5:
                chunk = chunk[:last_period + 1]
                end = start + last_period + 1

        chunks.append(chunk.strip())
        start = end - overlap_chars

    # Filter out very small chunks
    return [c for c in chunks if len(c) > 100]


def extract_building_info(filename: str, text: str) -> Dict:
    """Extract building name/address from filename or text."""
    # Clean filename
    name = Path(filename).stem
    name = name.replace("_", " ").replace("-", " ")

    # Look for address patterns in first 2000 chars
    address_pattern = r'\d+\s+[\w\s]+(?:Street|Avenue|Road|Boulevard|Place|Lane|Drive)'
    address_match = re.search(address_pattern, text[:2000], re.IGNORECASE)

    # Look for BIN pattern (optional)
    bin_pattern = r'\bBIN[:\s]+(\d{7})\b'
    bin_match = re.search(bin_pattern, text[:2000], re.IGNORECASE)

    # Look for BBL pattern (optional)
    bbl_pattern = r'\bBBL[:\s]+(\d{10})\b'
    bbl_match = re.search(bbl_pattern, text[:2000], re.IGNORECASE)

    return {
        "building_name": name,
        "address": address_match.group(0) if address_match else None,
        "bin": bin_match.group(1) if bin_match else None,
        "bbl": bbl_match.group(1) if bbl_match else None
    }


def embed_text(text: str) -> Optional[List[float]]:
    """Generate embedding using Gemini embedding-001."""
    import google.generativeai as genai

    try:
        result = genai.embed_content(
            model="models/embedding-001",
            content=text,
            task_type="retrieval_document"
        )
        return result['embedding']
    except Exception as e:
        print(f"  ❌ Embedding error: {e}")
        return None


def process_pdf(pdf_path: str, conn, dry_run: bool = False) -> int:
    """Process single PDF and store chunks with parallel embedding."""
    filename = Path(pdf_path).name
    print(f" 📄 {filename}")

    # Extract text
    pages = extract_text_from_pdf(pdf_path)
    if not pages:
        print(f"  ⚠️  No text extracted")
        return 0

    # Get building info
    full_text = " ".join([p["text"] for p in pages])
    info = extract_building_info(filename, full_text)

    print(f"  Building: {info['building_name']}")

    # Collect all chunks with metadata
    all_chunks: List[Tuple[str, int, int]] = []  # (text, chunk_index, page_number)
    for page_data in pages:
        chunks = chunk_text(page_data["text"])
        for i, chunk in enumerate(chunks):
            all_chunks.append((chunk, i, page_data["page"]))

    total_chunks = len(all_chunks)
    print(f"  {total_chunks} chunks to embed...")

    if dry_run:
        print(f"  ✅ Would store {total_chunks} chunks")
        return total_chunks

    # Embed chunks in parallel
    chunks_to_insert = []
    chunks_stored = 0

    def embed_chunk(chunk_data: Tuple[str, int, int]) -> Tuple[Optional[List[float]], str, int, int]:
        text, idx, page = chunk_data
        embedding = embed_text(text)
        return (embedding, text, idx, page)

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = {executor.submit(embed_chunk, c): c for c in all_chunks}

        for future in as_completed(futures):
            try:
                embedding, text, idx, page = future.result()
                if not embedding:
                    continue

                chunks_to_insert.append((
                    info["building_name"],
                    info["bin"],
                    info["bbl"],
                    info["address"],
                    text,
                    idx,
                    embedding,
                    filename,
                    page
                ))
                chunks_stored += 1

                # Batch insert when we reach BATCH_SIZE
                if len(chunks_to_insert) >= BATCH_SIZE:
                    insert_chunks_batch(conn, chunks_to_insert)
                    chunks_to_insert = []

            except Exception as e:
                print(f"  ⚠️  Chunk error: {e}")
                continue

    # Insert remaining chunks
    if chunks_to_insert:
        insert_chunks_batch(conn, chunks_to_insert)

    print(f"  ✅ Stored {chunks_stored}/{total_chunks} chunks")
    return chunks_stored


def insert_chunks_batch(conn, chunks: List[tuple]):
    """Insert a batch of chunks into the database."""
    try:
        cur = conn.cursor()
        execute_batch(
            cur,
            """
            INSERT INTO landmark_chunks
            (building_name, bin, bbl, address, chunk_text, chunk_index, embedding, source_file, page_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            chunks
        )
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"  ❌ Insert error: {e}")
        conn.rollback()


def get_processed_pdfs(conn) -> set:
    """Get set of already-processed PDF filenames from database."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT source_file FROM landmark_chunks")
        result = {row[0] for row in cur.fetchall()}
        cur.close()
        return result
    except Exception as e:
        print(f"⚠️  Could not fetch processed PDFs: {e}")
        return set()


def main():
    parser = argparse.ArgumentParser(
        description="Process NYC Landmarks PDF reports into chunks with Gemini embeddings"
    )
    parser.add_argument("pdf_dir", help="Directory containing PDFs")
    parser.add_argument("--limit", type=int, help="Max PDFs to process")
    parser.add_argument("--dry-run", action="store_true", help="Don't insert into database")
    args = parser.parse_args()

    # Validate PDF directory
    pdf_dir = Path(args.pdf_dir)
    if not pdf_dir.exists():
        print(f"❌ Directory not found: {pdf_dir}")
        sys.exit(1)

    # Get PDFs
    pdfs = list(pdf_dir.glob("**/*.pdf"))
    print(f"📚 Found {len(pdfs)} PDFs in {pdf_dir}")

    if args.limit:
        pdfs = pdfs[:args.limit]
        print(f"📌 Processing first {args.limit} PDFs")

    if not pdfs:
        print("❌ No PDFs found")
        sys.exit(1)

    # Check for required environment variables
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        print("❌ GEMINI_API_KEY not set")
        sys.exit(1)

    # Configure Gemini
    import google.generativeai as genai
    genai.configure(api_key=gemini_key)
    print("✅ Gemini API configured")

    # Connect to database
    if not args.dry_run:
        settings = get_settings()
        conn = psycopg2.connect(settings.database_url)
        print(f"✅ Connected to database")

        # Get already-processed PDFs for resume support
        processed = get_processed_pdfs(conn)
        if processed:
            print(f"📋 Found {len(processed)} already-processed PDFs")
            original_count = len(pdfs)
            pdfs = [p for p in pdfs if p.name not in processed]
            skipped = original_count - len(pdfs)
            if skipped > 0:
                print(f"⏭️  Skipping {skipped} already-processed PDFs")
                print(f"📌 {len(pdfs)} PDFs remaining to process")
    else:
        conn = None
        print("🔍 DRY RUN MODE - No database writes")

    if not pdfs:
        print("✅ All PDFs already processed!")
        if conn:
            conn.close()
        return

    # Process PDFs
    print("\n" + "="*60)
    print("Starting PDF processing...")
    print("="*60)

    total_chunks = 0
    total_pdfs = len(pdfs)
    for i, pdf in enumerate(pdfs, 1):
        print(f"\n[{i}/{total_pdfs}]", end="")
        try:
            total_chunks += process_pdf(str(pdf), conn, dry_run=args.dry_run)
        except Exception as e:
            print(f"❌ Error processing {pdf.name}: {e}")
            continue

    # Cleanup
    if conn:
        conn.close()

    print("\n" + "="*60)
    print(f"✅ Done! Processed {len(pdfs)} PDFs")
    print(f"📊 Total chunks: {total_chunks}")
    if args.dry_run:
        print("🔍 DRY RUN - No data was written to database")
    print("="*60)


if __name__ == "__main__":
    main()
