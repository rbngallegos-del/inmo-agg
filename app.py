from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Literal

# ===== Modelos =====
class Listing(BaseModel):
    id: str
    title: str
    price: float
    currency: str = "MXN"
    bedrooms: int
    bathrooms: Optional[float] = None
    type: Literal['casa', 'departamento', 'otro']
    furnished: bool
    location_city: str
    location_colonia: Optional[str] = None
    url: str
    source: str
    photos: List[str] = []

class SearchResponse(BaseModel):
    results: List[Listing]
    total: int

app = FastAPI(title="Inmo Aggregator", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True}

def demo_adapter():
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
            title="Casa 2 rec Unidad Nacional",
            price=10000,
            bedrooms=2,
            bathrooms=1.5,
            type="casa",
            furnished=True,
            location_city="Ciudad Madero",
            location_colonia="Unidad Nacional",
            url="https://ejemplo.local/casa-un",
            source="demo",
            photos=[],
        ),
    ]

@app.get("/search", response_model=SearchResponse)
async def search(
    cities: Optional[str] = Query(None),
    min_price: int = Query(0, ge=0),
    max_price: int = Query(10**9, ge=0),
    min_bedrooms: int = Query(0, ge=0),
    furnished: Optional[bool] = Query(None),
    property_type: str = Query("all", regex="^(casa|departamento|all)$"),
):
    city_set = {c.strip().lower() for c in cities.split(",")} if cities else None
    results = demo_adapter()

    def keep(l: Listing) -> bool:
        if city_set and l.location_city.lower() not in city_set:
            return False
        if not (min_price <= l.price <= max_price):
            return False
        if l.bedrooms < min_bedrooms:
            return False
        if furnished is not None and l.furnished != furnished:
            return False
        if property_type != "all" and l.type != property_type:
            return False
        return True

    filtered = [l for l in results if keep(l)]
    return SearchResponse(results=filtered, total=len(filtered))
