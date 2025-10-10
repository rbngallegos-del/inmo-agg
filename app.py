# app.py (v0.5.0) — sin REMAX
from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Callable, Any, Coroutine, Tuple
import asyncio, time, re, os, json, random
import httpx
from bs4 import BeautifulSoup

APP_VERSION = "0.5.0"

# --- Config ---
TTL_SECONDS = int(os.getenv("SCRAPE_TTL_SECONDS", "300") or "300")
NEG_TTL_SECONDS = int(os.getenv("SCRAPE_NEG_TTL_SECONDS", "120") or "120")
MAX_CARDS   = int(os.getenv("SCRAPE_MAX_CARDS", "60") or "60")
ADAPTER_TIMEOUT = float(os.getenv("ADAPTER_TIMEOUT_SECONDS", "8"))
CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "4") or "4")
DEFAULT_SOURCES = os.getenv("DEFAULT_SOURCES", "demo")

SITE_TIMEOUTS: Dict[str, float] = {}  # puedes tunear por sitio luego

# --- Modelos ---
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

# --- App ---
app = FastAPI(title="Inmo Aggregator", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def root():
    return {"service": "Inmo Aggregator", "status": "ok", "version": APP_VERSION}

@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)

@app.get("/health")
def health():
    return {"ok": True}

# --- Cache simple en memoria ---
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
SEM = asyncio.Semaphore(CONCURRENCY_LIMIT)

def cache_get_pos_or_neg(key: str):
    return CACHE.get(key)

def cache_set_pos(key: str, value):
    CACHE.set(key, value if value else [], ttl_seconds=TTL_SECONDS if value else NEG_TTL_SECONDS)

# --- Utilidades scraping genérico ---
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
]

PRICE_RE = re.compile(r"(\d[\d.,\s]*)")
BED_RE   = re.compile(r"(\d+)\s*(rec|recámaras|recamaras|habitaciones|hab|bed|cuartos?)", re.I)
BATH_RE  = re.compile(r"(\d+(?:\.\d+)?)\s*(baños|banos|ba\u00f1os|bath|baño)", re.I)

def _headers():
    return {
        "User-Agent": random.choice(UA_LIST),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.google.com/",
    }

def _to_abs(base: str, href: str) -> str:
    if not href: return ""
    if href.startswith(("http://","https://")): return href
    if href.startswith("//"): return "https:" + href
    if href.startswith("/"):
        b = base[:-1] if base.endswith("/") else base
        return b + href
    b = base if base.endswith("/") else base + "/"
    return b + href

def _normalize(s: str) -> str:
    return (s or "").replace("\xa0", " ").replace("\u202f", " ").replace("\u2009", " ").replace("\u2007", " ")

def _parse_price_from_text(text: str) -> float:
    if not text: return 0.0
    t = _normalize(text).lower()
    # $ 4,200,000 MXN / MN
    m = re.search(r"\$\s*([\d.,\s]+)", t)
    if m:
        try: return float(m.group(1).replace(",", "").replace(" ", ""))
        except: pass
    # 4.2 mdp / 4,2 millones
    m2 = re.search(r"([\d]+(?:[.,]\d+)?)\s*(mdp|m\.?d\.?p\.?|millones?)", t, re.I)
    if m2:
        num = m2.group(1).replace(",", ".")
        try: return float(num) * 1_000_000
        except: pass
    # precio: 4200000
    m3 = PRICE_RE.search(t.replace(",", ""))
    if m3:
        try: return float(m3.group(1).replace(" ", ""))
        except: pass
    return 0.0

def _pick_best_price(cands: List[float]) -> float:
    valid = [x for x in cands if x and 10_000 <= x <= 200_000_000]
    return max(valid) if valid else 0.0

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
    try: return int(m.group(1)) if m else None
    except: return None

def _baths(text: str) -> Optional[float]:
    if not text: return None
    m = BATH_RE.search(text.lower())
    try: return float(m.group(1)) if m else None
    except: return None

def _split_city_colonia(loc_text: str) -> Tuple[Optional[str], Optional[str]]:
    if not loc_text: return (None, None)
    parts = [p.strip() for p in re.split(r"[\/,\-\|·•,]+", loc_text) if p.strip()]
    city = parts[-1] if parts else None
    colonia = parts[0] if len(parts) >= 2 else None
    return (city, colonia)

