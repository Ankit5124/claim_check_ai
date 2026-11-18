from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Hashable, List, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Optional image-processing deps. The backend must keep working
# (rule-based, no image understanding) even if these aren't
# installed in the deployment environment, so import failures are
# swallowed and reported through IMAGE_ANALYSIS_AVAILABLE instead
# of crashing the app at import time.
try:
    from PIL import Image, ExifTags
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except Exception:
    PYTESSERACT_AVAILABLE = False

IMAGE_ANALYSIS_AVAILABLE = PIL_AVAILABLE
OCR_AVAILABLE = PIL_AVAILABLE and PYTESSERACT_AVAILABLE

# ============================================================
# Compliance / ClaimCheck AI backend
#
# Autonomous multimodal claim-verification agent:
#   1. Claim decomposition into atomic verification tasks
#   2. Credible-source search (Google News RSS + GDELT)
#   3. Evidence extraction + stance analysis per source
#   4. Corroboration / syndication-aware aggregation
#   5. Explainable verdict with confidence + reasoning
#   6. Multimodal input: text, image (OCR), or both
#
# The claim decomposition, stance classification and final
# reasoning steps use the Claude API when ANTHROPIC_API_KEY is
# set (LLM_ENABLED=True). If the key is missing, a call fails, or
# a response can't be parsed, every one of those steps falls back
# to the original deterministic/rule-based logic automatically so
# the app never breaks or hangs a demo on a flaky network call.
# ============================================================

APP_VERSION = "4.0.0"
SEARCH_PIPELINE_VERSION = "GOOGLE-RSS-FIRST-3.1.0"
MAX_SOURCES = 15
DEBUG_SEARCH_LIMIT = 100
REQUEST_TIMEOUT = 12
SEARCH_TIMEOUT = 6
GOOGLE_DECODE_TIMEOUT = 5
ARTICLE_TIMEOUT = 6
MAX_EVIDENCE_PER_SOURCE = 3

# ------------------------------------------------------------
# LLM (hybrid, optional)
# ------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
LLM_TIMEOUT = 15
LLM_ENABLED = bool(ANTHROPIC_API_KEY)

MAX_ATOMIC_CLAIMS = 4
MAX_LLM_EVIDENCE_SOURCES = 6

GOOGLE_RSS_BASE = "https://news.google.com/rss/search"
GOOGLE_BATCH_EXECUTE = (
    "https://news.google.com/_/DotsSplashUi/data/batchexecute"
)

GOOGLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

AGGREGATOR_DOMAINS = {
    "news.google.com",
    "google.com",
    "www.google.com",
    "gnews.io",
    "news.yahoo.com",
    "yahoo.com",
}

SOURCE_QUALITY = {
    # ---- Global wire services / highest tier ----
    "reuters.com": 0.98,
    "apnews.com": 0.98,
    "afp.com": 0.97,
    "bbc.com": 0.97,
    "bbc.co.uk": 0.97,

    # ---- Major global outlets ----
    "nytimes.com": 0.95,
    "washingtonpost.com": 0.95,
    "theguardian.com": 0.94,
    "wsj.com": 0.94,
    "ft.com": 0.94,
    "economist.com": 0.94,
    "npr.org": 0.93,
    "aljazeera.com": 0.92,
    "bloomberg.com": 0.93,
    "cnn.com": 0.90,
    "cbsnews.com": 0.90,
    "nbcnews.com": 0.90,
    "abcnews.go.com": 0.90,
    "usatoday.com": 0.88,
    "latimes.com": 0.90,
    "dw.com": 0.91,
    "france24.com": 0.90,
    "skynews.com": 0.90,
    "independent.co.uk": 0.87,
    "telegraph.co.uk": 0.87,
    "time.com": 0.88,
    "forbes.com": 0.82,

    # ---- Indian wire services / government ----
    "ptinews.com": 0.93,
    "aninews.in": 0.90,
    "pib.gov.in": 0.93,

    # ---- Major Indian outlets ----
    "thehindu.com": 0.94,
    "indianexpress.com": 0.93,
    "hindustantimes.com": 0.90,
    "ndtv.com": 0.90,
    "timesofindia.indiatimes.com": 0.87,
    "livemint.com": 0.88,
    "business-standard.com": 0.88,
    "financialexpress.com": 0.85,
    "moneycontrol.com": 0.84,
    "indiatoday.in": 0.85,
    "news18.com": 0.83,
    "deccanherald.com": 0.85,
    "telegraphindia.com": 0.82,
    "theprint.in": 0.84,
    "thewire.in": 0.82,
    "scroll.in": 0.83,
    "outlookindia.com": 0.80,
    "livelaw.in": 0.75,
    "newsonair.gov.in": 0.75,
    "inc.in": 0.65,
    "india.com": 0.65,
}

# Sources reported by many independent outlets get a small quality
# nudge on top of their static score (see apply_corroboration_quality_boost).
WIDE_CORROBORATION_QUALITY_BONUS = {
    4: 0.03,
    6: 0.06,
    8: 0.09,
}

class LookupCache:
    """Bounded, process-local cache for successful upstream lookups."""

    def __init__(self, max_entries: int, ttl_seconds: int):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: OrderedDict[Hashable, tuple[float, Dict[str, Any]]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: Hashable) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= time.monotonic():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: Hashable, value: Dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + self.ttl_seconds, value)
            self._entries.move_to_end(key)
            if len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)


NEWS_CACHE = LookupCache(max_entries=128, ttl_seconds=120)
GOOGLE_REDIRECT_CACHE = LookupCache(max_entries=512, ttl_seconds=1800)
ARTICLE_CACHE = LookupCache(max_entries=256, ttl_seconds=300)

SEARCH_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="news-search")
SOURCE_WORKERS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="source-enrichment")


app = FastAPI(
    title="ClaimCheck AI",
    version=APP_VERSION,
    description="Multimodal claim verification with optional Claude assistance.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Models
# ============================================================

class ClaimRequest(BaseModel):
    claim: str = Field(..., min_length=3, max_length=3000)
    max_sources: int = Field(
        default=MAX_SOURCES,
        ge=1,
        le=30,
    )


# ============================================================
# LLM client (hybrid: used when available, never required)
# ============================================================

def call_claude(
    system: str,
    user: str,
    max_tokens: int = 800,
) -> Optional[str]:
    """Call the Claude API. Returns None on any failure so callers
    can fall back to rule-based logic instead of raising."""

    if not LLM_ENABLED:
        return None

    try:
        response = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [
                    {"role": "user", "content": user},
                ],
            },
            timeout=LLM_TIMEOUT,
        )

        if response.status_code != 200:
            return None

        data = response.json()

        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )

        return text.strip() or None

    except Exception:
        return None


def call_claude_json(
    system: str,
    user: str,
    max_tokens: int = 800,
) -> Optional[Any]:
    """Same as call_claude but parses the reply as JSON. Tolerates
    stray markdown code fences around the JSON payload."""

    text = call_claude(system, user, max_tokens)

    if not text:
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except Exception:
        # Last resort: grab the first {...} or [...] block.
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, flags=re.S)

        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                return None

        return None


# ============================================================
# Generic helpers
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_text(value: Any) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def tokenize(value: Any) -> set[str]:
    words = normalize_text(value).split()
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "on",
        "for", "is", "are", "was", "were", "has", "have", "had",
        "with", "that", "this", "from", "by", "as", "at", "it",
        "its", "be", "been", "over", "after", "into", "about",
    }
    return {w for w in words if len(w) > 2 and w not in stop}


def similarity(a: str, b: str) -> float:
    ta = tokenize(a)
    tb = tokenize(b)

    if not ta or not tb:
        return 0.0

    return len(ta & tb) / len(ta | tb)


def story_fingerprint(title: str) -> str:
    normalized = normalize_text(title)
    words = sorted(tokenize(normalized))
    base = " ".join(words[:40])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def domain_from_url(url: str) -> str:
    if not url:
        return ""

    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def is_aggregator_domain(domain: str) -> bool:
    domain = (domain or "").lower().strip()
    return (
        domain in AGGREGATOR_DOMAINS
        or domain.endswith(".google.com")
        or domain.endswith(".googleusercontent.com")
    )


def source_quality(domain: str) -> float:
    domain = (domain or "").lower().strip()

    if is_aggregator_domain(domain):
        return 0.20

    if domain in SOURCE_QUALITY:
        return SOURCE_QUALITY[domain]

    # Recognize Indian/public-interest domains conservatively.
    if domain.endswith(".gov.in"):
        return 0.90

    if domain.endswith(".nic.in"):
        return 0.90

    return 0.50


def source_quality_label(score: float) -> str:
    if score >= 0.90:
        return "high"
    if score >= 0.75:
        return "medium"
    if score >= 0.60:
        return "lower"
    return "unknown"


def publisher_from_source(source: str) -> str:
    source = clean_text(source)

    if not source:
        return ""

    return source


