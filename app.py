# app.py
from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Callable, Any, Coroutine, Tuple
import asyncio, time, re, os, json
import httpx
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

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
app = FastAPI(title="Inmo Aggregator", version="0.3.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def root():
    return {"service": "Inmo Aggregator", "status": "ok", "version": "0.3.1"}

# Render hace HEAD /; respondemos 200 para evitar 405 en logs
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
MAX_CARDS   = int(os.getenv("SCRAPE_MAX_CARDS", "60") or "60")

# =========================
# Utilidades de scraping
# =========================
PRICE_RE = re.compile(r"(\d[\d.,\s]*)")
BED_RE   = re.compile(r"(\d+)\s*(rec|recámaras|habitaciones|hab)", re.I)
BATH_RE  = re.compile(r"(\d+(?:\.\d+)?)\s*(baños|banos|ba\u00f1os|bath)", re.I)

def _to_abs(base: str, href: str) -> str:
    if not href: return ""
    if href.startswith(("http://","https://")): return href
    if href.startswith("//"): return "https:" + href
    if href.startswith("/"):
        b = base[:-1] if base.endswith("/") else base
        return b + href
    b = base if base.endswith("/") else base + "/"
    return b + href

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

def _split_city_colonia(loc_text: str) -> Tuple[Optional[str], Optional[str]]:
    if not loc_text: return (None, None)
    parts = [p.strip() for p in re.split(r"[\/,\-\|·•]+", loc_text) if p.strip()]
    city = parts[-1] if parts else None
    colonia = parts[0] if len(parts) >= 2 else None
    return (city, colonia)

async def fetch_html(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=25.0, headers={"User-Agent": "InmoAgg/0.3.1"}) as client:
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
                    price = _parse_price(str(offers.get("price") or offers.get("priceSpecification", {}).get("price") or ""))
                elif isinstance(offers, list) and offers:
                    price = _parse_price(str(offers[0].get("price") or ""))
                else:
                    price = _parse_price(name)
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

    # Estricto: solo enlaces de DETALLE (evitar CTAs tipo /propiedades)
    anchors = soup.select('a[href*="/propiedad/"]')
    seen = set()

    def is_bad_title(t: str) -> bool:
        if not t: return True
        t = t.strip().lower()
        bad = {"busca una propiedad", "buscar", "ver más", "ver mas", "contacto", "más info", "mas info", "ver detalles"}
        return t in bad or len(t) < 3

    for a in anchors[: MAX_CARDS * 3]:
        href = a.get("href") or ""
        abs_url = _to_abs(base_url, href)
        if not abs_url or abs_url in seen:
            continue
        seen.add(abs_url)

        # sube a contenedor de tarjeta
        card = a
        for _ in range(6):
            if not card or getattr(card, "name", "").lower() in ("body","html"):
                break
            if card.select_one("img") or card.select_one(".price, .precio, [class*='price'], [class*='precio']") \
               or card.select_one(".location, .ubicacion, [class*='ubicacion'], [class*='location']"):
                break
            card = card.parent

        # título
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

        # precio
        price_text = ""
        price_node = None
        if card:
            for sel in [".price",".precio","[class*='price']","[class*='precio']"]:
                cand = card.select_one(sel)
                if cand:
                    price_node = cand
                    break
        if price_node:
            price_text = price_node.get_text(strip=True)
        else:
            t = card.get_text(" ", strip=True) if card else ""
            m = re.search(r"\$\s*[\d.,\s]+", t)
            if m: price_text = m.group(0)
        price = _parse_price(price_text)

        # ubicación
        loc_text = ""
        if card:
            for sel in [".location",".ubicacion","[class*='ubicacion']","[class*='location']"]:
                cand = card.select_one(sel)
                if cand:
                    loc_text = cand.get_text(strip=True)
                    break
        city, colonia = _split_city_colonia(loc_text)

        # imagen
        photo_url = None
        if card:
            img = card.select_one("img")
            if img and img.get("src"):
                photo_url = _to_abs(base_url, img["src"])

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

    items = parse_jsonld_listings(html, base_url=_to_abs(url,""), source=source)
    if len(items) < 5:
        items += parse_cards_heuristic(html, base_url=_to_abs(url,""), source=source)

    dedup: Dict[str, Listing] = {}
    for it in items:
        if it.url and it.url not in dedup:
            dedup[it.url] = it
    out = list(dedup.values())[:MAX_CARDS]

    if out:
        CACHE.set(cache_key, out, ttl_seconds=TTL_SECONDS)
    return out

# =========================
# RE/MAX: descubrimiento por sitemap + detalle
# =========================
async def remax_discover_detail_urls(max_urls: int = 40) -> List[str]:
    roots = ["https://remax.com.mx/sitemap.xml"]
    seen = set()
    found: List[str] = []

    async def fetch_text(url: str) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "InmoAgg/0.3.1"}) as c:
                r = await c.get(url, follow_redirects=True)
                if r.status_code != 200:
                    print(f"[remax] sitemap HTTP {r.status_code}: {url}")
                    return None
                return r.text
        except Exception as e:
            print(f"[remax] sitemap error {url}: {e}")
            return None

    queue = list(roots)
    while queue and len(found) < max_urls:
        sm_url = queue.pop(0)
        if sm_url in seen:
            continue
        seen.add(sm_url)

        xml = await fetch_text(sm_url)
        if not xml:
            continue

        try:
            root = ET.fromstring(xml)
        except Exception as e:
            print(f"[remax] ET parse error {sm_url}: {e}")
            continue

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        for sm in root.findall("sm:sitemap", ns):
            loc = sm.find("sm:loc", ns)
            if loc is not None and loc.text:
                loc_text = loc.text.strip()
                if loc_text not in seen:
                    queue.append(loc_text)

        for u in root.findall("sm:url", ns):
            loc = u.find("sm:loc", ns)
            if loc is None or not loc.text:
                continue
            loc_text = loc.text.strip()
            if "/propiedad/" in loc_text:
                found.append(loc_text)
                if len(found) >= max_urls:
                    break

    uniq: List[str] = []
    seen_u = set()
    for u in found:
        if u not in seen_u:
            uniq.append(u)
            seen_u.add(u)
        if len(uniq) >= max_urls:
            break
    return uniq

