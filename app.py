# app.py
from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Callable, Any, Coroutine
import asyncio, time, re, os
import httpx
from bs4 import BeautifulSoup

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
app = FastAPI(title="Inmo Aggregator", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def root():
    return {"service": "Inmo Aggregator", "status": "ok", "version": "0.3.0"}

@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)

@app.get("/health")
def health():
    return {"ok": True}

# =========================
# Cache simple (TTL)
# =========================
class SimpleCache:
    def __init__(self):
        self.data: Dict[str, Any] = {}
    def get(self, key: str):
        item = self.data.get(key)
        if not item: return None
        expiry, value = item
        if time.time() > expiry:
            self.data.pop(key, None)
            return None
        return value
    def set(self, key: str, value, ttl_seconds: int = 300):
        self.data[key] = (time.time() + ttl_seconds, value)

CACHE = SimpleCache()

TTL_SECONDS = int(os.getenv("SCRAPE_TTL_SECONDS", "300") or "300")
MAX_CARDS = int(os.getenv("SCRAPE_MAX_CARDS", "60") or "60")

# =========================
# Utilidades de scraping
# =========================
PRICE_RE = re.compile(r"(\d[\d.,\s]*)")
BED_RE   = re.compile(r"(\d+)\s*(rec|recámaras|habitaciones|hab)", re.I)
BATH_RE  = re.compile(r"(\d+(?:\.\d+)?)\s*(baños|banos|ba\u00f1os|bath)", re.I)

def _to_abs(url_base: str, href: str) -> str:
    if not href: return ""
    if href.startswith("http://") or href.startswith("https://"): return href
    if href.startswith("//"): return "https:" + href
    if href.startswith("/"):
        # normaliza host sin slash final
        base = url_base[:-1] if url_base.endswith("/") else url_base
        return base + href
    # relativo
    base = url_base if url_base.endswith("/") else url_base + "/"
    return base + href

def _parse_price(text: str) -> float:
    if not text: return 0.0
    raw = PRICE_RE.search(text.replace(",", ""))
    if not raw: return 0.0
    try: return float(raw.group(1).replace(" ", ""))
    except: return 0.0

def _infer_type(text: str) -> str:
    t = (text or "").lower()
    for k, v in [
        ("departamento", "departamento"),
        ("depto", "departamento"),
        ("casa", "casa"),
        ("oficina", "oficina"),
        ("bodega", "bodega"),
        ("local", "local"),
        ("terreno", "terreno"),
        ("edificio", "edificio"),
        ("loft", "departamento"),
        ("ph", "departamento"),
    ]:
        if k in t: return v
    return "otro"

def _beds(text: str) -> Optional[int]:
    if not text: return None
    m = BED_RE.search(text.lower())
    if not m: return None
    try: return int(m.group(1))
    except: return None

def _baths(text: str) -> Optional[float]:
    if not text: return None
    m = BATH_RE.search(text.lower())
    if not m: return None
    try: return float(m.group(1))
    except: return None

def _split_city_colonia(loc_text: str) -> (Optional[str], Optional[str]):
    if not loc_text: return (None, None)
    parts = [p.strip() for p in re.split(r"[\/,\-\|·•]+", loc_text) if p.strip()]
    city = parts[-1] if parts else None
    colonia = parts[0] if len(parts) >= 2 else None
    return (city, colonia)

async def fetch_html(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=25.0, headers={"User-Agent": "InmoAgg/0.3.0"}) as client:
            r = await client.get(url, follow_redirects=True)
            if r.status_code != 200:
                print(f"[scrape] HTTP {r.status_code} {url}")
                return None
            return r.text
    except httpx.HTTPError as e:
        print(f"[scrape] http error {url}: {e}")
        return None

