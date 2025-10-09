# app.py
from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Callable, Any, Coroutine, Tuple
import asyncio, time, re, os, json, gzip, random
import httpx
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

# =========================
# Config
# =========================
APP_VERSION = "0.3.9"
TTL_SECONDS = int(os.getenv("SCRAPE_TTL_SECONDS", "300") or "300")
NEG_TTL_SECONDS = int(os.getenv("SCRAPE_NEG_TTL_SECONDS", "120") or "120")
MAX_CARDS   = int(os.getenv("SCRAPE_MAX_CARDS", "60") or "60")
ADAPTER_TIMEOUT = float(os.getenv("ADAPTER_TIMEOUT_SECONDS", "8"))
CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "4") or "4")
DEFAULT_SOURCES = os.getenv("DEFAULT_SOURCES", "demo")

# Semillas RE/MAX
REMAX_SEEDS = [u.strip() for u in (os.getenv("REMAX_SEEDS", "https://remax.com.mx/propiedad/642420") or "").split(",") if u.strip()]

SITE_TIMEOUTS: Dict[str, float] = {"remax": 20.0}

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

# =========================
# Cache simple
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
SEM = asyncio.Semaphore(CONCURRENCY_LIMIT)

def cache_get_pos_or_neg(key: str):
    return CACHE.get(key)

def cache_set_pos(key: str, value):
    CACHE.set(key, value if value else [], ttl_seconds=TTL_SECONDS if value else NEG_TTL_SECONDS)

# =========================
# Utilidades
# =========================
PRICE_RE = re.compile(r"(\d[\d.,\s]*)")
BED_RE   = re.compile(r"(\d+)\s*(rec|recámaras|recamaras|habitaciones|hab|bed|cuartos?)", re.I)
BATH_RE  = re.compile(r"(\d+(?:\.\d+)?)\s*(baños|banos|ba\u00f1os|bath|baño)", re.I)

REMAX_ID_ABS_RE = re.compile(r"https?://(?:www\.)?remax\.com\.mx/propiedad/(\d+)")
REMAX_ID_REL_RE = re.compile(r"/propiedad/(\d+)")

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
]

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

def _parse_price_from_text(text: str) -> float:
    if not text: return 0.0
    # $ 4,200,000
    m = re.search(r"\$\s*([\d.,\s]+)", text)
    if m:
        try: return float(m.group(1).replace(",", "").replace(" ", ""))
        except: pass
    # Precio: 4200000 / 4,200,000 MXN / MN
    m = re.search(r"(?:precio|mxn|mn)\s*[:\-]?\s*([\d.,\s]+)", text, re.I)
    if m:
        try: return float(m.group(1).replace(",", "").replace(" ", ""))
        except: pass
    # genérico
    m = PRICE_RE.search(text.replace(",", ""))
    if m:
        try: return float(m.group(1).replace(" ", ""))
        except: pass
    return 0.0

def _pick_best_price(cands: List[float]) -> float:
    # filtra valores ridículos y toma el mayor
    valid = [x for x in cands if x and 10000 <= x <= 200_000_000]
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
                await asyncio.sleep(delay)
                delay *= 1.8
    return None

# ============ Parsers genéricos ============
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
        card = a
        for _ in range(6):
            if not card or getattr(card, "name", "").lower() in ("body","html"):
                break
            if card.select_one("img") or card.select_one(".price, .precio, [class*='price'], [class*='precio']") \
               or card.select_one(".location, .ubicacion, [class*='ubicacion'], [class*='location']"):
                break
            card = card.parent
        title = (a.get_text(strip=True) or "").strip()
        if is_bad_title(title):
            h = None
            for sel in ["h1","h2","h3",".card-title",".titulo",".title","[class*='titulo']","[class*='title']"]:
                h = card.select_one(sel) if card else None
                if h: title = h.get_text(strip=True); break
        if is_bad_title(title): continue
        price_text = ""
        if card:
            for sel in [".price",".precio","[class*='price']","[class*='precio']"]:
                cand = card.select_one(sel)
                if cand: price_text = cand.get_text(strip=True); break
        if not price_text:
            t = card.get_text(" ", strip=True) if card else ""
            m = re.search(r"\$\s*[\d.,\s]+", t)
            if m: price_text = m.group(0)
        price = _parse_price_from_text(price_text)
        loc_text = ""
        if card:
            for sel in [".location",".ubicacion","[class*='ubicacion']","[class*='location']"]:
                cand = card.select_one(sel)
                if cand: loc_text = cand.get_text(strip=True); break
        city, colonia = _split_city_colonia(loc_text)
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
        if len(out) >= MAX_CARDS: break
    return out

