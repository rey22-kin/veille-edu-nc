#!/usr/bin/env python3
"""
Veille automatique pour edu-nc.gouv.cd
----------------------------------------
Ce que le script surveille :
- Nouveaux articles (page /actualites) : orthographe, liens, images, infos manquantes
- Articles déjà publiés : modifications et suppressions
- Autres pages du site (celles du menu) : modifications et pages inaccessibles
- Documents (PDF, Word, Excel...) : liens cassés ou fichiers qui ne s'ouvrent pas
- Mots à surveiller (liste modifiable dans config.json)
- Disponibilité et vitesse du site

Sorties :
- Une alerte email à chaque scan où quelque chose de nouveau est détecté
- Un rapport hebdomadaire (lundi au dimanche) : python scan_site.py weekly
"""

import os
import re
import sys
import json
import time
import hashlib
import difflib
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urldefrag
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
SITE_URL = os.environ.get("SITE_URL", "https://edu-nc.gouv.cd").rstrip("/")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")
LANGUAGETOOL_API = "https://api.languagetool.org/v2/check"
LANG = "fr"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", SMTP_USER)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VeilleEduNC/1.0)"}
REQUEST_TIMEOUT = 15

LISTING_PATHS = ["/actualites"]
ARTICLE_PATH_PATTERN = re.compile(r"^/actualites/[a-z0-9\-]+/?$")

MAX_WATCHED_PAGES = 40          # nombre max de pages du menu surveillées
ARTICLE_RECHECK_DAYS = 60       # un article est revérifié pendant 60 jours
MAX_DOC_CHECKS_PER_RUN = 60     # nombre max de documents testés par scan
DOC_RECHECK_HOURS = 24          # un document est retesté au plus 1 fois par jour
MAX_LINES = 200                 # lignes de texte mémorisées par page
MAX_LINE_LEN = 220
TIME_BUDGET_SECONDS = 15 * 60   # le scan s'arrête proprement après 15 min

DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".zip", ".rar", ".csv", ".odt", ".ods")

# Sites qui bloquent les robots : leurs liens ne sont pas testés (faux positifs)
SKIP_LINK_DOMAINS = ("facebook.com", "fb.com", "twitter.com", "x.com",
                     "linkedin.com", "instagram.com", "tiktok.com",
                     "whatsapp.com", "wa.me", "t.me")

# Blocs de page ignorés pour détecter les modifications (menus, "articles liés"...)
NOISE_CLASS_HINTS = ("related", "similar", "share", "partage", "suggest",
                     "recent", "latest", "newsletter", "breadcrumb",
                     "pagination", "cookie")

MOIS_FR = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "décembre": 12,
}
DATE_PATTERN = re.compile(
    r"(\d{1,2})\s+(" + "|".join(MOIS_FR.keys()) + r")\s+(\d{4})", re.IGNORECASE
)

CATEGORY_LABELS = {
    "TYPOS": "Faute d'orthographe",
    "GRAMMAR": "Faute de grammaire",
    "PUNCTUATION": "Ponctuation",
    "CASING": "Majuscule/minuscule",
    "STYLE": "Style",
    "REDUNDANCY": "Répétition",
    "CONFUSED_WORDS": "Mot confondu",
    "TYPOGRAPHY": "Typographie",
}

DEFAULT_CONFIG = {
    "pages_a_surveiller_en_plus": [],
    "pages_a_ignorer": ["/en", "/actualites"],
    "mots_surveilles": [],
    "seuil_lenteur_secondes": 6,
    "selecteur_contenu_article": "",
}

# Pour chaque type d'événement : (emoji, titre de section, (singulier, pluriel))
KIND_META = {
    "site_indisponible": ("🚨", "DISPONIBILITÉ DU SITE", ("site inaccessible", "alertes site inaccessible")),
    "site_lent": ("🐢", "DISPONIBILITÉ DU SITE", ("site lent", "alertes site lent")),
    "site_retabli": ("✅", "DISPONIBILITÉ DU SITE", ("site rétabli", "site rétabli")),
    "listing_vide": ("⚠️", "STRUCTURE DU SITE", ("anomalie sur /actualites", "anomalies sur /actualites")),
    "menu_vide": ("⚠️", "STRUCTURE DU SITE", ("menu non détecté", "menu non détecté")),
    "article_modifie": ("✏️", "ARTICLES MODIFIÉS", ("article modifié", "articles modifiés")),
    "article_supprime": ("🗑️", "ARTICLES SUPPRIMÉS", ("article supprimé", "articles supprimés")),
    "page_modifiee": ("📄", "PAGES SURVEILLÉES MODIFIÉES", ("page modifiée", "pages modifiées")),
    "page_inaccessible": ("🔗", "PAGES DU SITE INACCESSIBLES", ("page inaccessible", "pages inaccessibles")),
    "document_casse": ("📎", "DOCUMENTS INACCESSIBLES", ("document inaccessible", "documents inaccessibles")),
    "document_retabli": ("📎", "DOCUMENTS DE NOUVEAU ACCESSIBLES", ("document rétabli", "documents rétablis")),
    "mot_surveille": ("🔎", "MOTS SURVEILLÉS", ("mot surveillé détecté", "mots surveillés détectés")),
    "config_invalide": ("⚙️", "CONFIGURATION", ("erreur dans config.json", "erreur dans config.json")),
}

