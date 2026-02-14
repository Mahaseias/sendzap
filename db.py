import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
        create table if not exists sessions (
            wa_from text primary key,
            state text not null,
            payload jsonb not null,
            updated_at timestamptz not null default now()
        );
        """))
        conn.execute(text("""
        create table if not exists proposals (
            id uuid primary key default gen_random_uuid(),
            wa_from text not null,
            client_email text not null,
            created_at timestamptz not null default now()
        );
        """))