def parse_jsonld_listings(html: str, base_url: str, source: str) -> List[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Listing] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "null")
        except Exception:
            continue
        # normaliza a lista
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
        else:
            continue
        for it in items:
            try:
                # RealEstateListing / Product / Offer
                name = str(it.get("name") or it.get("headline") or it.get("description") or "").strip()
                url  = _to_abs(base_url, str(it.get("url") or ""))
                # price
                price = 0.0
                offers = it.get("offers")
                if isinstance(offers, dict):
                    price = _parse_price(str(offers.get("price") or offers.get("priceSpecification", {}).get("price") or ""))
                elif isinstance(offers, list) and offers:
                    price = _parse_price(str(offers[0].get("price") or ""))
                else:
                    price = _parse_price(name)

                # address
                addr = it.get("address") or {}
                locality = None
                if isinstance(addr, dict):
                    locality = addr.get("addressLocality") or addr.get("addressRegion") or addr.get("streetAddress")
                elif isinstance(addr, str):
                    locality = addr

                # image
                img = it.get("image")
                photos: List[str] = []
                if isinstance(img, list):
                    photos = [ _to_abs(base_url, x) for x in img if isinstance(x, str) ]
                elif isinstance(img, str):
                    photos = [ _to_abs(base_url, img) ]

                # type guess / rooms
                fulltext = json.dumps(it, ensure_ascii=False).lower()
                ltype = _infer_type(name + " " + fulltext)
                beds  = _beds(fulltext)
                baths = _baths(fulltext)

                out.append(Listing(
                    id=f"{source}-jsonld-{len(out)}",
                    title=name or "Propiedad",
                    price=price,
                    currency="MXN",
                    bedrooms=beds,
                    bathrooms=baths,
                    type=ltype,
                    furnished=None,
                    location_city=locality,
                    location_colonia=None,
                    url=url or base_url,
                    source=source,
                    photos=photos,
                ))
            except Exception:
                continue
    return out

def parse_cards_heuristic(html: str, base_url: str, source: str) -> List[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Listing] = []

    # Aceptar SOLO páginas de detalle: /propiedad/...
    anchors = soup.select('a[href*="/propiedad/"]')
    seen = set()

    def is_bad_title(t: str) -> bool:
        if not t: return True
        t = t.strip().lower()
        # descartar CTAs y textos genéricos
        bad = {
            "busca una propiedad", "buscar", "ver más", "ver mas",
            "contacto", "más info", "mas info", "ver detalles"
        }
        return t in bad or len(t) < 3

    for a in anchors[: MAX_CARDS * 3]:
        href = a.get("href") or ""
        abs_url = _to_abs(base_url, href)
        if not abs_url or abs_url in seen: 
            continue
        seen.add(abs_url)

        # Subimos al contenedor de la tarjeta (article/.card/figure…)
        card = a
        for _ in range(6):
            if not card or getattr(card, "name", "").lower() in ("body", "html"):
                break
            if card.select_one("img") or card.select_one(".price, .precio, [class*='price'], [class*='precio']") \
               or card.select_one(".location, .ubicacion, [class*='ubicacion'], [class*='location']"):
                break
            card = card.parent

        # Título
        title = (a.get_text(strip=True) or "").strip()
        if is_bad_title(title):
            # prueba con encabezados dentro de la tarjeta
            h = None
            for sel in ["h1","h2","h3",".card-title",".titulo",".title","[class*='titulo']","[class*='title']"]:
                h = card.select_one(sel) if card else None
                if h:
                    title = h.get_text(strip=True)
                    break

        if is_bad_title(title):
            # si sigue siendo malo, descártalo
            continue

        # Precio
        price_text = ""
        price_node = None
        if card:
            for sel in [".price", ".precio", "[class*='price']", "[class*='precio']"]:
                cand = card.select_one(sel)
                if cand:
                    price_node = cand
                    break
        if price_node:
            price_text = price_node.get_text(strip=True)
        else:
            # como fallback, busca un patrón de $ dentro del texto de la tarjeta
            t = card.get_text(" ", strip=True) if card else ""
            m = re.search(r"\$\s*[\d.,\s]+", t)
            if m:
                price_text = m.group(0)

        price = _parse_price(price_text)

        # Ubicación
        loc_text = ""
        if card:
            for sel in [".location", ".ubicacion", "[class*='ubicacion']", "[class*='location']"]:
                cand = card.select_one(sel)
                if cand:
                    loc_text = cand.get_text(strip=True)
                    break

        city, colonia = _split_city_colonia(loc_text)

        # Imagen
        photo_url = None
        if card:
            img = card.select_one("img")
            if img and img.get("src"):
                photo_url = _to_abs(base_url, img["src"])

        # Tipo + cuartos/baños (heurístico por texto)
        card_text = ((title or "") + " " + (loc_text or "")).lower()
        ltype = _infer_type(card_text)
        beds  = _beds(card_text)
        baths = _baths(card_text)

        out.append(Listing(
            id=f"{source}-card-{len(out)}",
            title=title or "Propiedad",
            price=price,
            currency="MXN",
            bedrooms=beds,
            bathrooms=baths,
            type=ltype,
            furnished=None,
            location_city=city,
            location_colonia=colonia,
            url=abs_url,
            source=source,
            photos=[photo_url] if photo_url else [],
        ))

        if len(out) >= MAX_CARDS:
            break

    return out