START_TIME = time.monotonic()


def time_left():
    return TIME_BUDGET_SECONDS - (time.monotonic() - START_TIME)


# ---------------------------------------------------------------------------
# OUTILS GÉNÉRAUX
# ---------------------------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now_utc()).isoformat()


def parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def normalize(text):
    return " ".join(text.lower().replace("’", "'").split())


def hash_lines(lines):
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg["_erreur"] = None
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            if not isinstance(user, dict):
                raise ValueError("le fichier doit contenir un objet { ... }")
            for key, value in user.items():
                if not key.startswith("_"):
                    cfg[key] = value
        except (OSError, ValueError) as e:
            cfg["_erreur"] = str(e)
    return cfg


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_urls": [], "last_run": None, "articles": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def add_event(state, events, kind, title, url=None, details=None, count=1):
    ev = {"ts": iso(), "kind": kind, "title": title, "url": url,
          "details": details or [], "count": count}
    events.append(ev)
    state.setdefault("events", []).append(ev)
    return ev


def decode_response(r):
    """Évite les accents cassés quand le serveur n'indique pas l'encodage."""
    if "charset" not in r.headers.get("Content-Type", "").lower():
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def fetch_status(url):
    """Retourne (code HTTP, texte HTML) ou (None, None) si aucune réponse."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        return r.status_code, decode_response(r)
    except requests.RequestException:
        return None, None


# ---------------------------------------------------------------------------
# 1. DISPONIBILITÉ ET VITESSE DU SITE
# ---------------------------------------------------------------------------

def probe(url):
    try:
        start = time.monotonic()
        r = requests.get(url, headers=HEADERS, timeout=20)
        secs = round(time.monotonic() - start, 2)
        return {"url": url, "status": r.status_code, "seconds": secs,
                "ok": r.status_code < 400, "error": None, "text": decode_response(r)}
    except requests.RequestException as e:
        return {"url": url, "status": None, "seconds": None, "ok": False,
                "error": e.__class__.__name__, "text": ""}


def check_site_health(config):
    probes = []
    for path in ("/", "/actualites"):
        url = SITE_URL + path
        p = probe(url)
        if not p["ok"]:
            time.sleep(5)  # deuxième chance avant de déclarer le site en panne
            p = probe(url)
        probes.append(p)

    times = [p["seconds"] for p in probes if p["seconds"] is not None]
    max_seconds = max(times) if times else None
    try:
        threshold = float(config.get("seuil_lenteur_secondes", 6))
    except (TypeError, ValueError):
        threshold = 6.0

    if any(not p["ok"] for p in probes):
        status = "down"
    elif max_seconds is not None and max_seconds > threshold:
        status = "slow"
    else:
        status = "ok"
    return {"status": status, "probes": probes, "max_seconds": max_seconds,
            "threshold": threshold, "homepage_html": probes[0]["text"] if probes[0]["ok"] else ""}


def update_health(state, health, events):
    prev = state.get("site_status", {}).get("status", "ok")
    cur = health["status"]
    ts = iso()

    log = state.setdefault("uptime_log", [])
    log.append({"ts": ts, "status": cur, "seconds": health["max_seconds"]})
    cutoff = now_utc() - timedelta(days=21)
    state["uptime_log"] = [e for e in log if (parse_iso(e["ts"]) or now_utc()) >= cutoff]

    if cur != prev:
        details = []
        for p in health["probes"]:
            if p["status"] is not None:
                details.append(f"{p['url']} → réponse HTTP {p['status']} en {p['seconds']} s")
            else:
                details.append(f"{p['url']} → aucune réponse ({p['error']})")
        if cur == "down":
            add_event(state, events, "site_indisponible",
                      "Le site ne répond pas correctement", SITE_URL, details)
        elif cur == "slow":
            details.append(f"Seuil de lenteur : {health['threshold']:g} s")
            add_event(state, events, "site_lent",
                      "Le site est très lent", SITE_URL, details)
        else:
            add_event(state, events, "site_retabli",
                      "Le site répond de nouveau normalement", SITE_URL, details)
        state["site_status"] = {"status": cur, "since": ts}
    else:
        state.setdefault("site_status", {"status": cur, "since": ts})


# ---------------------------------------------------------------------------
# 2. TROUVER LES ARTICLES
# ---------------------------------------------------------------------------

def parse_french_date(text):
    if not text:
        return None
    m = DATE_PATTERN.search(text)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = MOIS_FR.get(month_name.lower())
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def scan_listing_pages():
    articles = {}
    for path in LISTING_PATHS:
        url = urljoin(SITE_URL + "/", path.lstrip("/"))
        status, html = fetch_status(url)
        if status is None or status >= 400:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            article_path = urlparse(a["href"]).path
            if not ARTICLE_PATH_PATTERN.match(article_path):
                continue
            full_url = urljoin(SITE_URL + "/", article_path.lstrip("/"))
            if full_url in articles:
                continue
            lastmod = None
            node = a
            for _ in range(5):
                if node is None:
                    break
                lastmod = parse_french_date(node.get_text(" ", strip=True))
                if lastmod:
                    break
                node = node.parent
            articles[full_url] = {"url": full_url, "lastmod": lastmod}
    return list(articles.values())


def find_sitemap_urls():
    candidates = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
                  "/sitemap-articles.xml", "/feed/", "/rss.xml"]
    found = []
    for path in candidates:
        url = urljoin(SITE_URL + "/", path.lstrip("/"))
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200 and len(r.content) > 100:
                found.append((url, r.content))
        except requests.RequestException:
            continue
    return found


def parse_sitemap_xml(content):
    articles = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return articles
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for url_el in root.findall(".//sm:url", ns) or root.findall(".//url"):
        loc = url_el.find("sm:loc", ns)
        loc = loc.text if loc is not None else url_el.findtext("loc")
        lastmod = url_el.find("sm:lastmod", ns)
        lastmod = lastmod.text if lastmod is not None else url_el.findtext("lastmod")
        if loc:
            articles.append({"url": loc.strip(), "lastmod": lastmod})
    for sm_el in root.findall(".//sm:sitemap/sm:loc", ns) or root.findall(".//sitemap/loc"):
        try:
            r = requests.get(sm_el.text.strip(), headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                articles.extend(parse_sitemap_xml(r.content))
        except requests.RequestException:
            continue
    return articles


def parse_rss(content):
    articles = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return articles
    for item in root.findall(".//item"):
        link = item.findtext("link")
        pubdate = item.findtext("pubDate")
        if link:
            articles.append({"url": link.strip(), "lastmod": pubdate})
    return articles


def get_all_articles():
    all_articles = {}
    for a in scan_listing_pages():
        all_articles[a["url"]] = a
    for url, content in find_sitemap_urls():
        parsed = parse_rss(content) if ("rss" in url or "feed" in url) else parse_sitemap_xml(content)
        for a in parsed:
            path = urlparse(a["url"]).path
            if path and path not in ("/", "") and a["url"] not in all_articles:
                all_articles[a["url"]] = a
    return list(all_articles.values())


# ---------------------------------------------------------------------------
# 3. EXTRACTION DU CONTENU D'UNE PAGE
# ---------------------------------------------------------------------------

def prepare(html, selector=None):
    """Retourne (page complète, zone de contenu nettoyée des menus et blocs parasites)."""
    full = BeautifulSoup(html, "html.parser")
    work = BeautifulSoup(html, "html.parser")

    for tag in work(["script", "style", "noscript", "svg", "iframe", "button",
                     "form", "nav", "aside"]):
        tag.extract()
    for tag in work(["header", "footer"]):
        # on garde l'en-tête d'un article (qui contient souvent le titre)
        if tag.find_parent(["article", "main"]) is None:
            tag.extract()
    for hint in NOISE_CLASS_HINTS:
        for el in work.select(f'[class*="{hint}"], [id*="{hint}"]'):
            if el.name in ("html", "body", "main", "article"):
                continue  # on ne supprime jamais le contenu principal par erreur
            el.extract()

    root = None
    if selector:
        try:
            root = work.select_one(selector)
        except Exception:
            root = None
    if root is None:
        arts = work.find_all("article")
        if len(arts) == 1:
            root = arts[0]
        else:
            root = work.find("main") or work.body or work
    return full, root


def extract_lines(root):
    lines, seen = [], set()
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "p", "li", "td", "th",
                             "blockquote", "figcaption"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if len(text) < 3 or text in seen:
            continue
        seen.add(text)
        lines.append(text[:MAX_LINE_LEN])
        if len(lines) >= MAX_LINES:
            break
    return lines


def extract_doc_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    docs = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if not url.startswith("http"):
            continue
        if urlparse(url).path.lower().endswith(DOC_EXTENSIONS) or a.has_attr("download"):
            label = " ".join(a.get_text(" ", strip=True).split())[:120]
            docs[url] = label or os.path.basename(urlparse(url).path)
    return docs


def diff_lines(old, new):
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(old[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(new[j1:j2])
    return added, removed


def known_titles(state):
    titles = set()
    for a in state.get("articles", {}).values():
        t = normalize(a.get("title") or "")
        if len(t) >= 15:
            titles.add(t)
    return titles


def is_noise_line(line, titles):
    """Ignore les lignes qui ne sont que des dates ou des titres d'autres articles."""
    n = normalize(line).rstrip("….  ").strip()
    if len(n) <= 30 and DATE_PATTERN.fullmatch(n):
        return True
    if len(n) >= 20 and any(n in t for t in titles):
        return True
    return False


