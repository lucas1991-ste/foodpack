# Packprint Image Dump

Script Python + GitHub Action che scarica le immagini di:

1. **Sito web** `https://packprint.com/` — immagini in **qualità originale**,
   organizzate per sezioni come le divide il sito (pagine, collezioni, prodotti)
2. **Instagram** `@packprint_com` — best-effort (funziona bene con un cookie di sessione)
3. **Facebook** `foodpack.green.it` — solo con un token Graph API

Le immagini vengono salvate nella repo e fungono da "database" indicizzato
dal file `manifest.json` (origine, sezione, hash SHA-256, data).

---

## Struttura output

```
images/
├── sito/
│   ├── packaging-alimentare/
│   │   ├── pagina/                  ← immagini della pagina sezione
│   │   ├── collezioni/<collezione>/ ← cover di ogni collezione
│   │   └── prodotti/<prodotto>/     ← tutte le foto originali del prodotto
│   ├── hospitality/…
│   ├── arredo-tavola/…
│   ├── retail/…
│   ├── pagine/<slug>/               ← pagine non di sezione (chi-siamo, design, …)
│   │   └── blog/<articolo>/
│   ├── collezioni/<slug>/           ← collezioni non legate a una sezione
│   └── prodotti/<handle>/           ← prodotti non mappati a una sezione
├── instagram/                       ← se IG_SESSIONID è configurato
└── facebook/                        ← se FB_PAGE_TOKEN è configurato
manifest.json                        ← indice: path, URL origine, sha256, sezione
```

- Le foto prodotto arrivano dall'endpoint JSON di Shopify (`/products/<handle>.json`),
  quindi sono i **file originali senza ridimensionamenti** (niente `?width=`).
- Le immagini sono deduplicate per **URL** e per **hash del contenuto**:
  la stessa foto usata su più pagine viene salvata una sola volta.
- La scansione rispetta `robots.txt` (il sito lo consente) con rate-limit gentile.

---

## Setup su GitHub (una volta sola)

1. Crea una **repo nuova vuota** (es. `packprint-images`).
2. Carica nella **radice** della repo questi 3 oggetti:
   - `sync_images.py`
   - `requirements.txt`
   - la cartella `.github/` (contiene `workflows/sync-images.yml`)
3. Fai commit. Fatto.

### Avviare il dump

GitHub → tab **Actions** → **Dump immagini packprint** → **Run workflow** → conferma.

L'azione gira **una volta sola** (nessun cron), scarica tutto e fa commit + push
delle immagini e del `manifest.json`. Durata stimata: 20–40 minuti
(≈450 pagine analizzate e oltre 1.500 immagini originali).

> **Nota dimensioni**: gli originali pesano. Prevedi 1–3 GB totali.
> GitHub lo regge, ma se vuoi alleggerire vedi la sezione *Opzioni*.

---

## Instagram e Facebook (opzionali)

Senza credenziali lo script **non si ferma**: segnala il blocco nel log e
consegna comunque tutte le immagini del sito. Per abilitare anche i social,
imposta i **Secrets** della repo (Settings → Secrets and variables → Actions).

### `IG_SESSIONID` (consigliato)

1. Da un browser, accedi a Instagram con l'account del brand.
2. Premi F12 → **Application/Archiviazione** → Cookies → `https://www.instagram.com`.
3. Copia il valore del cookie **`sessionid`**.
4. Crea il secret `IG_SESSIONID` con quel valore.

Lo script scaricherà le ultime 24 foto (impostabile con `IG_MAX_POSTS`),
inclusi i caroselli (una immagine per slide). Il cookie scade dopo settimane/mesi:
se un giorno tornano errori 401, rifai il login e aggiorna il secret.

### `FB_PAGE_TOKEN` (richiesto per Facebook)

Facebook blocca qualunque accesso anonimo alle foto di pagina: serve un token.