async def scrape_generic_list(url: str, source: str) -> List[Listing]:
    cache_key = f"scrape::{source}::{url}"
    cached = CACHE.get(cache_key)
    if cached is not None:
        return cached

    html = await fetch_html(url)
    if not html:
        return []

    # 1) JSON-LD primero
    items = parse_jsonld_listings(html, base_url=_to_abs(url, ""), source=source)
    # 2) Si no alcanzó, intenta heurística de tarjetas
    if len(items) < 5:
        items += parse_cards_heuristic(html, base_url=_to_abs(url, ""), source=source)

    # dedupe por URL
    dedup: Dict[str, Listing] = {}
    for it in items:
        if it.url and it.url not in dedup:
            dedup[it.url] = it
    out = list(dedup.values())[:MAX_CARDS]

    if out:
        CACHE.set(cache_key, out, ttl_seconds=TTL_SECONDS)
    return out

# =========================
# Adapters (demo + sitios)
# =========================
async def adapter_demo() -> List[Listing]:
    await asyncio.sleep(0.01)
    return [
        Listing(
            id="demo1", title="Depto 2 rec Col. Primavera", price=9000,
            bedrooms=2, bathrooms=1, type="departamento", furnished=False,
            location_city="Tampico", location_colonia="Primavera",
            url="https://ejemplo.local/depto-primavera", source="demo", photos=[]),
        Listing(
            id="demo2", title="Casa 3 rec Col. Del Bosque", price=18000,
            bedrooms=3, bathrooms=2, type="casa",
            location_city="Tampico", location_colonia="Del Bosque",
            url="https://ejemplo.local/casa-bosque", source="demo", photos=[]),
        Listing(
            id="demo3", title="Local comercial Centro", price=12000,
            bedrooms=None, bathrooms=1, type="local", furnished=False,
            location_city="Ciudad Madero", location_colonia="Centro",
            url="https://ejemplo.local/local-centro", source="demo", photos=[]),
    ]