def change_details(added, removed, limit=6):
    d = []
    if added:
        d.append(f"Texte ajouté ou modifié ({len(added)} ligne(s)) :")
        d.extend(f"   + {l[:MAX_LINE_LEN]}" for l in added[:limit])
        if len(added) > limit:
            d.append(f"   … et {len(added) - limit} autre(s) ligne(s)")
    if removed:
        d.append(f"Texte retiré ({len(removed)} ligne(s)) :")
        d.extend(f"   - {l[:MAX_LINE_LEN]}" for l in removed[:limit])
        if len(removed) > limit:
            d.append(f"   … et {len(removed) - limit} autre(s) ligne(s)")
    return d


# ---------------------------------------------------------------------------
# 4. MOTS À SURVEILLER
# ---------------------------------------------------------------------------

def find_keyword_hits(lines, keywords):
    hits = []
    for kw in keywords or []:
        if not isinstance(kw, dict) or not kw.get("mot"):
            continue
        flags = 0 if kw.get("respecter_la_casse") else re.IGNORECASE
        pattern = re.compile(r"(?<!\w)" + re.escape(kw["mot"]) + r"(?!\w)", flags)
        for line in lines:
            m = pattern.search(line)
            if not m:
                continue
            start, end = max(0, m.start() - 60), min(len(line), m.end() + 60)
            passage = ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")
            hits.append({
                "mot": m.group(0),
                "type": kw.get("type") or "Mot surveillé",
                "message": kw.get("message") or "Ce mot fait partie de la liste de surveillance.",
                "correction": kw.get("correction") or "",
                "passage": passage,
            })
    return hits[:30]


