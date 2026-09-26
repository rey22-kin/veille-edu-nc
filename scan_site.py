#!/usr/bin/env python3
"""
Veille automatique pour edu-nc.gouv.cd
----------------------------------------
- Repère les nouveaux articles publiés (via sitemap.xml ou flux RSS)
- Vérifie l'orthographe/grammaire (via l'API LanguageTool, gratuite)
- Vérifie les liens cassés et images sans texte alternatif
- Vérifie les métadonnées manquantes (titre, description, date, auteur)
- Compte les articles publiés dans les 7 derniers jours
- Envoie un rapport par email

Configuration : voir les variables d'environnement en bas du fichier
(à définir en secrets GitHub Actions, ou dans un fichier .env local).
"""

import os
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
SMTP_USER = os.environ.get("SMTP_USER")          # ex: toncompte@gmail.com
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")  # mot de passe d'application
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", SMTP_USER)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VeilleEduNC/1.0)"}
REQUEST_TIMEOUT = 15

# ---------------------------------------------------------------------------
# 1. TROUVER LES ARTICLES (sitemap ou RSS)
# ---------------------------------------------------------------------------

def find_sitemap_urls():
    """Essaie plusieurs emplacements courants de sitemap."""
    candidates = [
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/wp-sitemap.xml",
        "/sitemap-articles.xml",
        "/feed/",
        "/rss.xml",
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
    """Extrait les URLs (et lastmod si dispo) d'un sitemap XML."""
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

    # Si c'est un sitemap-index, aller chercher les sous-sitemaps
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
    for url, content in find_sitemap_urls():
        if "rss" in url or "feed" in url:
            parsed = parse_rss(content)
        else:
            parsed = parse_sitemap_xml(content)
        for a in parsed:
            # On ne garde que les pages qui ressemblent à des articles
            path = urlparse(a["url"]).path
            if path and path not in ("/", ""):
                all_articles[a["url"]] = a
    return list(all_articles.values())


# ---------------------------------------------------------------------------
# 2. ANALYSER UN ARTICLE (orthographe, bugs, métadonnées)
# ---------------------------------------------------------------------------

def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        return None


def check_spelling(text):
    """Envoie le texte à LanguageTool et retourne la liste des fautes."""
    if not text or len(text.strip()) < 20:
        return []
    # LanguageTool limite la taille des requêtes ; on tronque si besoin
    text = text[:15000]
    try:
        resp = requests.post(
            LANGUAGETOOL_API,
            data={"text": text, "language": LANG},
            timeout=30,
        )
        resp.raise_for_status()
        matches = resp.json().get("matches", [])
        errors = []
        for m in matches[:30]:  # on limite pour ne pas noyer le rapport
            context = m["context"]["text"]
            errors.append({
                "message": m["message"],
                "context": context,
                "suggestions": [r["value"] for r in m.get("replacements", [])[:3]],
            })
        return errors
    except requests.RequestException:
        return []


def check_links_and_images(soup, base_url):
    broken_links = []
    missing_alt = []

    # Images sans texte alternatif
    for img in soup.find_all("img"):
        if not img.get("alt", "").strip():
            src = img.get("src", "inconnu")
            missing_alt.append(src)

    # Liens (on vérifie seulement un échantillon pour rester rapide)
    links = [a.get("href") for a in soup.find_all("a") if a.get("href")]
    checked = 0
    for href in links:
        if checked >= 15:  # limite pour éviter les scans trop longs
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

    # Texte principal (heuristique simple : paragraphes)
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    full_text = " ".join(paragraphs)

    spelling_errors = check_spelling(full_text)
    broken_links, missing_alt = check_links_and_images(soup, url)
    missing_meta = check_missing_metadata(soup)

    title = soup.title.text.strip() if soup.title else url

    return {
        "url": url,
        "title": title,
        "spelling_errors": spelling_errors,
        "broken_links": broken_links,
        "missing_alt_images": missing_alt,
        "missing_metadata": missing_meta,
    }


# ---------------------------------------------------------------------------
# 3. ÉTAT (mémoriser les articles déjà vus)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_urls": [], "last_run": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 4. RAPPORT + EMAIL
# ---------------------------------------------------------------------------

def build_report(new_articles_analysis, weekly_count):
    lines = []
    lines.append(f"RAPPORT DE VEILLE — {SITE_URL}")
    lines.append(f"Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}")
    lines.append("=" * 60)
    lines.append(f"\nArticles publiés durant les 7 derniers jours : {weekly_count}")
    lines.append(f"Nouveaux articles détectés à ce scan : {len(new_articles_analysis)}\n")

    if not new_articles_analysis:
        lines.append("Aucun nouvel article depuis le dernier scan.")
        return "\n".join(lines)

    for art in new_articles_analysis:
        lines.append("-" * 60)
        lines.append(f"ARTICLE : {art.get('title', art['url'])}")
        lines.append(f"URL : {art['url']}")

        if art.get("error"):
            lines.append(f"  ⚠️ {art['error']}")
            continue

        errs = art["spelling_errors"]
        lines.append(f"\n  Fautes d'orthographe/grammaire détectées : {len(errs)}")
        for e in errs[:10]:
            lines.append(f"    - {e['message']}")
            lines.append(f"      Contexte : \"{e['context']}\"")
            if e["suggestions"]:
                lines.append(f"      Suggestions : {', '.join(e['suggestions'])}")

        if art["broken_links"]:
            lines.append(f"\n  Liens cassés/inaccessibles ({len(art['broken_links'])}) :")
            for link, code in art["broken_links"]:
                lines.append(f"    - {link} (statut: {code})")

        if art["missing_alt_images"]:
            lines.append(f"\n  Images sans texte alternatif ({len(art['missing_alt_images'])}) :")
            for src in art["missing_alt_images"][:5]:
                lines.append(f"    - {src}")

        if art["missing_metadata"]:
            lines.append(f"\n  Métadonnées manquantes : {', '.join(art['missing_metadata'])}")

        lines.append("")

    return "\n".join(lines)


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
# 5. PROGRAMME PRINCIPAL
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    seen = set(state.get("seen_urls", []))

    all_articles = get_all_articles()
    if not all_articles:
        print("Aucun article trouvé via sitemap/RSS. Vérifie l'URL ou la structure du site.")
        return

    new_articles = [a for a in all_articles if a["url"] not in seen]

    # Compter les articles de la semaine (si lastmod disponible)
    one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    weekly_count = 0
    for a in all_articles:
        if a.get("lastmod"):
            try:
                d = datetime.fromisoformat(a["lastmod"].replace("Z", "+00:00"))
                if d >= one_week_ago:
                    weekly_count += 1
            except ValueError:
                pass

    analyses = []
    for a in new_articles:
        print(f"Analyse de {a['url']} ...")
        analyses.append(analyze_article(a["url"]))
        time.sleep(1)  # pour ne pas surcharger l'API LanguageTool

    report = build_report(analyses, weekly_count)
    send_email(f"[Veille edu-nc.gouv.cd] {len(new_articles)} nouvel(aux) article(s)", report)

    # Mettre à jour l'état
    state["seen_urls"] = list(seen | {a["url"] for a in all_articles})
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
