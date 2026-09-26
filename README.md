# Veille automatique — edu-nc.gouv.cd

Ce petit projet surveille le site, détecte les nouveaux articles, vérifie
l'orthographe, les liens cassés, les images sans texte alternatif et les
métadonnées manquantes — puis t'envoie un rapport par email.

## Mise en place (10 minutes, gratuit)

### 1. Créer un dépôt GitHub
1. Va sur https://github.com/new
2. Crée un dépôt (ex: `veille-edu-nc`), en **privé** de préférence
3. Mets-y les 3 fichiers de ce dossier :
   - `scan_site.py`
   - `.github/workflows/veille.yml`
   - `README.md`

### 2. Configurer l'envoi d'email
Le script utilise Gmail par défaut (modifiable). Il te faut un
**mot de passe d'application** (pas ton mot de passe normal) :
1. Va sur https://myaccount.google.com/apppasswords
2. Active la validation en 2 étapes si ce n'est pas déjà fait
3. Génère un mot de passe d'application pour "Mail"
4. Garde-le précieusement, il te servira à l'étape suivante

Si tu utilises un autre fournisseur email (Outlook, Yahoo, etc.),
dis-le-moi et j'adapterai le `SMTP_HOST`/`SMTP_PORT`.

### 3. Ajouter les secrets sur GitHub
Dans ton dépôt : **Settings → Secrets and variables → Actions → New repository secret**

Ajoute ces 3 secrets :
| Nom | Valeur |
|---|---|
| `SMTP_USER` | ton adresse Gmail (ex: toncompte@gmail.com) |
| `SMTP_PASSWORD` | le mot de passe d'application généré à l'étape 2 |
| `ALERT_EMAIL_TO` | l'adresse où tu veux recevoir les alertes (peut être la même) |

### 4. Activer le workflow
- Le script tourne automatiquement toutes les 6 heures (modifiable dans
  `veille.yml`, ligne `cron`)
- Tu peux aussi le lancer manuellement : onglet **Actions** du dépôt →
  "Veille site edu-nc.gouv.cd" → **Run workflow**

## Comment ça fonctionne
- Le script lit le `sitemap.xml` (ou flux RSS) du site pour lister les
  articles — pas besoin d'accès admin
- Il compare avec `state.json` (créé/mis à jour automatiquement) pour
  savoir quels articles sont nouveaux
- Pour chaque nouvel article : vérification orthographe/grammaire
  (API LanguageTool, gratuite), liens cassés, images sans "alt",
  métadonnées manquantes
- Il compte aussi les articles publiés dans les 7 derniers jours
- Un email récapitulatif est envoyé à chaque exécution où au moins un
  nouvel article est trouvé

## Limites à connaître
- Si le site n'a pas de `sitemap.xml` ni de flux RSS accessible, le
  script ne trouvera aucun article — dis-le-moi si c'est le cas, on
  ajustera (scan de la page d'accueil, catégories, etc.)
- Le calcul "articles de la semaine" dépend de la présence d'une date
  (`lastmod`) dans le sitemap. Si le site n'en fournit pas, ce chiffre
  sera à 0 — on pourra alors se baser sur les dates de publication
  affichées sur chaque page à la place
- LanguageTool (l'API de correction) a des limites d'usage gratuites
  (~20 requêtes/minute) — largement suffisant pour un usage normal

## Besoin d'ajuster ?
Dis-moi et j'adapte le script :
- fréquence des scans
- ajout d'alertes Telegram/Slack en plus de l'email
- scan de pages spécifiques si pas de sitemap
- ajout d'autres vérifications (mots interdits, longueur du contenu, etc.)