def _httpx_timeout(read: float = 4.0):
    return httpx.Timeout(connect=4.0, read=read, write=4.0, pool=4.0)

async def fetch_html(url: str, read_timeout: float = 4.0, tries: int = 2) -> Optional[str]:
    delay = 0.6
    for attempt in range(tries):
        try:
            async with httpx.AsyncClient(timeout=_httpx_timeout(read=read_timeout),
                                         headers=_headers()) as client:
                r = await client.get(url, follow_redirects=True)
                if r.status_code != 200:
                    print(f"[scrape] HTTP {r.status_code} {url}")
                    raise httpx.HTTPStatusError("bad status", request=r.request, response=r)
                return r.text
        except Exception as e:
            print(f"[scrape] http error {url}: {e}")
            if attempt + 1 < tries:
                await asyncio.sleep(delay); delay *= 1.8
    return None

def _meta(soup: BeautifulSoup, key: str) -> Optional[str]:
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key}) \
          or soup.find("meta", attrs={"itemprop": key})
    return (tag.get("content") or "").strip() if tag and tag.get("content") else None

def _clean_photos_generic(urls: List[str], base_url: str) -> List[str]:
    """
    Mantén JPG/JPEG y filtra obvios íconos/logos/miniaturas por nombre.
    (No dependemos de un CDN específico.)
    """
    out: List[str] = []
    ban_words = ["icon", "logo", "sprite", "placeholder", "thumb", "avatar", "svg"]
    for u in urls:
        if not u: continue
        absu = _to_abs(base_url, u)
        low = absu.lower()
        if not (low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png")):
            continue
        if any(b in low for b in ban_words):
            continue
        if absu not in out:
            out.append(absu)
    # Priorizamos JPG/JPEG sobre PNG
    out_sorted = sorted(out, key=lambda x: (x.lower().endswith(".png"), x))
    return out_sorted[:12]

# --- Parsers genéricos ---
def parse_jsonld_listings(html: str, base_url: str, source: str) -> List[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Listing] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "null")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        for it in items:
            try:
                name = str(it.get("name") or it.get("headline") or it.get("description") or "").strip()
                url  = _to_abs(base_url, str(it.get("url") or ""))
                price = 0.0
                offers = it.get("offers")
                if isinstance(offers, dict):
                    price = _parse_price_from_text(str(offers.get("price") or offers.get("priceSpecification", {}).get("price") or ""))
                elif isinstance(offers, list) and offers:
                    price = _parse_price_from_text(str(offers[0].get("price") or ""))
                else:
                    price = _parse_price_from_text(name)
                addr = it.get("address") or {}
                locality = addr.get("addressLocality") if isinstance(addr, dict) else (addr if isinstance(addr, str) else None)
                img = it.get("image")
                photos: List[str] = []
                if isinstance(img, list):
                    photos = [_to_abs(base_url, x) for x in img if isinstance(x, str)]
                elif isinstance(img, str):
                    photos = [_to_abs(base_url, img)]
                fulltext = json.dumps(it, ensure_ascii=False).lower()
                ltype = _infer_type(name + " " + fulltext)
                beds  = _beds(fulltext); baths = _baths(fulltext)
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
                    photos=_clean_photos_generic(photos, base_url),
                ))
            except Exception:
                continue
    return out

