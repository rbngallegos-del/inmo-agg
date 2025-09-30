from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Literal

# =====================
# Modelos (Pydantic v2)
# =====================

class Listing(BaseModel):
    id: str
    title: str
    price: float
    currency: str = "MXN"
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    type: Literal["casa", "departamento", "terreno", "local", "oficina", "bodega", "otro"] = "otro"
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

# =====================
# App
# =====================

app = FastAPI(title="Inmo Aggregator", version="0.2.1")

# CORS abierto (ajusta en prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================
# Rutas básicas
# =====================

@app.get("/")
def root():
    return {"service": "Inmo Aggregator", "status": "ok", "version": "0.2.1"}

@app.get("/health")
def health():
    return {"ok": True}

# =====================
# Demo adapter (mock)
# Aquí enchufarás adapters reales (APIs permitidas/TOS ok)
# =====================

def demo_adapter_any() -> List[Listing]:
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

# =====================
# /search
# =====================

@app.get("/search", response_model=SearchResponse)
async def search(
    cities: Optional[str] = Query(None, description="Ciudades separadas por coma. Vacío = todas."),
    min_price: Optional[int] = Query(None, ge=0, description="Precio mínimo (MXN)."),
    max_price: Optional[int] = Query(None, ge=0, description="Precio máximo (MXN)."),
    min_bedrooms: Optional[int] = Query(None, ge=0, description="Recámaras mínimas (solo casa/departamento)."),
    furnished: Optional[bool] = Query(None, description="True/False; omitir para cualquiera."),
    property_types: Optional[str] = Query(None, description="Tipos CSV: casa,departamento,terreno,local,oficina,bodega,otro"),
    # compat hacia atrás
    property_type: Optional[str] = Query(None, description="DEPRECATED: usa property_types"),
    sources: Optional[str] = Query(None, description="Identificadores de adapters, separados por coma."),
    q: Optional[str] = Query(None, description="Texto libre a buscar en el título."),
    limit: int = Query(25, ge=1, le=100, description="Límite de elementos."),
    offset: int = Query(0, ge=0, description="Desplazamiento.")
):
    # 1) Traer datos (cuando tengas adapters reales, llama a cada fuente aquí)
    data = demo_adapter_any()

    # 2) Normalizar filtros
    # property_types puede venir en el nuevo param o en el legacy
    pt_raw = property_types or property_type
    type_set = {t.strip().lower() for t in pt_raw.split(",")} if pt_raw else None

    # Inferir tipo por q si no se mandó property_types
    if not type_set and q:
        kw_map = {
            "casa": "casa", "casas": "casa",
            "departamento": "departamento", "depa": "departamento", "departamentos": "departamento",
            "local": "local", "locales": "local",
            "oficina": "oficina", "oficinas": "oficina",
            "bodega": "bodega", "bodegas": "bodega",
            "terreno": "terreno", "terrenos": "terreno",
        }
        qlow = q.lower()
        for k, v in kw_map.items():
            if k in qlow:
                type_set = {v}
                break

    city_set = {c.strip().lower() for c in cities.split(",")} if cities else None

    # 3) Filtrado
    def keep(l: Listing) -> bool:
        # Ciudad
        if city_set and (not l.location_city or l.location_city.lower() not in city_set):
            return False
        # Precio
        if min_price is not None and l.price < min_price:
            return False
        if max_price is not None and l.price > max_price:
            return False
        # Tipo
        if type_set and l.type.lower() not in type_set:
            return False
        # Recámaras (solo residencial)
        if min_bedrooms is not None and l.type.lower() in {"casa", "departamento"}:
            if (l.bedrooms or 0) < min_bedrooms:
                return False
        # Amueblado (si el listing no lo especifica, no filtramos)
        if furnished is not None and l.furnished is not None and l.furnished != furnished:
            return False
        # Texto libre
        if q and q.lower() not in l.title.lower():
            return False
        return True

    filtered = [l for l in data if keep(l)]

    # 4) Paginación
    page = filtered[offset: offset + limit]
    next_off = offset + limit if offset + limit < len(filtered) else None

    # 5) Log de depuración (útil para ver qué envía el GPT)
    print({
        "cities": cities, "min_price": min_price, "max_price": max_price,
        "min_bedrooms": min_bedrooms, "furnished": furnished,
        "property_types": list(type_set) if type_set else None,
        "q": q, "limit": limit, "offset": offset, "sources": sources
    })

    return SearchResponse(results=page, total=len(filtered), next_offset=next_off)