# Mapa de sitios → URL de listado "obvia" (ajustable)
SITES: Dict[str, str] = {
    "remax":                    "https://remax.com.mx/propiedades",
    "aysainmobiliaria":        "https://www.aysainmobiliaria.com.mx/",               # revisar subruta de propiedades
    "cuvier":                  "https://cuvierbienesraices.inmo.co/",               # suele tener /propiedades
    "suma":                    "https://www.sumabienes.com/",                       # revisar /propiedades /listings
    "orva":                    "https://www.orvabienes.com.mx/Propiedades",
    "elizondo":                "https://www.elizondoinmuebles.com/",                # ajustar a catálogo
    "irles":                   "https://irles.mx/",                                 # ajustar
    "monteforte":              "https://www.monteforte.com.mx/",                    # ajustar
    "torres":                  "https://www.torresbienesraices.com/",               # ajustar
    "altara":                  "https://www.altararealestate.com/",                 # ajustar
    "altarainmobiliaria":      "https://www.altaraminmobiliaria.com/",              # ajustar
    "prourbe":                 "https://www.prourbe.mx/",                           # ajustar
    "inmobiliariaentampico":   "https://www.inmobiliariaentampico.com/",            # ajustar
    "elite":                   "https://www.bienesraiceselite.com.mx/",             # ajustar
    "leemar":                  "https://www.leemar.mx/",                             # ajustar
    "logica":                  "https://www.logicainmobiliaria.com.mx/",            # ajustar
    "realestatetampico":       "https://www.realestatetampico.com/",                # ajustar
    "3a":                      "https://www.3ainmobiliaria.mx/Propiedades",
    "inmobiliarialm":          "https://inmobiliarialm.com/",                        # ajustar
    "asesoresinmobiliarioss":  "https://www.asesoresinmobiliarioss.com/",           # ajustar
    "gdinmobiliarias":         "https://www.gdinmobiliarias.com/",                  # ajustar
    "neolife":                 "https://www.neolifeinmobiliaria.com/",              # ajustar
    "gestion365":              "https://www.gestioninmobiliaria365.com/",           # ajustar
}

# Adaptadores que llaman al motor genérico (uno por sitio)
async def adapter_site(key: str) -> List[Listing]:
    url = SITES.get(key)
    if not url: return []
    return await scrape_generic_list(url, source=key)

# Creamos funciones concretas para cada clave (por claridad)
async def adapter_remax() -> List[Listing]:                   return await adapter_site("remax")
async def adapter_aysainmobiliaria() -> List[Listing]:        return await adapter_site("aysainmobiliaria")
async def adapter_cuvier() -> List[Listing]:                  return await adapter_site("cuvier")
async def adapter_suma() -> List[Listing]:                    return await adapter_site("suma")
async def adapter_orva() -> List[Listing]:                    return await adapter_site("orva")
async def adapter_elizondo() -> List[Listing]:                return await adapter_site("elizondo")
async def adapter_irles() -> List[Listing]:                   return await adapter_site("irles")
async def adapter_monteforte() -> List[Listing]:              return await adapter_site("monteforte")
async def adapter_torres() -> List[Listing]:                  return await adapter_site("torres")
async def adapter_altara() -> List[Listing]:                  return await adapter_site("altara")
async def adapter_altarainmobiliaria() -> List[Listing]:      return await adapter_site("altarainmobiliaria")
async def adapter_prourbe() -> List[Listing]:                 return await adapter_site("prourbe")
async def adapter_inmobiliariaentampico() -> List[Listing]:   return await adapter_site("inmobiliariaentampico")
async def adapter_elite() -> List[Listing]:                   return await adapter_site("elite")
async def adapter_leemar() -> List[Listing]:                  return await adapter_site("leemar")
async def adapter_logica() -> List[Listing]:                  return await adapter_site("logica")
async def adapter_realestatetampico() -> List[Listing]:       return await adapter_site("realestatetampico")
async def adapter_3a() -> List[Listing]:                      return await adapter_site("3a")
async def adapter_inmobiliarialm() -> List[Listing]:          return await adapter_site("inmobiliarialm")
async def adapter_asesoresinmobiliarioss() -> List[Listing]:  return await adapter_site("asesoresinmobiliarioss")
async def adapter_gdinmobiliarias() -> List[Listing]:         return await adapter_site("gdinmobiliarias")
async def adapter_neolife() -> List[Listing]:                 return await adapter_site("neolife")
async def adapter_gestion365() -> List[Listing]:              return await adapter_site("gestion365")

