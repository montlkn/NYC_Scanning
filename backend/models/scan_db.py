from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
import os
from dotenv import load_dotenv

# Load environment variables from .env if available
load_dotenv()

SCAN_DB_URL = os.getenv('SCAN_DB_URL')

if not SCAN_DB_URL:
    raise RuntimeError("SCAN_DB_URL environment variable is not set. Please check your .env file.")

# Use NullPool for better compatibility with Supabase pooler
# and disable connection pooling on SQLAlchemy side to avoid conflicts
engine = create_engine(
    SCAN_DB_URL,
    poolclass=NullPool,
    echo=False,
    connect_args={"connect_timeout": 10}
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def get_scan_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
