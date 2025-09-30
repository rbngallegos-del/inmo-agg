from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Callable
import asyncio

# ===== Modelos =====
class Listing(BaseModel):
    id: str
    title: str
    price: float
    currency: str = "MXN"
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    type: str
    furnished: Optional[bool] = None
    location_city: Optional[str] = None
    location_colonia: Optional[str] = None
    url: str
    source: str
    photos: List[str] = []

class SearchResponse(BaseModel):
    results: List[Listing]
    total: int
    next_offset: Optional[int] = None

# ===== App =====
app = FastAPI(title="Inmo Aggregator", version="0.2.3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def root():
    return {"service": "Inmo Aggregator", "status": "ok", "version": "0.2.3"}

@app.get("/health")
def health():
    return {"ok": True}

# ===== Adapters demo =====
async def adapter_demo() -> List[Listing]:
    await asyncio.sleep(0.01)
    return [
        Listing(
            id="demo1",
            title="Depto 2 rec Col. Primavera",
            price=9000,
            bedrooms=2,
            bathrooms=1,
            type="departamento",
            furnished=False,
            location_city="Tampico",
            location_colonia="Primavera",
            url="https://ejemplo.local/depto-primavera",
            source="demo",
            photos=[],
        ),
        Listing(
            id="demo2",
            title="Casa 3 rec Col. Del Bosque",
            price=18000,
            bedrooms=3,
            bathrooms=2,
            type="casa",
            furnished=None,
            location_city="Tampico",
            location_colonia="Del Bosque",
            url="https://ejemplo.local/casa-bosque",
            source="demo",
            photos=[],
        ),
        Listing(
            id="demo3",
            title="Local comercial Centro",
            price=12000,
            bedrooms=None,
            bathrooms=1,
            type="local",
            furnished=False,
            location_city="Ciudad Madero",
            location_colonia="Centro",
            url="https://ejemplo.local/local-centro",
            source="demo",
            photos=[],
        ),
    ]

# Stubs de otros portales (vacíos por ahora)
async def adapter_remax() -> List[Listing]: return []
async def adapter_alemar() -> List[Listing]: return []
async def adapter_inmuebles24() -> List[Listing]: return []

# Regis