# ============================================================
# Google News RSS
# ============================================================

def google_news_url(query: str) -> str:
    params = {
        "q": query,
        "hl": "en-IN",
        "gl": "IN",
        "ceid": "IN:en",
    }

    return GOOGLE_RSS_BASE + "?" + urlencode(params)


def extract_google_article_id(url: str) -> str:
    try:
        path = urlparse(url).path.rstrip("/")
        match = re.search(
            r"/(?:rss/)?articles/([^/]+)$",
            path,
            flags=re.I,
        )
        if match:
            return match.group(1)

        parts = [p for p in path.split("/") if p]
        if parts:
            return parts[-1]
    except Exception:
        pass

    return ""


def parse_rss_date(value: str) -> str:
    return clean_text(value)


def search_google_news(
    query: str,
    limit: int = DEBUG_SEARCH_LIMIT,
) -> Dict[str, Any]:

    started = time.perf_counter()
    url = google_news_url(query)

    response = requests.get(
        url,
        headers=GOOGLE_HEADERS,
        timeout=SEARCH_TIMEOUT,
    )

    response.raise_for_status()

    root = ET.fromstring(response.content)
    channel = root.find("channel")

    if channel is None:
        return {
            "results": [],
            "debug": {
                "url": url,
                "status_code": response.status_code,
                "items_found": 0,
                "elapsed_seconds": round(
                    time.perf_counter() - started,
                    2,
                ),
            },
        }

    results: List[Dict[str, Any]] = []

    for item in channel.findall("item"):
        title = clean_text(
            item.findtext("title", default="")
        )

        description_html = item.findtext(
            "description",
            default="",
        )

        description_soup = BeautifulSoup(
            description_html,
            "html.parser",
        )

        description = clean_text(
            description_soup.get_text(
                " ",
                strip=True,
            )
        )

        link = clean_text(
            item.findtext("link", default="")
        )

        source_el = item.find("source")
        publisher = ""
        publisher_url = ""

        if source_el is not None:
            publisher = clean_text(
                source_el.text or ""
            )
            publisher_url = clean_text(
                source_el.attrib.get("url", "")
            )

        published = parse_rss_date(
            item.findtext(
                "pubDate",
                default="",
            )
        )

        article_id = extract_google_article_id(link)

        result = {
            "title": title,
            "description": description,
            "url": link,
            "google_url": link,
            "publisher": publisher,
            "publisher_url": publisher_url,
            "publisher_domain": domain_from_url(
                publisher_url
            ),
            "published": published,
            "engine": "Google News RSS",
            "article_id": article_id,
            "source_quality_score": source_quality(
                domain_from_url(publisher_url)
            ),
            "source_quality": source_quality_label(
                source_quality(
                    domain_from_url(publisher_url)
                )
            ),
        }

        result["story_fingerprint"] = story_fingerprint(
            title
        )

        results.append(result)

        if len(results) >= limit:
            break

    return {
        "results": results,
        "debug": {
            "url": url,
            "status_code": response.status_code,
            "items_found": len(
                channel.findall("item")
            ),
            "elapsed_seconds": round(
                time.perf_counter() - started,
                2,
            ),
        },
    }


# ============================================================
# GDELT optional fallback
# ============================================================

def search_gdelt(query: str) -> Dict[str, Any]:
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        "?query="
        + quote(query)
        + "&mode=artlist"
        + "&maxrecords=50"
        + "&format=json"
        + "&sort=datedesc"
    )

    started = time.perf_counter()

    try:
        response = requests.get(
            url,
            headers=GOOGLE_HEADERS,
            timeout=SEARCH_TIMEOUT,
        )

        if response.status_code != 200:
            return {
                "results": [],
                "debug": {
                    "url": url,
                    "status_code": response.status_code,
                    "elapsed_seconds": round(
                        time.perf_counter() - started,
                        2,
                    ),
                },
            }

        data = response.json()
        articles = data.get("articles", [])

        results = []

        for article in articles:
            article_url = (
                article.get("url")
                or article.get("sourceurl")
                or ""
            )

            domain = domain_from_url(article_url)

            results.append({
                "title": clean_text(
                    article.get("title", "")
                ),
                "description": "",
                "url": article_url,
                "google_url": "",
                "publisher": clean_text(
                    article.get("domain", "")
                ),
                "publisher_url": (
                    "https://" + domain
                    if domain
                    else ""
                ),
                "publisher_domain": domain,
                "published": clean_text(
                    article.get("seendate", "")
                ),
                "engine": "GDELT",
                "article_id": "",
                "source_quality_score": source_quality(
                    domain
                ),
                "source_quality": source_quality_label(
                    source_quality(domain)
                ),
                "story_fingerprint": story_fingerprint(
                    article.get("title", "")
                ),
            })

        return {
            "results": results,
            "debug": {
                "url": url,
                "status_code": response.status_code,
                "items_found": len(results),
                "elapsed_seconds": round(
                    time.perf_counter() - started,
                    2,
                ),
            },
        }

    except Exception as exc:
        return {
            "results": [],
            "debug": {
                "url": url,
                "status_code": None,
                "items_found": 0,
                "elapsed_seconds": round(
                    time.perf_counter() - started,
                    2,
                ),
                "error": str(exc),
            },
        }


# ============================================================
# Search merging
# ============================================================

