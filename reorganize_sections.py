#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Riorganizza le immagini gia' scaricate in una repo esistente.

Legge manifest.json, ricostruisce la mappa sezione->collezioni/prodotti
usando la gerarchia ufficiale del sito (mega-menu della homepage,
collezioni esclusive delle pagine sezione, pattern nel nome) e SPOSTA
i file nelle cartelle giuste. Nessun re-download: solo mv + manifest.

Uso (dalla radice della repo):
    pip install -r requirements.txt
    python reorganize_sections.py
"""

import json
import logging
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import sync_images as si

log = logging.getLogger("packprint-reorg")


def build_section_data() -> tuple[dict[str, str], dict[str, str]]:
    """
    Ritorna (coll_section, handle_section):
      - coll_section: collezione -> sezione (ordinata forte->debole)
      - handle_section: prodotto -> sezione (collezione piu' specifica)
    """
    page_colls: dict[str, set[str]] = {}
    for sec in si.SECTION_PAGES:
        try:
            r = si.get(f"{si.SITE_BASE}/pages/{sec}")
            page_colls[sec] = {
                unquote(h) for h in
                re.findall(r'href="/collections/([a-zA-Z0-9%.-]+)"', r.text)
            } if r.status_code == 200 else set()
        except Exception as exc:
            log.warning("Pagina sezione %s irraggiungibile: %s", sec, exc)
            page_colls[sec] = set()

    collections = si.fetch_sitemap_urls("collections")
    all_slugs = [h for h in
                 (si.handle_from_url(u, "collections") for u in collections)
                 if h]
    coll_section = si.build_section_map(page_colls, all_slugs)

    coll_products: dict[str, list[str]] = {}
    for u in collections:
        slug = si.handle_from_url(u, "collections")
        if not slug:
            continue
        try:
            pr = si.get(f"{u}/products.json?limit=250")
            if pr.status_code == 200:
                coll_products[slug] = [
                    p.get("handle", "") for p in pr.json().get("products", [])
                ]
        except Exception:
            pass

    handle_section: dict[str, str] = {}
    for slug, sec in coll_section.items():
        for h in coll_products.get(slug, []):
            handle_section.setdefault(h, sec)
    return coll_section, handle_section


def move_with_suffix(src: Path, dest_dir: Path) -> Path | None:
    """Sposta src in dest_dir; se esiste gia' un file con lo stesso nome e
    contenuto identico, elimina il duplicato; altrimenti aggiunge un suffisso."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        try:
            import hashlib
            h1 = hashlib.sha256(src.read_bytes()).hexdigest()
            h2 = hashlib.sha256(dest.read_bytes()).hexdigest()
            if h1 == h2:
                src.unlink()
                return dest
        except Exception:
            pass
        stem, ext = os.path.splitext(src.name)
        h = hashlib.sha1(str(dest_dir).encode()).hexdigest()[:6]
        dest = dest_dir / f"{stem}_{h}{ext}"
    shutil.move(str(src), str(dest))
    return dest


def prune_empty_dirs(base: Path) -> int:
    removed = 0
    for dirpath, dirnames, filenames in os.walk(base, topdown=False):
        p = Path(dirpath)
        if p != base and not any(p.iterdir()):
            p.rmdir()
            removed += 1
    return removed


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    manifest_path = si.MANIFEST_PATH
    if not manifest_path.exists():
        log.error("manifest.json non trovato: esegui prima sync_images.py")
        return 1
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = data.get("files", [])
    log.info("Manifest caricato: %d file", len(files))

    log.info("Ricostruisco la mappa sezioni dal sito (solo metadati)...")
    coll_section, handle_section = build_section_data()
    log.info("Collezioni classificate: %d | prodotti classificati: %d",
             len(coll_section), len(handle_section))

    out = si.OUTPUT_DIR
    moved, kept, missing = 0, 0, 0
    for f in files:
        old = Path(f.get("path", ""))
        parts = old.parts
        if len(parts) < 5 or parts[0] != out.name or parts[1] != "sito":
            kept += 1                      # instagram/facebook/pagine: intoccabili
            continue
        # due strutture possibili:
        #   images/sito/<sezione>/collezioni|prodotti/<slug>/<file>  (6 parti)
        #   images/sito/collezioni|prodotti/<slug>/<file>            (5 parti, generiche)
        area, kind = parts[2], parts[3]
        slug: str | None = None
        if area in ("collezioni", "prodotti") and len(parts) == 5:
            slug, kind = parts[3], area          # generica
        elif len(parts) >= 6 and parts[3] in ("collezioni", "prodotti"):
            slug = parts[4]                      # dentro una sezione
        if not slug:
            kept += 1            # pagina, pagine, blog, altre: intoccate
            continue

        new_rel: Path | None = None
        new_sec: str | None = None

        if kind == "collezioni":
            sec = coll_section.get(slug)
            if sec:
                new_rel = Path("sito") / sec / "collezioni" / slug / old.name
                new_sec = f"{sec}/collezioni/{slug}"
            else:
                new_rel = Path("sito") / "collezioni" / slug / old.name
                new_sec = f"collezioni/{slug}"
        elif kind == "prodotti":
            sec = handle_section.get(slug)
            if sec:
                new_rel = Path("sito") / sec / "prodotti" / slug / old.name
                new_sec = f"{sec}/prodotti/{slug}"
            else:
                new_rel = Path("sito") / "prodotti" / slug / old.name
                new_sec = f"prodotti/{slug}"

        if new_rel is None or (out / new_rel) == old:
            kept += 1
            continue
        if not old.exists():
            missing += 1
            continue
        dest = move_with_suffix(old, (out / new_rel).parent)
        if dest is None:
            kept += 1
            continue
        f["path"] = str(dest).replace(os.sep, "/")
        if new_sec:
            f["section"] = new_sec
        moved += 1

    removed = prune_empty_dirs(out)
    data["stats"]["riorganizzate"] = moved
    data["generated_at"] = data.get("generated_at")  # invariato
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(manifest_path)

    dist = Counter(f["section"].split("/")[0] for f in files
                   if f["source"] == "sito")
    log.info("File spostati: %d | invariati: %d | mancanti: %d | cartelle vuote rimosse: %d",
             moved, kept, missing, removed)
    log.info("Nuova distribuzione per sezione: %s",
             dict(sorted(dist.items(), key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
