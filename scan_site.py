#!/usr/bin/env python3
"""
Veille automatique pour edu-nc.gouv.cd
----------------------------------------
- Repère les nouveaux articles publiés (page /actualites, avec repli sur sitemap/RSS)
- Vérifie l'orthographe/grammaire (via l'API LanguageTool, gratuite)
- Vérifie les liens cassés et images sans texte alternatif
- Vérifie les métadonnées manquantes (titre, description, date, auteur)
- Envoie une alerte email à chaque nouvelle publication
- Envoie un rapport hebdomadaire (lundi au dimanche) chaque lundi matin
"""

import os
import re
import sys
import json
import time
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
SITE_URL = os.environ.get("SITE_URL", "https://edu-nc.gouv.cd")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
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

MOIS_FR = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "décembre": 12,
}
DATE_PATTERN = re.compile(
    r"(\d{1,2})\s+(" + "|".join(MOIS_FR.keys()) + r")\s+(\d{4})", re.IGNORECASE
)

# Traduction des catégories LanguageTool en français simple
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

# ---------------------------------------------------------------------------
# 1. TROUVER LES ARTICLES
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
        d = datetime(int(year), month, int(day), tzinfo=timezone.utc)
        return d.isoformat()
    except ValueError:
        return None


def scan_listing_pages():
    articles = {}
    for path in LISTING_PATHS:
        url = urljoin(SITE_URL, path)
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except requests.RequestException:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            parsed = urlparse(href)
            article_path = parsed.path
            if not ARTICLE_PATH_PATTERN.match(article_path):
                continue

            full_url = urljoin(SITE_URL, article_path)
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
    candidates = [
        "/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
        "/sitemap-articles.xml", "/feed/", "/rss.xml",
    ]
    found = []
    for path in candidates:
        url = urljoin(SITE_URL, path)
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
# 2. ANALYSER UN ARTICLE
# ---------------------------------------------------------------------------

def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r
    except requests.RequestException:
        return None


def check_spelling(text):
    """Retourne une liste de fautes claires : type, passage concerné, explication, correction."""
    if not text or len(text.strip()) < 20:
        return []
    text = text[:15000]
    try:
        resp = requests.post(
            LANGUAGETOOL_API, data={"text": text, "language": LANG}, timeout=30,
        )
        resp.raise_for_status()
        matches = resp.json().get("matches", [])
        errors = []
        for m in matches[:40]:
            context = m["context"]["text"]
            offset = m["context"]["offset"]
            length = m["context"]["length"]
            mot_fautif = context[offset:offset + length]
            category_id = m.get("rule", {}).get("category", {}).get("id", "")
            category_label = CATEGORY_LABELS.get(category_id, "Erreur linguistique")
            errors.append({
                "type": category_label,
                "passage": context,
                "mot_fautif": mot_fautif,
                "explication": m["message"],
                "suggestions": [r["value"] for r in m.get("replacements", [])[:3]],
            })
        return errors
    except requests.RequestException:
        return []


def check_links_and_images(soup, base_url):
    broken_links = []
    missing_alt = []
    for img in soup.find_all("img"):
        if not img.get("alt", "").strip():
            missing_alt.append(img.get("src", "inconnu"))

    links = [a.get("href") for a in soup.find_all("a") if a.get("href")]
    checked = 0
    for href in links:
        if checked >= 15:
            break
        full_url = urljoin(base_url, href)
        if not full_url.startswith("http"):
            continue
        try:
            r = requests.head(full_url, headers=HEADERS, timeout=8, allow_redirects=True)
            if r.status_code >= 400:
                broken_links.append((full_url, r.status_code))
        except requests.RequestException:
            broken_links.append((full_url, "inaccessible"))
        checked += 1

    return broken_links, missing_alt


def check_missing_metadata(soup):
    missing = []
    if not soup.title or not soup.title.text.strip():
        missing.append("titre de la page (<title>)")
    if not soup.find("meta", attrs={"name": "description"}):
        missing.append("meta description")
    if not soup.find("meta", attrs={"property": "article:published_time"}) and not soup.find("time"):
        missing.append("date de publication")
    if not soup.find(attrs={"class": lambda c: c and "author" in c.lower()}) and not soup.find("meta", attrs={"name": "author"}):
        missing.append("auteur")
    return missing


def analyze_article(url):
    resp = fetch_page(url)
    if resp is None:
        return {"url": url, "error": "page inaccessible"}

    soup = BeautifulSoup(resp.text, "html.parser")
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    full_text = " ".join(paragraphs)

    spelling_errors = check_spelling(full_text)
    broken_links, missing_alt = check_links_and_images(soup, url)
    missing_meta = check_missing_metadata(soup)
    title = soup.title.text.strip() if soup.title else url

    return {
        "url": url, "title": title, "spelling_errors": spelling_errors,
        "broken_links": broken_links, "missing_alt_images": missing_alt,
        "missing_metadata": missing_meta,
    }