def parse_cards_heuristic(html: str, base_url: str, source: str) -> List[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Listing] = []
    anchors = soup.select('a[href]')
    seen = set()

    def is_bad_title(t: str) -> bool:
        if not t: return True
        t = t.strip().lower()
        bad = {"busca una propiedad", "buscar", "ver más", "ver mas", "contacto", "más info", "mas info", "ver detalles"}
        return t in bad or len(t) < 3

    for a in anchors[: MAX_CARDS * 5]:
        href = a.get("href") or ""
        abs_url = _to_abs(base_url, href)
        if not abs_url or abs_url in seen:
            continue
        # Heurística: anclas que parecen cards (tienen img o un bloque con precio/ubicación)
        card = a
        for _ in range(6):
            if not card or getattr(card, "name", "").lower() in ("body","html"):
                break
            if card.select_one("img") or card.select_one(".price, .precio, [class*='price'], [class*='precio']") \
               or card.select_one(".location, .ubicacion, [class*='ubicacion'], [class*='location']"):
                break
            card = card.parent

        # Evita menús/footers vacíos
        title = (a.get_text(strip=True) or "").strip()
        if is_bad_title(title):
            h = None
            for sel in ["h1","h2","h3",".card-title",".titulo",".title","[class*='titulo']","[class*='title']"]:
                h = card.select_one(sel) if card else None
                if h:
                    title = h.get_text(strip=True)
                    break
        if is_bad_title(title):
            continue

        # Precio
        price_text = ""
        if card:
            for sel in [".price",".precio","[class*='price']","[class*='precio']"]:
                cand = card.select_one(sel)
                if cand:
                    price_text = cand.get_text(strip=True)
                    break
        if not price_text and card:
            t = card.get_text(" ", strip=True)
            m = re.search(r"\$\s*[\d.,\s]+", t)
            if m: price_text = m.group(0)
        price = _parse_price_from_text(price_text)

        # Ubicación
        loc_text = ""
        if card:
            for sel in [".location",".ubicacion","[class*='ubicacion']","[class*='location']"]:
                cand = card.select_one(sel)
                if cand:
                    loc_text = cand.get_text(strip=True)
                    break
        city, colonia = _split_city_colonia(loc_text)

        # Foto
        photo_url = None
        if card:
            img = card.select_one("img")
            if img and img.get("src"):
                photo_url = _to_abs(base_url, img["src"])

        card_text = ((title or "") + " " + (loc_text or "")).lower()
        ltype = _infer_type(card_text)
        beds  = _beds(card_text)
        baths = _baths(card_text)

        photos = _clean_photos_generic([photo_url] if photo_url else [], base_url)

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
            photos=photos,
        ))
        seen.add(abs_url)
        if len(out) >= MAX_CARDS: break
    return out

async def scrape_generic_list(url: str, source: str, read_timeout: float = 5.0) -> List[Listing]:
    cache_key = f"scrape::{source}::{url}"
    cached = cache_get_pos_or_neg(cache_key)
    if cached is not None:
        return cached
    html = await fetch_html(url, read_timeout=read_timeout, tries=2)
    if not html:
        cache_set_pos(cache_key, [])
        return []
    items = parse_jsonld_listings(html, base_url=_to_abs(url,""), source=source)
    if len(items) < 5:
        items += parse_cards_heuristic(html, base_url=_to_abs(url,""), source=source)
    # dedupe por URL
    dedup: Dict[str, Listing] = {}
    for it in items:
        if it.url and it.url not in dedup:
            dedup[it.url] = it
    out = list(dedup.values())[:MAX_CARDS]
    cache_set_pos(cache_key, out)
    return out

# --- Adapters (sin remax) ---
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

SITES: Dict[str, str] = {
    "aysainmobiliaria":        "https://www.aysainmobiliaria.com.mx/",
    "cuvier":                  "https://cuvierbienesraices.inmo.co/",
    "suma":                    "https://www.sumabienes.com/",
    "orva":                    "https://www.orvabienes.com.mx/Propiedades",
    "elizondo":                "https://www.elizondoinmuebles.com/",
    "irles":                   "https://irles.mx/",
    "monteforte":              "https://www.monteforte.com.mx/",
    "torres":                  "https://www.torresbienesraices.com/",
    "altara":                  "https://www.altararealestate.com/",
    "altarainmobiliaria":      "https://www.altaraminmobiliaria.com/",
    "prourbe":                 "https://www.prourbe.mx/",
    "inmobiliariaentampico":   "https://www.inmobiliariaentampico.com/",
    "elite":                   "https://www.bienesraiceselite.com.mx/",
    "leemar":                  "https://www.leemar.mx/",
    "logica":                  "https://www.logicainmobiliaria.com.mx/",
    "realestatetampico":       "https://www.realestatetampico.com/",
    "3a":                      "https://www.3ainmobiliaria.mx/Propiedades",
    "inmobiliarialm":          "https://inmobiliarialm.com/",
    "asesoresinmobiliarioss":  "https://www.asesoresinmobiliarioss.com/",
    "gdinmobiliarias":         "https://www.gdinmobiliarias.com/",
    "neolife":                 "https://www.neolifeinmobiliaria.com/",
    "gestion365":              "https://www.gestioninmobiliaria365.com/",
}

