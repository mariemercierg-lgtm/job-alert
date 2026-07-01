import smtplib
import os
import json
import requests
import re
import time
import urllib3
from html import unescape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Mots-clés larges couvrant les compétences de Marie :
# service client, coordination, gestion, commerce, événementiel, communication.
KEYWORDS = [
    "chargé de projet",
    "coordinateur",
    "assistant manager",
    "office manager",
    "chargé de communication",
    "responsable accueil",
    "responsable clientèle",
    "chargé de relations",
    "chef de projet",
    "community manager",
    "animateur réseau",
    "responsable équipe",
    "attaché commercial",
    "chargé développement",
]

LOCATIONS = ["Marseille", "Aix-en-Provence"]

FT_COMMUNES = {
    "Marseille": "13201",
    "Aix-en-Provence": "13001",
}

# Mots-clés à écarter systématiquement dans les titres d'offres.
# On garde une liste courte : le filtre IA fait le gros du travail.
EXCLUSIONS = [
    "stage", "alternance", "alternant", "apprentissage", "apprenti",
    "intern", "en alternance", "en stage", "contrat d'apprentissage",
    "technicien", "technicienne", "ouvrier", "ouvrière",
    "conducteur d'engins", "chauffeur", "livreur",
    "infirmier", "infirmière", "aide-soignant", "auxiliaire de vie",
    "nucléaire", "frigoriste", "électromécanicien",
    "ingénieur travaux", "ingénieur calcul", "projeteur",
]

SEEN_FILE = "seen_jobs.json"
TODAY_FILE = "today_jobs.json"
REJECTED_FILE = "rejected_keywords.json"
REJECTED_REASONS_FILE = "rejected_reasons.json"
AI_VERDICTS_FILE = "ai_verdicts.json"
AI_VERDICTS_MAX = 3000

# Mots-clés Service Public : on cible coordination, accueil, communication,
# développement local, événementiel, gestion de projet en collectivité.
SP_KEYWORDS = [
    "coordinateur", "chargé-de-projet", "communication",
    "accueil", "developpement-local", "evenementiel",
]

SP_PACA_DEPTS = {
    "04": "Alpes-de-Haute-Provence", "05": "Hautes-Alpes",
    "06": "Alpes-Maritimes", "13": "Bouches-du-Rhône",
    "83": "Var", "84": "Vaucluse",
}

SP_PACA_TEXT = [
    ("marseille", "Marseille"), ("aix-en-provence", "Aix-en-Provence"),
    ("toulon", "Toulon"), ("nice", "Nice"), ("avignon", "Avignon"),
    ("bouches-du-rhône", "Bouches-du-Rhône"), ("bouches-du-rhone", "Bouches-du-Rhône"),
    ("alpes-maritimes", "Alpes-Maritimes"), ("provence-alpes", "PACA"),
    ("paca", "PACA"), ("télétravail", "Télétravail"), ("teletravail", "Télétravail"),
]

PROFILE = """
Marie a un BTS Hôtellerie-Restauration option A (service et commercialisation)
et un MBA Dirigeant d'entreprise, commerce et entrepreneuriat.
Elle est en reconversion professionnelle et explore activement différents secteurs.
Ses compétences clés : service client, gestion d'équipe, sens commercial,
communication, organisation et coordination de projets.
Elle cherche un poste débutant à intermédiaire (pas besoin d'expérience préalable dans le secteur),
en CDI ou CDD, à Marseille ou dans la zone Aix-Marseille / Bouches-du-Rhône.
Elle est ouverte à tout secteur où ses compétences relationnelles, commerciales
et organisationnelles sont valorisées : événementiel, tourisme, immobilier,
médico-social (côté administratif/coordination), culture, ESS, secteur public local,
communication, commerce, hôtellerie, restauration managériale, etc.
Elle ne cherche PAS de postes très techniques (ingénierie, IT, santé médicale clinique),
ni de postes uniquement physiques ou de manutention.
"""


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = unescape(value)
    return " ".join(value.split())


def format_job_date(raw):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw or ""))
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else ""


def format_salary_range(lo, hi):
    def k(v):
        try:
            n = float(v)
            return f"{round(n / 1000)} k€" if n >= 1000 else ""
        except (TypeError, ValueError):
            return ""
    lo_s, hi_s = k(lo), k(hi)
    if lo_s and hi_s and lo_s != hi_s:
        return f"{lo_s} – {hi_s}"
    return lo_s or hi_s


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