def keyword_details(hits):
    d = []
    for h in hits:
        d.append(f"« {h['mot']} » — {h['type']} : {h['message']}")
        d.append(f"   Passage : {h['passage']}")
        if h["correction"]:
            d.append(f"   À utiliser plutôt : {h['correction']}")
    return d


# ---------------------------------------------------------------------------
# 5. ANALYSE D'UN ARTICLE (orthographe, liens, images, métadonnées)
# ---------------------------------------------------------------------------

def check_spelling(text):
    if not text or len(text.strip()) < 20:
        return []
    text = text[:15000]
    try:
        resp = requests.post(LANGUAGETOOL_API, data={"text": text, "language": LANG}, timeout=30)
        resp.raise_for_status()
        matches = resp.json().get("matches", [])
        errors = []
        for m in matches[:40]:
            context = m["context"]["text"]
            offset = m["context"]["offset"]
            length = m["context"]["length"]
            category_id = m.get("rule", {}).get("category", {}).get("id", "")
            errors.append({
                "type": CATEGORY_LABELS.get(category_id, "Erreur linguistique"),
                "passage": " ".join(context.split()),
                "mot_fautif": context[offset:offset + length].strip(),
                "explication": m["message"],
                "suggestions": [r["value"] for r in m.get("replacements", [])[:3]],
            })
        return errors
    except requests.RequestException:
        return []


def check_url_ok(url, timeout=8):
    """Teste un lien. Retourne (ok, détail)."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            r = requests.get(url, headers=HEADERS, timeout=timeout, stream=True, allow_redirects=True)
            r.close()
        return (r.status_code < 400), r.status_code
    except requests.RequestException:
        return False, "inaccessible"


def check_links_and_images(root, base_url):
    broken_links, missing_alt = [], []
    for img in root.find_all("img"):
        if not (img.get("alt") or "").strip():
            missing_alt.append(img.get("src") or "inconnu")

    links = []
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if not url.startswith("http"):
            continue
        host = urlparse(url).netloc.lower()
        if any(host == d or host.endswith("." + d) for d in SKIP_LINK_DOMAINS):
            continue
        if urlparse(url).path.lower().endswith(DOC_EXTENSIONS):
            continue  # les documents ont leur propre contrôle
        if url not in links:
            links.append(url)

    for url in links[:20]:
        ok, detail = check_url_ok(url)
        if not ok:
            broken_links.append((url, detail))
    return broken_links, missing_alt


def check_missing_metadata(soup):
    missing = []
    if not soup.title or not soup.title.text.strip():
        missing.append("titre de la page (<title>)")
    if not soup.find("meta", attrs={"name": "description"}):
        missing.append("meta description")
    if not soup.find("meta", attrs={"property": "article:published_time"}) and not soup.find("time"):
        missing.append("date de publication")
    if (not soup.find(attrs={"class": lambda c: c and "author" in c.lower()})
            and not soup.find("meta", attrs={"name": "author"})):
        missing.append("auteur")
    return missing


def analyze_html(url, html, config):
    full, root = prepare(html, config.get("selecteur_contenu_article") or None)
    lines = extract_lines(root)
    broken_links, missing_alt = check_links_and_images(root, url)
    title = full.title.text.strip() if full.title and full.title.text.strip() else url
    return {
        "url": url,
        "title": title,
        "lines": lines,
        "content_hash": hash_lines(lines),
        "spelling_errors": check_spelling("\n".join(lines)),
        "broken_links": broken_links,
        "missing_alt_images": missing_alt,
        "missing_metadata": check_missing_metadata(full),
        "docs": extract_doc_links(html, url),
    }


def analyze_article(url, config):
    status, html = fetch_status(url)
    if html is None or status >= 400:
        return {"url": url, "error": "page inaccessible"}
    return analyze_html(url, html, config)


def record_article(state, article, analysis):
    store = state.setdefault("articles", {})
    prev = store.get(article["url"], {})
    store[article["url"]] = {
        "title": analysis.get("title", article["url"]),
        "url": article["url"],
        "lastmod": article.get("lastmod") or prev.get("lastmod"),
        "analyzed_at": prev.get("analyzed_at") or iso(),
        "last_checked": iso(),
        "spelling_errors": analysis.get("spelling_errors", []),
        "broken_links": analysis.get("broken_links", []),
        "missing_alt_images": analysis.get("missing_alt_images", []),
        "missing_metadata": analysis.get("missing_metadata", []),
        "error": analysis.get("error"),
        "content_hash": analysis.get("content_hash"),
        "snapshot": analysis.get("lines", []),
        "status": "en_ligne",
    }


# ---------------------------------------------------------------------------
# 6. DOCUMENTS (PDF, Word, Excel...)
# ---------------------------------------------------------------------------

def register_docs(state, docs, found_on):
    store = state.setdefault("docs", {})
    for url, label in docs.items():
        e = store.setdefault(url, {"ok": None, "last_checked": None, "status": None})
        e["label"] = label
        e["found_on"] = found_on
        e["last_seen"] = iso()


def check_document(url):
    """Vérifie qu'un document existe et s'ouvre. Retourne (ok, explication)."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if r.status_code >= 400:
            r = requests.get(url, headers=HEADERS, timeout=15, stream=True, allow_redirects=True)
        status = r.status_code
        ctype = r.headers.get("Content-Type", "").lower()
        clen = r.headers.get("Content-Length")
        r.close()
    except requests.RequestException:
        return False, "le serveur ne répond pas"
    if status >= 400:
        return False, f"erreur {status} (fichier introuvable ou accès refusé)"
    if "text/html" in ctype:
        return False, "le lien ouvre une page web au lieu du fichier"
    if clen == "0":
        return False, "le fichier est vide"
    return True, "ok"