# ---------------------------------------------------------------------------
# 3. ÉTAT
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_urls": [], "last_run": None, "articles": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def record_article(state, article, analysis):
    """Sauvegarde le résultat d'analyse d'un article pour le rapport hebdomadaire."""
    state.setdefault("articles", {})
    state["articles"][article["url"]] = {
        "title": analysis.get("title", article["url"]),
        "url": article["url"],
        "lastmod": article.get("lastmod"),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "spelling_errors": analysis.get("spelling_errors", []),
        "broken_links": analysis.get("broken_links", []),
        "missing_alt_images": analysis.get("missing_alt_images", []),
        "missing_metadata": analysis.get("missing_metadata", []),
        "error": analysis.get("error"),
    }


# ---------------------------------------------------------------------------
# 4. FORMATAGE DU RAPPORT (fautes claires et explicites)
# ---------------------------------------------------------------------------

def format_article_block(art):
    lines = []
    lines.append("=" * 70)
    lines.append(f"📰 ARTICLE : {art.get('title', art['url'])}")
    lines.append(f"🔗 Lien : {art['url']}")
    if art.get("lastmod"):
        try:
            d = datetime.fromisoformat(art["lastmod"].replace("Z", "+00:00"))
            lines.append(f"📅 Publié le : {d.strftime('%d/%m/%Y')}")
        except (ValueError, TypeError):
            pass
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


def build_alert_report(new_articles_analysis):
    """Rapport envoyé immédiatement à chaque nouvelle publication."""
    lines = []
    lines.append(f"🔔 NOUVELLE PUBLICATION — {SITE_URL}")
    lines.append(f"Détecté le {datetime.now().strftime('%d/%m/%Y à %H:%M')}")
    lines.append("")
    for art in new_articles_analysis:
        lines.append(format_article_block(art))
    return "\n".join(lines)


def build_weekly_report(state):
    """Rapport hebdomadaire : tous les articles publiés du lundi au dimanche précédent."""
    now = datetime.now(timezone.utc)
    start_of_this_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    period_end = start_of_this_week
    period_start = period_end - timedelta(days=7)

    articles = list(state.get("articles", {}).values())
    week_articles = []
    for art in articles:
        if not art.get("lastmod"):
            continue
        try:
            d = datetime.fromisoformat(art["lastmod"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if period_start <= d < period_end:
            week_articles.append(art)

    week_articles.sort(key=lambda a: a.get("lastmod") or "")

    total_fautes = sum(len(a.get("spelling_errors", [])) for a in week_articles)
    total_liens_casses = sum(len(a.get("broken_links", [])) for a in week_articles)

    lines = []
    lines.append("#" * 70)
    lines.append(f"📊 RAPPORT HEBDOMADAIRE DE VEILLE — {SITE_URL}")
    lines.append(f"Semaine du {period_start.strftime('%d/%m/%Y')} au {(period_end - timedelta(days=1)).strftime('%d/%m/%Y')}")
    lines.append("#" * 70)
    lines.append("")
    lines.append("RÉSUMÉ")
    lines.append(f"  • Articles publiés cette semaine : {len(week_articles)}")
    lines.append(f"  • Total de fautes détectées : {total_fautes}")
    lines.append(f"  • Total de liens cassés détectés : {total_liens_casses}")
    lines.append("")

    if not week_articles:
        lines.append("Aucun article publié cette semaine.")
        return "\n".join(lines)

    lines.append("DÉTAIL PAR ARTICLE")
    lines.append("")
    for art in week_articles:
        lines.append(format_article_block(art))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. EMAIL
# ---------------------------------------------------------------------------

def send_email(subject, body):
    if not SMTP_USER or not SMTP_PASSWORD:
        print("SMTP non configuré — rapport affiché en console uniquement :\n")
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
# 6. PROGRAMMES PRINCIPAUX
# ---------------------------------------------------------------------------

def run_scan():
    """Scan normal : détecte les nouveaux articles et alerte immédiatement."""
    state = load_state()
    seen = set(state.get("seen_urls", []))

    all_articles = get_all_articles()
    if not all_articles:
        print("Aucun article trouvé sur /actualites ni via sitemap/RSS.")
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    new_articles = [a for a in all_articles if a["url"] not in seen]

    analyses = []
    for a in new_articles:
        print(f"Analyse de {a['url']} ...")
        analysis = analyze_article(a["url"])
        analyses.append(analysis)
        record_article(state, a, analysis)
        time.sleep(1)

    if analyses:
        report = build_alert_report(analyses)
        send_email(f"🔔 [Veille edu-nc.gouv.cd] {len(new_articles)} nouvel(aux) article(s)", report)

    state["seen_urls"] = list(seen | {a["url"] for a in all_articles})
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def run_weekly_report():
    """Génère et envoie le rapport hebdomadaire (à lancer le lundi matin)."""
    state = load_state()
    report = build_weekly_report(state)
    send_email(f"📊 [Veille edu-nc.gouv.cd] Rapport hebdomadaire", report)
    save_state(state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "weekly":
        run_weekly_report()
    else:
        run_scan()