EXCLUDED_LOG = []


def log_excluded(title, company, location, source, reason):
    EXCLUDED_LOG.append({
        "title": title, "company": company,
        "location": location, "source": source, "reason": reason,
    })


def get_exclusions():
    base = list(EXCLUSIONS)
    rejected = load_json(REJECTED_FILE, [])
    return base + rejected


def matches_location(value):
    text = (value or "").lower()
    allowed_terms = [
        "marseille", "aix", "aix-en-provence", "bouches", "paca",
        "provence", "13", "télétravail", "teletravail", "france",
    ]
    return any(term in text for term in allowed_terms)


_FT_TOKEN_CACHE = {"token": "", "expires_at": 0}


def get_ft_token():
    if _FT_TOKEN_CACHE["token"] and time.time() < _FT_TOKEN_CACHE["expires_at"]:
        return _FT_TOKEN_CACHE["token"]
    client_id = os.environ.get("FT_CLIENT_ID", "")
    client_secret = os.environ.get("FT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("  FT: identifiants absents")
        return ""
    try:
        r = requests.post(
            "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": f"api_offresdemploiv2 o2dsoffre application_{client_id}",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  FT token erreur {r.status_code}: {r.text[:300]}")
            return ""
        payload = r.json()
        token = payload.get("access_token", "")
        _FT_TOKEN_CACHE["token"] = token
        _FT_TOKEN_CACHE["expires_at"] = time.time() + payload.get("expires_in", 1500) - 60
        return token
    except Exception as e:
        print(f"  EXCEPTION token FT: {e}")
        return ""


def search_adzuna(keyword, location):
    app_id = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        print("  Adzuna: identifiants absents")
        return []
    exclusions = get_exclusions()
    url = (
        f"https://api.adzuna.com/v1/api/jobs/fr/search/1"
        f"?app_id={app_id}&app_key={app_key}"
        f"&results_per_page=10"
        f"&what={requests.utils.quote(keyword)}"
        f"&where={requests.utils.quote(location)}"
        f"&max_days_old=7"
        f"&content-type=application/json"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "exception" in data:
            print(f"  ERREUR Adzuna: {data['exception']}")
            return []
        results = data.get("results", [])
        print(f"  Adzuna '{keyword}' / '{location}' → {len(results)} brutes")
        jobs = []
        for job in results:
            title = job.get("title", "N/A")
            description = job.get("description", "")
            if any(excl in title.lower() for excl in exclusions):
                log_excluded(title, job.get("company", {}).get("display_name", "N/A"),
                             job.get("location", {}).get("display_name", location),
                             "Adzuna", "mot-clé exclu")
                continue
            jobs.append({
                "id": str(job.get("id", "")),
                "title": title,
                "company": job.get("company", {}).get("display_name", "N/A"),
                "location": job.get("location", {}).get("display_name", location),
                "url": job.get("redirect_url", ""),
                "description": description[:150] + "..." if description else "",
                "salary": format_salary_range(job.get("salary_min"), job.get("salary_max")),
                "date": job.get("created", ""),
                "source": "Adzuna",
            })
        return jobs
    except Exception as e:
        print(f"  EXCEPTION Adzuna: {e}")
        return []


def search_france_travail(keyword, location):
    exclusions = get_exclusions()
    try:
        token = get_ft_token()
        if not token:
            return []
        commune = FT_COMMUNES.get(location, "")
        url = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
        params = {
            "motsCles": keyword,
            "commune": commune,
            "distance": 30,
            "typeContrat": "CDI,CDD",
            "range": "0-9",
        }
        r = requests.get(url, params=params, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }, timeout=10)
        if r.status_code not in (200, 206):
            print(f"  FT '{keyword}' / '{location}' → HTTP {r.status_code}: {r.text[:300]}")
            return []
        data = r.json()
        results = data.get("resultats", [])
        print(f"  FT '{keyword}' / '{location}' → {len(results)} brutes")
        jobs = []
        for job in results:
            title = job.get("intitule", "N/A")
            description = job.get("description", "")
            if any(excl in title.lower() for excl in exclusions):
                log_excluded(title, job.get("entreprise", {}).get("nom", "N/A"),
                             job.get("lieuTravail", {}).get("libelle", location),
                             "France Travail", "mot-clé exclu")
                continue
            jobs.append({
                "id": job.get("id", ""),
                "title": title,
                "company": job.get("entreprise", {}).get("nom", "N/A"),
                "location": job.get("lieuTravail", {}).get("libelle", location),
                "url": job.get("origineOffre", {}).get("urlOrigine",
                    f"https://www.francetravail.fr/offres/recherche/detail/{job.get('id', '')}"),
                "description": description[:150] + "..." if description else "",
                "source": "France Travail",
            })
        return jobs
    except Exception as e:
        print(f"  EXCEPTION FT: {e}")
        return []


def search_hellowork(keyword, location):
    exclusions = get_exclusions()
    try:
        from bs4 import BeautifulSoup
        url = (
            f"https://www.hellowork.com/fr-fr/emploi/recherche.html"
            f"?k={requests.utils.quote(keyword)}"
            f"&l={requests.utils.quote(location)}"
            f"&c=CDI,CDD"
        )
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "fr-FR",
        }, timeout=10)
        print(f"  Hellowork status={r.status_code} len={len(r.text)}")
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.find_all(["article", "li"], attrs={"data-id": True})
        if not cards:
            cards = soup.find_all("div", class_=lambda c: c and "job" in str(c).lower())
        print(f"  Hellowork '{keyword}' / '{location}' → {len(cards)} cartes")
        jobs = []
        for card in cards[:10]:
            title_el = card.find(["h2", "h3", "a"])
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if any(excl in title.lower() for excl in exclusions):
                continue
            link_el = card.find("a", href=True)
            href = link_el["href"] if link_el else ""
            full_url = href if href.startswith("http") else "https://www.hellowork.com" + href
            company_el = card.find(["span", "p"], class_=lambda c: c and any(
                w in str(c).lower() for w in ["company", "entreprise"]))
            jobs.append({
                "id": full_url,
                "title": title,
                "company": company_el.get_text(strip=True) if company_el else "N/A",
                "location": location,
                "url": full_url,
                "description": "",
                "source": "Hellowork",
            })
        print(f"  Hellowork '{keyword}' / '{location}' → {len(jobs)} après filtre")
        return jobs
    except Exception as e:
        print(f"  EXCEPTION Hellowork: {e}")
        return []