def check_documents(state, events):
    store = state.get("docs", {})
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    due = []
    for url, e in store.items():
        last = parse_iso(e.get("last_checked"))
        if last is None or now_utc() - last >= timedelta(hours=DOC_RECHECK_HOURS):
            due.append((last or epoch, url))
    due.sort()

    newly_broken, recovered = [], []
    for _, url in due[:MAX_DOC_CHECKS_PER_RUN]:
        if time_left() < 60:
            break
        ok, why = check_document(url)
        e = store[url]
        prev_ok = e.get("ok")
        e.update({"ok": ok, "status": why, "last_checked": iso()})
        if not ok and prev_ok in (True, None):
            newly_broken.append((url, e))
        elif ok and prev_ok is False:
            recovered.append((url, e))
        time.sleep(0.2)

    def doc_lines(items):
        d = []
        for url, e in items[:25]:
            d.append(f"{e.get('label') or url}")
            d.append(f"   Lien : {url}")
            if e.get("found_on"):
                d.append(f"   Trouvé sur : {e['found_on']}")
            if not e.get("ok"):
                d.append(f"   Problème : {e.get('status')}")
        if len(items) > 25:
            d.append(f"… et {len(items) - 25} autre(s) document(s)")
        return d

    if newly_broken:
        add_event(state, events, "document_casse",
                  f"{len(newly_broken)} document(s) inaccessible(s)", None,
                  doc_lines(newly_broken), count=len(newly_broken))
    if recovered:
        add_event(state, events, "document_retabli",
                  f"{len(recovered)} document(s) de nouveau accessible(s)", None,
                  doc_lines(recovered), count=len(recovered))


# ---------------------------------------------------------------------------
# 7. ARTICLES DÉJÀ PUBLIÉS : MODIFICATIONS ET SUPPRESSIONS
# ---------------------------------------------------------------------------

def article_date(entry):
    return parse_iso(entry.get("lastmod")) or parse_iso(entry.get("analyzed_at"))


def recheck_known_articles(state, config, skip_urls, events):
    titles = known_titles(state)
    cutoff = now_utc() - timedelta(days=ARTICLE_RECHECK_DAYS)
    store = state.get("articles", {})

    for url, entry in list(store.items()):
        if url in skip_urls or entry.get("status") == "supprime":
            continue
        d = article_date(entry)
        if d is not None and d < cutoff:
            continue
        if time_left() < 180:
            break

        status, html = fetch_status(url)
        time.sleep(0.4)
        if status is None:
            continue  # problème réseau passager : on ne dit rien
        title = entry.get("title") or url

        if status in (404, 410):
            entry["status"] = "supprime"
            entry["deleted_at"] = iso()
            add_event(state, events, "article_supprime", f"Article supprimé : {title}", url,
                      [f"La page répond « introuvable » (erreur {status})."])
            continue
        if status >= 400:
            continue

        register_docs(state, extract_doc_links(html, url), url)
        full, root = prepare(html, config.get("selecteur_contenu_article") or None)
        lines = extract_lines(root)
        new_hash = hash_lines(lines)
        entry["last_checked"] = iso()

        if not entry.get("content_hash"):
            # première fois qu'on voit cet article avec le nouveau système : simple point de départ
            entry["content_hash"], entry["snapshot"] = new_hash, lines
            continue
        if new_hash == entry["content_hash"]:
            continue

        added, removed = diff_lines(entry.get("snapshot", []), lines)
        added = [l for l in added if not is_noise_line(l, titles)]
        removed = [l for l in removed if not is_noise_line(l, titles)]
        if added or removed:
            add_event(state, events, "article_modifie", f"Article modifié : {title}", url,
                      change_details(added, removed))
            hits = find_keyword_hits(added, config.get("mots_surveilles"))
            if hits:
                add_event(state, events, "mot_surveille",
                          f"Mot surveillé dans un article modifié : {title}", url,
                          keyword_details(hits), count=len(hits))
            analysis = analyze_html(url, html, config)  # nouvelle analyse (fautes, liens...)
            record_article(state, {"url": url, "lastmod": entry.get("lastmod")}, analysis)
        else:
            entry["content_hash"], entry["snapshot"] = new_hash, lines