async def scrape_generic_list(url: str, source: str, read_timeout: float = 4.0) -> List[Listing]:
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
    dedup: Dict[str, Listing] = {}
    for it in items:
        if it.url and it.url not in dedup:
            dedup[it.url] = it
    out = list(dedup.values())[:MAX_CARDS]
    cache_set_pos(cache_key, out)
    return out

# =========================
# RE/MAX helpers
# =========================
def _extract_embedded_state_ids(html: str) -> List[str]:
    urls: List[str] = []
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S | re.I)
    if m:
        try:
            data = json.loads(m.group(1))
            text = json.dumps(data, ensure_ascii=False)
            for m2 in REMAX_ID_ABS_RE.finditer(text):
                urls.append(f"https://www.remax.com.mx/propiedad/{m2.group(1)}")
            for m3 in REMAX_ID_REL_RE.finditer(text):
                urls.append(f"https://www.remax.com.mx/propiedad/{m3.group(1)}")
        except Exception:
            pass
    m = re.search(r"window\.__NUXT__\s*=\s*(\{.*?\});", html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            text = json.dumps(data, ensure_ascii=False)
            for m2 in REMAX_ID_ABS_RE.finditer(text):
                urls.append(f"https://www.remax.com.mx/propiedad/{m2.group(1)}")
            for m3 in REMAX_ID_REL_RE.finditer(text):
                urls.append(f"https://www.remax.com.mx/propiedad/{m3.group(1)}")
        except Exception:
            pass
    m = re.search(r"__APOLLO_STATE__\s*=\s*(\{.*?\});", html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            text = json.dumps(data, ensure_ascii=False)
            for m2 in REMAX_ID_ABS_RE.finditer(text):
                urls.append(f"https://www.remax.com.mx/propiedad/{m2.group(1)}")
            for m3 in REMAX_ID_REL_RE.finditer(text):
                urls.append(f"https://www.remax.com.mx/propiedad/{m3.group(1)}")
        except Exception:
            pass
    seen = set(); out: List[str] = []
    for u in urls:
        if u not in seen:
            out.append(u); seen.add(u)
    return out

async def remax_discover_detail_urls(max_urls: int = 30, max_sitemaps: int = 6) -> List[str]:
    roots = ["https://www.remax.com.mx/sitemap.xml"]
    seen = set(); seen_sitemaps = 0; found: List[str] = []
    async def fetch_sitemap(url: str) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=_httpx_timeout(read=8.0),
                                         headers=_headers()) as c:
                r = await c.get(url, follow_redirects=True)
                if r.status_code != 200:
                    print(f"[remax] sitemap HTTP {r.status_code}: {url}")
                    return None
                content = r.content or b""
                ct = (r.headers.get("content-type") or "").lower()
                ce = (r.headers.get("content-encoding") or "").lower()
                if url.endswith(".gz") or "gzip" in ce or "application/x-gzip" in ct:
                    try: content = gzip.decompress(content)
                    except Exception: pass
                try: return content.decode("utf-8", "ignore")
                except Exception: return content.decode("latin-1", "ignore")
        except Exception as e:
            print(f"[remax] sitemap error {url}: {e}")
            return None
    queue = list(roots)
    while queue and len(found) < max_urls and seen_sitemaps < max_sitemaps:
        sm_url = queue.pop(0)
        if sm_url in seen: continue
        seen.add(sm_url); seen_sitemaps += 1
        xml = await fetch_sitemap(sm_url)
        if not xml: continue
        try:
            root = ET.fromstring(xml)
        except Exception as e:
            print(f"[remax] ET parse error {sm_url}: {e}")
            continue
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for sm in root.findall("sm:sitemap", ns):
            loc = sm.find("sm:loc", ns)
            if loc is not None and loc.text and len(queue) + seen_sitemaps < max_sitemaps:
                loc_text = loc.text.strip()
                if loc_text not in seen: queue.append(loc_text)
        for u in root.findall("sm:url", ns):
            loc = u.find("sm:loc", ns)
            if loc is None or not loc.text: continue
            loc_text = loc.text.strip()
            m1 = REMAX_ID_ABS_RE.search(loc_text)
            m2 = REMAX_ID_REL_RE.search(loc_text)
            if m1 or m2:
                url = f"https://www.remax.com.mx/propiedad/{(m1 or m2).group(1)}"
                found.append(url)
                if len(found) >= max_urls: break
    seen_u = set(); uniq: List[str] = []
    for u in found:
        if u not in seen_u:
            uniq.append(u); seen_u.add(u)
        if len(uniq) >= max_urls: break
    return uniq

async def remax_collect_ids_from_indexes(max_urls: int = 30) -> List[str]:
    index_urls = [
        "https://www.remax.com.mx/propiedades",
        "https://www.remax.com.mx/propiedades/venta",
        "https://www.remax.com.mx/propiedades/renta",
        "https://www.remax.com.mx/propiedades/tampico_tamaulipas/venta",
    ]
    out: List[str] = []; seen = set()
    for url in index_urls:
        if len(out) >= max_urls: break
        html = await fetch_html(url, read_timeout=7.0, tries=2)
        if not html: continue
        embedded = _extract_embedded_state_ids(html)
        for u in embedded:
            if u not in seen:
                out.append(u); seen.add(u)
                if len(out) >= max_urls: break
        if len(out) >= max_urls: break
        for m in REMAX_ID_ABS_RE.finditer(html):
            u = f"https://www.remax.com.mx/propiedad/{m.group(1)}"
            if u not in seen:
                out.append(u); seen.add(u)
                if len(out) >= max_urls: break
        if len(out) >= max_urls: break
        for m in REMAX_ID_REL_RE.finditer(html):
            u = f"https://www.remax.com.mx/propiedad/{m.group(1)}"
            if u not in seen:
                out.append(u); seen.add(u)
                if len(out) >= max_urls: break
    return out[:max_urls]

def _meta(soup: BeautifulSoup, key: str) -> Optional[str]:
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key}) \
          or soup.find("meta", attrs={"itemprop": key})
    return (tag.get("content") or "").strip() if tag and tag.get("content") else None