1. Vai su [developers.facebook.com](https://developers.facebook.com) → **My Apps** → **Create App** (tipo *Business*).
2. Nell'app aggiungi il prodotto **Facebook Graph API**.
3. **Tools → Graph API Explorer**:
   - seleziona la tua app in *Meta App*;
   - genera un **User Access Token** con permessi `pages_show_list`, `pages_read_engagement`, `pages_manage_metadata`;
   - con **Page Access Token** → *Get Page Access Token* scegli la pagina `foodpack.green.it`.
4. Copia il token e crea il secret `FB_PAGE_TOKEN`.

Token page di lunga durata: dal Graph API Explorer → **Exchange Access Token**
oppure genera il token direttamente dalla pagina (Impostazioni → Piattaforme
collegate → se presente "Genera token"). Se il token scade, lo script lo
segnala nei log: basta aggiornare il secret e rilanciare.

---

## Opzioni (variabili d'ambiente)

Tutte facoltative. Puoi inserirle nel workflow (`env:` del passo "Scarica le
immagini") o usarle in locale.

| Variabile | Default | Significato |
|---|---|---|
| `OUTPUT_DIR` | `images` | Cartella di destinazione |
| `MANIFEST_PATH` | `manifest.json` | Percorso dell'indice JSON |
| `SITE_BASE` | `https://packprint.com` | Base del sito |
| `SECTION_PAGES` | `packaging-alimentare,hospitality,arredo-tavola,retail` | Pagine che definiscono le sezioni (l'ordine decide l'assegnazione di collezioni/prodotti) |
| `IG_USERNAME` | `packprint_com` | Profilo Instagram |
| `IG_SESSIONID` | *(vuoto)* | Cookie di sessione Instagram |
| `IG_MAX_POSTS` | `24` | Quanti post IG scaricare |
| `FB_PAGE` | `foodpack.green.it` | Pagina Facebook |
| `FB_PAGE_TOKEN` | *(vuoto)* | Token Graph API della pagina |
| `FB_MAX_PHOTOS` | `300` | Quante foto FB scaricare |
| `CONCURRENCY` | `6` | Download paralleli |
| `DELAY` | `0.25` | Pausa tra richieste (secondi) |
| `MAX_FILE_MB` | `25` | Salta file oltre questa dimensione |
| `MIN_FILE_KB` | `5` | Salta file più piccoli (icone) |
| `SITE_MAX_PAGES` / `SITE_MAX_COLLECTIONS` / `SITE_MAX_PRODUCTS` | `0` (=tutti) | Limiti per test rapidi |
| `EXCLUDE_KEYWORDS` | `favicon,flag,sprite,placeholder,blank,loading,pixel,1x1` | URL da escludere |

---

## Esecuzione in locale (alternativa alla Action)

```bash
pip install -r requirements.txt
export IG_SESSIONID="..."        # opzionale
export FB_PAGE_TOKEN="..."       # opzionale
python sync_images.py
```

## Ri-esecuzioni e dedup

Lo script è pensato per **una singola esecuzione**, ma è ri-eseguibile senza
danni: se `manifest.json` è presente salta le immagini già scaricate
(controllo per URL e per hash) e aggiunge solo le nuove.

## Riorganizzare le sezioni di una repo già popolata

Come decide la sezione di una collezione/prodotto:

1. **Mega-menu della homepage** — la gerarchia ufficiale del sito
2. **Collezioni linkate da una sola pagina sezione** (es. bar, hotel, macelleria → hospitality)
3. **Pattern nel nome** (es. `packaging-alimentare-vedi-tutti`, `shopper-*`)

Le restanti (~55 collezioni di catalogo non attribuibili a una sezione)
finiscono in `images/sito/collezioni/` e `images/sito/prodotti/`.

Se hai già eseguito il dump con una versione precedente dello script (o vuoi
riallineare dopo un cambio di menu del sito), esegui una volta:

**Actions → Riorganizza immagini per sezione → Run workflow**

`reorganize_sections.py` legge il manifest, ricalcola la mappa dal sito (solo
metadati, ~110 richieste leggere) e **sposta i file già scaricati** nelle
cartelle giuste, aggiornando `manifest.json`. Nessun re-download.

## Risoluzione problemi

- **Il job finisce ma mancano Instagram/Facebook** → normale senza secrets:
  leggi i `WARNING` nel log, spiegano cosa impostare.
- **Errore 429 in Azioni** → il sito sta limitando le richieste; rilancia dopo
  qualche ora o aumenta `DELAY` (es. `0.5`).
- **`RPC failed; HTTP 408` / `remote end hung up` durante il push** → era il
  push unico di ~2 GB in timeout. La versione attuale della Action committa e
  spinge **a blocchi di 100 file con 5 retry**, quindi non dovrebbe più
  accadere; se ricompare, abbassa `BATCH` (es. `50`) nello step "Commit e push
  a blocchi". I blocchi già spinti restano nella repo: un nuovo run scarica
  solo ciò che manca.
- **Repo troppo grande per il push** → riduci `MAX_FILE_MB`/`FB_MAX_PHOTOS`
  `IG_MAX_POSTS`, oppure converti `images/` in [Git LFS](https://git-lfs.com):
  `git lfs install && git lfs track "images/**"`.
- **`resource not reachable` su Instagram anche con cookie** → il cookie è
  scaduto: rigenera `IG_SESSIONID`.