def search_apec():
    """APEC : portail des offres cadres. Utile pour les postes de chargé de projet,
    coordinateur, responsable communication ou développement à Marseille/Aix."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    exclusions = get_exclusions()
    jobs = []
    seen_ids = set()
    url = "https://www.apec.fr/cms/webservices/rechercheOffre"
    for kw in KEYWORDS:
        body = {
            "motsCles": kw,
            "fonctions": [], "lieux": [], "typesContrat": [], "typesConvention": [],
            "niveauxExperience": [], "secteursActivite": [], "statutPoste": [],
            "typesTeletravail": [], "idsEtablissement": [], "sorts": [],
            "activeFiltre": False,
            "pagination": {"startIndex": 0, "range": 20},
        }
        try:
            r = requests.post(url, json=body, headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"  APEC '{kw}' → HTTP {r.status_code}")
                continue
            results = r.json().get("resultats", [])
            print(f"  APEC '{kw}' → {len(results)} offres")
            for job in results:
                job_id = str(job.get("numeroOffre") or job.get("id") or "")
                if not job_id or job_id in seen_ids:
                    continue
                # Garder uniquement les offres en PACA / Marseille / Aix
                lieu = job.get("lieuTexte", "").lower()
                if not matches_location(lieu):
                    continue
                seen_ids.add(job_id)
                title = job.get("intitule", "")
                if any(excl in title.lower() for excl in exclusions):
                    log_excluded(title, job.get("nomCommercial", "N/A"),
                                 job.get("lieuTexte", ""), "APEC", "mot-clé exclu")
                    continue
                description = job.get("texteOffre", "")
                jobs.append({
                    "id": job_id,
                    "title": clean_text(title),
                    "company": job.get("nomCommercial", "N/A"),
                    "location": job.get("lieuTexte", ""),
                    "url": f"https://www.apec.fr/candidat/recherche-emploi.html/detail-offre/{job_id}",
                    "description": (description[:200] + "...") if description else "",
                    "salary": job.get("salaireTexte", ""),
                    "date": job.get("datePublication", ""),
                    "source": "APEC",
                })
        except Exception as e:
            print(f"  EXCEPTION APEC '{kw}': {e}")
    print(f"  APEC total → {len(jobs)} offres")
    return jobs


def _sp_paca_location(job_url, card_text):
    m = re.search(r"reference-O0(\d{2})", job_url)
    if m:
        return SP_PACA_DEPTS.get(m.group(1))
    for term, label in SP_PACA_TEXT:
        if term in card_text:
            return label
    return None


def search_service_public():
    """Choisir le service public : offres de collectivités et établissements publics
    en PACA. Intéressant pour les postes de coordinateur, chargé de communication,
    accueil ou développement local en Région, Métropole, Mairie, etc."""
    exclusions = get_exclusions()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept-Language": "fr-FR",
    }
    jobs = []
    seen_urls = set()
    for kw in SP_KEYWORDS:
        try:
            from bs4 import BeautifulSoup
            url = f"https://choisirleservicepublic.gouv.fr/nos-offres/filtres/mot-cles/{kw}/"
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"  ServicePublic '{kw}' → HTTP {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            offer_links = soup.find_all("a", href=lambda h: h and "/offre-emploi/" in h)
            print(f"  ServicePublic '{kw}' → {len(offer_links)} offres")
            for a in offer_links:
                href = a.get("href", "")
                job_url = href if href.startswith("http") else "https://choisirleservicepublic.gouv.fr" + href
                if job_url in seen_urls:
                    continue
                title = a.get_text(strip=True)
                if not title or len(title) < 5:
                    continue
                cont = a.find_parent(["article", "li"]) or a.parent
                card_text = cont.get_text(" ", strip=True).lower() if cont else ""
                location = _sp_paca_location(job_url, card_text)
                if not location:
                    continue
                seen_urls.add(job_url)
                if any(excl in title.lower() for excl in exclusions):
                    log_excluded(title, "Fonction publique", location, "Service Public", "mot-clé exclu")
                    continue
                jobs.append({
                    "id": job_url,
                    "title": clean_text(title),
                    "company": "Fonction publique",
                    "location": location,
                    "url": job_url,
                    "description": "",
                    "source": "Service Public",
                })
        except Exception as e:
            print(f"  EXCEPTION ServicePublic '{kw}': {e}")
    print(f"  ServicePublic total → {len(jobs)} offres PACA après filtre")
    return jobs


MISTRAL_BATCH_SIZE = 25


def _filter_jobs_batch(jobs, reasons_text, verdicts=None):
    mistral_key = os.environ["MISTRAL_API_KEY"]
    jobs_text = "\n".join([
        f"{i}. TITRE: {job['title']} | ENTREPRISE: {job['company']} | LIEU: {job['location']}"
        + (f"\n   Description: {job.get('description', '')[:200]}" if job.get('description') else "")
        for i, job in enumerate(jobs)
    ])

    prompt = f"""Tu es un assistant de recherche d'emploi pour une candidate en reconversion. Voici son profil :
{PROFILE}