# Registro de adapters
ADAPTERS: Dict[str, Callable[[], Coroutine[Any, Any, List[Listing]]]] = {
    "demo": adapter_demo,
    "remax": adapter_remax,
    "aysainmobiliaria": adapter_aysainmobiliaria,
    "cuvier": adapter_cuvier,
    "suma": adapter_suma,
    "orva": adapter_orva,
    "elizondo": adapter_elizondo,
    "irles": adapter_irles,
    "monteforte": adapter_monteforte,
    "torres": adapter_torres,
    "altara": adapter_altara,
    "altarainmobiliaria": adapter_altarainmobiliaria,
    "prourbe": adapter_prourbe,
    "inmobiliariaentampico": adapter_inmobiliariaentampico,
    "elite": adapter_elite,
    "leemar": adapter_leemar,
    "logica": adapter_logica,
    "realestatetampico": adapter_realestatetampico,
    "3a": adapter_3a,
    "inmobiliarialm": adapter_inmobiliarialm,
    "asesoresinmobiliarioss": adapter_asesoresinmobiliarioss,
    "gdinmobiliarias": adapter_gdinmobiliarias,
    "neolife": adapter_neolife,
    "gestion365": adapter_gestion365,
}

# =========================
# Búsqueda (mismo contrato que ya tenías)
# =========================
class SearchRequest(BaseModel):
    cities: Optional[List[str]] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_bedrooms: Optional[int] = None
    furnished: Optional[bool] = None
    property_types: Optional[List[str]] = None
    q: Optional[str] = None
    sources: Optional[List[str]] = None
    offset: int = 0
    limit: int = 25

def _csv_to_list(s: Optional[str]) -> Optional[List[str]]:
    if not s: return None
    return [x.strip() for x in s.split(",") if x.strip()]

async def _collect_sources(sources: Optional[List[str]]) -> List[Listing]:
    keys = sources if sources else list(ADAPTERS.keys())
    tasks = [ADAPTERS[k]() for k in keys if k in ADAPTERS]
    if not tasks: return []
    groups = await asyncio.gather(*tasks)
    out: List[Listing] = []
    for g in groups:
        out.extend(g)
    return out

def _passes_filters(l: Listing, q: SearchRequest) -> bool:
    if q.cities:
        if (l.location_city or "").lower() not in {c.lower() for c in q.cities}:
            return False
    if q.min_price is not None and l.price < q.min_price: return False
    if q.max_price is not None and l.price > q.max_price: return False
    if q.min_bedrooms is not None:
        tipo = (l.type or "").lower()
        if tipo in {"casa", "departamento", "depto"}:
            if l.bedrooms is None or l.bedrooms < q.min_bedrooms:
                return False
    if q.furnished is not None:
        if l.furnished is None or l.furnished is not q.furnished:
            return False
    if q.property_types:
        if (l.type or "").lower() not in {t.lower() for t in q.property_types}:
            return False
    if q.q and q.q.lower() not in (l.title or "").lower():
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
@app.get("/search", response_model=SearchResponse)
async def search_get(
    cities: Optional[str] = Query(None, description="CSV de ciudades"),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    min_bedrooms: Optional[int] = Query(None, ge=0),
    furnished: Optional[bool] = Query(None),
    property_types: Optional[str] = Query(None, description="CSV de tipos"),
    q: Optional[str] = Query(None),
    sources: Optional[str] = Query(None, description="CSV de fuentes (demo,remax,...)"),
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

@app.get("/search-listings", response_model=SearchResponse)
async def search_listings_alias(**kwargs):
    return await search_get(**kwargs)

@app.get("/searchListings", response_model=SearchResponse)
async def searchListings_alias(**kwargs):
    return await search_get(**kwargs)

@app.post("/search", response_model=SearchResponse)
async def search_post(req: SearchRequest):
    return await _search_core(req)