# ---------------------------------------------------------------------------
# 8. AUTRES PAGES DU SITE (menu)
# ---------------------------------------------------------------------------

def clean_url(url):
    p = urlparse(url)
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}" + (f"?{p.query}" if p.query else "")


def discover_watched_pages(config, homepage_html):
    ignore = [str(p).rstrip("/") for p in config.get("pages_a_ignorer", [])]
    host = urlparse(SITE_URL).netloc
    found = {}
    if homepage_html:
        soup = BeautifulSoup(homepage_html, "html.parser")
        zones = soup.find_all(["nav", "header", "footer"]) or [soup]
        for zone in zones:
            for a in zone.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                url = urldefrag(urljoin(SITE_URL + "/", href))[0]
                p = urlparse(url)
                if p.netloc != host:
                    continue
                path = p.path.rstrip("/")
                if not path or path.lower().endswith(DOC_EXTENSIONS):
                    continue
                if any(path == ig or path.startswith(ig + "/") for ig in ignore):
                    continue
                label = " ".join(a.get_text(" ", strip=True).split())[:80] or path
                found.setdefault(clean_url(url), label)
    for extra in config.get("pages_a_surveiller_en_plus", []) or []:
        found.setdefault(clean_url(urljoin(SITE_URL + "/", str(extra))), str(extra))
    return dict(list(found.items())[:MAX_WATCHED_PAGES])


def check_watched_pages(state, config, homepage_html, events):
    pages = discover_watched_pages(config, homepage_html)
    if not pages:
        if not state.get("menu_vide"):
            add_event(state, events, "menu_vide",
                      "Aucune page du menu n'a pu être repérée sur l'accueil", SITE_URL,
                      ["Le menu est peut-être chargé par JavaScript : les autres pages du site ne sont pas surveillées.",
                       "Solution : lister les pages à surveiller dans config.json (pages_a_surveiller_en_plus)."])
        state["menu_vide"] = True
    else:
        state["menu_vide"] = False
    store = state.setdefault("pages", {})
    titles = known_titles(state)
    keywords = config.get("mots_surveilles")

    for url, label in pages.items():
        if time_left() < 240:
            break
        status, html = fetch_status(url)
        time.sleep(0.4)
        entry = store.get(url)
        if status is None:
            continue

        if status >= 400:
            if not entry or not entry.get("broken"):
                add_event(state, events, "page_inaccessible",
                          f"Page du menu inaccessible : {label}", url,
                          [f"La page répond avec l'erreur {status}."])
            store[url] = {**(entry or {}), "label": label, "broken": True,
                          "last_checked": iso()}
            continue

        register_docs(state, extract_doc_links(html, url), url)
        full, root = prepare(html)
        lines = extract_lines(root)
        new_hash = hash_lines(lines)

        if not entry or "hash" not in entry:
            # première visite : point de départ, sans alerte de modification
            hits = find_keyword_hits(lines, keywords)
            if hits:
                add_event(state, events, "mot_surveille",
                          f"Mot surveillé sur la page : {label}", url,
                          keyword_details(hits), count=len(hits))
            store[url] = {"label": label, "hash": new_hash, "lines": lines, "broken": False,
                          "last_checked": iso(), "last_changed": iso()}
            continue

        entry.update({"label": label, "broken": False, "last_checked": iso()})
        if new_hash == entry["hash"]:
            continue

        added, removed = diff_lines(entry.get("lines", []), lines)
        added = [l for l in added if not is_noise_line(l, titles)]
        removed = [l for l in removed if not is_noise_line(l, titles)]
        if added or removed:
            add_event(state, events, "page_modifiee", f"Page modifiée : {label}", url,
                      change_details(added, removed))
            hits = find_keyword_hits(added, keywords)
            if hits:
                add_event(state, events, "mot_surveille",
                          f"Mot surveillé dans une page modifiée : {label}", url,
                          keyword_details(hits), count=len(hits))
            entry["last_changed"] = iso()
        entry["hash"], entry["lines"] = new_hash, lines

    if pages:
        for url in [u for u in store if u not in pages]:
            del store[url]  # pages qui ne sont plus dans le menu


# ---------------------------------------------------------------------------
# 9. MISE EN FORME DES RAPPORTS
# ---------------------------------------------------------------------------