def _price_candidates_from_meta(soup: BeautifulSoup) -> List[float]:
    keys = [
        "product:price:amount", "og:price:amount", "twitter:data1",
        "price", "product:price", "product:price:currency"
    ]
    out: List[float] = []
    for k in keys:
        v = _meta(soup, k)
        if v:
            out.append(_parse_price_from_text(v))
    # Busca itemprop price explícito
    el = soup.find(attrs={"itemprop": "price"})
    if el and el.get("content"):
        out.append(_parse_price_from_text(el.get("content")))
    if el and el.get_text(strip=True):
        out.append(_parse_price_from_text(el.get_text(strip=True)))
    return [x for x in out if x]

def _clean_photos(urls: List[str], base_url: str) -> List[str]:
    out: List[str] = []
    for u in urls:
        if not u: continue
        absu = _to_abs(base_url, u)
        low = absu.lower()
        # Solo fotos de propiedades del CDN; evita íconos y assets generales
        if "cdn.remax.com.mx/properties/" not in low:
            continue
        if any(low.endswith(ext) for ext in [".jpg", ".jpeg", ".png"]):
            if absu not in out:
                out.append(absu)
    return out[:12]

def extract_from_property_detail(html: str, base_url: str, source: str, url: str, idx: int) -> Optional[Listing]:
    # 1) JSON-LD primero
    items = parse_jsonld_listings(html, base_url=base_url, source=source)
    if items:
        best = sorted(items, key=lambda x: (x.price or 0), reverse=True)[0]
        best.url = url
        # fotos filtradas
        best.photos = _clean_photos(best.photos, base_url)
        return best

    # 2) Metas + heurísticas robustas
    soup = BeautifulSoup(html, "html.parser")

    # Título
    title = _meta(soup, "og:title") or ""
    if not title:
        for sel in ["h1",".title",".titulo","[class*='title']","[class*='titulo']"]:
            h = soup.select_one(sel)
            if h: title = h.get_text(strip=True); break

    # Precio: candidatas de metas + texto global; elegimos el mayor válido
    cand_prices = _price_candidates_from_meta(soup)
    body_text = soup.get_text(" ", strip=True)
    cand_prices.append(_parse_price_from_text(body_text))
    price_val = _pick_best_price(cand_prices)

    # Fotos: solo cdn de propiedades
    photos: List[str] = []
    og_img = _meta(soup, "og:image")
    if og_img: photos.append(og_img)
    for img in soup.select('img[src]'):
        if img.get("src"): photos.append(img["src"])
    photos = _clean_photos(photos, base_url)

    # Ciudad/colonia: solo breadcrumbs o addressLocality (nada de og:title)
    city = None; colonia = None
    bc = soup.select("nav.breadcrumb a, .breadcrumb a, [class*='breadcrumb'] a")
    if bc:
        tail = bc[-1].get_text(" ", strip=True)
        c1, c2 = _split_city_colonia(tail)
        city = city or c1; colonia = colonia or c2

    # Recámaras/baños por texto
    low = body_text.lower()
    beds  = _beds(low)
    baths = _baths(low)

    ltype = _infer_type((title or "") + " " + low)

    if not title and price_val == 0 and not photos:
        return None

    return Listing(
        id=f"{source}-detail-{idx}",
        title=title or "Propiedad",
        price=price_val,
        currency="MXN",
        bedrooms=beds,
        bathrooms=baths,
        type=ltype,
        furnished=None,
        location_city=city,
        location_colonia=colonia,
        url=url,
        source=source,
        photos=photos,
    )