def deduplicate_search_results(
    results: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:

    output: List[Dict[str, Any]] = []
    seen_urls = set()
    seen_same_publisher_title = set()

    for item in results:
        url = item.get("url", "")
        publisher_domain = (
            item.get("publisher_domain")
            or domain_from_url(
                item.get("publisher_url", "")
            )
        )

        title = item.get("title", "")
        title_key = (
            publisher_domain,
            normalize_text(title),
        )

        if url and url in seen_urls:
            continue

        if title_key in seen_same_publisher_title:
            continue

        if url:
            seen_urls.add(url)

        seen_same_publisher_title.add(title_key)
        output.append(item)

        if len(output) >= limit:
            break

    return output


def search_news(
    query: str,
    max_sources: int = MAX_SOURCES,
) -> Dict[str, Any]:

    started = time.perf_counter()
    cache_key = (query, max_sources)
    cached = NEWS_CACHE.get(cache_key)
    if cached is not None:
        return {
            "results": cached["results"],
            "debug": {**cached["debug"], "cached": True, "elapsed_seconds": 0.0},
        }

    try:
        google = search_google_news(query, limit=DEBUG_SEARCH_LIMIT)
    except (requests.RequestException, ET.ParseError) as exc:
        response = getattr(exc, "response", None)
        google = {
            "results": [],
            "debug": {
                "url": google_news_url(query),
                "status_code": response.status_code if response is not None else None,
                "items_found": 0,
                "error": str(exc),
            },
        }

    google_results = google["results"]

    # Google RSS is primary. GDELT is only used if RSS fails.
    gdelt = {
        "results": [],
        "debug": {
            "items_found": 0,
            "status_code": None,
        },
    }

    if not google_results:
        gdelt = search_gdelt(query)

    merged = google_results + gdelt["results"]

    selected = deduplicate_search_results(
        merged,
        max_sources,
    )

    result = {
        "results": selected,
        "debug": {
            "engine": "Google News RSS",
            "google": google["debug"],
            "gdelt": gdelt["debug"],
            "google_results": len(
                google_results
            ),
            "gdelt_results": len(
                gdelt["results"]
            ),
            "selected_results": len(selected),
            "elapsed_seconds": round(
                time.perf_counter() - started,
                2,
            ),
        },
    }
    if selected:
        NEWS_CACHE.put(cache_key, result)
    return result


# ============================================================
# Google News URL decoder
# ============================================================

def is_google_news_article_url(url: str) -> bool:
    domain = domain_from_url(url)
    path = urlparse(url).path.lower()

    return (
        domain == "news.google.com"
        and "/articles/" in path
    )


def get_google_decoding_params(
    google_url: str,
) -> Dict[str, Any]:

    article_id = extract_google_article_id(
        google_url
    )

    if not article_id:
        raise ValueError(
            "Could not extract Google News article ID"
        )

    # Google serves the signed decoding parameters
    # from /articles/{id}.
    page_url = (
        "https://news.google.com/articles/"
        + article_id
    )

    response = requests.get(
        page_url,
        headers=GOOGLE_HEADERS,
        timeout=GOOGLE_DECODE_TIMEOUT,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    # Current/known form.
    candidates = []

    for node in soup.select(
        "[data-n-a-id][data-n-a-sg][data-n-a-ts]"
    ):
        candidates.append(node)

    # Common older form.
    for node in soup.select(
        "c-wiz > div[data-n-a-sg][data-n-a-ts]"
    ):
        candidates.append(node)

    chosen = None

    for node in candidates:
        node_id = clean_text(
            node.get("data-n-a-id", "")
        )

        if not node_id or node_id == article_id:
            chosen = node
            break

    if chosen is None and candidates:
        chosen = candidates[0]

    if chosen is None:
        # Regex fallback because Google's markup can change.
        signature_match = re.search(
            r'data-n-a-sg="([^"]+)"',
            response.text,
        )
        timestamp_match = re.search(
            r'data-n-a-ts="([^"]+)"',
            response.text,
        )
        id_match = re.search(
            r'data-n-a-id="([^"]+)"',
            response.text,
        )

        signature = (
            signature_match.group(1)
            if signature_match
            else ""
        )
        timestamp = (
            timestamp_match.group(1)
            if timestamp_match
            else ""
        )
        node_id = (
            id_match.group(1)
            if id_match
            else article_id
        )
    else:
        signature = clean_text(
            chosen.get("data-n-a-sg", "")
        )
        timestamp = clean_text(
            chosen.get("data-n-a-ts", "")
        )
        node_id = clean_text(
            chosen.get("data-n-a-id", "")
        ) or article_id

    if not signature or not timestamp:
        raise ValueError(
            "Google News decoding signature/timestamp "
            "not found"
        )

    return {
        "gn_art_id": node_id,
        "requested_article_id": article_id,
        "signature": signature,
        "timestamp": timestamp,
    }


def extract_url_from_batchexecute(
    response_text: str,
) -> str:

    text = response_text or ""

    # Most useful modern response marker.
    patterns = [
        r'\[\\"garturlres\\",\\"(https?://[^"\\]+)',
        r'\["garturlres","(https?://[^"]+)',
        r'"garturlres","(https?://[^"]+)',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.I,
        )

        if match:
            candidate = match.group(1)

            candidate = (
                candidate
                .replace("\\/", "/")
                .replace("\\u003d", "=")
                .replace("\\u0026", "&")
            )

            if (
                candidate.startswith("http://")
                or candidate.startswith("https://")
            ):
                return candidate

    # Known nested JSON response form.
    try:
        chunks = text.split("\n\n")

        if len(chunks) >= 2:
            payload_text = chunks[1]

            parsed = json.loads(payload_text)

            def walk(value: Any) -> Optional[str]:
                if isinstance(value, str):
                    if (
                        value.startswith("https://")
                        or value.startswith("http://")
                    ):
                        if not is_aggregator_domain(
                            domain_from_url(value)
                        ):
                            return value
                    return None

                if isinstance(value, list):
                    for child in value:
                        found = walk(child)
                        if found:
                            return found

                if isinstance(value, dict):
                    for child in value.values():
                        found = walk(child)
                        if found:
                            return found

                return None

            found = walk(parsed)

            if found:
                return found

    except Exception:
        pass

    return ""


def decode_google_news_article(
    google_url: str,
) -> Dict[str, Any]:

    if not is_google_news_article_url(
        google_url
    ):
        return {
            "success": False,
            "resolved_url": "",
            "error": "Not a Google News article URL",
        }

    params = get_google_decoding_params(
        google_url
    )

    article_id = params["gn_art_id"]
    timestamp = params["timestamp"]
    signature = params["signature"]

    inner_request = (
        '["garturlreq",'
        '[["X","X",["X","X"],null,null,1,1,"US:en",'
        'null,1,null,null,null,null,null,0,1],'
        '"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{article_id}",'
        f'{timestamp},'
        f'"{signature}"]'
    )

    rpc_payload = [
        "Fbv4je",
        inner_request,
    ]

    body = "f.req=" + quote(
        json.dumps(
            [[rpc_payload]],
            separators=(",", ":"),
        )
    )

    headers = {
        **GOOGLE_HEADERS,
        "Content-Type": (
            "application/x-www-form-urlencoded;"
            "charset=UTF-8"
        ),
        "Referer": "https://news.google.com/",
        "Origin": "https://news.google.com",
    }

    response = requests.post(
        GOOGLE_BATCH_EXECUTE,
        params={"rpcids": "Fbv4je"},
        headers=headers,
        data=body,
        timeout=GOOGLE_DECODE_TIMEOUT,
    )

    response.raise_for_status()

    resolved = extract_url_from_batchexecute(
        response.text
    )

    if not resolved:
        raise ValueError(
            "Google batchexecute returned no publisher URL"
        )

    resolved_domain = domain_from_url(
        resolved
    )

    if is_aggregator_domain(resolved_domain):
        raise ValueError(
            "Google returned another aggregator URL"
        )

    return {
        "success": True,
        "resolved_url": resolved,
        "resolved_domain": resolved_domain,
        "status_code": response.status_code,
        "article_id": params[
            "requested_article_id"
        ],
    }


def resolve_google_redirect(
    url: str,
) -> Dict[str, Any]:

    if not url:
        return {
            "resolved_url": "",
            "resolved_domain": "",
            "redirected": False,
            "status_code": None,
            "error": "Empty URL",
            "method": "empty",
        }

    # IMPORTANT:
    # The previous code called canonical_url(), which did not exist.
    # This version does not call that function at all.

    if not is_google_news_article_url(url):
        domain = domain_from_url(url)

        return {
            "resolved_url": url,
            "resolved_domain": domain,
            "redirected": False,
            "status_code": None,
            "error": None,
            "method": "direct",
        }

    cached = GOOGLE_REDIRECT_CACHE.get(url)
    if cached is not None:
        return {
            **cached,
            "method": "cache",
        }

    try:
        decoded = decode_google_news_article(
            url
        )

        result = {
            "resolved_url": decoded[
                "resolved_url"
            ],
            "resolved_domain": decoded[
                "resolved_domain"
            ],
            "redirected": True,
            "status_code": decoded[
                "status_code"
            ],
            "error": None,
            "method": "batchexecute",
        }

        GOOGLE_REDIRECT_CACHE.put(url, result)
        return result

    except Exception as exc:
        result = {
            "resolved_url": "",
            "resolved_domain": "",
            "redirected": False,
            "status_code": getattr(
                exc,
                "response",
                None,
            ).status_code
            if getattr(
                exc,
                "response",
                None,
            )
            is not None
            else None,
            "error": str(exc),
            "method": "batchexecute",
        }

        return result


# ============================================================
# Article extraction
# ============================================================

def extract_article_text(
    url: str,
) -> Dict[str, Any]:

    if not url:
        return {
            "article_available": False,
            "article_text": "",
            "status_code": None,
            "error": "Empty article URL",
        }

    cached = ARTICLE_CACHE.get(url)
    if cached is not None:
        return cached

    try:
        response = requests.get(
            url,
            headers=GOOGLE_HEADERS,
            timeout=ARTICLE_TIMEOUT,
            allow_redirects=True,
        )

        final_url = response.url
        final_domain = domain_from_url(
            final_url
        )

        if is_aggregator_domain(final_domain):
            return {
                "article_available": False,
                "article_text": "",
                "status_code": response.status_code,
                "error": (
                    "Publisher request ended on "
                    "an aggregator page"
                ),
                "final_url": final_url,
            }

        if response.status_code != 200:
            return {
                "article_available": False,
                "article_text": "",
                "status_code": response.status_code,
                "error": (
                    f"HTTP {response.status_code}"
                ),
                "final_url": final_url,
            }

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        for tag in soup(
            [
                "script",
                "style",
                "noscript",
                "svg",
                "nav",
                "header",
                "footer",
                "aside",
                "form",
                "iframe",
            ]
        ):
            tag.decompose()

        article = (
            soup.find("article")
            or soup.find(
                "main"
            )
            or soup.body
        )

        if article is None:
            return {
                "article_available": False,
                "article_text": "",
                "status_code": response.status_code,
                "error": "No readable article body found",
                "final_url": final_url,
            }

        text = clean_text(
            article.get_text(
                " ",
                strip=True,
            )
        )

        if len(text) < 250:
            # Fallback to paragraphs.
            paragraphs = [
                clean_text(
                    p.get_text(
                        " ",
                        strip=True,
                    )
                )
                for p in soup.find_all("p")
            ]

            paragraphs = [
                p for p in paragraphs
                if len(p) >= 30
            ]

            text = clean_text(
                " ".join(paragraphs)
            )

        if len(text) < 120:
            return {
                "article_available": False,
                "article_text": text,
                "status_code": response.status_code,
                "error": (
                    "Article text too short"
                ),
                "final_url": final_url,
            }

        result = {
            "article_available": True,
            "article_text": text[:30000],
            "status_code": response.status_code,
            "error": None,
            "final_url": final_url,
        }
        ARTICLE_CACHE.put(url, result)
        return result

    except Exception as exc:
        return {
            "article_available": False,
            "article_text": "",
            "status_code": None,
            "error": str(exc),
        }


# ============================================================
# Image analysis (OCR + basic metadata)
# ============================================================
#
# This is what actually makes the backend "multimodal": images are
# no longer accepted-and-ignored. Text is extracted via OCR and fed
# into the exact same claim-verification pipeline used for typed
# claims. EXIF metadata is checked for coarse, non-conclusive
# signals (editing software, missing capture data) that are useful
# for a human reviewer weighing "is this taken out of context" —
# they are surfaced as hints, never used to compute the verdict.

EDITING_SOFTWARE_HINTS = (
    "photoshop", "gimp", "lightroom", "canva", "picsart",
    "snapseed", "facetune",
)


def extract_exif_summary(image_bytes: bytes) -> Dict[str, Any]:
    if not PIL_AVAILABLE:
        return {
            "available": False,
            "reason": "Pillow is not installed on the server.",
        }

    try:
        image = Image.open(io.BytesIO(image_bytes))
        width, height = image.size

        raw_exif = {}
        try:
            exif_data = image.getexif()
            if exif_data:
                for tag_id, value in exif_data.items():
                    tag = ExifTags.TAGS.get(tag_id, str(tag_id))
                    raw_exif[tag] = str(value)
        except Exception:
            raw_exif = {}

        software = clean_text(
            raw_exif.get("Software", "")
        )

        possible_editing = any(
            hint in software.lower()
            for hint in EDITING_SOFTWARE_HINTS
        )

        return {
            "available": True,
            "format": image.format,
            "width": width,
            "height": height,
            "has_exif_metadata": bool(raw_exif),
            "software_tag": software or None,
            "possible_editing_software_detected": (
                possible_editing
            ),
            "capture_datetime": raw_exif.get(
                "DateTimeOriginal"
            ) or raw_exif.get("DateTime"),
            "note": (
                "Metadata is easily stripped or forged and is "
                "informational only, not proof of manipulation "
                "or authenticity."
            ),
        }

    except Exception as exc:
        return {
            "available": False,
            "reason": str(exc),
        }


def ocr_image(image_bytes: bytes) -> Dict[str, Any]:
    if not PIL_AVAILABLE:
        return {
            "available": False,
            "text": "",
            "reason": "Pillow is not installed on the server.",
        }

    if not PYTESSERACT_AVAILABLE:
        return {
            "available": False,
            "text": "",
            "reason": (
                "pytesseract / the system tesseract-ocr binary is "
                "not installed on the server."
            ),
        }

    try:
        image = Image.open(io.BytesIO(image_bytes))
        raw_text = pytesseract.image_to_string(image)
        text = clean_text(raw_text)

        return {
            "available": True,
            "text": text,
            "char_count": len(text),
        }

    except Exception as exc:
        return {
            "available": False,
            "text": "",
            "reason": str(exc),
        }


# ============================================================
# Claim / evidence logic
# ============================================================

def split_sentences(text: str) -> List[str]:
    text = clean_text(text)

    if not text:
        return []

    parts = re.split(
        r"(?<=[.!?])\s+",
        text,
    )

    return [
        clean_text(p)
        for p in parts
        if len(clean_text(p)) >= 35
    ]


def extract_claims(claim: str) -> List[str]:
    claim = clean_text(claim)

    if not claim:
        return []

    parts = re.split(
        r"\s+(?:and|but|while|whereas|because|although)\s+",
        claim,
        flags=re.I,
    )

    claims = [
        clean_text(part)
        for part in parts
        if len(clean_text(part)) >= 20
    ]

    return claims or [claim]


# ============================================================
# Claim decomposition into verification tasks (hybrid)
# ============================================================
#
# This turns the raw input into 1-4 atomic, independently
# checkable sub-claims, each with its own search query. It's the
# literal "decomposes them into verification tasks" step from the
# problem statement. When the LLM is unavailable, extract_claims()
# above provides a deterministic fallback with the same shape.

def rule_based_decompose(claim: str) -> List[Dict[str, str]]:
    atoms = extract_claims(claim)[:MAX_ATOMIC_CLAIMS]

    return [
        {
            "claim": atom,
            "type": "general",
            "search_query": atom,
        }
        for atom in atoms
    ]


def llm_decompose_claim(
    claim: str,
    ocr_text: str = "",
) -> Optional[List[Dict[str, str]]]:

    if not LLM_ENABLED:
        return None

    context_block = (
        f'\nText extracted from an attached image (OCR): "{ocr_text}"'
        if ocr_text
        else ""
    )

    system = (
        "You are the claim-decomposition module of a fact-checking "
        "agent. Break the input into 1-4 atomic, independently "
        "verifiable factual claims (skip pure opinion). For each "
        "atomic claim, classify its type and write a short, "
        "keyword-style news-search query that would find reporting "
        "on it. Respond with ONLY a JSON array, no prose, in this "
        "exact shape: "
        '[{"claim": "...", "type": "event|statistic|quote|'
        'attribution|policy|other", "search_query": "..."}]'
    )

    user = f'Claim: "{claim}"{context_block}'

    parsed = call_claude_json(system, user, max_tokens=600)

    if not isinstance(parsed, list) or not parsed:
        return None

    atoms = []

    for entry in parsed[:MAX_ATOMIC_CLAIMS]:
        if not isinstance(entry, dict):
            continue

        atom_claim = clean_text(entry.get("claim", ""))
        search_query = clean_text(
            entry.get("search_query", "")
        ) or atom_claim

        if len(atom_claim) < 5:
            continue

        atoms.append({
            "claim": atom_claim,
            "type": clean_text(
                entry.get("type", "other")
            ) or "other",
            "search_query": search_query,
        })

    return atoms or None


def decompose_claim(
    claim: str,
    ocr_text: str = "",
) -> Dict[str, Any]:

    llm_atoms = llm_decompose_claim(claim, ocr_text)

    if llm_atoms:
        return {
            "atomic_claims": llm_atoms,
            "method": "llm",
        }

    return {
        "atomic_claims": rule_based_decompose(claim),
        "method": "rule_based",
    }


def keyword_overlap(
    claim: str,
    sentence: str,
) -> float:
    a = tokenize(claim)
    b = tokenize(sentence)

    if not a or not b:
        return 0.0

    return len(a & b) / max(
        len(a),
        1,
    )


SUPPORT_WORDS = {
    "confirmed",
    "confirms",
    "reported",
    "reports",
    "found",
    "finds",
    "said",
    "says",
    "ruled",
    "orders",
    "ordered",
    "challenged",
    "challenge",
    "case",
    "petition",
    "plea",
    "court",
    "legal",
    "lawsuit",
    "resign",
    "resignation",
    "demand",
    "demands",
    "election commission",
}

CONTRADICTION_PATTERNS = [
    r"\bdenied\b",
    r"\bdenies\b",
    r"\bnot true\b",
    r"\bfalse\b",
    r"\bincorrect\b",
    r"\bno evidence\b",
    r"\bnot the case\b",
    r"\brefuted\b",
    r"\bdebunked\b",
    r"\bcontradicts\b",
]


def classify_sentence(
    claim: str,
    sentence: str,
) -> str:

    overlap = keyword_overlap(
        claim,
        sentence,
    )

    if overlap < 0.16:
        return "context"

    lower = sentence.lower()

    for pattern in CONTRADICTION_PATTERNS:
        if re.search(pattern, lower):
            return "contradiction"

    support_hits = sum(
        1
        for word in SUPPORT_WORDS
        if word in lower
    )

    if overlap >= 0.28 and support_hits > 0:
        return "support"

    if overlap >= 0.40:
        return "support"

    return "context"


def evidence_from_article(
    claim: str,
    article_text: str,
) -> Dict[str, List[str]]:

    supporting = []
    contradicting = []
    context = []

    for sentence in split_sentences(
        article_text
    ):
        kind = classify_sentence(
            claim,
            sentence,
        )

        if kind == "support":
            supporting.append(sentence)

        elif kind == "contradiction":
            contradicting.append(sentence)

        elif kind == "context":
            if keyword_overlap(
                claim,
                sentence,
            ) >= 0.20:
                context.append(sentence)

    return {
        "supporting": supporting[
            :MAX_EVIDENCE_PER_SOURCE
        ],
        "contradicting": contradicting[
            :MAX_EVIDENCE_PER_SOURCE
        ],
        "context": context[
            :MAX_EVIDENCE_PER_SOURCE
        ],
        "method": "rule_based",
    }


def llm_evidence_from_article(
    claim: str,
    article_text: str,
    title: str,
) -> Optional[Dict[str, Any]]:
    """Ask Claude to actually read the article and judge its stance
    toward the claim, with a quoted sentence and a one-line reason.
    This is the semantic replacement for keyword-overlap scoring;
    used only on a capped number of top sources for cost/latency."""

    if not LLM_ENABLED or not article_text:
        return None

    system = (
        "You are the evidence-analysis module of a fact-checking "
        "agent. Given a claim and an excerpt of a news article, "
        "decide whether the article SUPPORTS the claim, "
        "CONTRADICTS it, gives relevant CONTEXT without directly "
        "confirming or denying it, or is IRRELEVANT. Quote the one "
        "sentence from the article that best justifies your call "
        "(verbatim, do not invent text) and give a one-sentence "
        "reason. Respond with ONLY JSON in this exact shape: "
        '{"stance": "support|contradict|context|irrelevant", '
        '"key_sentence": "...", "reasoning": "..."}'
    )

    user = (
        f'Claim: "{claim}"\n'
        f'Article title: "{title}"\n'
        f'Article excerpt: "{article_text[:4000]}"'
    )

    parsed = call_claude_json(system, user, max_tokens=350)

    if not isinstance(parsed, dict):
        return None

    stance = clean_text(
        parsed.get("stance", "")
    ).lower()

    if stance not in {
        "support", "contradict", "context", "irrelevant",
    }:
        return None

    key_sentence = clean_text(
        parsed.get("key_sentence", "")
    )
    reasoning = clean_text(
        parsed.get("reasoning", "")
    )

    supporting = [key_sentence] if (
        stance == "support" and key_sentence
    ) else []
    contradicting = [key_sentence] if (
        stance == "contradict" and key_sentence
    ) else []
    context = [key_sentence] if (
        stance == "context" and key_sentence
    ) else []

    return {
        "supporting": supporting,
        "contradicting": contradicting,
        "context": context,
        "stance_reasoning": reasoning,
        "method": "llm",
    }


def get_evidence_for_article(
    claim: str,
    article_text: str,
    title: str,
    use_llm: bool,
) -> Dict[str, Any]:

    if use_llm:
        llm_result = llm_evidence_from_article(
            claim, article_text, title,
        )

        if llm_result is not None:
            return llm_result

    return evidence_from_article(claim, article_text)


# ============================================================
# Source enrichment
# ============================================================

def enrich_source(
    source: Dict[str, Any],
    claim: str,
    use_llm: bool = False,
) -> Dict[str, Any]:

    item = dict(source)

    google_url = (
        item.get("google_url")
        or item.get("url")
        or ""
    )

    publisher_domain = (
        item.get("publisher_domain")
        or domain_from_url(
            item.get("publisher_url", "")
        )
    )

    item["publisher_domain"] = (
        publisher_domain
    )
    item["source_quality_score"] = source_quality(
        publisher_domain
    )
    item["source_quality"] = (
        source_quality_label(
            item["source_quality_score"]
        )
    )

    resolution = resolve_google_redirect(
        google_url
    )

    item["resolved_url"] = resolution.get(
        "resolved_url",
        "",
    )
    item["resolved_domain"] = resolution.get(
        "resolved_domain",
        "",
    )
    item["resolution_method"] = resolution.get(
        "method",
        "",
    )
    item["resolution_error"] = resolution.get(
        "error",
        None,
    )

    # If decoder resolved the real publisher URL,
    # prefer that domain.
    if item["resolved_domain"]:
        item["publisher_domain"] = (
            item["resolved_domain"]
        )

    if item["publisher_domain"]:
        item["source_quality_score"] = (
            source_quality(
                item["publisher_domain"]
            )
        )
        item["source_quality"] = (
            source_quality_label(
                item["source_quality_score"]
            )
        )

    article_url = (
        item["resolved_url"]
        or item.get("url")
        or ""
    )

    extraction = extract_article_text(
        article_url
    )

    item["article_available"] = extraction.get(
        "article_available",
        False,
    )
    item["article_status_code"] = (
        extraction.get("status_code")
    )
    item["article_error"] = extraction.get(
        "error"
    )

    if extraction.get("final_url"):
        item["final_url"] = extraction[
            "final_url"
        ]

    article_text = extraction.get(
        "article_text",
        "",
    )

    item["article_text"] = article_text

    # Full-article scraping fails often (Google News redirect
    # decoding is fragile, many publishers block scrapers/paywall).
    # Rather than surface zero evidence in that case, fall back to
    # the RSS snippet text the search engine already gave us. It's
    # shorter and less reliable, so it's tagged as "snippet" and
    # weighted down in compute_confidence() rather than treated the
    # same as a fully-read article.
    evidence_source = "none"
    text_for_evidence = article_text

    if not text_for_evidence:
        snippet = clean_text(item.get("description", ""))

        if len(snippet) >= 35:
            text_for_evidence = snippet
            evidence_source = "snippet"
    else:
        evidence_source = "full_article"

    evidence = get_evidence_for_article(
        claim,
        text_for_evidence,
        item.get("title", ""),
        use_llm,
    )

    item["evidence"] = evidence
    item["evidence_method"] = evidence.get(
        "method", "rule_based"
    )
    item["evidence_source"] = evidence_source
    item["stance_reasoning"] = evidence.get(
        "stance_reasoning", ""
    )

    item["supports_claim"] = bool(
        evidence["supporting"]
    )
    item["contradicts_claim"] = bool(
        evidence["contradicting"]
    )

    item["supporting_evidence"] = (
        evidence["supporting"]
    )
    item["contradicting_evidence"] = (
        evidence["contradicting"]
    )
    item["context_evidence"] = (
        evidence["context"]
    )

    item["is_independent"] = (
        bool(item["publisher_domain"])
        and not is_aggregator_domain(
            item["publisher_domain"]
        )
    )

    return item


def enrich_source_safely(
    source: Dict[str, Any],
    claim: str,
    use_llm: bool,
) -> Dict[str, Any]:
    try:
        return enrich_source(source, claim, use_llm)
    except Exception as exc:
        return {
            **source,
            "resolved_url": "",
            "resolved_domain": "",
            "article_available": False,
            "article_status_code": None,
            "article_error": str(exc),
            "supports_claim": False,
            "contradicts_claim": False,
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "context_evidence": [],
            "is_independent": True,
        }


# ============================================================
# Syndication / copied story detection
# ============================================================

def apply_syndication_clusters(
    sources: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:

    clusters: List[List[int]] = []

    for index, source in enumerate(sources):
        placed = False

        title = source.get(
            "title",
            "",
        )
        text = source.get(
            "article_text",
            "",
        )

        for cluster in clusters:
            representative = sources[
                cluster[0]
            ]

            rep_title = representative.get(
                "title",
                "",
            )
            rep_text = representative.get(
                "article_text",
                "",
            )

            title_sim = similarity(
                title,
                rep_title,
            )

            body_sim = similarity(
                text[:6000],
                rep_text[:6000],
            )

            # High title similarity OR high body similarity
            # indicates likely duplication.
            if (
                title_sim >= 0.82
                or body_sim >= 0.72
            ):
                cluster.append(index)
                placed = True
                break

        if not placed:
            clusters.append([index])

    cluster_map = {}

    for cluster_id, cluster in enumerate(
        clusters,
        start=1,
    ):
        for index in cluster:
            cluster_map[index] = cluster_id

    for index, source in enumerate(sources):
        source["story_cluster_id"] = (
            cluster_map.get(index)
        )

        cluster_id = source[
            "story_cluster_id"
        ]

        cluster_members = [
            s
            for i, s in enumerate(sources)
            if cluster_map.get(i)
            == cluster_id
        ]

        domains = {
            s.get("publisher_domain")
            for s in cluster_members
            if s.get("publisher_domain")
        }

        source["cluster_size"] = len(
            cluster_members
        )
        source["cluster_domains"] = sorted(
            domains
        )

        # A source is independent from the cluster
        # when it is not merely a near-copy of another
        # publisher's article.
        source["is_independent"] = (
            len(domains) == 1
            or cluster_members[0] is source
            or source.get(
                "article_available",
                False,
            )
        )

    return sources


# ============================================================
# Corroboration
# ============================================================

def get_publisher_domain(
    source: Dict[str, Any],
) -> str:

    return (
        source.get("publisher_domain")
        or source.get("resolved_domain")
        or domain_from_url(
            source.get("resolved_url", "")
        )
        or domain_from_url(
            source.get("url", "")
        )
    )


def independent_support_domains(
    sources: List[Dict[str, Any]],
) -> set[str]:

    return {
        get_publisher_domain(source)
        for source in sources
        if source.get("supports_claim")
        and source.get("is_independent", True)
        and get_publisher_domain(source)
        and not is_aggregator_domain(
            get_publisher_domain(source)
        )
    }


def independent_contradiction_domains(
    sources: List[Dict[str, Any]],
) -> set[str]:

    return {
        get_publisher_domain(source)
        for source in sources
        if source.get("contradicts_claim")
        and source.get("is_independent", True)
        and get_publisher_domain(source)
        and not is_aggregator_domain(
            get_publisher_domain(source)
        )
    }


def build_corroboration(
    sources: List[Dict[str, Any]],
) -> Dict[str, Any]:

    support_domains = (
        independent_support_domains(
            sources
        )
    )

    contradiction_domains = (
        independent_contradiction_domains(
            sources
        )
    )

    independent_domains = {
        get_publisher_domain(s)
        for s in sources
        if s.get("is_independent", True)
        and get_publisher_domain(s)
        and not is_aggregator_domain(
            get_publisher_domain(s)
        )
    }

    return {
        "independent_sources": len(
            independent_domains
        ),
        "supporting_independent_sources": len(
            support_domains
        ),
        "contradicting_independent_sources": len(
            contradiction_domains
        ),
        "supporting_domains": sorted(
            support_domains
        ),
        "contradicting_domains": sorted(
            contradiction_domains
        ),
        "unique_publisher_domains": sorted(
            independent_domains
        ),
    }


# ============================================================
# Confidence
# ============================================================

def graduated_bonus(count: int, tiers: Dict[int, float]) -> float:
    """Return the bonus for the highest tier threshold `count`
    reaches. tiers like {4: 0.06, 6: 0.12, 8: 0.18}."""

    bonus = 0.0
    for threshold in sorted(tiers):
        if count >= threshold:
            bonus = tiers[threshold]
    return bonus


# Corroborating stance (support or contradiction) across
# independent outlets is the strongest confidence signal.
STANCE_CORROBORATION_BONUS_TIERS = {
    4: 0.06,
    5: 0.10,
    6: 0.14,
    7: 0.17,
    8: 0.20,
}

# Even without classified stance, a story simply appearing across
# many independent outlets (6-7+) is itself a credibility signal —
# widely-syndicated real stories are much more common than
# widely-syndicated hoaxes reported as straight news by many
# separate, unrelated publishers.
REPORTING_BREADTH_BONUS_TIERS = {
    4: 0.02,
    6: 0.05,
    8: 0.08,
}


def compute_confidence(
    sources: List[Dict[str, Any]],
    corroboration: Dict[str, Any],
) -> float:

    if not sources:
        return 0.05

    # Sources where we actually have some text to judge from —
    # either a fully scraped article or, when scraping/redirect
    # resolution failed, the search engine's own snippet. Full
    # articles count fully toward coverage; snippets count at half
    # weight since they're much shorter and less reliable.
    full_articles = [
        s for s in sources
        if s.get("evidence_source") == "full_article"
    ]
    snippets = [
        s for s in sources
        if s.get("evidence_source") == "snippet"
    ]
    available = full_articles + snippets

    support = corroboration[
        "supporting_independent_sources"
    ]

    contradiction = corroboration[
        "contradicting_independent_sources"
    ]

    independent = corroboration[
        "independent_sources"
    ]

    effective_coverage = (
        len(full_articles) + 0.5 * len(snippets)
    ) / max(len(sources), 1)

    coverage_score = min(
        effective_coverage,
        1.0,
    ) * 0.25

    source_score = min(
        independent / 5.0,
        1.0,
    ) * 0.20

    corroboration_score = min(
        max(
            support,
            contradiction,
        ) / 5.0,
        1.0,
    ) * 0.35

    quality_values = [
        s.get(
            "source_quality_score",
            0.5,
        )
        for s in available
    ]

    quality = (
        sum(quality_values)
        / len(quality_values)
        if quality_values
        else 0.5
    )

    quality_score = quality * 0.20

    confidence = (
        coverage_score
        + source_score
        + corroboration_score
        + quality_score
    )

    # If we couldn't read anything at all (no full article, no
    # usable snippet) for any source, keep confidence low regardless
    # of how the math above worked out.
    if not available:
        confidence = min(
            confidence,
            0.20,
        )

    # Graduated bonuses: the more independent outlets agree (on
    # stance, or simply on reporting the same story at all), the
    # higher the confidence — scaling smoothly rather than a single
    # flat jump at an arbitrary threshold.
    confidence += graduated_bonus(
        max(support, contradiction),
        STANCE_CORROBORATION_BONUS_TIERS,
    )
    confidence += graduated_bonus(
        independent,
        REPORTING_BREADTH_BONUS_TIERS,
    )

    return round(
        min(
            max(confidence, 0.0),
            0.99,
        ),
        3,
    )


# ============================================================
# Verdict
# ============================================================

def determine_verdict(
    supporting: List[Dict[str, Any]],
    contradicting: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    confidence: float,
) -> str:

    if not sources:
        return "UNVERIFIED"

    support_domains = {
        get_publisher_domain(source)
        for source in supporting
        if (
            source.get(
                "is_independent",
                True,
            )
            and get_publisher_domain(source)
            and not is_aggregator_domain(
                get_publisher_domain(source)
            )
        )
    }

    contradiction_domains = {
        get_publisher_domain(source)
        for source in contradicting
        if (
            source.get(
                "is_independent",
                True,
            )
            and get_publisher_domain(source)
            and not is_aggregator_domain(
                get_publisher_domain(source)
            )
        )
    }

    if (
        support_domains
        and contradiction_domains
    ):
        return "MIXED"

    if (
        len(support_domains) >= 2
        and confidence >= 0.62
    ):
        return "TRUE"

    if (
        len(contradiction_domains) >= 2
        and confidence >= 0.62
    ):
        return "FALSE"

    if supporting or contradicting:
        return "RELATED INFORMATION"

    return "UNVERIFIED"


def build_fallback_summary(verdict: str) -> str:
    if verdict == "TRUE":
        return (
            "The claim is supported by multiple "
            "independent sources with extracted "
            "article evidence."
        )
    if verdict == "FALSE":
        return (
            "The claim is contradicted by multiple "
            "independent sources with extracted "
            "article evidence."
        )
    if verdict == "MIXED":
        return (
            "The retrieved sources contain both "
            "supporting and contradicting evidence."
        )
    if verdict == "RELATED INFORMATION":
        return (
            "Relevant reporting was found, but the "
            "available evidence does not meet the "
            "independent corroboration threshold."
        )
    return "No sufficiently direct evidence was retrieved."


def llm_generate_reasoning(
    claim: str,
    verdict: str,
    confidence: float,
    atomic_claims: List[Dict[str, str]],
    supporting_evidence: List[Dict[str, Any]],
    contradicting_evidence: List[Dict[str, Any]],
    corroboration: Dict[str, Any],
) -> Optional[str]:

    if not LLM_ENABLED:
        return None

    def fmt_evidence(items: List[Dict[str, Any]]) -> str:
        lines = []
        for entry in items[:6]:
            lines.append(
                f"- ({entry.get('source', 'unknown source')}) "
                f"{entry.get('evidence', '')}"
            )
        return "\n".join(lines) if lines else "(none found)"

    system = (
        "You are the reasoning/explanation module of a "
        "fact-checking agent. Given a verdict that was already "
        "computed deterministically from evidence counts, write a "
        "short, plain-language explanation (3-5 sentences) of WHY "
        "that verdict follows from the evidence. Reference the "
        "number of independent sources on each side. Be neutral, "
        "concrete, and do not invent facts beyond what's given. Do "
        "not restate the verdict label as your first word. Respond "
        "with plain text only, no JSON, no markdown headers."
    )

    user = (
        f'Claim: "{claim}"\n'
        f"Sub-claims checked: "
        f"{[a.get('claim', '') for a in atomic_claims]}\n"
        f"Computed verdict: {verdict}\n"
        f"Confidence score: {confidence}\n"
        f"Independent supporting sources: "
        f"{corroboration.get('supporting_independent_sources', 0)} "
        f"({corroboration.get('supporting_domains', [])})\n"
        f"Independent contradicting sources: "
        f"{corroboration.get('contradicting_independent_sources', 0)} "
        f"({corroboration.get('contradicting_domains', [])})\n"
        f"Supporting evidence sentences:\n"
        f"{fmt_evidence(supporting_evidence)}\n"
        f"Contradicting evidence sentences:\n"
        f"{fmt_evidence(contradicting_evidence)}"
    )

    text = call_claude(system, user, max_tokens=350)

    return clean_text(text) if text else None


# ============================================================
# Verification
# ============================================================

def perform_verification(
    claim: str,
    max_sources: int = MAX_SOURCES,
    ocr_context: str = "",
) -> Dict[str, Any]:

    started = time.perf_counter()

    claim = clean_text(claim)

    if not claim:
        return {
            "success": False,
            "error": "Claim is empty",
        }

    decomposition = decompose_claim(claim, ocr_context)
    atomic_claims = decomposition["atomic_claims"]
    decomposition_method = decomposition["method"]

    claims = [a["claim"] for a in atomic_claims]

    search_started = time.perf_counter()
    seen_queries = set()
    pending_searches = []
    merged_raw_sources: List[Dict[str, Any]] = []
    search_debug_runs = []

    queries = [a["search_query"] for a in atomic_claims] or [claim]

    for query in queries:
        key = normalize_text(query)

        if not query or key in seen_queries:
            continue

        seen_queries.add(key)
        pending_searches.append((query, SEARCH_WORKERS.submit(search_news, query, max_sources)))

    # Merge in query order so the source limit selects the same candidates.
    for query, future in pending_searches:
        run = future.result()
        search_debug_runs.append({
            "query": query,
            "debug": run["debug"],
        })
        merged_raw_sources.extend(run["results"])

    search_result = {
        "results": deduplicate_search_results(
            merged_raw_sources, max_sources,
        ),
        "debug": {
            "queries": search_debug_runs,
            "decomposition_method": decomposition_method,
        },
    }

    raw_sources = search_result[
        "results"
    ]
    search_elapsed = time.perf_counter() - search_started

    # Only the top-quality sources go through the (slower, paid)
    # LLM evidence classifier; the rest use the free rule-based
    # classifier. Both write into the same fields downstream.
    llm_eligible_urls = {
        s.get("url")
        for s in sorted(
            raw_sources,
            key=lambda s: s.get("source_quality_score", 0.5),
            reverse=True,
        )[:MAX_LLM_EVIDENCE_SOURCES]
    }

    enrichment_started = time.perf_counter()
    pending_sources = [
        SOURCE_WORKERS.submit(
            enrich_source_safely,
            source,
            claim,
            use_llm=source.get("url") in llm_eligible_urls,
        )
        for source in raw_sources
    ]
    enriched_sources = [future.result() for future in pending_sources]
    enrichment_elapsed = time.perf_counter() - enrichment_started

    enriched_sources = (
        apply_syndication_clusters(
            enriched_sources
        )
    )

    supporting = [
        source
        for source in enriched_sources
        if source.get(
            "supports_claim"
        )
    ]

    contradicting = [
        source
        for source in enriched_sources
        if source.get(
            "contradicts_claim"
        )
    ]

    context_sources = [
        source
        for source in enriched_sources
        if (
            source.get("evidence_source") in (
                "full_article", "snippet",
            )
            and not source.get(
                "supports_claim"
            )
            and not source.get(
                "contradicts_claim"
            )
            and source.get("context_evidence")
        )
    ]

    corroboration = build_corroboration(
        enriched_sources
    )

    confidence = compute_confidence(
        enriched_sources,
        corroboration,
    )

    verdict = determine_verdict(
        supporting,
        contradicting,
        enriched_sources,
        confidence,
    )

    supporting_evidence = []

    for source in supporting:
        for evidence in source.get(
            "supporting_evidence",
            [],
        ):
            supporting_evidence.append({
                "source": source.get(
                    "publisher",
                    "",
                ),
                "publisher_domain": get_publisher_domain(
                    source
                ),
                "title": source.get(
                    "title",
                    "",
                ),
                "evidence": evidence,
                "url": source.get(
                    "resolved_url"
                )
                or source.get(
                    "url"
                ),
            })

    contradicting_evidence = []

    for source in contradicting:
        for evidence in source.get(
            "contradicting_evidence",
            [],
        ):
            contradicting_evidence.append({
                "source": source.get(
                    "publisher",
                    "",
                ),
                "publisher_domain": get_publisher_domain(
                    source
                ),
                "title": source.get(
                    "title",
                    "",
                ),
                "evidence": evidence,
                "url": source.get(
                    "resolved_url"
                )
                or source.get(
                    "url"
                ),
            })

    # Do not expose full article bodies in the public API.
    # evidence_type lets the frontend actually filter/badge sources
    # correctly (it previously looked for a field that was never
    # set, so every source rendered as "contextual"/"unavailable").
    public_sources = []

    for source in enriched_sources:
        item = dict(source)
        item.pop(
            "article_text",
            None,
        )

        if item.get("supports_claim"):
            item["evidence_type"] = "supporting"
        elif item.get("contradicts_claim"):
            item["evidence_type"] = "contradicting"
        elif item.get("evidence_source") in (
            "full_article", "snippet",
        ):
            item["evidence_type"] = "contextual"
        else:
            item["evidence_type"] = "unavailable"

        public_sources.append(item)

    fallback_summary = build_fallback_summary(verdict)

    reasoning = llm_generate_reasoning(
        claim,
        verdict,
        confidence,
        atomic_claims,
        supporting_evidence,
        contradicting_evidence,
        corroboration,
    ) or fallback_summary

    # `summary` stays for backward compatibility with the existing
    # frontend; `reasoning` is the richer, evidence-grounded
    # explanation (LLM-authored when available).
    summary = reasoning

    elapsed = round(
        time.perf_counter() - started,
        3,
    )

    return {
        "success": True,
        "claim": claim,
        "verdict": verdict,
        "confidence": confidence,
        "confidence_percentage": round(
            confidence * 100
        ),
        "summary": summary,
        "reasoning": reasoning,
        "claims": claims,
        "atomic_claims": atomic_claims,
        "decomposition_method": decomposition_method,
        "sources": public_sources,
        "supporting_sources": [
            {
                "title": s.get(
                    "title",
                    "",
                ),
                "publisher": s.get(
                    "publisher",
                    "",
                ),
                "publisher_domain": get_publisher_domain(
                    s
                ),
                "url": s.get(
                    "resolved_url"
                )
                or s.get(
                    "url"
                ),
            }
            for s in supporting
        ],
        "contradicting_sources": [
            {
                "title": s.get(
                    "title",
                    "",
                ),
                "publisher": s.get(
                    "publisher",
                    "",
                ),
                "publisher_domain": get_publisher_domain(
                    s
                ),
                "url": s.get(
                    "resolved_url"
                )
                or s.get(
                    "url"
                ),
            }
            for s in contradicting
        ],
        "supporting_evidence": supporting_evidence[
            :20
        ],
        "contradicting_evidence": contradicting_evidence[
            :20
        ],
        "context_sources": [
            {
                "title": s.get(
                    "title",
                    "",
                ),
                "publisher": s.get(
                    "publisher",
                    "",
                ),
                "publisher_domain": get_publisher_domain(
                    s
                ),
                "url": s.get(
                    "resolved_url"
                )
                or s.get(
                    "url"
                ),
            }
            for s in context_sources
        ],
        "verification_tasks": {
            "claim_extraction": bool(
                claims
            ),
            "news_search": bool(
                raw_sources
            ),
            "article_resolution": any(
                bool(
                    s.get(
                        "resolved_url"
                    )
                )
                for s in enriched_sources
            ),
            "article_extraction": any(
                s.get(
                    "article_available"
                )
                for s in enriched_sources
            ),
            "evidence_detection": bool(
                supporting_evidence
                or contradicting_evidence
            ),
            "syndication_detection": True,
            "independent_source_analysis": True,
            "corroboration": True,
            "claim_decomposition": decomposition_method == "llm",
            "llm_reasoning": reasoning != fallback_summary,
        },
        "source_analysis": {
            "sources_returned": len(
                enriched_sources
            ),
            "articles_extracted": sum(
                1
                for s in enriched_sources
                if s.get(
                    "article_available"
                )
            ),
            "snippet_only_sources": sum(
                1
                for s in enriched_sources
                if s.get("evidence_source") == "snippet"
            ),
            "unreadable_sources": sum(
                1
                for s in enriched_sources
                if s.get("evidence_source") in (None, "none")
            ),
            "independent_sources": corroboration[
                "independent_sources"
            ],
            "unique_publisher_domains": len(
                corroboration[
                    "unique_publisher_domains"
                ]
            ),
            "unique_story_clusters": len({
                s.get(
                    "story_cluster_id"
                )
                for s in enriched_sources
                if s.get(
                    "story_cluster_id"
                ) is not None
            }),
        },
        "corroboration": corroboration,
        "debug": {
            "app_version": APP_VERSION,
            "search_pipeline": SEARCH_PIPELINE_VERSION,
            "google_rss_enabled": True,
            "gdelt_enabled": True,
            "genai_enabled": LLM_ENABLED,
            "genai_model": ANTHROPIC_MODEL if LLM_ENABLED else None,
            "decomposition_method": decomposition_method,
            "llm_evidence_sources_eligible": len(
                llm_eligible_urls
            ),
            "llm_evidence_sources_used": sum(
                1 for s in enriched_sources
                if s.get("evidence_method") == "llm"
            ),
            "article_resolution_enabled": True,
            "article_extraction_enabled": True,
            "syndication_detection_enabled": True,
            "max_sources": max_sources,
            "verification_time_seconds": elapsed,
            "search_time_seconds": round(search_elapsed, 3),
            "source_enrichment_time_seconds": round(enrichment_elapsed, 3),
            "note": (
                "Confidence is a deterministic verification "
                "score, not a calibrated statistical probability. "
                "Reasoning text may be LLM-generated (from the "
                "same evidence) or a rule-based fallback; see "
                "verification_tasks.llm_reasoning."
            ),
            "search": search_result["debug"],
        },
    }


# ============================================================
# Routes
# ============================================================

@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "success": True,
        "name": "ClaimCheck AI",
        "version": APP_VERSION,
        "pipeline": SEARCH_PIPELINE_VERSION,
        "genai_enabled": LLM_ENABLED,
        "image_analysis_enabled": IMAGE_ANALYSIS_AVAILABLE,
        "ocr_enabled": OCR_AVAILABLE,
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "healthy",
        "version": APP_VERSION,
        "search_pipeline": SEARCH_PIPELINE_VERSION,
    }


@app.get("/debug/version")
def debug_version() -> Dict[str, Any]:
    return {
        "success": True,
        "app_version": APP_VERSION,
        "search_pipeline": SEARCH_PIPELINE_VERSION,
        "genai_enabled": LLM_ENABLED,
        "genai_model": ANTHROPIC_MODEL if LLM_ENABLED else None,
        "image_analysis_enabled": IMAGE_ANALYSIS_AVAILABLE,
        "ocr_enabled": OCR_AVAILABLE,
        "google_rss_enabled": True,
        "gdelt_enabled": True,
        "article_resolution_enabled": True,
        "article_extraction_enabled": True,
        "syndication_detection_enabled": True,
        "max_sources": MAX_SOURCES,
    }


@app.get("/debug/google")
def debug_google(
    q: str = "Election Commission",
) -> Dict[str, Any]:

    try:
        result = search_google_news(
            q,
            DEBUG_SEARCH_LIMIT,
        )

        return {
            "success": True,
            "query": q,
            "count": len(
                result["results"]
            ),
            "debug": result["debug"],
            "results": result["results"][
                :10
            ],
        }

    except Exception as exc:
        return {
            "success": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


@app.get("/debug/gdelt")
def debug_gdelt(
    q: str = "Election Commission",
) -> Dict[str, Any]:

    result = search_gdelt(q)

    return {
        "success": True,
        "query": q,
        "count": len(
            result["results"]
        ),
        "debug": result["debug"],
        "results": result["results"][
            :10
        ],
    }


@app.get("/debug/search")
def debug_search(
    q: str = "Election Commission",
) -> Dict[str, Any]:

    result = search_news(
        q,
        DEBUG_SEARCH_LIMIT,
    )

    return {
        "success": True,
        "query": q,
        "count": len(
            result["results"]
        ),
        "engine": "Google News RSS",
        "debug": result["debug"],
        "results": result["results"][
            :10
        ],
    }


@app.get("/debug/fetch")
def debug_fetch(
    url: str = "",
    q: str = "",
) -> Dict[str, Any]:

    try:
        # If no URL is supplied, automatically fetch one
        # Google News result for the supplied query.
        if not url:
            if not q:
                q = "Election Commission"

            search_result = search_news(
                q,
                1,
            )

            if not search_result[
                "results"
            ]:
                return {
                    "success": False,
                    "error": (
                        "No Google News results found"
                    ),
                    "query": q,
                }

            url = search_result[
                "results"
            ][0].get(
                "google_url"
            ) or search_result[
                "results"
            ][0].get(
                "url"
            )

        resolution = (
            resolve_google_redirect(
                url
            )
        )

        publisher_domain = (
            resolution.get(
                "resolved_domain",
                "",
            )
        )

        if is_aggregator_domain(
            publisher_domain
        ):
            publisher_domain = ""

        return {
            "success": True,
            "input_url": url,
            "resolved_url": resolution.get(
                "resolved_url",
                "",
            ),
            "publisher": {
                "publisher": publisher_domain,
                "publisher_domain": publisher_domain,
                "resolved_domain": resolution.get(
                    "resolved_domain",
                    "",
                ),
                "source_quality_score": source_quality(
                    publisher_domain
                ),
                "source_quality": source_quality_label(
                    source_quality(
                        publisher_domain
                    )
                ),
            },
            "debug": {
                "input_url": url,
                "final_url": resolution.get(
                    "resolved_url",
                    "",
                ),
                "redirected": resolution.get(
                    "redirected",
                    False,
                ),
                "status_code": resolution.get(
                    "status_code"
                ),
                "error": resolution.get(
                    "error"
                ),
                "method": resolution.get(
                    "method"
                ),
            },
        }

    except Exception as exc:
        return {
            "success": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


@app.get("/debug/network")
def debug_network() -> Dict[str, Any]:

    checks = {}

    for name, url in {
        "google_rss": google_news_url(
            "Election Commission"
        ),
        "gdelt": (
            "https://api.gdeltproject.org/"
            "api/v2/doc/doc"
            "?query=Election%20Commission"
            "&mode=artlist&maxrecords=1"
            "&format=json"
        ),
    }.items():

        started = time.perf_counter()

        try:
            response = requests.get(
                url,
                headers=GOOGLE_HEADERS,
                timeout=8,
            )

            checks[name] = {
                "status_code": response.status_code,
                "elapsed_seconds": round(
                    time.perf_counter()
                    - started,
                    2,
                ),
            }

        except Exception as exc:
            checks[name] = {
                "status_code": None,
                "error": str(exc),
                "elapsed_seconds": round(
                    time.perf_counter()
                    - started,
                    2,
                ),
            }

    return {
        "success": True,
        "checks": checks,
    }


@app.get("/debug/query")
def debug_query(
    q: str = "Election Commission",
) -> Dict[str, Any]:

    result = search_news(
        q,
        MAX_SOURCES,
    )

    return {
        "success": True,
        "query": q,
        "count": len(
            result["results"]
        ),
        "results": [
            {
                "title": s.get(
                    "title"
                ),
                "publisher": s.get(
                    "publisher"
                ),
                "publisher_domain": s.get(
                    "publisher_domain"
                ),
                "google_url": s.get(
                    "google_url"
                ),
                "article_id": s.get(
                    "article_id"
                ),
            }
            for s in result[
                "results"
            ]
        ],
        "debug": result["debug"],
    }


@app.post("/verify")
def verify(
    request: ClaimRequest,
) -> Dict[str, Any]:

    try:
        return perform_verification(
            request.claim,
            request.max_sources,
        )

    except Exception as exc:
        return {
            "success": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


@app.post("/verify/image")
def verify_image(
    file: UploadFile = File(...),
) -> Dict[str, Any]:

    data = file.file.read()

    if not data:
        return {
            "success": False,
            "error": "Empty image file",
        }

    exif = extract_exif_summary(data)
    ocr = ocr_image(data)
    ocr_text = ocr.get("text", "")

    image_analysis = {
        "filename": file.filename,
        "content_type": file.content_type,
        "size_bytes": len(data),
        "exif": exif,
        "ocr": ocr,
    }

    # Not enough readable text to form a claim: don't fabricate one.
    if len(ocr_text) < 15:
        return {
            "success": True,
            "verdict": "UNVERIFIED",
            "confidence": 0.0,
            "confidence_percentage": 0,
            "summary": (
                "No readable factual claim could be extracted from "
                "this image via OCR."
                if OCR_AVAILABLE else
                "OCR is not available on this server, so no text "
                "could be extracted from the image."
            ),
            "reasoning": (
                "Try Text or Text + Image mode instead, or upload "
                "an image that contains clear, legible text."
            ),
            "claim": "",
            "sources": [],
            "image_analysis": image_analysis,
        }

    result = perform_verification(
        ocr_text,
        MAX_SOURCES,
    )

    result["claim_source"] = "image_ocr"
    result["image_analysis"] = image_analysis

    return result


@app.post("/verify/multimodal")
def verify_multimodal(
    claim: str = "",
    file: Optional[UploadFile] = File(
        default=None
    ),
) -> Dict[str, Any]:

    claim = clean_text(claim)
    image_analysis: Optional[Dict[str, Any]] = None
    ocr_text = ""

    if file is not None:
        data = file.file.read()

        exif = extract_exif_summary(data)
        ocr = ocr_image(data)
        ocr_text = ocr.get("text", "")

        image_analysis = {
            "filename": file.filename,
            "content_type": file.content_type,
            "size_bytes": len(data),
            "exif": exif,
            "ocr": ocr,
        }

    effective_claim = claim or ocr_text

    if not effective_claim:
        return {
            "success": False,
            "error": (
                "No claim text was provided and no readable text "
                "was found in the image."
            ),
            "image_analysis": image_analysis,
        }

    # Pass the OCR text as extra context to claim decomposition even
    # when the user also typed a claim, so decomposition can use
    # both signals; the claim actually verified is the user's typed
    # text when present (that's their stated assertion), falling
    # back to OCR text only when they left the text box empty.
    result = perform_verification(
        effective_claim,
        MAX_SOURCES,
        ocr_context=ocr_text if claim else "",
    )

    result["claim_source"] = "user_text" if claim else "image_ocr"

    if image_analysis is not None:
        result["image_analysis"] = image_analysis

        if claim and ocr_text:
            consistency = similarity(claim, ocr_text)

            result["image_text_consistency"] = {
                "ocr_text_excerpt": ocr_text[:300],
                "similarity_to_claim": round(consistency, 3),
                "note": (
                    "Low similarity may indicate the image is "
                    "unrelated to, or used out of context with, "
                    "the stated claim."
                    if consistency < 0.15 else
                    "The image's text appears related to the "
                    "stated claim."
                ),
            }

    return result


# ============================================================
# Local development
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
