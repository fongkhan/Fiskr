"""
Recherche adverse media (roadmap P3-2).

Fournit a l'analyste une revue de presse negative sur un nom (client ou
liste) au moment de l'adjudication d'une alerte. Le fournisseur par defaut
est le flux RSS public de Google News (gratuit, sans partenariat de
donnees) : le nom est recherche conjointement avec des mots-cles LCB-FT
(blanchiment, sanctions, fraude, corruption...).

Strictement informatif : les resultats ne modifient jamais un score ni un
statut de criblage — la decision reste a l'analyste (les solutions a base
de donnees presse propriétaires type Dow Jones/Factiva restent superieures
en couverture ; ce connecteur est concu pour etre remplacable via la
configuration `adverse_media.provider`).
"""
import logging
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from fiskr.config import config

logger = logging.getLogger("fiskr.adverse_media")

DEFAULT_KEYWORDS = [
    "sanctions", "blanchiment", "money laundering", "fraude", "fraud",
    "corruption", "terrorisme", "terrorism", "gel des avoirs",
]


def get_adverse_media_config() -> Dict[str, Any]:
    cfg = config.get("adverse_media", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "provider": cfg.get("provider", "google_news_rss"),
        "language": cfg.get("language", "fr"),
        "max_results": int(cfg.get("max_results", 10)),
        "keywords": cfg.get("keywords") or DEFAULT_KEYWORDS,
    }


def build_google_news_query(name: str, keywords: List[str]) -> str:
    """Requete presse : le nom exact ET au moins un mot-cle LCB-FT."""
    joined = " OR ".join(f'"{k}"' if " " in k else k for k in keywords)
    return f'"{name}" ({joined})'


def _google_news_rss_url(query: str, language: str) -> str:
    lang = (language or "fr").lower()
    country = "FR" if lang == "fr" else "US"
    ceid = f"{country}:{lang}"
    return (
        f"https://news.google.com/rss/search?q={quote(query)}"
        f"&hl={lang}&gl={country}&ceid={quote(ceid)}"
    )


def parse_rss_items(rss_text: str, max_results: int) -> List[Dict[str, str]]:
    """Parse un flux RSS 2.0 en liste d'articles {title, link, published, source}."""
    root = ET.fromstring(rss_text)
    articles = []
    for item in root.iter("item"):
        source_elem = next((c for c in item if c.tag.rsplit("}", 1)[-1] == "source"), None)
        articles.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "source": (source_elem.text or "").strip() if source_elem is not None else "",
        })
        if len(articles) >= max_results:
            break
    return articles


def search_adverse_media(name: str,
                         fetcher: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """
    Recherche presse negative sur un nom. Retourne la requete construite et
    les articles trouves. `fetcher(url) -> str` est injectable (tests).
    Leve RuntimeError si le fournisseur est desactive ou inconnu.
    """
    cfg = get_adverse_media_config()
    if not cfg["enabled"]:
        raise RuntimeError("La recherche adverse media est désactivée (adverse_media.enabled).")
    if cfg["provider"] != "google_news_rss":
        raise RuntimeError(f"Fournisseur adverse media inconnu : {cfg['provider']}")

    name = (name or "").strip()
    if not name:
        raise ValueError("Un nom est requis pour la recherche adverse media.")

    query = build_google_news_query(name, cfg["keywords"])
    url = _google_news_rss_url(query, cfg["language"])

    if fetcher is None:
        from fiskr.sync import http_get_text
        fetcher = lambda u: http_get_text(u, timeout=30.0)

    rss_text = fetcher(url)
    articles = parse_rss_items(rss_text, cfg["max_results"])
    logger.info(f"Adverse media « {name} » : {len(articles)} article(s).")
    return {
        "name": name,
        "provider": cfg["provider"],
        "query": query,
        "articles": articles,
    }