Offres récemment jugées non pertinentes et raisons (apprends-en) :
{reasons_text}

Offres du jour à évaluer :
{jobs_text}

RÈGLES DE DÉCISION :

GARDE (keep=true) si le poste est accessible sans expérience préalable dans le secteur,
valorise des compétences relationnelles, organisationnelles ou commerciales,
et est localisé à Marseille, Aix-en-Provence ou dans la zone Bouches-du-Rhône.
Exemples à garder : chargé(e) de projet, coordinateur/trice, assistant(e) manager,
chargé(e) de communication, responsable accueil/clientèle, office manager,
community manager, attaché(e) commercial(e), animateur/trice réseau,
chargé(e) de développement, chef(fe) de projet événementiel, responsable d'équipe,
poste en hôtellerie/tourisme managérial, coordination médico-sociale ou culturelle.

REJETTE (keep=false) dans ces cas :
- postes très techniques : ingénierie, IT, développement logiciel, médecine, paramédical clinique
- postes uniquement physiques ou de manutention sans dimension managériale
- stage, alternance, contrat d'apprentissage
- postes hors zone géographique (pas Marseille/Aix/BDR)
- postes demandant une expérience longue (5+ ans) dans un domaine très spécialisé
- similaire aux offres rejetées ci-dessus

CAS LIMITES (borderline=true) : pour les postes où tu hésites (secteur nouveau pour elle
mais compétences transférables visibles), garde-les (keep=true) mais marque borderline=true.
Les rejets absolus ci-dessus restent rejetés (keep=false).