async def adapter_site(key: str) -> List[Listing]:
    url = SITES.get(key)
    if not url: return []
    try:
        return await scrape_generic_list(url, source=key, read_timeout=5.0)
    except Exception as e:
        print(f"[{key}] adapter error suprimido: {e}")
        return []

# Registro de adapters
ADAPTERS: Dict[str, Callable[[], Coroutine[Any, Any, List[Listing]]]] = {
    "demo": adapter_demo,
    "aysainmobiliaria": adapter_site,
    "cuvier": adapter_site,
    "suma": adapter_site,
    "orva": adapter_site,
    "elizondo": adapter_site,
    "irles": adapter_site,
    "monteforte": adapter_site,
    "torres": adapter_site,
    "altara": adapter_site,
    "altarainmobiliaria": adapter_site,
    "prourbe": adapter_site,
    "inmobiliariaentampico": adapter_site,
    "elite": adapter_site,
    "leemar": adapter_site,
    "logica": adapter_site,
    "realestatetampico": adapter_site,
    "3a": adapter_site,
    "inmobiliarialm": adapter_site,
    "asesoresinmobiliarioss": adapter_site,
    "gdinmobiliarias": adapter_site,
    "neolife": adapter_site,
    "gestion365": adapter_site,
}

# --- Búsqueda ---
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

async def _run_adapter(key: str, fn: Callable[[], Coroutine[Any, Any, List[Listing]]]) -> List[Listing]:
    async with SEM:
        timeout = SITE_TIMEOUTS.get(key, ADAPTER_TIMEOUT)
        try:
            # adapter_site necesita saber el key -> usamos un wrapper simple
            if fn is adapter_site:
                async def run(): return await adapter_site(key)
                return await asyncio.wait_for(run(), timeout=timeout)
            else:
                return await asyncio.wait_for(fn(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"[adapter:{key}] timeout > {timeout}s")
            return []
        except Exception as e:
            print(f"[adapter:{key}] error suprimido: {e}")
            return []

async def _collect_sources(sources: Optional[List[str]]) -> List[Listing]:
    keys = sources if sources else [s.strip() for s in DEFAULT_SOURCES.split(",") if s.strip()]
    tasks = [asyncio.create_task(_run_adapter(k, ADAPTERS[k])) for k in keys if k in ADAPTERS]
    if not tasks: return []
    results = await asyncio.gather(*tasks, return_exceptions=False)
    out: List[Listing] = []
    for res in results: out.extend(res)
    return out

def _passes_filters(l: Listing, q: SearchRequest) -> bool:
    if q.cities and (l.location_city or "").lower() not in {c.lower() for c in q.cities}:
        return False
    if q.min_price is not None and l.price < q.min_price: return False
    if q.max_price is not None and l.price > q.max_price: return False
    if q.min_bedrooms is not None:
        tipo = (l.type or "").lower()
        if tipo in {"casa","departamento","depto"}:
            if l.bedrooms is None or l.bedrooms < q.min_bedrooms:
                return False
    if q.furnished is not None:
        if l.furnished is None or l.furnished is not q.furnished:
            return False
    if q.property_types and (l.type or "").lower() not in {t.lower() for t in q.property_types}:
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

# --- Endpoints ---
@app.get("/search", response_model=SearchResponse)
async def search_get(
    cities: Optional[str] = Query(None, description="CSV de ciudades"),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    min_bedrooms: Optional[int] = Query(None, ge=0),
    furnished: Optional[bool] = Query(None),
    property_types: Optional[str] = Query(None, description="CSV de tipos"),
    q: Optional[str] = Query(None),
    sources: Optional[str] = Query(None, description="CSV de fuentes (demo,orva,3a,...)"),
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