# =========================
# Adapters
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
    "remax":                    "https://www.remax.com.mx/propiedades",
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
        return await scrape_generic_list(url, source=key, read_timeout=4.0)
    except Exception as e:
        print(f"[{key}] adapter error suprimido: {e}")
        return []

async def adapter_remax() -> List[Listing]:
    try:
        base_list_url = SITES["remax"]
        # 1) Lista por si exponen algo estático
        cards = await scrape_generic_list(base_list_url, source="remax", read_timeout=7.0)
        if cards:
            return cards

        # 2) Semillas
        detail_urls: List[str] = []
        for u in REMAX_SEEDS:
            if REMAX_ID_ABS_RE.search(u) and u not in detail_urls:
                detail_urls.append(u)

        # 3) Índices
        more = await remax_collect_ids_from_indexes(max_urls=max(0, 28 - len(detail_urls)))
        for u in more:
            if u not in detail_urls: detail_urls.append(u)

        # 4) Sitemaps
        if len(detail_urls) < 12:
            sm = await remax_discover_detail_urls(max_urls=30, max_sitemaps=6)
            for u in sm:
                if u not in detail_urls: detail_urls.append(u)

        detail_urls = detail_urls[:MAX_CARDS]
        if not detail_urls:
            cache_set_pos("scrape::remax::fallback", [])
            return []

        listings: List[Listing] = []
        for i, u in enumerate(detail_urls):
            if len(listings) >= MAX_CARDS: break
            html = await fetch_html(u, read_timeout=9.0, tries=2)
            if not html: continue
            item = extract_from_property_detail(
                html, base_url="https://www.remax.com.mx", source="remax", url=u, idx=i
            )
            if item:
                listings.append(item)

        cache_set_pos("scrape::remax::fallback", listings)
        return listings
    except Exception as e:
        print(f"[remax] adapter error suprimido: {e}")
        return []

# Wrappers resto
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

async def _run_adapter(key: str, fn: Callable[[], Coroutine[Any, Any, List[Listing]]]) -> List[Listing]:
    async with SEM:
        timeout = SITE_TIMEOUTS.get(key, ADAPTER_TIMEOUT)
        try:
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
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=False)
    out: List[Listing] = []
    for res in results:
        out.extend(res)
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