def extract_from_property_detail(html: str, base_url: str, source: str, url: str, idx: int) -> Optional[Listing]:
    items = parse_jsonld_listings(html, base_url=base_url, source=source)
    if items:
        items_sorted = sorted(items, key=lambda x: (x.price or 0), reverse=True)
        best = items_sorted[0]
        best.url = url
        return best

    soup = BeautifulSoup(html, "html.parser")
    title = ""
    for sel in ["h1",".title",".titulo","[class*='title']","[class*='titulo']"]:
        h = soup.select_one(sel)
        if h:
            title = h.get_text(strip=True)
            break

    price_text = ""
    for sel in [".price",".precio","[class*='price']","[class*='precio']"]:
        p = soup.select_one(sel)
        if p:
            price_text = p.get_text(strip=True)
            break
    if not price_text:
        body = soup.get_text(" ", strip=True)
        m = re.search(r"\$\s*[\d.,\s]+", body)
        if m: price_text = m.group(0)

    img_url = None
    img = soup.select_one("img")
    if img and img.get("src"):
        img_url = _to_abs(base_url, img["src"])

    ltype = _infer_type(title)
    beds  = _beds(html.lower())
    baths = _baths(html.lower())

    if not title and _parse_price(price_text) == 0:
        return None

    return Listing(
        id=f"{source}-detail-{idx}",
        title=title or "Propiedad",
        price=_parse_price(price_text),
        currency="MXN",
        bedrooms=beds,
        bathrooms=baths,
        type=ltype,
        furnished=None,
        location_city=None,
        location_colonia=None,
        url=url,
        source=source,
        photos=[img_url] if img_url else [],
    )

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

SITES: Dict[str, str] = {
    "remax":                    "https://remax.com.mx/propiedades",
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
        return await scrape_generic_list(url, source=key)
    except Exception as e:
        print(f"[{key}] adapter error suprimido: {e}")
        return []

async def adapter_remax() -> List[Listing]:
    try:
        base_list_url = "https://remax.com.mx/propiedades"
        cards = await scrape_generic_list(base_list_url, source="remax")
        if cards:
            return cards

        detail_urls = await remax_discover_detail_urls(max_urls=40)
        if not detail_urls:
            return []

        listings: List[Listing] = []
        for i, u in enumerate(detail_urls):
            if len(listings) >= MAX_CARDS:
                break
            html = await fetch_html(u)
            if not html:
                continue
            item = extract_from_property_detail(
                html, base_url="https://remax.com.mx", source="remax", url=u, idx=i
            )
            if item:
                listings.append(item)

        if listings:
            CACHE.set("scrape::remax::fallback", listings, ttl_seconds=TTL_SECONDS)
        return listings
    except Exception as e:
        print(f"[remax] adapter error suprimido: {e}")
        return []

# Wrappers para el resto de sitios
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

# Registro
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
# Búsqueda
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
    groups = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[Listing] = []
    for g in groups:
        if isinstance(g, Exception):
            print(f"[adapter] error suprimido: {g}")
            continue
        out.extend(g)
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
    sources: Optional[str] = Query(None, description="CSV de fuentes (demo,remax,orva,...)"),
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
