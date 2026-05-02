from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.database import Base, engine
from backend.models import credit_card, income_source, loan, transaction, user, wallet  # noqa: F401
from backend.routers import auth, credit_cards, income_sources, loans, summary, transactions, wallets

# Create all tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="OpenCashFlow",
    description="A multi-platform personal cash flow management application",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routers
app.include_router(auth.router)
app.include_router(wallets.router)
app.include_router(transactions.router)
app.include_router(credit_cards.router)
app.include_router(loans.router)
app.include_router(income_sources.router)
app.include_router(summary.router)

# Serve frontend static files
_frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(_frontend_dir / "static")), name="static")


@app.get("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(str(_frontend_dir / "index.html"))


@app.get("/health")
def health_check():
    return {"status": "ok"}
