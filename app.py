# app.py
from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Callable, Any, Coroutine
import asyncio

# =========================
# Modelos
# =========================
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

# =========================
# App
# =========================
app = FastAPI(title="Inmo Aggregator", version="0.2.4")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def root():
    return {"service": "Inmo Aggregator", "status": "ok", "version": "0.2.4"}

# (Render hace HEAD / como healthcheck; respondemos 200 para callar el 405)
@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)

@app.get("/health")
def health():
    return {"ok": True}

# (Opcional: logger rápido de requests para depurar rutas; puedes comentarlo)
# @app.middleware("http")
# async def log_requests(request, call_next):
#    print(f">>> {request.method} {request.url.path}?{request.url.query}")
#    resp = await call_next(request)
#    print(f"<<< {resp.status_code} {request.url.path}")
#    return resp

# =========================
# Adapters (demo y stubs)
# =========================
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

# Stubs de otras fuentes (agregaremos lógica real después)
async def adapter_remax() -> List[Listing]: return []
async def adapter_alemar() -> List[Listing]: return []
async def adapter_inmuebles24() -> List[Listing]: return []

# =========================
# Registro y búsqueda
# =========================
ADAPTERS: Dict[str, Callable[[], Coroutine[Any, Any, List[Listing]]]] = {
    "demo": adapter_demo,
    "remax": adapter_remax,
    "alemar": adapter_alemar,
    "inmuebles24": adapter_inmuebles24,
}

class SearchRequest(BaseModel):
    # Alineado con tu openapi.yaml (snake_case)
    cities: Optional[List[str]] = None            # ["Tampico","Ciudad Madero"]
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_bedrooms: Optional[int] = None
    furnished: Optional[bool] = None
    property_types: Optional[List[str]] = None    # ["casa","departamento","local",...]
    q: Optional[str] = None                       # texto libre en título
    sources: Optional[List[str]] = None           # ["demo","remax",...]
    offset: int = 0
    limit: int = 25

def _csv_to_list(s: Optional[str]) -> Optional[List[str]]:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]

async def _collect_sources(sources: Optional[List[str]]) -> List[Listing]:
    keys = sources if sources else list(ADAPTERS.keys())
    tasks = [ADAPTERS[k]() for k in keys if k in ADAPTERS]
    if not tasks:
        return []
    groups = await asyncio.gather(*tasks)
    out: List[Listing] = []
    for g in groups:
        out.extend(g)
    return out

def _passes_filters(l: Listing, q: SearchRequest) -> bool:
    # cities
    if q.cities:
        cities_lc = {c.lower() for c in q.cities}
        if (l.location_city or "").lower() not in cities_lc:
            return False
    # price
    if q.min_price is not None and l.price < q.min_price:
        return False
    if q.max_price is not None and l.price > q.max_price:
        return False
    # bedrooms (solo para casa/departamento)
    if q.min_bedrooms is not None:
        tipo = (l.type or "").lower()
        if tipo in {"casa", "departamento", "depto"}:
            if l.bedrooms is None or l.bedrooms < q.min_bedrooms:
                return False
    # furnished
    if q.furnished is not None:
        if l.furnished is None or l.furnished is not q.furnished:
            return False
    # property_types
    if q.property_types:
        tipos = {t.lower() for t in q.property_types}
        if (l.type or "").lower() not in tipos:
            return False
    # q en título
    if q.q:
        if q.q.lower() not in (l.title or "").lower():
            return False
    return True

async def _search_core(q: SearchRequest) -> SearchResponse:
    data = await _collect_sources(q.sources)
    filtered = [l for l in data if _passes_filters(l, q)]
    start = max(q.offset, 0)
    end = start + (q.limit if q.limit and q.limit > 0 else 25)
    page = filtered[start:end]
    next_off = end if end < len(filtered) else None
    return SearchResponse(results=page, total=len(filtered), next_offset=next_off)

# =========================
# Endpoints
# =========================
# GET /search (tal como en tu openapi.yaml)
@app.get("/search", response_model=SearchResponse)
async def search_get(
    cities: Optional[str] = Query(None, description="CSV de ciudades"),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    min_bedrooms: Optional[int] = Query(None, ge=0),
    furnished: Optional[bool] = Query(None),
    property_types: Optional[str] = Query(None, description="CSV de tipos"),
    q: Optional[str] = Query(None),
    sources: Optional[str] = Query(None, description="CSV de fuentes"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    req = SearchRequest(
        cities=_csv_to_list(cities),
        min_price=min_price,
        max_price=max_price,
        min_bedrooms=min_bedrooms,
        furnished=furnished,
        property_types=_csv_to_list(property_types),
        q=q,
        sources=_csv_to_list(sources),
        limit=limit,
        offset=offset,
    )
    return await _search_core(req)

# Alias opcionales por compatibilidad si alguna acción vieja los llama:
@app.get("/search-listings", response_model=SearchResponse)
async def search_listings_alias(**kwargs):
    return await search_get(**kwargs)

@app.get("/searchListings", response_model=SearchResponse)
async def searchListings_alias(**kwargs):
    return await search_get(**kwargs)

# POST /search (por si quieres mandar JSON en vez de querystring)
@app.post("/search", response_model=SearchResponse)
async def search_post(req: SearchRequest):
    return await _search_core(req)