def format_article_block(art):
    lines = ["=" * 70, f"📰 ARTICLE : {art.get('title', art['url'])}", f"🔗 Lien : {art['url']}"]
    d = parse_iso(art.get("lastmod"))
    if d:
        lines.append(f"📅 Publié le : {d.strftime('%d/%m/%Y')}")
    if art.get("status") == "supprime":
        lines.append("🗑️  Cet article a été supprimé du site depuis.")
    lines.append("")

    if art.get("error"):
        lines.append(f"⚠️  Problème : {art['error']}")
        return "\n".join(lines)

    errs = art.get("spelling_errors", [])
    lines.append(f"✏️  FAUTES DÉTECTÉES : {len(errs)}")
    if errs:
        lines.append("")
        for i, e in enumerate(errs, 1):
            lines.append(f"  {i}. [{e['type']}]")
            lines.append(f"     Passage concerné : \"...{e['passage']}...\"")
            lines.append(f"     Mot/expression visé : « {e['mot_fautif']} »")
            lines.append(f"     Explication : {e['explication']}")
            if e["suggestions"]:
                lines.append(f"     Correction suggérée : {' / '.join(e['suggestions'])}")
            lines.append("")
    else:
        lines.append("     Aucune faute détectée.")
        lines.append("")

    if art.get("broken_links"):
        lines.append(f"🔗 LIENS CASSÉS : {len(art['broken_links'])}")
        for link, code in art["broken_links"]:
            lines.append(f"     - {link} (statut : {code})")
        lines.append("")
    if art.get("missing_alt_images"):
        lines.append(f"🖼️  IMAGES SANS DESCRIPTION (texte alternatif) : {len(art['missing_alt_images'])}")
        for src in art["missing_alt_images"][:5]:
            lines.append(f"     - {src}")
        lines.append("")
    if art.get("missing_metadata"):
        lines.append(f"📋 INFORMATIONS MANQUANTES : {', '.join(art['missing_metadata'])}")
        lines.append("")
    return "\n".join(lines)


def format_events(events):
    """Regroupe les événements par section, dans un ordre logique."""
    out = []
    sections = []
    for kind, (emoji, section, _) in KIND_META.items():
        if section not in [s for s, _ in sections]:
            sections.append((section, emoji))
    for section, emoji in sections:
        group = [e for e in events if e["kind"] in KIND_META and KIND_META[e["kind"]][1] == section]
        if not group:
            continue
        out.append("-" * 70)
        out.append(f"{emoji} {section}")
        out.append("-" * 70)
        for ev in group:
            out.append(f"• {ev['title']}")
            if ev.get("url"):
                out.append(f"  Lien : {ev['url']}")
            for d in ev.get("details", []):
                out.append(f"  {d}")
            out.append("")
    return "\n".join(out)


def build_alert(events, analyses):
    parts = []
    if analyses:
        n = len(analyses)
        parts.append("1 nouvel article" if n == 1 else f"{n} nouveaux articles")
    for kind, (_, _, (sg, pl)) in KIND_META.items():
        n = sum(e.get("count", 1) for e in events if e["kind"] == kind)
        if n:
            parts.append(f"{n} {sg if n == 1 else pl}")
    urgent = any(e["kind"] == "site_indisponible" for e in events)
    subject = ("🚨" if urgent else "🔔") + " [Veille edu-nc.gouv.cd] " + " · ".join(parts)

    lines = [f"🔔 ALERTE VEILLE — {SITE_URL}",
             f"Détecté le {datetime.now().strftime('%d/%m/%Y à %H:%M')}", ""]
    if events:
        lines.append(format_events(events))
    if analyses:
        lines.append("-" * 70)
        lines.append("📰 NOUVEAUX ARTICLES PUBLIÉS")
        lines.append("-" * 70)
        for art in analyses:
            lines.append(format_article_block(art))
    return subject, "\n".join(lines)


def uptime_summary(state, start, end):
    log = [e for e in state.get("uptime_log", [])
           if (parse_iso(e["ts"]) and start <= parse_iso(e["ts"]) < end)]
    if not log:
        return "Aucune vérification enregistrée sur la période."
    incidents = [e for e in log if e["status"] != "ok"]
    times = [e["seconds"] for e in log if e.get("seconds") is not None]
    avg = f"{sum(times) / len(times):.1f} s" if times else "n/a"
    return (f"{len(log)} vérifications · {len(incidents)} incident(s) "
            f"(site lent ou inaccessible) · temps de réponse moyen : {avg}")


def build_weekly_report(state, preview=False):
    now = now_utc()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    if preview:
        start, end = monday, now + timedelta(seconds=1)
    else:
        start, end = monday - timedelta(days=7), monday
    last_day = (end - timedelta(days=1)) if not preview else now

    week_articles = []
    for art in state.get("articles", {}).values():
        d = parse_iso(art.get("lastmod"))
        if d and start <= d < end:
            week_articles.append(art)
    week_articles.sort(key=lambda a: a.get("lastmod") or "")

    week_events = [e for e in state.get("events", [])
                   if parse_iso(e["ts"]) and start <= parse_iso(e["ts"]) < end]

    def total(kind):
        return sum(e.get("count", 1) for e in week_events if e["kind"] == kind)

    broken_docs = [(u, e) for u, e in state.get("docs", {}).items() if e.get("ok") is False]
    fautes = sum(len(a.get("spelling_errors", [])) for a in week_articles)
    liens = sum(len(a.get("broken_links", [])) for a in week_articles)

    L = ["#" * 70,
         f"📊 RAPPORT HEBDOMADAIRE DE VEILLE — {SITE_URL}",
         ("APERÇU : semaine en cours, du " if preview else "Semaine du ")
         + f"{start.strftime('%d/%m/%Y')} au {last_day.strftime('%d/%m/%Y')}",
         "#" * 70, "", "RÉSUMÉ",
         f"  • Articles publiés : {len(week_articles)}",
         f"  • Fautes détectées dans ces articles : {fautes}",
         f"  • Liens cassés dans ces articles : {liens}",
         f"  • Articles modifiés après publication : {total('article_modifie')}",
         f"  • Articles supprimés : {total('article_supprime')}",
         f"  • Pages du site modifiées : {total('page_modifiee')}",
         f"  • Pages du menu inaccessibles : {total('page_inaccessible')}",
         f"  • Documents devenus inaccessibles : {total('document_casse')}",
         f"  • Documents actuellement inaccessibles (au total) : {len(broken_docs)}",
         f"  • Mots surveillés détectés : {total('mot_surveille')}",
         f"  • Disponibilité du site : {uptime_summary(state, start, end)}",
         ""]

    if week_events:
        L.append("POINTS D'ATTENTION DE LA SEMAINE")
        L.append(format_events(week_events))

    if broken_docs:
        L.append("-" * 70)
        L.append("📎 DOCUMENTS ACTUELLEMENT INACCESSIBLES (à corriger)")
        L.append("-" * 70)
        for url, e in broken_docs[:40]:
            L.append(f"• {e.get('label') or url}")
            L.append(f"  Lien : {url}")
            if e.get("found_on"):
                L.append(f"  Trouvé sur : {e['found_on']}")
            L.append(f"  Problème : {e.get('status')}")
            L.append("")

    L.append("DÉTAIL PAR ARTICLE PUBLIÉ")
    L.append("")
    if not week_articles:
        L.append("Aucun article publié sur cette période.")
    for art in week_articles:
        L.append(format_article_block(art))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 10. EMAIL