Réponds UNIQUEMENT avec un JSON (sans texte avant/après, sans backticks).
Chaque objet : index, keep (bool), borderline (bool), reason.
[{{"index": 0, "keep": true, "borderline": false, "reason": "coordinatrice, compétences relationnelles, Marseille"}}]"""

    r = requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {mistral_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "mistral-small-latest",
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=30,
    )
    text = r.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"```json|```", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    decisions = json.loads(text)

    kept = []
    for decision in decisions:
        idx = decision.get("index")
        if idx is None or idx >= len(jobs):
            continue
        job = jobs[idx]
        keep = bool(decision.get("keep"))
        borderline = bool(decision.get("borderline"))
        reason = decision.get('reason', '')
        if verdicts is not None:
            verdicts[ai_key(job)] = {"keep": keep, "reason": reason, "borderline": borderline}
        if keep:
            job["borderline"] = borderline
            kept.append(job)
        else:
            print(f"  IA exclu: {job['title']} → {reason}")
            log_excluded(job['title'], job['company'], job.get('location', ''),
                         job.get('source', ''), f"IA: {reason}")
    return kept


def filter_jobs_with_ai(jobs):
    mistral_key = os.environ.get("MISTRAL_API_KEY", "")
    if not mistral_key:
        print("  Mistral: clé API absente, pas de filtrage IA")
        return jobs
    if not jobs:
        return jobs

    rejected_reasons = load_json(REJECTED_REASONS_FILE, [])
    reasons_text = "\n".join([
        f"- \"{r['title']}\" chez {r['company']} → Raison : {r['reason']}"
        for r in rejected_reasons[-30:]
    ]) if rejected_reasons else "Aucun rejet enregistré."

    verdicts = load_json(AI_VERDICTS_FILE, {})
    kept = []
    to_evaluate = []
    for job in jobs:
        cached = verdicts.get(ai_key(job))
        if cached is None:
            to_evaluate.append(job)
        elif cached.get("keep"):
            job["borderline"] = cached.get("borderline", False)
            kept.append(job)
        else:
            log_excluded(job['title'], job['company'], job.get('location', ''),
                         job.get('source', ''), f"IA (cache): {cached.get('reason', '')}")
    print(f"  Cache IA : {len(jobs) - len(to_evaluate)} offre(s) déjà jugée(s), "
          f"{len(to_evaluate)} à évaluer")

    for start in range(0, len(to_evaluate), MISTRAL_BATCH_SIZE):
        batch = to_evaluate[start:start + MISTRAL_BATCH_SIZE]
        try:
            kept += _filter_jobs_batch(batch, reasons_text, verdicts)
        except Exception as e:
            print(f"  EXCEPTION Mistral (lot {start}-{start+len(batch)}): {e}")
            print("  → lot écarté par sécurité")

    if len(verdicts) > AI_VERDICTS_MAX:
        verdicts = dict(list(verdicts.items())[-AI_VERDICTS_MAX:])
    save_json(AI_VERDICTS_FILE, verdicts)

    print(f"  Mistral: {len(kept)}/{len(jobs)} offres conservées "
          f"({len(to_evaluate)} réellement évaluées par l'IA)")
    return kept


def ai_key(job):
    return f"{job['title'].lower().strip()}|{job['company'].lower().strip()}"


def deduplicate(jobs):
    seen = set()
    unique = []
    for job in jobs:
        key = (job["title"].lower().strip(), job["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


def mark_seen(jobs, seen_ids):
    for job in jobs:
        key = f"{job['title'].lower()}|{job['company'].lower()}"
        job["is_new"] = key not in seen_ids
    return jobs


def categorize(jobs):
    marseille, aix_bdr = [], []
    for job in jobs:
        loc = job["location"].lower()
        if "marseille" in loc:
            marseille.append(job)
        else:
            aix_bdr.append(job)
    return marseille, aix_bdr


def section_html(title, emoji, jobs, color):
    if not jobs:
        return ""
    new_count = sum(1 for j in jobs if j.get("is_new"))
    source_colors = {
        "Adzuna": "#4a90a4",
        "France Travail": "#003189",
        "Hellowork": "#d95f02",
        "Service Public": "#000091",
        "APEC": "#e2001a",
    }
    html = f"""
    <div style="margin:2rem 0 1rem">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:1rem">
            <span style="font-size:20px">{emoji}</span>
            <h2 style="margin:0;font-size:17px;font-weight:500;color:{color}">{title}</h2>
            <span style="font-size:13px;color:#888;background:#f0f0f0;padding:2px 10px;border-radius:20px">{len(jobs)} offre(s)</span>
            {f'<span style="font-size:13px;color:#fff;background:#e05c2a;padding:2px 10px;border-radius:20px">🆕 {new_count} nouvelle(s)</span>' if new_count else ''}
        </div>
    """
    for job in jobs:
        is_new = job.get("is_new", True)
        source = job.get("source", "")
        sc = source_colors.get(source, "#888")
        badge_new = '<span style="font-size:11px;color:#fff;background:#e05c2a;padding:1px 8px;border-radius:10px;margin-left:8px">NOUVEAU</span>' if is_new else '<span style="font-size:11px;color:#888;background:#f0f0f0;padding:1px 8px;border-radius:10px;margin-left:8px">Déjà vu</span>'
        badge_source = f'<span style="font-size:11px;color:#fff;background:{sc};padding:1px 8px;border-radius:10px;margin-left:6px">{source}</span>'
        badge_borderline = ('<span style="font-size:11px;color:#fff;background:#e0a800;padding:1px 8px;border-radius:10px;margin-left:6px">⚠️ À VÉRIFIER</span>'
                            if job.get("borderline") else '')
        meta_bits = []
        if job.get("salary"):
            meta_bits.append(f"💰 {job['salary']}")
        date_str = format_job_date(job.get("date", ""))
        if date_str:
            meta_bits.append(f"🗓️ {date_str}")
        meta_line = (
            f'<p style="margin:0 0 5px 0;font-size:13px;color:#777">{" &nbsp;|&nbsp; ".join(meta_bits)}</p>'
            if meta_bits else ""
        )
        html += f"""
        <div style="margin-bottom:16px;padding:14px;border-left:4px solid {color};background:{'#fff8f5' if is_new else '#f9f9f9'};border-radius:4px">
            <h3 style="margin:0 0 6px 0">
                <a href="{job['url']}" style="color:{color};text-decoration:none">{job['title']}</a>
                {badge_new}{badge_source}{badge_borderline}
            </h3>
            <p style="margin:0 0 5px 0;color:#555;font-size:14px">
                🏢 <strong>{job['company']}</strong> &nbsp;|&nbsp; 📍 {job['location']}
            </p>
            {meta_line}
            <p style="margin:0;font-size:13px;color:#777">{job['description']}</p>
        </div>
        """
    html += "</div>"
    return html


def excluded_section_html(excluded_log):
    if not excluded_log:
        return ""
    rows = ""
    for item in excluded_log[:100]:
        rows += f"""
        <tr>
            <td style="padding:6px 10px;font-size:12px;color:#555;border-bottom:1px solid #eee">{item['title']}</td>
            <td style="padding:6px 10px;font-size:12px;color:#888;border-bottom:1px solid #eee">{item['company']}</td>
            <td style="padding:6px 10px;font-size:12px;color:#888;border-bottom:1px solid #eee">{item.get('location', '')}</td>
            <td style="padding:6px 10px;font-size:12px;color:#888;border-bottom:1px solid #eee">{item['source']}</td>
            <td style="padding:6px 10px;font-size:12px;color:#b56900;border-bottom:1px solid #eee">{item['reason']}</td>
        </tr>
        """
    return f"""
    <details style="margin-top:2.5rem;padding:14px;background:#fafafa;border-radius:8px;border:0.5px solid #e0e0e0">
        <summary style="cursor:pointer;font-size:14px;color:#555;font-weight:500">
            🗂️ Voir les {len(excluded_log)} offre(s) écartée(s) aujourd'hui
        </summary>
        <table style="width:100%;border-collapse:collapse;margin-top:10px">
            <tr style="text-align:left">
                <th style="padding:6px 10px;font-size:11px;color:#999;text-transform:uppercase">Titre</th>
                <th style="padding:6px 10px;font-size:11px;color:#999;text-transform:uppercase">Entreprise</th>
                <th style="padding:6px 10px;font-size:11px;color:#999;text-transform:uppercase">Lieu</th>
                <th style="padding:6px 10px;font-size:11px;color:#999;text-transform:uppercase">Source</th>
                <th style="padding:6px 10px;font-size:11px;color:#999;text-transform:uppercase">Raison</th>
            </tr>
            {rows}
        </table>
    </details>
    """


def build_email(jobs, feedback_url, excluded_log=None):
    today = datetime.now().strftime("%d/%m/%Y")
    marseille, aix_bdr = categorize(jobs)
    total = len(jobs)
    new_total = sum(1 for j in jobs if j.get("is_new"))

    if not total:
        return f"""
        <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px">
        <h2 style="color:#c0392b">🔍 Alerte emploi Marseille — {today}</h2>
        <p>Aucune nouvelle offre trouvée aujourd'hui.</p>
        </body></html>
        """

    body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px">
    <h2 style="color:#c0392b">🔍 Alerte emploi Marseille — {today}</h2>
    <p style="color:#555">{total} offre(s) dont <strong style="color:#e05c2a">{new_total} nouvelle(s)</strong> — Marseille ({len(marseille)}) · Aix / Bouches-du-Rhône ({len(aix_bdr)})</p>
    <a href="{feedback_url}" style="display:inline-block;margin:8px 0 16px;padding:10px 20px;background:#c0392b;color:#fff;border-radius:6px;text-decoration:none;font-size:14px">
        👎 Signaler des offres non pertinentes
    </a>
    <hr style="border:1px solid #e0e0e0">
    """

    body += section_html("Marseille", "🔵", marseille, "#1a5276")
    if marseille and aix_bdr:
        body += '<hr style="border:0.5px solid #e0e0e0;margin:1rem 0">'
    body += section_html("Aix-en-Provence / Bouches-du-Rhône", "🟢", aix_bdr, "#1e8449")
    body += excluded_section_html(excluded_log or [])
    body += "</body></html>"
    return body


