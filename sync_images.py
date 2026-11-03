#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Packprint image dump
====================
Scarica le immagini di:
  1. Sito web https://packprint.com/   -> immagini ORIGINALI, organizzate per
     sezioni come le divide il sito (pagine / collezioni / prodotti).
  2. Instagram @packprint_com          -> best-effort (consigliato secret IG_SESSIONID)
  3. Facebook foodpack.green.it        -> solo con token (secret FB_PAGE_TOKEN)

Uso tipico (una sola esecuzione):
    pip install -r requirements.txt
    python sync_images.py

Tutti i parametri sono configurabili con variabili d'ambiente (vedi README).
Lo script e' ri-eseguibile: se manifest.json e' gia' presente salta i file
gia' scaricati (dedup per URL e per hash SHA-256 del contenuto).
"""

import concurrent.futures as cf
import hashlib
import html as htmllib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse, unquote

import requests
from bs4 import BeautifulSoup

try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    HTTPAdapter = Retry = None

# --------------------------------------------------------------------------- #
# Configurazione (env vars con default sensati)
# --------------------------------------------------------------------------- #
def env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


OUTPUT_DIR       = Path(env_str("OUTPUT_DIR", "images"))
MANIFEST_PATH    = Path(env_str("MANIFEST_PATH", "manifest.json"))

SITE_BASE        = env_str("SITE_BASE", "https://packprint.com").rstrip("/")
SECTION_PAGES    = [s.strip() for s in env_str(
    "SECTION_PAGES",
    "packaging-alimentare,hospitality,arredo-tavola,retail"
).split(",") if s.strip()]

IG_USERNAME      = env_str("IG_USERNAME", "packprint_com")
IG_SESSIONID     = os.environ.get("IG_SESSIONID", "").strip()
IG_MAX_POSTS     = env_int("IG_MAX_POSTS", 24)

FB_PAGE          = env_str("FB_PAGE", "foodpack.green.it")
FB_PAGE_TOKEN    = os.environ.get("FB_PAGE_TOKEN", "").strip()
FB_MAX_PHOTOS    = env_int("FB_MAX_PHOTOS", 300)

SITE_MAX_PAGES       = env_int("SITE_MAX_PAGES", 0)        # 0 = illimitato
SITE_MAX_COLLECTIONS = env_int("SITE_MAX_COLLECTIONS", 0)
SITE_MAX_PRODUCTS    = env_int("SITE_MAX_PRODUCTS", 0)

CONCURRENCY      = env_int("CONCURRENCY", 6)
REQUEST_TIMEOUT  = env_int("REQUEST_TIMEOUT", 30)
DELAY            = float(os.environ.get("DELAY", "0.25"))
MAX_FILE_MB      = env_int("MAX_FILE_MB", 25)
MIN_FILE_KB      = env_int("MIN_FILE_KB", 5)
RETRIES          = env_int("RETRIES", 2)

# parole chiave che identificano asset di interfaccia da NON scaricare
EXCLUDE_KEYWORDS = [k.strip().lower() for k in env_str(
    "EXCLUDE_KEYWORDS",
    "favicon,flag,sprite,placeholder,blank,loading,pixel,1x1"
).split(",") if k.strip()]

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

log = logging.getLogger("packprint-dump")

# --------------------------------------------------------------------------- #
# Statistiche globali
# --------------------------------------------------------------------------- #
STATS = OrderedDict([
    ("sito_scaricate", 0), ("sito_gia_presenti", 0), ("sito_saltate", 0),
    ("sito_errori", 0),
    ("instagram_scaricate", 0), ("instagram_saltate", 0), ("instagram_errori", 0),
    ("facebook_scaricate", 0), ("facebook_saltate", 0), ("facebook_errori", 0),
])

MANIFEST_FILES = []          # lista di dict -> manifest.json
HASH_SEEN = {}               # sha256 -> path gia' salvato
URL_SEEN = {}                # source_url -> path gia' salvato (per resume)

_manifest_lock = threading.Lock()
_write_lock = threading.Lock()


def load_manifest() -> None:
    """Carica un eventuale manifest precedente per il resume."""
    if not MANIFEST_PATH.exists():
        return
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        for f in data.get("files", []):
            p = Path(f.get("path", ""))
            u = f.get("source_url", "")
            if u and p.exists() and p.stat().st_size > 0:
                URL_SEEN[u] = str(p)
                if f.get("sha256"):
                    HASH_SEEN[f["sha256"]] = str(p)
        log.info("Resume: %d file gia' presenti nel manifest", len(URL_SEEN))
    except Exception as exc:
        log.warning("Manifest precedente illeggibile (%s): riparto da zero", exc)


def save_manifest() -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": SITE_BASE,
        "instagram": IG_USERNAME,
        "facebook": FB_PAGE,
        "stats": dict(STATS),
        "files": MANIFEST_FILES,
    }
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def record_file(path: Path, source_url: str, sha: str, nbytes: int,
                source: str, section: str) -> None:
    with _manifest_lock:
        MANIFEST_FILES.append({
            "path": str(path).replace(os.sep, "/"),
            "source": source,
            "section": section,
            "source_url": source_url,
            "sha256": sha,
            "bytes": nbytes,
            "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        URL_SEEN[source_url] = str(path).replace(os.sep, "/")
        HASH_SEEN[sha] = str(path).replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def make_session(extra_headers: dict | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})
    if extra_headers:
        s.headers.update(extra_headers)
    if HTTPAdapter is not None and Retry is not None:
        retry = Retry(total=RETRIES, connect=RETRIES, read=RETRIES,
                      backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "HEAD"))
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=CONCURRENCY)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    return s


SESSION = make_session()


def get(url: str, **kw) -> requests.Response:
    """GET con rate-limit gentile."""
    time.sleep(DELAY)
    kw.setdefault("timeout", REQUEST_TIMEOUT)
    return SESSION.get(url, **kw)


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def abs_url(u: str, base: str = SITE_BASE + "/") -> str | None:
    """Normalizza a URL assoluto https."""
    if not u:
        return None
    u = htmllib.unescape(u.strip())
    if u.startswith("data:") or u.startswith("blob:"):
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("http://"):
        u = "https://" + u[7:]
    elif not u.startswith("http"):
        u = requests.compat.urljoin(base, u)
    return u


def original_shopify_url(u: str) -> str:
    """
    Riporta un URL CDN Shopify alla dimensione originale:
      - toglie i parametri di ridimensionamento (width, height, crop, ...)
        mantenendo 'v' (versione cache);
      - toglie i suffissi legacy _1024x.jpg / _800x800@2x.png ecc.
    """
    u = abs_url(u) or u
    p = urlparse(u)
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k == "v"])
    path = p.path
    m = re.search(r"_(\d+x\d*|x\d+)(@2x|@3x)?\.(jpg|jpeg|png|webp|gif)$",
                  path, re.I)
    if m:
        path = path[:m.start()] + "." + m.group(3)
    return urlunparse(("https", p.netloc, path, "", q, ""))


def is_excluded(u: str) -> bool:
    low = u.lower()
    if "/cdn/shop/t/" in low:               # asset del tema (icone, css, js)
        return True
    if any(k in low for k in EXCLUDE_KEYWORDS):
        return True
    ext = Path(urlparse(low).path).suffix
    return ext not in IMAGE_EXT


def safe_slug(s: str, maxlen: int = 80) -> str:
    s = unquote(s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")
    return s[:maxlen] or "senza-nome"


def filename_from_url(u: str) -> str:
    path = urlparse(u).path
    name = Path(path).name or "immagine"
    name = safe_slug(name, 100)
    if "." not in name:
        name += ".jpg"
    return name


def unique_path(folder: Path, name: str, source_url: str) -> Path:
    """Se nella stessa cartella esiste gia' un file con lo stesso nome ma URL
    d'origine diverso, aggiunge un suffisso corto derivato dall'URL."""
    same_name = list(folder.glob(name)) if folder.exists() else []
    if not same_name:
        return folder / name
    h = hashlib.sha1(source_url.encode()).hexdigest()[:6]
    stem, ext = os.path.splitext(name)
    return folder / f"{stem}_{h}{ext}"


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def download_image(url: str, folder: Path, source: str, section: str,
                   referer: str = "") -> Path | None:
    """
    Scarica un'immagine in originale. Ritorna il path salvato oppure None
    se e' stata saltata (dup, gia' presente, errore).
    """
    orig = original_shopify_url(url)
    if is_excluded(orig):
        STATS[f"{source}_saltate"] = STATS.get(f"{source}_saltate", 0) + 1
        return None
    with _manifest_lock:
        if orig in URL_SEEN:
            STATS[f"{source}_gia_presenti"] = STATS.get(f"{source}_gia_presenti", 0) + 1
            return None

    folder.mkdir(parents=True, exist_ok=True)
    name = filename_from_url(orig)
    dest = unique_path(folder, name, orig)
    tmp_path: Path | None = None

    for attempt in range(1 + RETRIES):
        try:
            headers = {"Referer": referer} if referer else {}
            with SESSION.get(orig, timeout=REQUEST_TIMEOUT, stream=True,
                             headers=headers) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                ctype = r.headers.get("Content-Type", "")
                if ctype and not ctype.startswith("image/"):
                    raise RuntimeError(f"content-type {ctype}")

                with tempfile.NamedTemporaryFile(dir=folder, delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                    sha = hashlib.sha256()
                    size = 0
                    for chunk in r.iter_content(64 * 1024):
                        tmp.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)

            if size < MIN_FILE_KB * 1024:
                tmp_path.unlink(missing_ok=True)
                STATS[f"{source}_saltate"] = STATS.get(f"{source}_saltate", 0) + 1
                log.debug("Troppo piccola (%d B): %s", size, orig)
                return None
            if size > MAX_FILE_MB * 1024 * 1024:
                tmp_path.unlink(missing_ok=True)
                STATS[f"{source}_saltate"] = STATS.get(f"{source}_saltate", 0) + 1
                log.warning("Troppo grande (%.1f MB, max %d): %s",
                            size / 1048576, MAX_FILE_MB, orig)
                return None

            digest = sha.hexdigest()
            with _write_lock:
                if digest in HASH_SEEN:
                    tmp_path.unlink(missing_ok=True)
                    STATS[f"{source}_gia_presenti"] = STATS.get(
                        f"{source}_gia_presenti", 0) + 1
                    return None
                if dest.exists():
                    stem, ext = os.path.splitext(dest.name)
                    h = hashlib.sha1(orig.encode()).hexdigest()[:6]
                    dest = dest.with_name(f"{stem}_{h}{ext}")
                shutil.move(str(tmp_path), dest)
                tmp_path = None

            record_file(dest, orig, digest, size, source, section)
            STATS[f"{source}_scaricate"] = STATS.get(f"{source}_scaricate", 0) + 1
            log.info("Salvata %s", dest)
            return dest
        except Exception as exc:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
                tmp_path = None
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            STATS[f"{source}_errori"] = STATS.get(f"{source}_errori", 0) + 1
            log.warning("Errore su %s: %s", orig, exc)
            return None
    return None


def download_many(items: list[tuple[str, Path, str, str, str]]) -> None:
    """items: (url, folder, source, section, referer)"""
    if not items:
        return
    with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(download_image, *it) for it in items]
        for f in cf.as_completed(futures):
            f.result()


# --------------------------------------------------------------------------- #
# Estrazione immagini da HTML
# --------------------------------------------------------------------------- #
def extract_from_html(html: str, base_url: str) -> set[str]:
    urls: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")

    def add(candidate: str) -> None:
        if not candidate:
            return
        candidate = candidate.strip().split(" ")[0]
        if not candidate:
            return
        u = original_shopify_url(candidate)
        if u:
            urls.add(u)

    for tag in soup.find_all("img"):
        add(tag.get("src") or "")
        for attr in ("srcset", "data-srcset"):
            ss = tag.get(attr)
            if ss:
                for cand in ss.split(","):
                    add(cand)
        for attr, val in tag.attrs.items():
            if (isinstance(val, str) and attr.startswith("data-")
                    and val.strip().startswith(("http", "//"))):
                add(val)

    for tag in soup.find_all("source"):
        ss = tag.get("srcset")
        if ss:
            for cand in ss.split(","):
                add(cand)

    for meta in soup.find_all("meta"):
        if (meta.get("property") in ("og:image", "og:image:secure_url")
                or meta.get("name") == "twitter:image"):
            add(meta.get("content") or "")

    for tag in soup.find_all(style=True):
        for m in re.finditer(r"url\((['\"]?)([^'\")]+)\1\)", tag["style"]):
            add(m.group(2))

    return {u for u in (abs_url(x, base_url) for x in urls) if u}


def collect_download_jobs(urls: set[str], folder: Path, source: str,
                          section: str, referer: str) -> list:
    jobs = []
    for u in urls:
        orig = original_shopify_url(u)
        if is_excluded(orig):
            continue
        with _manifest_lock:
            already = orig in URL_SEEN
        if already:
            STATS[f"{source}_gia_presenti"] = STATS.get(f"{source}_gia_presenti", 0) + 1
            continue
        jobs.append((orig, folder, source, section, referer))
    return jobs


# --------------------------------------------------------------------------- #
# 1) SITO: sitemap -> sezioni -> collezioni -> prodotti
# --------------------------------------------------------------------------- #
def handle_from_url(u: str, kind: str) -> str | None:
    """
    Estrae l'handle da un URL tipo https://sito/<kind>/<handle>.
    Ritorna None per URL non validi (es. homepage inclusa da Shopify
    nella sitemap prodotti) o con percorsi piu' lunghi.
    """
    path = urlparse(u).path
    parts = [x for x in path.split("/") if x]
    if len(parts) == 2 and parts[0] == kind:
        return unquote(parts[1])
    return None


def fetch_sitemap_urls(kind: str) -> list[str]:
    """
    kind in {products, pages, collections, articles}
    Ritorna gli URL (versione italiana) presi dalla sitemap.
    """
    idx = get(f"{SITE_BASE}/sitemap.xml")
    idx.raise_for_status()
    locs = re.findall(r"<loc>([^<]+)</loc>", idx.text)
    out: list[str] = []
    for sm in locs:
        if f"/en/sitemap_{kind}" in sm:
            continue
        if f"sitemap_{kind}" in sm:
            r = get(sm)
            if r.status_code != 200:
                continue
            for loc in re.findall(r"<loc>([^<]+)</loc>", r.text):
                if "/en/" in loc:
                    continue
                if "packprint.com" in loc:
                    out.append(loc.replace("http://", "https://"))
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def sync_site() -> None:
    log.info("=== SITO: %s ===", SITE_BASE)

    # -- 1. mappa sezione -> collezioni (dal menu delle pagine sezione) ------ #
    coll_section: dict[str, str] = {}      # collection handle -> sezione
    page_jobs: list = []                   # download differiti
    page_urls_done: set[str] = set()

    for sec in SECTION_PAGES:
        url = f"{SITE_BASE}/pages/{sec}"
        try:
            r = get(url)
            r.raise_for_status()
        except Exception as exc:
            log.warning("Pagina sezione %s non raggiungibile: %s", sec, exc)
            continue
        log.info("Sezione '%s': analizzo la pagina", sec)
        handles = re.findall(r'href="/collections/([a-zA-Z0-9%.-]+)"', r.text)
        for h in dict.fromkeys(handles):
            coll_section.setdefault(unquote(h), sec)
        imgs = extract_from_html(r.text, url)
        folder = OUTPUT_DIR / "sito" / safe_slug(sec) / "pagina"
        page_jobs += collect_download_jobs(
            imgs - page_urls_done, folder, "sito", f"{sec}/pagina", url)
        page_urls_done |= imgs

    # -- 2. sitemap ---------------------------------------------------------- #
    pages        = fetch_sitemap_urls("pages")
    collections  = fetch_sitemap_urls("collections")
    products     = fetch_sitemap_urls("products")
    articles     = fetch_sitemap_urls("articles")
    log.info("Sitemap: %d pagine, %d collezioni, %d prodotti, %d articoli",
             len(pages), len(collections), len(products), len(articles))

    if SITE_MAX_PAGES:
        pages = pages[:SITE_MAX_PAGES]
        articles = articles[:SITE_MAX_PAGES]
    if SITE_MAX_COLLECTIONS:
        collections = collections[:SITE_MAX_COLLECTIONS]
    if SITE_MAX_PRODUCTS:
        products = products[:SITE_MAX_PRODUCTS]

    pages = [u for u in pages if handle_from_url(u, "pages")]
    collections = [u for u in collections if handle_from_url(u, "collections")]
    products = [u for u in products if handle_from_url(u, "products")]
    articles = [u for u in articles if "/blogs/" in u and u.rstrip("/").count("/") >= 4]
    log.info("Dopo validazione URL: %d pagine, %d collezioni, %d prodotti",
             len(pages), len(collections), len(products))

    # -- 3. pagine non-sezione e articoli ------------------------------------ #
    section_slugs = set(SECTION_PAGES)
    for u in pages:
        slug = handle_from_url(u, "pages")
        if not slug:
            continue
        if slug in section_slugs:
            continue                       # gia' gestita sopra
        try:
            r = get(u)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        imgs = extract_from_html(r.text, u)
        folder = OUTPUT_DIR / "sito" / "pagine" / safe_slug(slug)
        page_jobs += collect_download_jobs(
            imgs - page_urls_done, folder, "sito", f"pagine/{slug}", u)
        page_urls_done |= imgs

    for u in articles:
        slug = u.rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            continue
        try:
            r = get(u)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        imgs = extract_from_html(r.text, u)
        folder = OUTPUT_DIR / "sito" / "pagine" / "blog" / safe_slug(slug)
        page_jobs += collect_download_jobs(
            imgs - page_urls_done, folder, "sito", f"pagine/blog/{slug}", u)
        page_urls_done |= imgs

    # -- 4. collezioni ------------------------------------------------------- #
    # handle -> products (solo per le collezioni mappate a una sezione)
    coll_products: dict[str, list[str]] = {}
    for u in collections:
        slug = handle_from_url(u, "collections")
        if not slug:
            continue
        sec = coll_section.get(slug)
        folder = (OUTPUT_DIR / "sito" / safe_slug(sec) / "collezioni" / safe_slug(slug)
                  if sec else
                  OUTPUT_DIR / "sito" / "collezioni" / safe_slug(slug))
        try:
            r = get(u)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        imgs = extract_from_html(r.text, u)
        page_jobs += collect_download_jobs(
            imgs - page_urls_done, folder, "sito",
            f"{sec}/collezioni/{slug}" if sec else f"collezioni/{slug}", u)
        page_urls_done |= imgs

        if sec:
            try:
                pr = get(f"{u}/products.json?limit=250")
                if pr.status_code == 200:
                    for p in pr.json().get("products", []):
                        coll_products.setdefault(slug, []).append(p.get("handle", ""))
            except Exception:
                pass

    # -- 5. prodotti --------------------------------------------------------- #
    handle_section: dict[str, str] = {}
    for slug, section in coll_section.items():
        for h in coll_products.get(slug, []):
            handle_section.setdefault(h, section)

    prod_jobs: list = []
    for u in products:
        handle = handle_from_url(u, "products")
        if not handle:
            continue
        sec = handle_section.get(handle)
        folder = (OUTPUT_DIR / "sito" / safe_slug(sec) / "prodotti" / safe_slug(handle)
                  if sec else
                  OUTPUT_DIR / "sito" / "prodotti" / safe_slug(handle))
        section_lbl = f"{sec}/prodotti/{handle}" if sec else f"prodotti/{handle}"

        img_urls: list[str] | None = None
        try:
            r = get(f"{SITE_BASE}/products/{quote(handle)}.json")
            if r.status_code == 200:
                img_urls = [i.get("src", "") for i in
                            r.json().get("product", {}).get("images", [])]
                img_urls = [i for i in img_urls if i]
        except Exception:
            img_urls = None
        if img_urls is None:
            # fallback: pagina HTML del prodotto
            try:
                r = get(u)
                img_urls = list(extract_from_html(r.text, u)) \
                    if r.status_code == 200 else []
            except Exception:
                img_urls = []
        prod_jobs += collect_download_jobs(
            set(img_urls) - page_urls_done, folder, "sito", section_lbl, u)
        page_urls_done |= set(img_urls)

    log.info("Avvio download: %d immagini da pagine/collezioni, %d da prodotti",
             len(page_jobs), len(prod_jobs))
    download_many(page_jobs)
    download_many(prod_jobs)
    save_manifest()
    log.info("Sito completato: %d nuove, %d gia' presenti, %d saltate, %d errori",
             STATS["sito_scaricate"], STATS["sito_gia_presenti"],
             STATS["sito_saltate"], STATS["sito_errori"])


# --------------------------------------------------------------------------- #
# 2) INSTAGRAM (best-effort)
# --------------------------------------------------------------------------- #
IG_APP_ID = "936619743392459"


def sync_instagram() -> None:
    if not IG_USERNAME:
        return
    log.info("=== INSTAGRAM: @%s ===", IG_USERNAME)
    folder = OUTPUT_DIR / "instagram"

    headers = {
        "x-ig-app-id": IG_APP_ID,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/{IG_USERNAME}/",
    }
    if IG_SESSIONID:
        headers["Cookie"] = f"sessionid={IG_SESSIONID}"
        log.info("Uso la sessione fornita (secret IG_SESSIONID)")
    else:
        log.info("Nessun IG_SESSIONID: tento accesso anonimo (spesso bloccato)")

    try:
        r = get(f"https://www.instagram.com/api/v1/users/web_profile_info/"
                f"?username={IG_USERNAME}", headers=headers)
        if r.status_code in (401, 403, 429):
            log.warning("Instagram ha bloccato l'accesso anonimo (HTTP %s). "
                        "Per scaricare le foto imposta il secret IG_SESSIONID "
                        "(vedi README). Proseguo senza Instagram.", r.status_code)
            return
        r.raise_for_status()
        data = r.json().get("data", {}).get("user", {})
    except Exception as exc:
        STATS["instagram_errori"] = STATS.get("instagram_errori", 0) + 1
        log.warning("Instagram non disponibile: %s. Proseguo senza Instagram.", exc)
        return

    if not data:
        log.warning("Instagram: profilo vuoto o non raggiungibile.")
        return

    user_id = data.get("id", "")
    pic = data.get("profile_pic_url_hd") or data.get("profile_pic_url")
    if pic:
        download_image(pic, folder, "instagram", "profilo",
                       referer="https://www.instagram.com/")

    edges = (data.get("edge_owner_to_timeline_media") or {}).get("edges", [])
    nodes = [dict(e.get("node", {})) for e in edges]

    # paginazione best-effort (richiede sessione valida)
    if IG_SESSIONID and user_id and 0 < len(nodes) < IG_MAX_POSTS:
        max_id = ""
        for _ in range(10):
            if len(nodes) >= IG_MAX_POSTS:
                break
            try:
                rr = get(f"https://www.instagram.com/api/v1/feed/user/{user_id}/"
                         f"?count=12" + (f"&max_id={max_id}" if max_id else ""),
                         headers=headers)
                if rr.status_code != 200:
                    break
                body = rr.json()
                items = body.get("items", [])
                if not items:
                    break
                for it in items:
                    nodes.append({
                        "shortcode": it.get("code", ""),
                        "media_type": it.get("media_type"),
                        "image_versions2": it.get("image_versions2", {}),
                        "carousel_media": it.get("carousel_media", []),
                    })
                max_id = body.get("next_max_id", "")
                if not max_id:
                    break
            except Exception:
                break

    def best_url(node: dict) -> str:
        cands = (node.get("image_versions2") or {}).get("candidates", [])
        if cands:
            best = max(cands, key=lambda c: c.get("width", 0) * c.get("height", 0))
            return best.get("url", "")
        return node.get("display_url", "") or ""

    jobs: list = []
    seen_posts: set[str] = set()
    for node in nodes:
        if len(seen_posts) >= IG_MAX_POSTS:
            break
        code = node.get("shortcode") or node.get("code") or ""
        if not code or code in seen_posts:
            continue
        seen_posts.add(code)
        sec = f"post_{code}"
        children = (node.get("edge_sidecar_to_children") or {}).get("edges", [])
        if children:
            for ch in children:
                u = (ch.get("node") or {}).get("display_url", "")
                if u:
                    jobs.append((u, folder, "instagram", sec,
                                 "https://www.instagram.com/"))
        elif node.get("carousel_media"):
            for i, child in enumerate(node["carousel_media"], start=1):
                cands = (child.get("image_versions2") or {}).get("candidates", [])
                if cands:
                    best = max(cands, key=lambda c: c.get("width", 0) * c.get("height", 0))
                    if best.get("url"):
                        jobs.append((best["url"], folder, "instagram", sec,
                                     "https://www.instagram.com/"))
        else:
            u = best_url(node)   # immagini e thumbnail dei video
            if u:
                jobs.append((u, folder, "instagram", sec,
                             "https://www.instagram.com/"))

    for u, f, s, sec, ref in jobs:
        download_image(u, f, s, sec, ref)
    save_manifest()
    log.info("Instagram completato: %d nuove, %d saltate, %d errori",
             STATS["instagram_scaricate"], STATS["instagram_saltate"],
             STATS["instagram_errori"])


# --------------------------------------------------------------------------- #
# 3) FACEBOOK (solo con token Graph API)
# --------------------------------------------------------------------------- #
def sync_facebook() -> None:
    if not FB_PAGE:
        return
    log.info("=== FACEBOOK: %s ===", FB_PAGE)
    if not FB_PAGE_TOKEN:
        log.warning("Nessun FB_PAGE_TOKEN: Facebook non permette l'accesso "
                    "anonimo alle foto di pagina. Imposta il secret FB_PAGE_TOKEN "
                    "(vedi README). Proseguo senza Facebook.")
        return

    folder = OUTPUT_DIR / "facebook"
    api = f"https://graph.facebook.com/v21.0/{quote(FB_PAGE)}"

    try:
        r = get(api, params={"fields": "name,picture.type(large)",
                             "access_token": FB_PAGE_TOKEN})
        r.raise_for_status()
        info = r.json()
        log.info("Pagina trovata: %s", info.get("name", FB_PAGE))
        pic = (info.get("picture") or {}).get("data", {}).get("url", "")
        if pic:
            download_image(pic, folder, "facebook", "profilo",
                           referer="https://www.facebook.com/")

        n = 0
        url = api + "/photos"
        while url and n < FB_MAX_PHOTOS:
            r = get(url, params={"type": "uploaded",
                                 "fields": "images,created_time,link",
                                 "limit": 100,
                                 "access_token": FB_PAGE_TOKEN})
            r.raise_for_status()
            data = r.json()
            for ph in data.get("data", []):
                if n >= FB_MAX_PHOTOS:
                    break
                images = sorted(ph.get("images", []),
                                key=lambda i: i.get("width", 0) * i.get("height", 0),
                                reverse=True)
                if images:
                    src = images[0].get("source", "")
                    if src:
                        download_image(src, folder, "facebook",
                                       f"foto_{ph.get('id', '')}",
                                       referer="https://www.facebook.com/")
                        n += 1
            url = (data.get("paging") or {}).get("next", "")
    except Exception as exc:
        STATS["facebook_errori"] = STATS.get("facebook_errori", 0) + 1
        log.warning("Facebook non disponibile (token scaduto o permessi "
                    "insufficienti?): %s. Proseguo senza Facebook.", exc)
        return
    save_manifest()
    log.info("Facebook completato: %d nuove, %d saltate, %d errori",
             STATS["facebook_scaricate"], STATS["facebook_saltate"],
             STATS["facebook_errori"])


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("Output: %s | manifest: %s", OUTPUT_DIR, MANIFEST_PATH)
    load_manifest()

    site_ok = True
    try:
        sync_site()
    except Exception as exc:
        site_ok = False
        log.error("SITO: errore fatale: %s", exc)

    try:
        sync_instagram()
    except Exception as exc:
        log.warning("Instagram: problema non fatale: %s", exc)

    try:
        sync_facebook()
    except Exception as exc:
        log.warning("Facebook: problema non fatale: %s", exc)

    save_manifest()

    total = sum(v for k, v in STATS.items() if k.endswith("_scaricate"))
    log.info("=" * 60)
    log.info("RIEPILOGO")
    for k, v in STATS.items():
        if v:
            log.info("  %-24s %d", k.replace("_", " "), v)
    log.info("Totale nuove immagini salvate: %d", total)
    log.info("Manifest: %s", MANIFEST_PATH)

    return 0 if site_ok else 1


if __name__ == "__main__":
    sys.exit(main())