# ---------------------------------------------------------------------------

def send_email(subject, body):
    if not SMTP_USER or not SMTP_PASSWORD:
        print("SMTP non configuré — rapport affiché en console uniquement :\n")
        print(subject)
        print(body)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [ALERT_EMAIL_TO], msg.as_string())
    print(f"Email envoyé à {ALERT_EMAIL_TO}")


# ---------------------------------------------------------------------------
# 11. PROGRAMME PRINCIPAL
# ---------------------------------------------------------------------------

def prune_state(state):
    now = now_utc()
    state["events"] = [e for e in state.get("events", [])
                       if (parse_iso(e["ts"]) or now) >= now - timedelta(days=28)]
    for url, entry in list(state.get("articles", {}).items()):
        d = article_date(entry)
        if d and d < now - timedelta(days=120):
            del state["articles"][url]
        elif d and d < now - timedelta(days=45):
            entry.pop("snapshot", None)
    for url, e in list(state.get("docs", {}).items()):
        seen = parse_iso(e.get("last_seen"))
        if seen and seen < now - timedelta(days=60):
            del state["docs"][url]


def finish_run(state, events, analyses):
    if events or analyses:
        subject, body = build_alert(events, analyses)
        send_email(subject, body)
    state["last_run"] = iso()
    prune_state(state)
    save_state(state)


def run_scan():
    state = load_state()
    config = load_config()
    events = []

    if config.get("_erreur"):
        add_event(state, events, "config_invalide",
                  "Le fichier config.json contient une erreur : il est ignoré pour l'instant",
                  None, [config["_erreur"],
                         "Corrige le fichier (virgules, guillemets, accolades) ou demande de l'aide."])

    # 1. Le site répond-il ?
    health = check_site_health(config)
    update_health(state, health, events)
    if health["status"] == "down":
        finish_run(state, events, [])
        return

    # 2. Nouveaux articles
    seen = set(state.get("seen_urls", []))
    all_articles = get_all_articles()
    if not all_articles:
        if not state.get("listing_empty"):
            add_event(state, events, "listing_vide",
                      "Aucun article trouvé sur /actualites alors que le site répond",
                      SITE_URL + "/actualites",
                      ["La structure de la page a peut-être changé : le script ne repère plus les articles."])
        state["listing_empty"] = True
    else:
        state["listing_empty"] = False

    new_articles = [a for a in all_articles if a["url"] not in seen]
    analyses, done_urls = [], set()
    for a in new_articles:
        if time_left() < 240:
            break
        print(f"Analyse de {a['url']} ...")
        analysis = analyze_article(a["url"], config)
        if analysis.get("error"):
            continue  # on réessaiera au prochain scan
        done_urls.add(a["url"])
        analyses.append(analysis)
        record_article(state, a, analysis)
        register_docs(state, analysis.get("docs", {}), a["url"])
        hits = find_keyword_hits(analysis["lines"], config.get("mots_surveilles"))
        if hits:
            add_event(state, events, "mot_surveille",
                      f"Mot surveillé dans un nouvel article : {analysis['title']}", a["url"],
                      keyword_details(hits), count=len(hits))
        time.sleep(1)  # ne pas surcharger l'API de correction

    # 3. Articles déjà publiés : modifiés ou supprimés ?
    recheck_known_articles(state, config, {a["url"] for a in new_articles}, events)

    # 4. Autres pages du site
    check_watched_pages(state, config, health["homepage_html"], events)

    # 5. Documents
    check_documents(state, events)

    state["seen_urls"] = list(seen | done_urls)
    finish_run(state, events, analyses)


def run_weekly_report():
    state = load_state()
    preview = (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
               or "--apercu" in sys.argv)
    report = build_weekly_report(state, preview=preview)
    subject = "📊 [Veille edu-nc.gouv.cd] Rapport hebdomadaire"
    if preview:
        subject += " (aperçu de la semaine en cours)"
    send_email(subject, report)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "weekly":
        run_weekly_report()
    else:
        run_scan()