def send_email(html_body, job_count):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_PASSWORD"]
    gmail_to = os.environ["GMAIL_TO"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔍 {job_count} offre(s) à Marseille — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"] = gmail_user
    msg["To"] = gmail_to
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, gmail_to, msg.as_string())
    print(f"Email envoyé avec {job_count} offres !")


if __name__ == "__main__":
    seen_ids = set(load_json(SEEN_FILE, []))
    print(f"{len(seen_ids)} offres déjà vues en mémoire")

    tasks = []
    for keyword in KEYWORDS:
        for location in LOCATIONS:
            tasks.append((search_adzuna, (keyword, location)))
            tasks.append((search_france_travail, (keyword, location)))
            tasks.append((search_hellowork, (keyword, location)))
    for fn in (search_apec, search_service_public):
        tasks.append((fn, ()))

    all_jobs = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fn, *args) for fn, args in tasks]
        for (fn, _), future in zip(tasks, futures):
            try:
                all_jobs += future.result()
            except Exception as e:
                print(f"  EXCEPTION {getattr(fn, '__name__', fn)}: {e}")

    all_jobs = deduplicate(all_jobs)
    print(f"\n{len(all_jobs)} offres uniques avant filtrage IA")
    all_jobs = filter_jobs_with_ai(all_jobs)

    jobs = mark_seen(all_jobs, seen_ids)

    new_seen = seen_ids | {f"{j['title'].lower()}|{j['company'].lower()}" for j in jobs}
    save_json(SEEN_FILE, list(new_seen))
    save_json(TODAY_FILE, jobs)

    repo = os.environ.get("GITHUB_REPOSITORY", "babybixxh/job-alert-marseille")
    feedback_url = f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/feedback.html"

    print(f"\nTotal : {len(jobs)} offres uniques")
    print(f"Total écarté : {len(EXCLUDED_LOG)} offres")

    sources_count = {}
    for j in jobs:
        sources_count[j.get("source", "?")] = sources_count.get(j.get("source", "?"), 0) + 1
    print(f"Répartition par source (offres conservées) : {sources_count}")

    html = build_email(jobs, feedback_url, EXCLUDED_LOG)
    send_email(html, len(jobs))
