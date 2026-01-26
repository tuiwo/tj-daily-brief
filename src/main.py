import os
import re
import time
import json
import smtplib
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import html as html_lib
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Optional, Tuple, List, Dict

import requests
import yaml
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent   # repo 根目录（src 的上一级）

def resolve_path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


# OpenAlex paging: per-page is 1..200
OPENALEX_PER_PAGE_MIN = 1
OPENALEX_PER_PAGE_MAX = 200

# OpenAlex filter OR: max 100 values in one request
OPENALEX_FILTER_OR_MAX = 100

# Polite throttling (avoid 429)
POLITE_SLEEP_SEC = 0.12

BAD_OPENALEX_IDS_FILE = "data/bad_openalex_ids.txt"

RUN_STATS = {
    "openalex_requests": 0,
    "openalex_failures": 0,
    "openrouter_requests": 0,
    "openrouter_failures": 0,
    "openrouter_parse_errors": 0,
}

OPENALEX_ENABLED = True


# -------------------------
# Small utils
# -------------------------

def now_local(tz: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(tz))


def clamp_int(x: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(x)
        return max(lo, min(hi, v))
    except Exception:
        return default


def normalize(s: str) -> str:
    return (s or "").lower()


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x or 0)
    except Exception:
        return default


def should_send_now(cfg: dict) -> bool:
    now = now_local(cfg["timezone"])
    dbg = (os.getenv("DEBUG", "") or "").strip()
    if dbg:
        print(f"DEBUG tz={cfg['timezone']} now={now.isoformat()} hour={now.hour} send_hour_local={cfg['send_hour_local']}")
    return now.hour == int(cfg["send_hour_local"])


def validate_config(cfg_raw: dict, cfg_flat: dict) -> None:
    errors = []
    if not cfg_flat.get("use_llm_brief", False):
        errors.append("llm.use_llm_brief must be true (LLM-only output required).")
    if not (os.getenv("OPENROUTER_API_KEY") or "").strip():
        errors.append("Missing OPENROUTER_API_KEY (required for LLM-only output).")
    if not (os.getenv("S2_API_KEY") or "").strip():
        print("[WARN] S2_API_KEY missing: S2 enhancements disabled.")
    if errors:
        raise RuntimeError("Config validation failed: " + " | ".join(errors))


# -------------------------
# Config: load + flatten to old-style keys
# -------------------------
def load_config(path="config.yml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def deep_get(d: dict, path: str, default=None):
    """
    deep_get(cfg, "llm.max_tokens") -> cfg["llm"]["max_tokens"]
    """
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def flatten_global_cfg(cfg: dict) -> dict:
    out = dict(cfg)

    # Normalize nested -> flat for downstream functions
    out["openalex_per_page"] = clamp_int(deep_get(cfg, "openalex.per_page", 200), OPENALEX_PER_PAGE_MIN, OPENALEX_PER_PAGE_MAX, 200)

    out["use_s2_recommendations"] = bool(cfg.get("use_s2_recommendations", True))
    out["s2_limit"] = safe_int(deep_get(cfg, "s2.limit", 20), 20)
    out["s2_retries"] = safe_int(deep_get(cfg, "s2.retries", 2), 2)
    out["s2_backoff_sec"] = safe_int(deep_get(cfg, "s2.backoff_sec", 5), 5)

    # LLM (OpenRouter)
    out["use_llm_brief"] = bool(deep_get(cfg, "llm.use_llm_brief", False))
    out["openrouter_model"] = deep_get(cfg, "llm.openrouter_model", "z-ai/glm-4.5-air:free")
    out["llm_temperature"] = float(deep_get(cfg, "llm.temperature", 0.2))
    out["llm_max_tokens"] = safe_int(deep_get(cfg, "llm.max_tokens", 520), 520)
    out["llm_timeout_sec"] = safe_int(deep_get(cfg, "llm.timeout_sec", 60), 60)
    out["llm_retries"] = safe_int(deep_get(cfg, "llm.retries", 2), 2)
    out["llm_backoff_sec"] = safe_int(deep_get(cfg, "llm.backoff_sec", 3), 3)
    out["llm_max_items_per_run"] = safe_int(deep_get(cfg, "llm.max_items_per_run", 18), 18)
    out["llm_cache_file"] = deep_get(cfg, "llm.cache_file", "llm_cache.json")
    out["llm_max_concurrency"] = safe_int(deep_get(cfg, "llm.max_concurrency", 4), 4)
    out["llm_pending_store"] = deep_get(cfg, "llm.pending_store", "data/pending_llm.json")
    out["llm_pending_max_retries"] = safe_int(deep_get(cfg, "llm.pending_max_retries", 5), 5)
    out["llm_pending_max_days"] = safe_int(deep_get(cfg, "llm.pending_max_days", 7), 7)
    out["llm_prompt_version"] = deep_get(cfg, "llm.prompt_version", "brief_v3_2026-01-25")

    # Misc
    out["seen_days_keep"] = safe_int(cfg.get("seen_days_keep", 30), 30)
    out["prefer_recent_days"] = safe_int(cfg.get("prefer_recent_days", 180), 180)

    return out


def flatten_profile_cfg(global_cfg_flat: dict, profile: dict) -> dict:
    """
    Convert your new nested profile structure to flat keys used by logic.
    """
    out = dict(global_cfg_flat)

    # Basic identity
    out["profile_id"] = profile.get("id") or ""
    out["enabled"] = bool(profile.get("enabled", True))
    out["topic_cn"] = profile.get("title_cn") or profile.get("topic_cn") or profile.get("name") or out["profile_id"] or "未命名主题"

    # Search + keywords
    out["search_query"] = (profile.get("search_query") or "").strip()
    out["latest_days"] = safe_int(profile.get("latest_days", 30), 30)

    out["keywords"] = profile.get("keywords") or []
    out["exclude_keywords"] = profile.get("exclude_keywords") or []

    # Top buckets (new: profile.top.xxx)
    top = profile.get("top") or {}
    out["top_latest"] = safe_int(top.get("latest", 5), 5)
    out["top_classic"] = safe_int(top.get("classic", 2), 2)
    out["top_reco_s2"] = safe_int(top.get("reco_s2", 10), 10)
    out["top_reco_oa"] = safe_int(top.get("reco_oa", 10), 10)
    out["top_pub_latest"] = safe_int(top.get("pub_latest", 8), 8)
    out["top_pub_classic"] = safe_int(top.get("pub_classic", 8), 8)
    out["top_graph_ref_classic"] = safe_int(top.get("graph_ref_classic", 10), 10)
    out["top_graph_citedby_keyfollow"] = safe_int(top.get("graph_citedby_keyfollow", 10), 10)

    # related_works
    rw = profile.get("related_works") or {}
    out["max_related_per_seed"] = safe_int(rw.get("max_related_per_seed", 25), 25)

    # seeds
    seeds = profile.get("seeds") or {}
    out["seeds_positive_file"] = seeds.get("positive_file") or ""
    out["seeds_negative_file"] = seeds.get("negative_file") or ""
    # Legacy fallback: profile.path + seeds_positive/negative.txt
    out["profile_path"] = profile.get("path") or ""

    # publishers
    pubs = profile.get("publishers") or {}
    out["enable_publisher_pools"] = bool(pubs.get("enable_publisher_pools", True))
    out["preferred_publishers"] = pubs.get("preferred_publishers") or []
    out["publisher_boost"] = safe_int(pubs.get("publisher_boost", 6), 6)
    out["doaj_penalty"] = safe_int(pubs.get("doaj_penalty", 2), 2)

    # graph
    g = profile.get("graph") or {}
    out["graph_max_references_per_seed"] = safe_int(g.get("max_references_per_seed", 60), 60)
    out["graph_max_citedby_per_seed"] = safe_int(g.get("max_citedby_per_seed", 60), 60)

    out["graph_ref_classic_years_ago"] = safe_int(g.get("ref_classic_years_ago", 5), 5)
    out["graph_ref_min_cited_by"] = safe_int(g.get("ref_min_cited_by", 30), 30)

    out["graph_follow_years"] = safe_int(g.get("follow_years", 3), 3)
    out["graph_follow_min_cited_by"] = safe_int(g.get("follow_min_cited_by", 10), 10)

    # conflict detection knobs (optional)
    out["enable_dual_track_on_conflict"] = bool(profile.get("enable_dual_track_on_conflict", True))
    out["dual_track_use_seed_keywords"] = bool(profile.get("dual_track_use_seed_keywords", True))
    out["seeds_query_max_seeds"] = safe_int(profile.get("seeds_query_max_seeds", 10), 10)
    out["seeds_query_max_terms"] = safe_int(profile.get("seeds_query_max_terms", 12), 12)
    out["conflict_seed_hit_rel"] = safe_int(profile.get("conflict_seed_hit_rel", 2), 2)
    out["conflict_seed_avg_rel_min"] = float(profile.get("conflict_seed_avg_rel_min", 1.2))
    out["conflict_seed_hit_ratio_min"] = float(profile.get("conflict_seed_hit_ratio_min", 0.35))

    # Allow per-profile override of global s2/llm if present
    # (Keep it simple: if profile provides nested s2/llm keys, accept them.)
    if isinstance(profile.get("s2"), dict):
        ps2 = profile["s2"]
        out["s2_limit"] = safe_int(ps2.get("limit", out["s2_limit"]), out["s2_limit"])
        out["s2_retries"] = safe_int(ps2.get("retries", out["s2_retries"]), out["s2_retries"])
        out["s2_backoff_sec"] = safe_int(ps2.get("backoff_sec", out["s2_backoff_sec"]), out["s2_backoff_sec"])

    if isinstance(profile.get("llm"), dict):
        pllm = profile["llm"]
        out["use_llm_brief"] = bool(pllm.get("use_llm_brief", out["use_llm_brief"]))
        out["openrouter_model"] = pllm.get("openrouter_model", out["openrouter_model"])
        out["llm_temperature"] = float(pllm.get("temperature", out["llm_temperature"]))
        out["llm_max_tokens"] = safe_int(pllm.get("max_tokens", out["llm_max_tokens"]), out["llm_max_tokens"])
        out["llm_timeout_sec"] = safe_int(pllm.get("timeout_sec", out["llm_timeout_sec"]), out["llm_timeout_sec"])
        out["llm_retries"] = safe_int(pllm.get("retries", out["llm_retries"]), out["llm_retries"])
        out["llm_backoff_sec"] = safe_int(pllm.get("backoff_sec", out["llm_backoff_sec"]), out["llm_backoff_sec"])
        out["llm_max_items_per_run"] = safe_int(pllm.get("max_items_per_run", out["llm_max_items_per_run"]), out["llm_max_items_per_run"])
        out["llm_cache_file"] = pllm.get("cache_file", out["llm_cache_file"])
        out["llm_max_concurrency"] = safe_int(pllm.get("max_concurrency", out["llm_max_concurrency"]), out["llm_max_concurrency"])
        out["llm_pending_store"] = pllm.get("pending_store", out["llm_pending_store"])
        out["llm_pending_max_retries"] = safe_int(pllm.get("pending_max_retries", out["llm_pending_max_retries"]), out["llm_pending_max_retries"])
        out["llm_pending_max_days"] = safe_int(pllm.get("pending_max_days", out["llm_pending_max_days"]), out["llm_pending_max_days"])
        out["llm_prompt_version"] = pllm.get("prompt_version", out["llm_prompt_version"])

    return out


def get_seed_paths(profile_cfg: dict) -> Tuple[Path, Path]:
    """
    Prefer config seeds files:
      seeds_positive_file / seeds_negative_file
    fallback to legacy:
      profile_path + seeds_positive.txt / seeds_negative.txt
    """
    pos = (profile_cfg.get("seeds_positive_file") or "").strip()
    neg = (profile_cfg.get("seeds_negative_file") or "").strip()
    if pos and neg:
        return resolve_path(pos), resolve_path(neg)

    base = resolve_path(profile_cfg.get("profile_path", ""))
    return base / "seeds_positive.txt", base / "seeds_negative.txt"


# -------------------------
# HTTP helper (GET/POST JSON with retry)
# -------------------------
def _http_should_retry(status: int) -> bool:
    if status in (400, 401, 403, 404):
        return False
    if status in (408, 429):
        return True
    return 500 <= status < 600


def _http_retry_after_seconds(headers: dict) -> Optional[int]:
    ra = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    if not ra:
        return None
    try:
        return int(ra)
    except Exception:
        return None


def _http_backoff_seconds(backoff_sec: int, attempt: int, retry_after: Optional[int] = None) -> float:
    base = retry_after if retry_after is not None else backoff_sec * (2 ** attempt)
    jitter = random.uniform(0.0, max(0.1, base * 0.1))
    return base + jitter


def _extract_work_id_from_url(url: str) -> str:
    if "/works/" not in url:
        return ""
    return url.split("/works/")[-1].split("?")[0]


def http_request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    data: Optional[str] = None,
    timeout: int = 60,
    retries: int = 3,
    backoff_sec: int = 3,
    retry_on_status: Tuple[int, ...] = (429,),
) -> dict:
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, params=params, headers=headers, data=data, timeout=timeout)

            status = r.status_code
            if status in (400, 401, 403, 404):
                if status == 404:
                    req_url = r.request.url
                    work_id = _extract_work_id_from_url(req_url)
                    print(f"[HTTP] 404 Not Found url={req_url} work_id={work_id} status=404")
                return {}

            if _http_should_retry(status) or status in retry_on_status:
                if attempt < retries:
                    retry_after = _http_retry_after_seconds(r.headers) if status == 429 else None
                    sleep_s = _http_backoff_seconds(backoff_sec, attempt, retry_after=retry_after)
                    print(f"{method} {url} retryable status={status}; retry in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                r.raise_for_status()

            r.raise_for_status()
            if (os.getenv("DEBUG", "") or "").strip():
                print(f"[HTTP] {method} {r.request.url} status={r.status_code}")
            return r.json()

        except requests.RequestException as e:
            if attempt < retries:
                sleep_s = _http_backoff_seconds(backoff_sec, attempt)
                print(f"{method} {url} exception: {e}; retry in {sleep_s:.1f}s")
                time.sleep(sleep_s)
                continue
            raise


def selftest_http_retry():
    assert _http_should_retry(404) is False
    assert _http_should_retry(500) is True
    assert _http_should_retry(429) is True
    assert _http_retry_after_seconds({"Retry-After": "3"}) == 3
    sleep_s = _http_backoff_seconds(2, 0, retry_after=5)
    assert sleep_s >= 5
    print("http retry selftest ok")

# -------------------------
# OpenAlex auth helpers (api_key + mailto)
# -------------------------
def openalex_api_key() -> str:
    """
    Read OpenAlex API key from env. You said GitHub Actions already has OPENALEX_API_KEY.
    OpenAlex expects api_key as a query parameter.   [oai_citation:1‡docs.openalex.org](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication)
    """
    return (os.getenv("OPENALEX_API_KEY") or "").strip()


def openalex_apply_auth(params: Optional[dict] = None, mailto: str = "") -> dict:
    """
    Ensure every OpenAlex request carries api_key and (optionally) mailto.
    - api_key is required (and will be mandatory soon per OpenAlex docs).  [oai_citation:2‡docs.openalex.org](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication)
    - mailto is optional but recommended for polite usage.
    """
    out = dict(params or {})
    key = openalex_api_key()
    if key and "api_key" not in out:
        out["api_key"] = key
    if mailto and "mailto" not in out:
        out["mailto"] = mailto
    return out


def load_bad_openalex_ids(path: str = BAD_OPENALEX_IDS_FILE) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except Exception:
        return set()


def mark_bad_openalex_id(work_id: str, path: str = BAD_OPENALEX_IDS_FILE) -> None:
    if not work_id:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(work_id + "\n")
    except Exception:
        return


BAD_OPENALEX_IDS = load_bad_openalex_ids()
# -------------------------
# OpenAlex helpers
# -------------------------
def reconstruct_abstract(inv_idx):
    if not inv_idx:
        return ""
    pos2word = {}
    for word, poses in inv_idx.items():
        for p in poses:
            pos2word[p] = word
    return " ".join(pos2word[i] for i in sorted(pos2word))

def openalex_get(params: dict, mailto: str = "", debug: Optional[dict] = None) -> dict:
    if not OPENALEX_ENABLED:
        return {}
    params = openalex_apply_auth(params, mailto=mailto)
    RUN_STATS["openalex_requests"] += 1
    data = http_request_json("GET", "https://api.openalex.org/works", params=params, timeout=60, retries=3, backoff_sec=3)
    if not data:
        RUN_STATS["openalex_failures"] += 1
    if (os.getenv("DEBUG", "") or "").strip() and debug:
        prepared = requests.Request("GET", "https://api.openalex.org/works", params=params).prepare()
        meta = data.get("meta") or {}
        results = data.get("results") or []
        filter_str = params.get("filter", "") or ""
        filter_has_type = "type:" in filter_str
        filter_has_type_crossref = "type_crossref:" in filter_str
        print(f"[OA] kind={debug.get('kind','')} profile={debug.get('profile','')}")
        print(f"search_query={params.get('search','')}")
        print(f"filter={filter_str}")
        print(f"DEBUG OA filter_has_type={filter_has_type} filter_has_type_crossref={filter_has_type_crossref}")
        if any(x in filter_str for x in ("type:journal-article", "type:proceedings-article", "type:posted-content")):
            print("WARNING: OA filter uses type with crossref values; this likely yields 0 results")
        print(f"sort={params.get('sort','')} per_page={params.get('per_page','')}")
        print(f"FINAL_URL={prepared.url}")
        print(f"meta.count={meta.get('count')} results_len={len(results)}")
    return data


def openalex_get_work_by_id(openalex_id: str, mailto: str = "") -> Optional[dict]:
    if not openalex_id:
        return None
    if not OPENALEX_ENABLED:
        return None
    oid = openalex_id.strip()
    if oid.startswith("https://openalex.org/"):
        work_id = oid.split("/")[-1]
    else:
        work_id = oid
    if work_id in BAD_OPENALEX_IDS:
        return None
    url = f"https://api.openalex.org/works/{work_id}"

    params = openalex_apply_auth({}, mailto=mailto)

    try:
        RUN_STATS["openalex_requests"] += 1
        data = http_request_json("GET", url, params=params, timeout=60, retries=2, backoff_sec=2)
        if not data:
            RUN_STATS["openalex_failures"] += 1
            if work_id not in BAD_OPENALEX_IDS:
                BAD_OPENALEX_IDS.add(work_id)
                mark_bad_openalex_id(work_id)
            return None
        return data
    except requests.HTTPError as e:
        if "404" in str(e):
            return None
        raise
    except Exception:
        return None





def normalize_doi(doi: str) -> str:
    d = (doi or "").strip()
    if not d:
        return ""
    d = d.lower()
    d = d.replace("doi:", "").strip()
    if d.startswith("http://"):
        d = "https://" + d[len("http://"):]
    if d.startswith("https://doi.org/"):
        return d
    return "https://doi.org/" + d


def openalex_find_work_by_doi(doi: str, mailto: str = "") -> Optional[dict]:
    doi_url = normalize_doi(doi)
    if not doi_url:
        return None
    params = {"filter": f"doi:{doi_url}", "per_page": 1}
    if mailto:
        params["mailto"] = mailto
    data = openalex_get(params, mailto=mailto)
    results = data.get("results", [])
    return results[0] if results else None


def pick_best_url(work: dict) -> str:
    doi = work.get("doi")
    if doi:
        return doi
    primary = (work.get("primary_location") or {}).get("landing_page_url")
    if primary:
        return primary
    return work.get("id", "")


def short_openalex_id(oid: str) -> str:
    s = (oid or "").strip()
    if not s:
        return ""
    if s.startswith("https://openalex.org/"):
        return s.split("/")[-1]
    return s


def fetch_works_by_openalex_ids(ids: list[str], mailto: str = "", per_page: int = 200) -> list[dict]:
    ids = [short_openalex_id(x) for x in ids if short_openalex_id(x)]
    if not ids:
        return []

    per_page = clamp_int(per_page, OPENALEX_PER_PAGE_MIN, OPENALEX_PER_PAGE_MAX, 200)

    out: list[dict] = []
    for i in range(0, len(ids), OPENALEX_FILTER_OR_MAX):
        part = ids[i:i + OPENALEX_FILTER_OR_MAX]
        params = {"filter": f"openalex:{'|'.join(part)}", "per_page": min(per_page, OPENALEX_PER_PAGE_MAX)}
        if mailto:
            params["mailto"] = mailto
        data = openalex_get(params, mailto=mailto)
        out.extend(data.get("results", []) or [])
        time.sleep(POLITE_SLEEP_SEC)
    return out


def fetch_latest_and_classic(profile_cfg: dict, mailto: str) -> Tuple[list[dict], list[dict]]:
    raw_query = profile_cfg.get("search_query") or " ".join((profile_cfg.get("keywords") or [])[:6])
    cleaned_query, kept_tokens = clean_search_query(raw_query)
    query = cleaned_query or raw_query

    today = dt.date.today()

    # classic cutoff: default 2 years ago (can be adjusted if you want)
    classic_to = (today - dt.timedelta(days=365 * 2)).isoformat()

    per_page = clamp_int(profile_cfg.get("openalex_per_page", 200), OPENALEX_PER_PAGE_MIN, OPENALEX_PER_PAGE_MAX, 200)
    base = {"search": query, "per_page": per_page}
    if mailto:
        base["mailto"] = mailto

    if (os.getenv("DEBUG", "") or "").strip():
        print(f"[DEBUG] raw_query={raw_query} cleaned_query={cleaned_query} kept_tokens={len(kept_tokens)}")

    windows = [int(profile_cfg.get("latest_days", 30)), 90, 180, 365]
    seen_windows = set()
    windows = [w for w in windows if not (w in seen_windows or seen_windows.add(w))]
    set_profile_debug(profile_cfg, "latest_backoff_steps", [])

    latest = []
    latest_filter = ""
    latest_data = {}
    for i, days in enumerate(windows):
        from_date = (today - dt.timedelta(days=int(days))).isoformat()
        latest_filter = f"from_publication_date:{from_date}"
        if (os.getenv("DEBUG", "") or "").strip():
            print(f"[{profile_cfg.get('topic_cn','')}] OA latest params: from_date={from_date} query={query}")
            print(f"[{profile_cfg.get('topic_cn','')}] OA latest filter: {latest_filter}")
        latest_data = openalex_get({
            **base,
            "filter": latest_filter,
            "sort": "publication_date:desc",
        } ,mailto=mailto, debug={"kind": "latest", "profile": profile_cfg.get("topic_cn","")})
        latest = latest_data.get("results", [])
        meta = latest_data.get("meta") or {}
        latest_count = safe_int(meta.get("count", 0), 0)
        if latest_count == 0:
            print(f"[OA] latest_backoff window_days={days} meta.count={meta.get('count')} results_len={len(latest)}")
            steps = profile_cfg.get("debug", {}).get("latest_backoff_steps") or []
            steps.append(days)
            set_profile_debug(profile_cfg, "latest_backoff_steps", steps)
        if len(latest) > 0:
            set_profile_debug(profile_cfg, "latest_window_days", days)
            break
        if latest_count == 0:
            probe_search = openalex_get(
                {"search": query, "per_page": 1},
                mailto=mailto,
                debug={"kind": "latest_probe_search", "profile": profile_cfg.get("topic_cn","")},
            )
            probe_filter = openalex_get(
                {"filter": latest_filter, "per_page": 1},
                mailto=mailto,
                debug={"kind": "latest_probe_filter", "profile": profile_cfg.get("topic_cn","")},
            )
            search_count = safe_int((probe_search.get("meta") or {}).get("count", 0), 0)
            filter_count = safe_int((probe_filter.get("meta") or {}).get("count", 0), 0)
            if search_count > 0 and filter_count > 0:
                fallback_data = openalex_get(
                    {**base, "sort": "publication_date:desc"},
                    mailto=mailto,
                    debug={"kind": "latest_fallback_combo_zero", "profile": profile_cfg.get("topic_cn","")},
                )
                fallback_results = fallback_data.get("results", []) or []
                if fallback_results:
                    filtered = []
                    has_date = False
                    for w in fallback_results:
                        pdate = (w.get("publication_date") or "").strip()
                        if pdate:
                            has_date = True
                        if not pdate or pdate >= from_date:
                            filtered.append(w)
                    if has_date:
                        latest = filtered
                        set_profile_debug(profile_cfg, "latest_status", "empty_due_to_combo_filter; fallback=client_side_date_filter")
                    else:
                        latest = fallback_results
                        set_profile_debug(profile_cfg, "latest_status", "empty_due_to_combo_filter; fallback=date_filter_skipped")
                    set_profile_debug(profile_cfg, "latest_window_days", days)
                    break

    if len(latest) == 0:
        probe_data = openalex_get(
            {"filter": latest_filter, "per_page": 1},
            mailto=mailto,
            debug={"kind": "latest_probe_filter_final", "profile": profile_cfg.get("topic_cn","")},
        )
        probe_meta = probe_data.get("meta") or {}
        if safe_int(probe_meta.get("count", 0), 0) > 0 and kept_tokens:
            no_search_data = openalex_get(
                {"filter": latest_filter, "sort": "publication_date:desc", "per_page": 200},
                mailto=mailto,
                debug={"kind": "latest_fallback_no_search", "profile": profile_cfg.get("topic_cn","")},
            )
            fallback_works = no_search_data.get("results", []) or []
            fetched = len(fallback_works)
            qset = set(kept_tokens)
            scored = []
            for w in fallback_works:
                title = w.get("title") or ""
                abstract = reconstruct_abstract(w.get("abstract_inverted_index")) or (w.get("abstract") or "")
                title_overlap = len(set(tokenize_en(title)) & qset)
                abstract_overlap = len(set(tokenize_en(abstract)) & qset)
                score = 2 * title_overlap + abstract_overlap
                scored.append((score, w))
            scored.sort(key=lambda x: x[0], reverse=True)
            kept = [w for score, w in scored if score >= 2]
            if not kept:
                top = scored[:10]
                max_score = top[0][0] if top else 0
                if max_score == 0:
                    print(f"[OA] latest_fallback_no_search fetched={fetched} kept=0 reason=no_overlap")
                    latest = []
                else:
                    latest = [w for _, w in top]
                    print(f"[OA] latest_fallback_no_search fetched={fetched} kept={len(latest)} mode=topk_low_score")
            else:
                latest = kept
                print(f"[OA] latest_fallback_no_search fetched={fetched} kept={len(latest)}")

            if scored:
                examples = []
                for score, w in scored[:3]:
                    examples.append(f"({score}, {w.get('title','')[:60]}, {w.get('publication_date')})")
                print(f"[OA] latest_fallback_top examples: {', '.join(examples)}")
        else:
            if not kept_tokens:
                print("[OA] latest_fallback_no_search skipped: empty cleaned_query tokens")
            else:
                print(f"[OA] latest_fallback_no_search skipped: meta.count={probe_meta.get('count')}")

    classic_filter = f"to_publication_date:{classic_to}"
    if (os.getenv("DEBUG", "") or "").strip():
        print(f"[{profile_cfg.get('topic_cn','')}] OA classic params: classic_to={classic_to} query={query}")
        print(f"[{profile_cfg.get('topic_cn','')}] OA classic filter: {classic_filter}")

    classic_data = openalex_get({
        **base,
        "filter": classic_filter,
        "sort": "cited_by_count:desc",
    },mailto=mailto, debug={"kind": "classic", "profile": profile_cfg.get("topic_cn","")})
    classic = classic_data.get("results", [])
    if (os.getenv("DEBUG", "") or "").strip() and len(classic) == 0:
        openalex_get(
            {"search": query, "per_page": 1},
            mailto=mailto,
            debug={"kind": "classic_probe_search", "profile": profile_cfg.get("topic_cn","")},
        )
        openalex_get(
            {"filter": classic_filter, "per_page": 1},
            mailto=mailto,
            debug={"kind": "classic_probe_filter", "profile": profile_cfg.get("topic_cn","")},
        )

    return latest, classic


def openalex_get_url(url: str, params: dict, timeout: int = 60, mailto: str = "") -> dict:
    params = openalex_apply_auth(params, mailto=mailto)
    return http_request_json("GET", url, params=params, timeout=timeout, retries=3, backoff_sec=3)


# -------------------------
# Seeds load
# -------------------------
def load_seed_dois(path: str | Path) -> list[str]:
    p = resolve_path(path)
    if not p.exists():
        return []
    out: list[str] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
    return out


# -------------------------
# Relevance / tags / rule-based brief
# -------------------------
def relevance_score(title: str, abstract: str, keywords: list[str]) -> int:
    t = normalize(title)
    a = normalize(abstract)
    score = 0
    for kw in keywords or []:
        k = kw.lower()
        if k in t:
            score += 3
        elif k in a:
            score += 1
    return score



def excluded(title: str, abstract: str, exclude_keywords: list[str]) -> bool:
    t = normalize(title)
    a = normalize(abstract)
    for k in (exclude_keywords or []):
        kk = (k or "").strip().lower()
        if not kk:
            continue
        if kk in t or kk in a:
            return True
    return False





def extract_numbers(text: str) -> str:
    hits = re.findall(r"(\d+(?:\.\d+)?\s*(?:°c|℃|k|%))", normalize(text))
    uniq = []
    for h in hits:
        h = h.replace(" ", "")
        if h not in uniq:
            uniq.append(h)
    return ", ".join(uniq[:6])


def guess_tags(text: str) -> list[str]:
    t = normalize(text)
    tags = []
    mapping = [
        ("TSEP", ["tsep", "temperature sensitive electrical parameter"]),
        ("电参法(Vce/Vf/Rds)", ["vce", "vce(sat)", "vf", "forward voltage", "rds(on)"]),
        ("电热模型/热阻抗", ["electro-thermal", "thermal impedance", "foster", "cauer"]),
        ("滤波/估计", ["kalman", "ukf", "ekf", "observer", "state estimation"]),
        ("器件:SiC", ["sic"]),
        ("器件:IGBT", ["igbt"]),
        ("模块/封装", ["power module", "module", "packaging"]),
    ]
    for name, keys in mapping:
        if any(k in t for k in keys):
            tags.append(name)
    return tags[:4]


def human_brief_cn(title: str, abstract: str) -> str:
    tags = guess_tags(title + " " + abstract)
    nums = extract_numbers(abstract)

    sents = re.split(r"(?<=[.!?])\s+", (abstract or "").strip())
    sents = [s for s in sents if len(s) > 40]
    explain = " ".join(sents[:2]) if sents else "（摘要信息不足：建议点开链接快速判断是否与你的在线监测链路相关。）"

    return "\n".join([
        "一句话：这篇工作围绕在线估算/监测给出可实现的技术路径。",
        f"方法线索：{(' / '.join(tags)) if tags else '未从摘要里识别到明确方法关键词'}",
        f"可量化指标：{nums if nums else '摘要未给出明确数值（或需读全文/图表）'}",
        f"拆解：{explain}",
        "建议：若你在做标定/在线估算链路/误差评估，这篇优先读；否则先收藏观察。"
    ])


# -------------------------
# Enrich / dedupe / seen / ranking
# -------------------------
def enrich(profile_cfg: dict, works: list[dict], bucket: str = "", publisher_id_set: Optional[set[str]] = None) -> list[dict]:
    publisher_id_set = publisher_id_set or set()
    out = []
    for w in works or []:
        title = w.get("title") or ""
        abstract = reconstruct_abstract(w.get("abstract_inverted_index"))
        if excluded(title, abstract, profile_cfg.get("exclude_keywords", [])):
            continue

        src = ((w.get("primary_location") or {}).get("source") or {})
        host_org = src.get("host_organization") or ""
        is_in_doaj = bool(src.get("is_in_doaj", False))

        out.append({
            "work_id": w.get("id") or "",
            "title": title,
            "abstract": abstract,
            "publication_year": w.get("publication_year"),
            "publication_date": w.get("publication_date"),
            "cited_by_count": w.get("cited_by_count", 0) or 0,
            "venue": src.get("display_name") or "",
            "doi": w.get("doi"),
            "url": pick_best_url(w),
            "relevance": relevance_score(title, abstract, profile_cfg.get("keywords", [])),
            "bucket": bucket,
            "host_org": host_org,
            "is_in_doaj": is_in_doaj,
            "publisher_hit": (host_org in publisher_id_set) if host_org else False,
            "via": w.get("_via", "openalex"),
            "profile_cn": profile_cfg.get("topic_cn") or "",
        })
    return out


def enrich_s2(profile_cfg: dict, papers: list[dict], bucket: str = "reco_s2") -> list[dict]:
    out = []
    for p in papers or []:
        title = p.get("title") or ""
        abstract = p.get("abstract") or ""
        if excluded(title, abstract, profile_cfg.get("exclude_keywords", [])):
            continue

        ext = p.get("externalIds") or {}
        doi = ext.get("DOI") or ""
        doi_url = f"https://doi.org/{doi}" if doi else ""
        url = p.get("url") or doi_url

        out.append({
            "work_id": p.get("paperId") or p.get("corpusId") or "",
            "title": title,
            "abstract": abstract,
            "publication_year": p.get("year"),
            "publication_date": None,
            "cited_by_count": p.get("citationCount", 0) or 0,
            "venue": p.get("venue") or "",
            "doi": doi_url,
            "url": url or doi_url,
            "relevance": relevance_score(title, abstract, profile_cfg.get("keywords", [])),
            "bucket": bucket,
            "via": p.get("_via", "official_s2"),
            "profile_cn": profile_cfg.get("topic_cn") or "",
        })
    return out


def dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items or []:
        key = it.get("doi") or it.get("url") or it.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def load_seen(path="seen.json") -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen: dict, path="seen.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def filter_seen(profile_cfg: dict, items: list[dict], seen: dict) -> list[dict]:
    keep_days = int(profile_cfg.get("seen_days_keep", 30))
    today = dt.date.today()

    # cleanup expired
    cleaned = {}
    for k, v in (seen or {}).items():
        try:
            d = dt.date.fromisoformat(v)
            if (today - d).days <= keep_days:
                cleaned[k] = v
        except Exception:
            pass
    seen.clear()
    seen.update(cleaned)

    out = []
    for it in items or []:
        key = it.get("doi") or it.get("url") or it.get("title")
        if not key:
            continue
        if key in seen:
            continue
        out.append(it)
    return out


def rank_score(profile_cfg: dict, it: dict) -> int:
    score = int(it.get("relevance", 0)) * 10
    cites = safe_int(it.get("cited_by_count", 0), 0)
    score += int((cites ** 0.5) * 3)

    if it.get("publisher_hit"):
        score += int(profile_cfg.get("publisher_boost", 6)) * 10

    if it.get("is_in_doaj"):
        score -= int(profile_cfg.get("doaj_penalty", 2)) * 10

    return score


def pick_top(profile_cfg: dict, items: list[dict], n: int) -> list[dict]:
    return sorted(items or [], key=lambda x: rank_score(profile_cfg, x), reverse=True)[:n]


def pick_top_cited(items: list[dict], n: int) -> list[dict]:
    return sorted(items or [], key=lambda x: safe_int(x.get("cited_by_count", 0), 0), reverse=True)[:n]


def merge_bucket(profile_cfg: dict, key: str, list_a: list[dict], list_b: list[dict]) -> list[dict]:
    merged = dedupe((list_a or []) + (list_b or []))
    top_map = {
        "latest": "top_latest",
        "classic": "top_classic",
        "pub_latest": "top_pub_latest",
        "pub_classic": "top_pub_classic",
    }
    top_key = top_map.get(key)
    n = int(profile_cfg.get(top_key, 0)) if top_key else 0
    if key in ("classic", "pub_classic"):
        return pick_top_cited(merged, n)
    return pick_top(profile_cfg, merged, n)


# -------------------------
# Publisher pools (OpenAlex publishers -> IDs cached)
# -------------------------
PUBLISHER_CACHE = "publisher_ids.json"


def resolve_publishers_openalex_ids(names: list[str], mailto: str = "") -> dict[str, str]:
    if os.path.exists(PUBLISHER_CACHE):
        try:
            with open(PUBLISHER_CACHE, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = {}
    else:
        cached = {}

    out = dict(cached)
    changed = False

    for name in (names or []):
        if name in out and (out[name] or "").startswith("https://openalex.org/P"):
            continue

        params = {"search": name, "per_page": 5}
        if mailto:
            params["mailto"] = mailto

        params = openalex_apply_auth(params, mailto=mailto)
        data = http_request_json("GET", "https://api.openalex.org/publishers", params=params, timeout=60, retries=2, backoff_sec=2)
        results = data.get("results", []) or []
        out[name] = (results[0].get("id", "") if results else "")
        changed = True
        time.sleep(POLITE_SLEEP_SEC)

    if changed:
        with open(PUBLISHER_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    return out


def fetch_publisher_pools(profile_cfg: dict, mailto: str, publisher_ids: list[str]) -> Tuple[list[dict], list[dict]]:
    if not publisher_ids:
        return [], []

    raw_query = profile_cfg.get("search_query") or " ".join((profile_cfg.get("keywords") or [])[:6])
    cleaned_query, _ = clean_search_query(raw_query)
    query = cleaned_query or raw_query

    today = dt.date.today()
    from_date = (today - dt.timedelta(days=int(profile_cfg["latest_days"]))).isoformat()
    classic_to = (today - dt.timedelta(days=365 * 2)).isoformat()
    per_page = clamp_int(profile_cfg.get("openalex_per_page", 200), OPENALEX_PER_PAGE_MIN, OPENALEX_PER_PAGE_MAX, 200)
    base = {"search": query, "per_page": per_page}
    if mailto:
        base["mailto"] = mailto

    pubs_or = "|".join(publisher_ids)
    pub_latest_filter = f"from_publication_date:{from_date},primary_location.source.host_organization:{pubs_or}"
    pub_classic_filter = f"to_publication_date:{classic_to},primary_location.source.host_organization:{pubs_or}"

    if (os.getenv("DEBUG", "") or "").strip():
        print(f"[{profile_cfg.get('topic_cn','')}] OA publisher pools pubs_or={pubs_or}")
        print(f"[{profile_cfg.get('topic_cn','')}] OA pub_latest filter: {pub_latest_filter}")
        print(f"[{profile_cfg.get('topic_cn','')}] OA pub_classic filter: {pub_classic_filter}")

    pub_latest_data = openalex_get({
        **base,
        "filter": pub_latest_filter,
        "sort": "cited_by_count:desc",
    }, mailto=mailto, debug={"kind": "pub_latest", "profile": profile_cfg.get("topic_cn","")})
    pub_latest = pub_latest_data.get("results", [])
    pub_latest_count = safe_int((pub_latest_data.get("meta") or {}).get("count", 0), 0)
    if pub_latest_count == 0:
        probe_search = openalex_get(
            {"search": query, "per_page": 1},
            mailto=mailto,
            debug={"kind": "pub_latest_probe_search", "profile": profile_cfg.get("topic_cn","")},
        )
        probe_filter = openalex_get(
            {"filter": pub_latest_filter, "per_page": 1},
            mailto=mailto,
            debug={"kind": "pub_latest_probe_filter", "profile": profile_cfg.get("topic_cn","")},
        )
        search_count = safe_int((probe_search.get("meta") or {}).get("count", 0), 0)
        filter_count = safe_int((probe_filter.get("meta") or {}).get("count", 0), 0)
        if search_count > 0 and filter_count > 0:
            fallback_filter = f"from_publication_date:{from_date}"
            fallback_data = openalex_get(
                {**base, "filter": fallback_filter, "sort": "cited_by_count:desc"},
                mailto=mailto,
                debug={"kind": "pub_latest_fallback_combo_zero", "profile": profile_cfg.get("topic_cn","")},
            )
            fallback_results = fallback_data.get("results", []) or []
            pub_set = set(publisher_ids or [])
            filtered = []
            has_host = False
            for w in fallback_results:
                host_org = ((w.get("primary_location") or {}).get("source") or {}).get("host_organization") or ""
                if host_org:
                    has_host = True
                if not host_org or host_org in pub_set:
                    filtered.append(w)
            if not has_host:
                pub_latest = fallback_results
                set_profile_debug(profile_cfg, "pub_latest_status", "empty_due_to_combo_filter; fallback=publisher_filter_skipped")
            else:
                pub_latest = filtered
                set_profile_debug(profile_cfg, "pub_latest_status", "empty_due_to_combo_filter; fallback=client_side_publisher_filter")

    pub_classic_data = openalex_get({
        **base,
        "filter": pub_classic_filter,
        "sort": "cited_by_count:desc",
    }, mailto=mailto, debug={"kind": "pub_classic", "profile": profile_cfg.get("topic_cn","")})
    pub_classic = pub_classic_data.get("results", [])
    if (os.getenv("DEBUG", "") or "").strip() and len(pub_classic) == 0:
        openalex_get(
            {"search": query, "per_page": 1},
            mailto=mailto,
            debug={"kind": "pub_classic_probe_search", "profile": profile_cfg.get("topic_cn","")},
        )
        openalex_get(
            {"filter": pub_classic_filter, "per_page": 1},
            mailto=mailto,
            debug={"kind": "pub_classic_probe_filter", "profile": profile_cfg.get("topic_cn","")},
        )

    return pub_latest, pub_classic


# -------------------------
# OpenAlex related_works + citation graph buckets
# -------------------------
def fetch_recommendations_from_seeds(profile_cfg: dict, mailto: str, pos_path: Path, neg_path: Path) -> list[dict]:
    pos = load_seed_dois(pos_path)
    neg = set(normalize_doi(x) for x in load_seed_dois(neg_path))
    if not pos:
        return []

    max_related = int(profile_cfg.get("max_related_per_seed", 25))
    all_ids: list[str] = []
    seed_doi_urls = set()

    for doi in pos:
        w = openalex_find_work_by_doi(doi, mailto)
        time.sleep(POLITE_SLEEP_SEC)
        if not w:
            continue
        doi_url = w.get("doi")
        if doi_url:
            seed_doi_urls.add(doi_url)
        rel = w.get("related_works") or []
        all_ids.extend(rel[:max_related])

    recos: list[dict] = []
    seen = set()
    for oid in all_ids:
        if oid in seen:
            continue
        seen.add(oid)
        w = openalex_get_work_by_id(oid, mailto)
        time.sleep(POLITE_SLEEP_SEC)
        if not w:
            continue
        doi_url = w.get("doi") or ""
        if doi_url and (doi_url in neg or doi_url in seed_doi_urls):
            continue
        recos.append(w)

    return recos


def fetch_graph_buckets_from_seeds(profile_cfg: dict, mailto: str, pos_path: Path, neg_path: Path) -> Tuple[list[dict], list[dict]]:
    pos = load_seed_dois(pos_path)
    neg = set(normalize_doi(x) for x in load_seed_dois(neg_path))
    if not pos:
        return [], []

    max_ref = int(profile_cfg.get("graph_max_references_per_seed", 60))
    max_citedby = int(profile_cfg.get("graph_max_citedby_per_seed", 60))

    refs_ids: list[str] = []
    citedby_ids: list[str] = []
    seed_doi_urls = set()

    for doi in pos:
        w = openalex_find_work_by_doi(doi, mailto)
        time.sleep(POLITE_SLEEP_SEC)
        if not w:
            continue

        doi_url = w.get("doi")
        if doi_url:
            seed_doi_urls.add(doi_url)

        refs = w.get("referenced_works") or []
        if refs:
            refs_ids.extend(refs[:max_ref])

        cited_by_url = w.get("cited_by_api_url") or ""
        if cited_by_url:
            params = {"per_page": 100, "sort": "cited_by_count:desc"}
            if mailto:
                params["mailto"] = mailto
            try:
                data = openalex_get_url(cited_by_url, params=params, timeout=60,mailto=mailto)
                results = data.get("results", []) or []
                for cw in results[:max_citedby]:
                    oid = cw.get("id")
                    if oid:
                        citedby_ids.append(oid)
            except Exception as e:
                print(f"cited_by fetch failed for seed {doi}: {e}")

    def uniq_short_ids(raw_ids: list[str]) -> list[str]:
        uniq = []
        seen = set()
        for oid in raw_ids:
            sid = short_openalex_id(oid)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            uniq.append(sid)
        return uniq

    per_page = clamp_int(profile_cfg.get("openalex_per_page", 200), OPENALEX_PER_PAGE_MIN, OPENALEX_PER_PAGE_MAX, 200)

    refs_works = fetch_works_by_openalex_ids(uniq_short_ids(refs_ids), mailto=mailto, per_page=per_page)
    citedby_works = fetch_works_by_openalex_ids(uniq_short_ids(citedby_ids), mailto=mailto, per_page=per_page)

    def drop_neg_and_seed(works: list[dict]) -> list[dict]:
        out = []
        for ww in works or []:
            d = ww.get("doi") or ""
            if d and (d in neg or d in seed_doi_urls):
                continue
            out.append(ww)
        return out

    return drop_neg_and_seed(refs_works), drop_neg_and_seed(citedby_works)


# -------------------------
# Semantic Scholar recs
# -------------------------
def s2_headers() -> dict:
    key = (os.getenv("S2_API_KEY") or "").strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["x-api-key"] = key
    return h


def doi_to_s2_pid(doi: str) -> str:
    d = (doi or "").strip()
    d = d.replace("doi:", "").strip()
    d = d.replace("https://doi.org/", "").strip()
    d = d.replace("http://doi.org/", "").strip()
    return f"DOI:{d}" if d else ""


def fetch_s2_recommendations_from_seeds(profile_cfg: dict, pos_path: Path, neg_path: Path) -> list[dict]:
    if not profile_cfg.get("use_s2_recommendations", True):
        return []

    pos = load_seed_dois(pos_path)
    neg = load_seed_dois(neg_path)
    positive = [doi_to_s2_pid(d) for d in pos if doi_to_s2_pid(d)]
    negative = [doi_to_s2_pid(d) for d in neg if doi_to_s2_pid(d)]
    if not positive:
        return []

    url = "https://api.semanticscholar.org/recommendations/v1/papers"
    params = {
        "fields": "title,abstract,year,citationCount,venue,externalIds,url",
        "limit": int(profile_cfg.get("s2_limit", 20)),
    }
    payload = {"positivePaperIds": positive, "negativePaperIds": negative}

    retries = int(profile_cfg.get("s2_retries", 2))
    base_backoff = int(profile_cfg.get("s2_backoff_sec", 3))

    if not (os.getenv("S2_API_KEY") or "").strip():
        print("[INFO] S2_API_KEY missing: using unauthenticated mode (slow/limited).")
        set_profile_debug(profile_cfg, "s2_auth", "unauthenticated")

    for attempt in range(retries + 1):
        try:
            data = http_request_json(
                "POST",
                url,
                params=params,
                headers=s2_headers(),
                data=json.dumps(payload),
                timeout=20,
                retries=0,  # manual loop
            )
            recs = data.get("recommendedPapers", []) or []
            print(f"S2 ok, recs={len(recs)} (profile={profile_cfg.get('topic_cn')})")
            return recs
        except Exception as e:
            if attempt < retries:
                sleep_s = base_backoff * (2 ** attempt)
                print(f"S2 exception: {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
            print(f"[WARN] S2 exception: {e}; skipping.")
            return []

    return []


# -------------------------
# Unpaywall
# -------------------------
def bare_doi(doi_or_url: str) -> str:
    s = (doi_or_url or "").strip()
    if not s:
        return ""
    s = s.lower().replace("doi:", "").strip()
    s = s.replace("https://doi.org/", "").replace("http://doi.org/", "")
    return s.strip()


def unpaywall_lookup(doi_or_url: str, email: str, timeout: int = 20) -> Optional[dict]:
    doi = bare_doi(doi_or_url)
    if not doi or not email:
        return None
    url = f"https://api.unpaywall.org/v2/{doi}"
    try:
        return http_request_json("GET", url, params={"email": email}, timeout=timeout, retries=2, backoff_sec=2)
    except Exception:
        return None


def attach_fulltext_links(profile_cfg: dict, items: list[dict]) -> list[dict]:
    email = (os.getenv("UNPAYWALL_EMAIL") or "").strip()
    if not email:
        return items

    timeout = safe_int(profile_cfg.get("unpaywall_timeout", 20), 20)

    cache: dict[str, dict] = {}
    for it in items or []:
        d = bare_doi(it.get("doi") or "")
        if not d:
            continue

        if d in cache:
            data = cache[d]
        else:
            data = unpaywall_lookup(d, email, timeout=timeout) or {}
            cache[d] = data
            time.sleep(POLITE_SLEEP_SEC)

        if not data:
            continue

        best = data.get("best_oa_location") or {}
        it["pdf_url"] = best.get("url_for_pdf") or ""
        it["oa_landing"] = best.get("url_for_landing_page") or ""
        it["oa_status"] = data.get("oa_status") or ""
        it["oa_license"] = best.get("license") or ""
        it["oa_version"] = best.get("version") or ""

    return items


# -------------------------
# OpenRouter LLM briefs
# -------------------------
LLM_STATUS_READY = "ready"
LLM_STATUS_PENDING = "pending_llm"
LLM_STATUS_FAILED = "failed_llm"

LLM_JSON_SCHEMA = {
    "name": "brief_schema",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title_cn": {"type": "string"},
            "one_liner": {"type": "string"},
            "why_relevant": {"type": "string"},
            "key_takeaways": {"type": "array", "items": {"type": "string"}},
            "caveats": {"type": "string"},
            "recommended_action": {"type": "string"},
        },
        "required": [
            "title_cn",
            "one_liner",
            "why_relevant",
            "key_takeaways",
            "caveats",
            "recommended_action",
        ],
    },
}
def openrouter_headers() -> Optional[dict]:
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return None
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    site = (os.getenv("OPENROUTER_SITE_URL") or "").strip()
    app = (os.getenv("OPENROUTER_APP_NAME") or "").strip()
    if site:
        h["HTTP-Referer"] = site
    if app:
        h["X-Title"] = app
    return h


class LlmError(RuntimeError):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def openrouter_chat_json(profile_cfg: dict, messages: list[dict]) -> dict:
    headers = openrouter_headers()
    if not headers:
        raise LlmError("missing_key", "OPENROUTER_API_KEY missing")

    url = "https://openrouter.ai/api/v1/chat/completions"
    payload = {
        "model": profile_cfg.get("openrouter_model", "z-ai/glm-4.5-air:free"),
        "messages": messages,
        "temperature": float(profile_cfg.get("llm_temperature", 0.2)),
        "max_tokens": int(profile_cfg.get("llm_max_tokens", 520)),
        "stream": False,
        "response_format": {"type": "json_schema", "json_schema": LLM_JSON_SCHEMA},
        "plugins": [{"id": "response-healing"}],
    }

    retries = int(profile_cfg.get("llm_retries", 2))
    backoff = int(profile_cfg.get("llm_backoff_sec", 3))
    timeout = int(profile_cfg.get("llm_timeout_sec", 60))

    for attempt in range(retries + 1):
        try:
            RUN_STATS["openrouter_requests"] += 1
            r = requests.request("POST", url, headers=headers, data=json.dumps(payload), timeout=timeout)
            status = r.status_code
            if status == 429 or status == 408 or (500 <= status < 600):
                if attempt < retries:
                    retry_after = _http_retry_after_seconds(r.headers) if status == 429 else None
                    sleep_s = _http_backoff_seconds(backoff, attempt, retry_after=retry_after)
                    print(f"OpenRouter retryable status={status}; retry in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
            if status >= 400:
                RUN_STATS["openrouter_failures"] += 1
                if status == 429:
                    raise LlmError("rate_limited", f"OpenRouter status={status}")
                if 400 <= status < 500:
                    raise LlmError("http_4xx", f"OpenRouter status={status}")
                raise LlmError("http_5xx", f"OpenRouter status={status}")
            data = r.json()
            content = (((data.get("choices") or [])[0] or {}).get("message") or {}).get("content", "").strip()
            return parse_llm_json(content)
        except requests.RequestException as e:
            RUN_STATS["openrouter_failures"] += 1
            if attempt < retries:
                sleep_s = _http_backoff_seconds(backoff, attempt)
                print(f"OpenRouter exception: {e}; retry in {sleep_s:.1f}s")
                time.sleep(sleep_s)
                continue
            raise LlmError("timeout", str(e))
        except ValueError as e:
            RUN_STATS["openrouter_parse_errors"] += 1
            if attempt < retries:
                sleep_s = _http_backoff_seconds(backoff, attempt)
                print(f"OpenRouter parse error: {e}; retry in {sleep_s:.1f}s")
                time.sleep(sleep_s)
                continue
            raise LlmError("parse_error", str(e))


def load_llm_cache(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_llm_cache(cache: dict, path: str):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def llm_cache_key(it: dict, profile_cfg: dict) -> str:
    base = (it.get("work_id") or it.get("doi") or it.get("url") or it.get("title") or "").strip()
    if not base:
        return ""
    model = profile_cfg.get("openrouter_model", "")
    version = profile_cfg.get("llm_prompt_version", "")
    return f"{base}|{model}|{version}"


def parse_llm_json(content: str) -> dict:
    if not content:
        raise ValueError("empty llm content")
    raw = content.strip()
    try:
        data = json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("llm output not json")
        data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("llm output not dict")
    required = ["title_cn", "one_liner", "why_relevant", "key_takeaways", "caveats", "recommended_action"]
    for k in required:
        if k == "key_takeaways":
            v = data.get(k)
            if not isinstance(v, list) or not v:
                raise ValueError("llm json missing field: key_takeaways")
            continue
        v = (data.get(k) or "").strip() if isinstance(data.get(k), str) else ""
        if not v:
            raise ValueError(f"llm json missing field: {k}")
    return data


def format_llm_brief(brief: dict) -> str:
    if not isinstance(brief, dict):
        return ""
    if brief.get("source") == "abstract_fallback":
        summary = (brief.get("summary") or "").strip()
        note = "（摘要回退）"
        return f"{note}\n{summary}" if summary else f"{note}\nNo abstract available."
    parts = []
    mapping = [
        ("标题", "title_cn"),
        ("一句话", "one_liner"),
        ("为何相关", "why_relevant"),
        ("建议", "recommended_action"),
        ("注意", "caveats"),
    ]
    for label, key in mapping:
        val = (brief.get(key) or "").strip() if isinstance(brief.get(key), str) else ""
        if val:
            parts.append(f"{label}：{val}")
    takeaways = brief.get("key_takeaways")
    if isinstance(takeaways, list) and takeaways:
        joined = "；".join(str(x).strip() for x in takeaways if str(x).strip())
        if joined:
            parts.append(f"要点：{joined}")
    return "\n".join(parts)


def build_fallback_brief(it: dict, reason: str) -> dict:
    abstract = (it.get("abstract") or "").strip()
    if not abstract:
        abstract = "No abstract available."
    return {
        "title_cn": (it.get("title") or "").strip(),
        "one_liner": abstract,
        "why_relevant": "LLM brief failed; fallback to abstract.",
        "key_takeaways": [abstract],
        "caveats": "LLM brief failed; fallback to abstract.",
        "recommended_action": "Manual review recommended.",
        "summary": abstract,
        "source": "abstract_fallback",
        "failure_reason": reason,
    }


def item_identity(it: dict) -> str:
    return (it.get("work_id") or it.get("doi") or it.get("url") or it.get("title") or "")[:64]


def build_llm_prompt_cn(it: dict, prompt_version: str) -> list[dict]:
    title = (it.get("title") or "").strip()
    abstract = (it.get("abstract") or "").strip()
    venue = (it.get("venue") or "").strip()
    year = it.get("publication_year") or ""
    cites = it.get("cited_by_count") or 0
    doi = it.get("doi") or ""
    url = it.get("url") or ""
    pdf = it.get("pdf_url") or ""
    bucket = it.get("bucket") or ""

    sys = (
        "你是我的研究助理。"
        "请只基于我提供的论文元数据与摘要，生成中文科研简报。"
        "严禁编造论文中不存在的实验、指标、结论。"
        "若摘要信息不足，请明确写“信息不足/需读全文”。"
        "输出必须是严格 JSON，不要包含任何额外文本或 Markdown。"
    )

    user = f"""请为下列论文生成中文科研简报，输出严格 JSON，格式固定为：

{{
  "title_cn": "...",
  "one_liner": "...",
  "why_relevant": "...",
  "key_takeaways": ["...", "..."],
  "caveats": "...",
  "recommended_action": "..."
}}

说明：
- title_cn: 中文标题（可简化）
- why_relevant: 与主题/工程链路的相关性
- key_takeaways: 2-4 条要点
- caveats: 需注意的限制或信息不足
- recommended_action: 该论文值得如何处理（阅读/复现/收藏/忽略）

元数据：
- 标题：{title}
- 来源/期刊/会议：{venue}
- 年份：{year}
- 引用：{cites}
- DOI：{doi}
- 主页：{url}
- PDF：{pdf if pdf else "无"}
- 分类桶：{bucket}
- prompt_version：{prompt_version}

摘要：
{abstract if abstract else "（无摘要）"}
"""
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def llm_brief_cn(profile_cfg: dict, it: dict) -> dict:
    prompt_version = profile_cfg.get("llm_prompt_version", "")
    return openrouter_chat_json(profile_cfg, build_llm_prompt_cn(it, prompt_version))


def load_pending_store(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pending_store(pending: dict, path: str) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def pending_record(profile_cfg: dict, it: dict, reason: str, retry_count: int = 0) -> dict:
    today = dt.date.today().isoformat()
    return {
        "profile_cn": it.get("profile_cn") or profile_cfg.get("topic_cn") or "",
        "item": {
            "work_id": it.get("work_id") or "",
            "doi": it.get("doi") or "",
            "url": it.get("url") or "",
            "title": it.get("title") or "",
            "abstract": it.get("abstract") or "",
            "venue": it.get("venue") or "",
            "publication_year": it.get("publication_year"),
            "publication_date": it.get("publication_date"),
            "cited_by_count": it.get("cited_by_count") or 0,
            "bucket": it.get("bucket") or "",
            "pdf_url": it.get("pdf_url") or "",
        },
        "first_seen": today,
        "last_attempt": today,
        "last_error": reason,
        "retry_count": retry_count,
        "error_type": reason,
        "prompt_version": profile_cfg.get("llm_prompt_version", ""),
        "model": profile_cfg.get("openrouter_model", ""),
    }


def prune_pending_store(pending: dict, max_retries: int, max_days: int) -> dict:
    today = dt.date.today()
    kept = {}
    for k, rec in (pending or {}).items():
        try:
            first_seen = dt.date.fromisoformat(rec.get("first_seen") or today.isoformat())
        except Exception:
            first_seen = today
        if rec.get("retry_count", 0) >= max_retries:
            continue
        if (today - first_seen).days > max_days:
            continue
        kept[k] = rec
    return kept


def apply_llm_briefs(global_cfg_flat: dict, lists: list[list[dict]]) -> dict:
    stats = {"pending": 0, "ready": 0, "failed": 0, "fallback": 0}
    max_n = int(global_cfg_flat.get("llm_max_items_per_run", 18))
    cache_path = global_cfg_flat.get("llm_cache_file", "llm_cache.json")
    pending_path = global_cfg_flat.get("llm_pending_store", "data/pending_llm.json")
    max_concurrency = int(global_cfg_flat.get("llm_max_concurrency", 4))
    max_retries = int(global_cfg_flat.get("llm_pending_max_retries", 5))
    max_days = int(global_cfg_flat.get("llm_pending_max_days", 7))

    cache = load_llm_cache(cache_path)
    pending = prune_pending_store(load_pending_store(pending_path), max_retries, max_days)

    items_by_key: dict[str, dict] = {}
    for lst in lists:
        for it in lst:
            k = llm_cache_key(it, global_cfg_flat)
            if not k:
                continue
            items_by_key[k] = it
    if (os.getenv("STRICT_MODE", "") or "").strip().lower() in ("1", "true", "yes"):
        max_n = max(max_n, len(items_by_key))

    if not global_cfg_flat.get("use_llm_brief", False) or not openrouter_headers():
        reason = "llm_disabled_or_missing_key"
        print("LLM brief unavailable; all items queued for pending.")
        for k, it in items_by_key.items():
            it["llm_status"] = LLM_STATUS_READY
            it["llm_failure_reason"] = reason
            it["llm_failure_type"] = "llm_http_error"
            it["llm_brief"] = build_fallback_brief(it, "llm_http_error")
            it["brief_text"] = format_llm_brief(it["llm_brief"])
            it["brief_source"] = "abstract" if (it.get("abstract") or "").strip() else "title"
            it["brief_status"] = "fallback"
            stats["fallback"] += 1
        save_pending_store(pending, pending_path)
        return stats

    for k, it in items_by_key.items():
        cached = cache.get(k)
        if isinstance(cached, dict):
            it["llm_brief"] = cached
            it["llm_status"] = LLM_STATUS_READY
            it["brief_text"] = format_llm_brief(cached)
            it["brief_source"] = "llm"
            it["brief_status"] = "ok"
            stats["ready"] += 1

    def enqueue_from_pending() -> list[tuple[str, dict, dict]]:
        out = []
        for k, rec in pending.items():
            item = items_by_key.get(k) or dict(rec.get("item") or {})
            out.append((k, item, rec))
        return out

    def enqueue_new_items() -> list[tuple[str, dict, dict]]:
        out = []
        for k, it in items_by_key.items():
            if it.get("llm_status") == LLM_STATUS_READY:
                continue
            if k in pending:
                continue
            out.append((k, it, {}))
        return out

    queue = enqueue_from_pending() + enqueue_new_items()
    queue = queue[:max_n]
    print(f"LLM briefs: need_generate={len(queue)} max_per_run={max_n}")
    queued_keys = {k for k, _, _ in queue}
    for k, it in items_by_key.items():
        if it.get("llm_status") == LLM_STATUS_READY:
            continue
        if k in queued_keys:
            continue
        it["llm_status"] = LLM_STATUS_READY
        it["llm_failure_reason"] = "llm_deferred"
        it["llm_failure_type"] = "llm_deferred"
        it["llm_brief"] = build_fallback_brief(it, "llm_deferred")
        it["brief_text"] = format_llm_brief(it["llm_brief"])
        it["brief_source"] = "abstract" if (it.get("abstract") or "").strip() else "title"
        it["brief_status"] = "fallback"
        it["brief_error"] = "deferred_for_next_run"
        stats["fallback"] += 1

    def worker(k: str, it: dict) -> tuple[str, Optional[dict], Optional[str], Optional[str]]:
        try:
            brief = llm_brief_cn(global_cfg_flat, it)
            return k, brief, None, None
        except LlmError as e:
            return k, None, e.error_type, str(e)
        except Exception as e:
            return k, None, "unknown_error", str(e)

    futures = []
    with ThreadPoolExecutor(max_workers=max_concurrency) as ex:
        for k, it, _ in queue:
            futures.append(ex.submit(worker, k, it))
        for idx, fut in enumerate(as_completed(futures), 1):
            k, brief, err_type, err_msg = fut.result()
            it = items_by_key.get(k)
            if brief:
                cache[k] = brief
                if it is not None:
                    it["llm_brief"] = brief
                    it["llm_status"] = LLM_STATUS_READY
                    it["brief_text"] = format_llm_brief(brief)
                    it["brief_source"] = "llm"
                    it["brief_status"] = "ok"
                pending.pop(k, None)
                stats["ready"] += 1
                ident = item_identity(it or {})
                print(f"LLM briefs: ok {idx}/{len(futures)} key={k[:32]} id={ident}")
            else:
                if it is not None:
                    mapped = {
                        "timeout": "llm_timeout",
                        "rate_limited": "llm_http_error",
                        "http_4xx": "llm_http_error",
                        "http_5xx": "llm_http_error",
                        "parse_error": "llm_parse_error",
                        "unknown_error": "llm_unknown",
                    }.get(err_type or "unknown_error", "llm_unknown")
                    it["llm_status"] = LLM_STATUS_READY
                    it["llm_failure_reason"] = err_msg or mapped
                    it["llm_failure_type"] = mapped
                    it["llm_brief"] = build_fallback_brief(it, mapped)
                    it["brief_text"] = format_llm_brief(it["llm_brief"])
                    it["brief_source"] = "abstract" if (it.get("abstract") or "").strip() else "title"
                    it["brief_status"] = "fallback"
                    it["brief_error"] = err_msg or mapped
                prev = pending.get(k, {})
                retry_count = int(prev.get("retry_count", 0)) + 1
                pending.pop(k, None)
                stats["fallback"] += 1
                ident = item_identity(it or {})
                print(f"[WARN] LLM briefs: failed key={k[:32]} id={ident} type={err_type} err={err_msg} fallback=abstract")
                if (os.getenv("DEBUG", "") or "").strip():
                    print(f"[DEBUG] brief_fallback key={k[:32]} id={ident}")

    save_llm_cache(cache, cache_path)
    save_pending_store(pending, pending_path)
    return stats


def summarize_llm_items(items: list[dict]) -> tuple[int, int, dict, int]:
    ready = 0
    failed = 0
    fallback = 0
    reasons: dict[str, int] = {}
    for it in items or []:
        has_brief = bool((it.get("brief_text") or "").strip() or it.get("llm_brief"))
        if has_brief:
            ready += 1
            if it.get("brief_status") == "fallback" or it.get("brief_source") in ("abstract", "title"):
                fallback += 1
                reason = it.get("llm_failure_type") or it.get("llm_failure_reason") or "llm_unknown"
                reasons[reason] = reasons.get(reason, 0) + 1
        else:
            failed += 1
            reason = it.get("llm_failure_type") or it.get("llm_failure_reason") or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
    return ready, failed, reasons, fallback


# -------------------------
# Conflict detection (optional dual-track)
# -------------------------
STOPWORDS = {
    "the","and","or","for","with","from","into","via","using","use","based","study",
    "a","an","to","of","in","on","by","at","is","are","was","were","be","been","being",
    "method","methods","analysis","results","model","models","system","systems","paper",
    "approach","approaches","review","reviews","application","applications",
}
QUERY_STOPWORDS = STOPWORDS | {
    "this","that","have","has","had","without","within","including","include","including",
    "components","component","design","designs","performance","using","use","based",
}

def tokenize_en(text: str) -> list[str]:
    text = (text or "").lower()
    toks = re.findall(r"[a-z][a-z0-9\-]{2,}", text)
    out = []
    for t in toks:
        if t in STOPWORDS:
            continue
        if len(t) < 3:
            continue
        out.append(t)
    return out


def clean_search_query(raw: str, max_tokens: int = 12) -> Tuple[str, list[str]]:
    raw = (raw or "").strip()
    tokens = re.findall(r"[a-z][a-z0-9\-]{1,}", raw.lower())
    kept: list[str] = []
    seen = set()
    for t in tokens:
        if len(t) <= 2:
            continue
        if t in QUERY_STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        kept.append(t)
        if len(kept) >= max_tokens:
            break
    return " ".join(kept), kept


def set_profile_debug(profile_cfg: dict, key: str, value: str) -> None:
    dbg = profile_cfg.setdefault("debug", {})
    dbg[key] = value

def build_seed_query_from_works(seed_works: list[dict], max_terms: int = 12) -> str:
    freq: dict[str, int] = {}
    for w in seed_works or []:
        text = f"{w.get('title','')} {w.get('abstract','')}"
        for t in tokenize_en(text):
            freq[t] = freq.get(t, 0) + 1
    if not freq:
        return ""
    ranked = sorted(freq.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    terms = [t for t, _ in ranked[:max_terms]]
    return " ".join(terms)

def fetch_seed_works_brief(mailto: str, pos_path: Path, limit: int = 10) -> list[dict]:
    dois = load_seed_dois(pos_path)[:limit]
    out = []
    for doi in dois:
        w = openalex_find_work_by_doi(doi, mailto)
        time.sleep(POLITE_SLEEP_SEC)
        if not w:
            continue
        title = w.get("title") or ""
        abstract = reconstruct_abstract(w.get("abstract_inverted_index"))
        out.append({"doi": w.get("doi") or normalize_doi(doi), "title": title, "abstract": abstract})
    return out

def conflict_check(profile_cfg: dict, seed_works: list[dict]) -> Tuple[bool, dict]:
    kws = profile_cfg.get("keywords") or []
    if not kws or not seed_works:
        return False, {"reason": "no_keywords_or_no_seeds", "avg_rel": None, "hit_ratio": None}

    scores = []
    hit = 0
    for w in seed_works:
        s = relevance_score(w.get("title",""), w.get("abstract",""), kws)
        scores.append(s)
        if s >= int(profile_cfg.get("conflict_seed_hit_rel", 2)):
            hit += 1
    avg_rel = sum(scores) / max(1, len(scores))
    hit_ratio = hit / max(1, len(scores))

    avg_min = float(profile_cfg.get("conflict_seed_avg_rel_min", 1.2))
    hit_min = float(profile_cfg.get("conflict_seed_hit_ratio_min", 0.35))

    is_conflict = (avg_rel < avg_min) or (hit_ratio < hit_min)
    meta = {"avg_rel": avg_rel, "hit_ratio": hit_ratio, "avg_min": avg_min, "hit_min": hit_min}
    return is_conflict, meta


# -------------------------
# HTML builder (kept similar, uses it["llm_brief"] only)
# -------------------------
def build_html(
    profile_cfg: dict,
    latest: list[dict],
    classic: list[dict],
    reco_s2: list[dict],
    reco_oa: list[dict],
    pub_latest: list[dict],
    pub_classic: list[dict],
    pub_map: dict,
    graph_ref_classic: list[dict],
    graph_citedby_keyfollow: list[dict],
) -> str:
    date_str = now_local(profile_cfg["timezone"]).strftime("%Y-%m-%d (%a)")
    build_sha = (os.getenv("GITHUB_SHA", "") or "")[:7]
    run_id = os.getenv("GITHUB_RUN_ID", "")

    pub_lines = []
    for name, pid in (pub_map or {}).items():
        pub_lines.append(f"{name} ✓" if pid else f"{name} ✗")
    pub_status = " / ".join(pub_lines) if pub_lines else "（未配置 preferred_publishers）"
    debug_lines = []
    dbg = profile_cfg.get("debug") or {}
    if dbg.get("latest_status"):
        debug_lines.append(f"latest_status={dbg.get('latest_status')}")
    if dbg.get("pub_latest_status"):
        debug_lines.append(f"pub_latest_status={dbg.get('pub_latest_status')}")
    if dbg.get("seeds_track_status"):
        debug_lines.append(str(dbg.get("seeds_track_status")))
    if dbg.get("s2_auth"):
        debug_lines.append(f"s2_auth={dbg.get('s2_auth')}")
    debug_status = " / ".join(debug_lines)

    def tag_pill(text: str, tone: str = "neutral") -> str:
        bg = {"neutral": "#F3F4F6", "good": "#ECFDF3", "warn": "#FFF7ED"}.get(tone, "#F3F4F6")
        fg = {"neutral": "#374151", "good": "#166534", "warn": "#9A3412"}.get(tone, "#374151")
        bd = {"neutral": "#E5E7EB", "good": "#BBF7D0", "warn": "#FED7AA"}.get(tone, "#E5E7EB")
        return f"""
          <span style="
            display:inline-block;
            padding:2px 10px;
            border-radius:999px;
            border:1px solid {bd};
            background:{bg};
            color:{fg};
            font-size:12px;
            line-height:18px;
            margin-right:6px;
            white-space:nowrap;
          ">{text}</span>
        """

    def source_badge(it: dict) -> str:
        bucket = it.get("bucket")
        if bucket == "reco_s2":
            return tag_pill("S2猜你喜欢 · 官方", "good")
        if bucket == "reco_oa":
            return tag_pill("OpenAlex · related_works", "neutral")
        if bucket == "pub_latest":
            return tag_pill("出版商精选 · 最新", "good")
        if bucket == "pub_classic":
            return tag_pill("出版商精选 · 经典", "good")
        if bucket == "graph_ref_classic":
            return tag_pill("引用图谱 · 根论文", "warn")
        if bucket == "graph_citedby_keyfollow":
            return tag_pill("引用图谱 · 关键后续", "warn")
        if bucket == "latest":
            return tag_pill("关键词 · 最新", "neutral")
        if bucket == "classic":
            return tag_pill("关键词 · 经典", "neutral")
        return tag_pill(bucket or "未知来源", "neutral")

    def meta_line(it: dict) -> str:
        venue = (it.get("venue") or "Unknown venue").strip()
        year = it.get("publication_year") or ""
        cites = safe_int(it.get("cited_by_count", 0), 0)
        rel = safe_int(it.get("relevance", 0), 0)

        extra = []
        if it.get("publisher_hit"):
            extra.append(tag_pill("目标出版商", "good"))
        if it.get("is_in_doaj"):
            extra.append(tag_pill("DOAJ", "warn"))

        return f"""
          <div style="margin-top:8px;color:#6B7280;font-size:13px;line-height:18px;">
            <span>{venue}</span>
            <span style="margin:0 6px;">·</span>
            <span>{year}</span>
            <span style="margin:0 6px;">·</span>
            <span>引用 {cites}</span>
            <span style="margin:0 6px;">·</span>
            <span>relevance {rel}</span>
            <span style="margin-left:10px;">{''.join(extra)}</span>
          </div>
        """

    def action_links(it: dict) -> str:
        main_url = (it.get("url") or "").strip()
        pdf_url = (it.get("pdf_url") or "").strip()
        oa_landing = (it.get("oa_landing") or "").strip()

        links = []
        if pdf_url:
            links.append(f"""
              <a href="{pdf_url}" target="_blank" rel="noreferrer" style="
                display:inline-block;padding:6px 10px;border-radius:10px;border:1px solid #E5E7EB;
                background:#FFFFFF;color:#111827;text-decoration:none;font-size:13px;margin-right:8px;
              ">PDF</a>
            """)
        if oa_landing and oa_landing != main_url:
            links.append(f"""
              <a href="{oa_landing}" target="_blank" rel="noreferrer" style="
                display:inline-block;padding:6px 10px;border-radius:10px;border:1px solid #E5E7EB;
                background:#FFFFFF;color:#111827;text-decoration:none;font-size:13px;margin-right:8px;
              ">落地页</a>
            """)
        if main_url:
            links.append(f"""
              <a href="{main_url}" target="_blank" rel="noreferrer" style="
                display:inline-block;padding:6px 10px;border-radius:10px;border:1px solid #111827;
                background:#111827;color:#FFFFFF;text-decoration:none;font-size:13px;
              ">打开</a>
            """)
        return f"""<div style="margin-top:12px;">{''.join(links)}</div>"""

    def card(it: dict) -> str:
        title = (it.get("title") or "").strip()
        brief_src = (it.get("brief_text") or "").strip()
        if not brief_src:
            brief_src = format_llm_brief(it.get("llm_brief"))
        if not brief_src:
            abstract = (it.get("abstract") or "").strip()
            if abstract:
                brief_src = abstract[:420]
            elif title:
                brief_src = f"本文研究：{title}"
            else:
                return ""

        brief_html = (
            brief_src.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;")
                     .replace("\n", "<br>")
        )

        title_link = (it.get("url") or "").strip() or "#"

        return f"""
        <div style="margin:12px 0;padding:14px 16px;border:1px solid #E5E7EB;border-radius:14px;background:#FFFFFF;
                    box-shadow:0 1px 2px rgba(0,0,0,0.04);">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;">
            <div style="min-width:0;">
              <div style="margin-bottom:8px;">
                {source_badge(it)}
                {tag_pill("全文可得" if it.get("pdf_url") else "无全文", "good" if it.get("pdf_url") else "neutral")}
              </div>

              <div style="font-size:16px;font-weight:750;line-height:22px;color:#111827;">
                <a href="{title_link}" target="_blank" rel="noreferrer" style="color:#111827;text-decoration:none;">
                  {title if title else "（无标题）"}
                </a>
              </div>

              {meta_line(it)}
            </div>
          </div>

          <div style="margin-top:12px;padding:12px 12px;border-radius:12px;background:#F9FAFB;color:#111827;
                      font-size:14px;line-height:20px;">
            {brief_html}
          </div>

          {action_links(it)}
        </div>
        """

    def section(title: str, desc: str, items: list[dict], empty_text: str) -> str:
        if not items:
            return ""
        header = f"""
          <div style="margin-top:18px;margin-bottom:6px;">
            <div style="font-size:15px;font-weight:800;color:#111827;line-height:20px;">{title}</div>
            <div style="margin-top:4px;color:#6B7280;font-size:13px;line-height:18px;">{desc}</div>
          </div>
        """
        body = "".join(card(x) for x in items)
        return header + body

    top_stats = [
        tag_pill(f"推荐 {len(reco_s2) + len(reco_oa)}", "good"),
        tag_pill(f"出版商精选 {len(pub_latest) + len(pub_classic)}", "good"),
        tag_pill(f"图谱 {len(graph_ref_classic) + len(graph_citedby_keyfollow)}", "warn"),
        tag_pill(f"最新 {len(latest)}", "neutral"),
        tag_pill(f"经典 {len(classic)}", "neutral"),
    ]
    top_stats_html = "".join(top_stats)
    all_count = (
        len(latest) + len(classic) + len(reco_s2) + len(reco_oa) +
        len(pub_latest) + len(pub_classic) + len(graph_ref_classic) + len(graph_citedby_keyfollow)
    )
    empty_profile_block = """
      <div style="margin:12px 0 4px;padding:12px 14px;border:1px dashed #E5E7EB;border-radius:14px;color:#6B7280;
                  background:#FAFAFA;font-size:13px;">
        今日为空：没有抓到任何匹配论文。建议检查 seeds_positive.txt / search_query / keywords / latest_days 等配置。
      </div>
    """

    latest_empty_note = ""
    if not latest:
        dbg = profile_cfg.get("debug") or {}
        latest_window = dbg.get("latest_window_days") or profile_cfg.get("latest_days")
        steps = dbg.get("latest_backoff_steps") or []
        steps_str = "→".join(str(x) for x in steps if x)
        status = (dbg.get("latest_status") or "").strip()
        lines = []
        if steps_str:
            lines.append(f"已自动扩大时间窗至 {latest_window} 天仍为空。")
        if "fallback=client_side_publisher_filter" in status:
            lines.append("组合过滤疑似过严，已改用客户端 publisher 过滤。")
        elif "fallback=publisher_filter_skipped" in status or "fallback=date_filter_skipped" in status:
            lines.append("组合过滤疑似过严，已跳过 publisher 过滤。")
        elif "fallback=client_side_date_filter" in status:
            lines.append("组合过滤疑似过严，已改用客户端日期过滤。")
        if not lines:
            lines.append("当前检索窗口内未匹配到最新条目。")
        lines.append("建议：可在 seeds_positive 补充更具体关键词以提高命中。")
        latest_empty_note = f"""
        <div style="margin:10px 0 4px;padding:12px 14px;border:1px dashed #E5E7EB;border-radius:14px;color:#6B7280;
                    background:#FAFAFA;font-size:13px;line-height:18px;">
          { "<br>".join(lines) }
        </div>
        """

    sections_html = ""
    if all_count > 0:
        sections_html = f"""
        {section("⭐ S2猜你喜欢","更偏“你可能也喜欢”：由种子论文 + 正/负例偏好驱动。",
                 reco_s2,"今日没有产出（或被跳过/限流），不影响其它栏目。")}

        {section("🧭 OpenAlex 脉络扩展","沿种子论文 related_works 扩展：更像“同一簇文献”。",
                 reco_oa,"今日为空：请检查 seeds_positive.txt 的 DOI 是否有效，或调大 max_related_per_seed。")}

        {section("🏷️ 出版商精选 · 最新","只看目标出版商池（IEEE/Elsevier/Springer/Wiley）中的近期高价值工作。",
                 pub_latest,"今日为空：可能 publisher 解析失败、关键词过窄、或当天返回不足。")}

        {section("🏷️ 出版商精选 · 经典","只看目标出版商池中的“高引用经典/基础工作”。",
                 pub_classic,"今日为空：可能 classic 条件过严或引用阈值设置过高。")}

        {section("📚 引用图谱 · 经典根论文（references）","更像“地基/根论文/综述”：从 seeds 的参考文献向后追溯。",
                 graph_ref_classic,"今日为空：可能 seeds 数量不足、年限/最低引用阈值过严、或引用图谱抓取失败。")}

        {section("🛰️ 引用图谱 · 关键后续（cited-by）","更像“重要延展/路线分叉”：从 seeds 的被引文献向前追踪。",
                 graph_citedby_keyfollow,"今日为空：可能 follow_years 太短、最低引用过高，或 seeds 覆盖不足。")}

        {section(f"🆕 最新进展（全域 · 近 {profile_cfg['latest_days']} 天）",
                 "全域关键词检索：用于补齐图谱/出版商池没覆盖到的最新进展。",
                 latest,"")}
        {latest_empty_note}

        {section("🏛️ 经典/高影响力（全域）","全域关键词检索：用引用数主导补齐经典工作。",
                 classic,"今日未抓到足够匹配的经典条目。")}

        <div style="margin-top:16px;color:#9CA3AF;font-size:12px;line-height:18px;padding:0 2px;">
          提示：引用图谱栏目高度依赖 seeds 的质量；建议持续把你认可的“根论文/综述/标志性论文”补进 seeds_positive.txt。
        </div>
        """

    return f"""
    <html>
    <body style="margin:0;padding:0;background:#F5F5F4;">
      <div style="max-width:900px;margin:0 auto;padding:22px 14px;">
        <div style="padding:18px 18px;border:1px solid #E7E5E4;border-radius:16px;background:#FFFFFF;
                    box-shadow:0 1px 2px rgba(0,0,0,0.04);">
          <div style="font-size:18px;font-weight:900;color:#111827;line-height:24px;">
            {profile_cfg['topic_cn']} · 每日科研简报
          </div>
          <div style="margin-top:6px;color:#6B7280;font-size:13px;line-height:18px;">
            {date_str} · tz={profile_cfg["timezone"]} · sha={build_sha} · run={run_id}
          </div>

          <div style="margin-top:12px;">{top_stats_html}</div>

          <div style="margin-top:14px;color:#6B7280;font-size:12.5px;line-height:18px;">
            <div>数据源：OpenAlex（检索/引用图谱/related_works） + Semantic Scholar（S2）。</div>
            <div>出版商池：按 primary_location.source.host_organization 过滤，增强 IEEE / Elsevier / Springer / Wiley 覆盖。</div>
            <div>出版商识别：{pub_status}</div>
            {f"<div>调试：{debug_status}</div>" if debug_status else ""}
          </div>
        </div>

        <div style="margin-top:14px;"></div>

        {empty_profile_block if all_count == 0 else ""}
        {sections_html}
      </div>
    </body>
    </html>
    """


def strip_email_body(html: str) -> str:
    if not html:
        return ""
    m = re.search(r"<body[^>]*>(.*)</body>", html, flags=re.DOTALL | re.IGNORECASE)
    return (m.group(1).strip() if m else html)


# -------------------------
# Email
# -------------------------
def send_email(subject: str, html: str):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASS"]
    to_email = os.environ["TO_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(host, port) as s:
        s.ehlo()
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, [to_email], msg.as_string())


# -------------------------
# Main per-profile run
# -------------------------
@dataclass
class ProfileRunResult:
    profile_cfg: dict
    pub_map: dict
    conflict: bool
    conflict_meta: dict
    seed_query: str
    track_a: dict
    track_b: Optional[dict]
    seeds_side: dict  # reco_s2, reco_oa, graph_ref_classic, graph_citedby_keyfollow


def run_track(profile_cfg: dict, mailto: str, pub_id_set: set[str], pub_ids: list[str], seen: dict) -> dict:
    pub_latest_raw, pub_classic_raw = ([], [])
    if profile_cfg.get("enable_publisher_pools", True) and pub_ids:
        pub_latest_raw, pub_classic_raw = fetch_publisher_pools(profile_cfg, mailto, pub_ids)

    latest_raw, classic_raw = fetch_latest_and_classic(profile_cfg, mailto)

    if (os.getenv("DEBUG","") or "").strip():
        print(f"[{profile_cfg.get('topic_cn')}] OpenAlex raw: latest={len(latest_raw)} classic={len(classic_raw)}")
    
    latest_items = filter_seen(profile_cfg, dedupe(enrich(profile_cfg, latest_raw, "latest", publisher_id_set=pub_id_set)), seen)
    classic_items = filter_seen(profile_cfg, dedupe(enrich(profile_cfg, classic_raw, "classic", publisher_id_set=pub_id_set)), seen)

    if (os.getenv("DEBUG","") or "").strip():
        print(f"[{profile_cfg.get('topic_cn')}] after seen: latest_items={len(latest_items)} classic_items={len(classic_items)}")
    
    latest = pick_top(profile_cfg, latest_items, int(profile_cfg.get("top_latest", 5)))
    classic = pick_top(profile_cfg, classic_items, int(profile_cfg.get("top_classic", 2)))

    pub_latest_items = filter_seen(profile_cfg, dedupe(enrich(profile_cfg, pub_latest_raw, "pub_latest", publisher_id_set=pub_id_set)), seen)
    pub_classic_items = filter_seen(profile_cfg, dedupe(enrich(profile_cfg, pub_classic_raw, "pub_classic", publisher_id_set=pub_id_set)), seen)

    pub_latest = pick_top(profile_cfg, pub_latest_items, int(profile_cfg.get("top_pub_latest", 8)))
    pub_classic = pick_top_cited(pub_classic_items, int(profile_cfg.get("top_pub_classic", 8)))

    # Fulltext enrichment only for chosen
    attach_fulltext_links(profile_cfg, latest)
    attach_fulltext_links(profile_cfg, classic)
    attach_fulltext_links(profile_cfg, pub_latest)
    attach_fulltext_links(profile_cfg, pub_classic)

    return {"latest": latest, "classic": classic, "pub_latest": pub_latest, "pub_classic": pub_classic}


def run_profile(global_cfg_flat: dict, profile: dict, mailto: str, seen: dict) -> Optional[ProfileRunResult]:
    profile_cfg = flatten_profile_cfg(global_cfg_flat, profile)
    if not profile_cfg.get("enabled", True):
        return None

    pos_path, neg_path = get_seed_paths(profile_cfg)
    print(f"[{profile_cfg['topic_cn']}] cwd={Path.cwd()}")
    print(f"[{profile_cfg['topic_cn']}] SCRIPT_DIR={SCRIPT_DIR}")
    print(f"[{profile_cfg['topic_cn']}] pos_path={pos_path} exists={pos_path.exists()}")
    print(f"[{profile_cfg['topic_cn']}] neg_path={neg_path} exists={neg_path.exists()}")
    if pos_path.exists():
        print(f"[{profile_cfg['topic_cn']}] pos_size={pos_path.stat().st_size}")
    else:
        print(f"[INFO] [{profile_cfg['topic_cn']}] seeds_positive.txt missing; fallback to profile query.")
        pos_path.parent.mkdir(parents=True, exist_ok=True)
        pos_path.touch()
        print(f"[INFO] [{profile_cfg['topic_cn']}] created empty seeds_positive.txt")
    if not neg_path.exists():
        print(f"[INFO] [{profile_cfg['topic_cn']}] seeds_negative.txt missing; negative filter disabled.")
        neg_path.parent.mkdir(parents=True, exist_ok=True)
        neg_path.touch()
        print(f"[INFO] [{profile_cfg['topic_cn']}] created empty seeds_negative.txt")

    # Build seed_query + conflict detection
    seed_works = fetch_seed_works_brief(mailto, pos_path, limit=int(profile_cfg.get("seeds_query_max_seeds", 10)))
    if not seed_works:
        print(f"[INFO] [{profile_cfg['topic_cn']}] seeds_positive empty; seeds-based query disabled.")
        set_profile_debug(profile_cfg, "seeds_track_status", "seeds track disabled (empty seeds_positive)")
    seed_query = build_seed_query_from_works(seed_works, max_terms=int(profile_cfg.get("seeds_query_max_terms", 12)))

    if not (profile_cfg.get("search_query") or "").strip():
        fallback = seed_query or " ".join((profile_cfg.get("keywords") or [])[:8])
        profile_cfg["search_query"] = fallback
        print(f"[{profile_cfg['topic_cn']}] auto search_query: {profile_cfg['search_query'][:120]}")

    is_conflict, conflict_meta = conflict_check(profile_cfg, seed_works)
    enable_dual = bool(profile_cfg.get("enable_dual_track_on_conflict", True)) and is_conflict and bool(seed_query.strip())

    if is_conflict:
        print(f"[{profile_cfg['topic_cn']}] CONFLICT suspected: {conflict_meta} seed_query='{seed_query[:80]}'")

    # Publishers
    pub_names = profile_cfg.get("preferred_publishers", []) or []
    pub_map = resolve_publishers_openalex_ids(pub_names, mailto) if pub_names else {}
    pub_ids = [v for v in pub_map.values() if (v or "").startswith("https://openalex.org/P")]
    pub_id_set = set(pub_ids)

    # Track A
    track_a = run_track(profile_cfg, mailto, pub_id_set, pub_ids, seen)

    # Track B (optional)
    track_b = None
    if enable_dual:
        cfg_b = dict(profile_cfg)
        cfg_b["topic_cn"] = f"{profile_cfg['topic_cn']}（Track B: Seeds 自动 Query）"
        cfg_b["search_query"] = seed_query
        if profile_cfg.get("dual_track_use_seed_keywords", True) and seed_query:
            cfg_b["keywords"] = seed_query.split()[:20]
        track_b = run_track(cfg_b, mailto, pub_id_set, pub_ids, seen)

    # Seeds-side: reco + graph
    reco_oa_raw = fetch_recommendations_from_seeds(profile_cfg, mailto, pos_path, neg_path)
    reco_oa = pick_top_cited(
        filter_seen(profile_cfg, dedupe(enrich(profile_cfg, reco_oa_raw, "reco_oa", publisher_id_set=pub_id_set)), seen),
        int(profile_cfg.get("top_reco_oa", 10))
    )
    attach_fulltext_links(profile_cfg, reco_oa)

    refs_raw, citedby_raw = fetch_graph_buckets_from_seeds(profile_cfg, mailto, pos_path, neg_path)

    ref_items = filter_seen(profile_cfg, dedupe(enrich(profile_cfg, refs_raw, "graph_ref_classic", publisher_id_set=pub_id_set)), seen)
    years_ago = int(profile_cfg.get("graph_ref_classic_years_ago", 5))
    year_cut = dt.date.today().year - years_ago
    ref_items = [x for x in ref_items if (x.get("publication_year") or 9999) <= year_cut]
    min_cites = int(profile_cfg.get("graph_ref_min_cited_by", 30))
    ref_items = [x for x in ref_items if safe_int(x.get("cited_by_count", 0), 0) >= min_cites]
    graph_ref_classic = pick_top_cited(ref_items, int(profile_cfg.get("top_graph_ref_classic", 10)))
    attach_fulltext_links(profile_cfg, graph_ref_classic)

    cited_items = filter_seen(profile_cfg, dedupe(enrich(profile_cfg, citedby_raw, "graph_citedby_keyfollow", publisher_id_set=pub_id_set)), seen)
    follow_years = int(profile_cfg.get("graph_follow_years", 3))
    follow_cut = dt.date.today().year - follow_years
    cited_items = [x for x in cited_items if (x.get("publication_year") or 0) >= follow_cut]
    follow_min_cites = int(profile_cfg.get("graph_follow_min_cited_by", 10))
    cited_items = [x for x in cited_items if safe_int(x.get("cited_by_count", 0), 0) >= follow_min_cites]
    graph_citedby_keyfollow = pick_top(profile_cfg, cited_items, int(profile_cfg.get("top_graph_citedby_keyfollow", 10)))
    attach_fulltext_links(profile_cfg, graph_citedby_keyfollow)

    # S2 recs
    reco_s2_raw = fetch_s2_recommendations_from_seeds(profile_cfg, pos_path, neg_path)
    reco_s2 = pick_top_cited(
        filter_seen(profile_cfg, dedupe(enrich_s2(profile_cfg, reco_s2_raw, "reco_s2")), seen),
        int(profile_cfg.get("top_reco_s2", 10))
    )
    attach_fulltext_links(profile_cfg, reco_s2)

    seeds_side = {
        "reco_s2": reco_s2,
        "reco_oa": reco_oa,
        "graph_ref_classic": graph_ref_classic,
        "graph_citedby_keyfollow": graph_citedby_keyfollow,
    }

    return ProfileRunResult(
        profile_cfg=profile_cfg,
        pub_map=pub_map,
        conflict=is_conflict,
        conflict_meta=conflict_meta,
        seed_query=seed_query,
        track_a=track_a,
        track_b=track_b,
        seeds_side=seeds_side,
    )


def mark_seen(seen: dict, today_str: str, *lists: list[dict]):
    for lst in lists:
        for it in lst or []:
            k = it.get("doi") or it.get("url") or it.get("title")
            if k:
                seen[k] = today_str


# -------------------------
# Main
# -------------------------
def main():
    cfg_raw = load_config()
    cfg = flatten_global_cfg(cfg_raw)

    validate_config(cfg_raw, cfg)

    
    # === DEBUG (safe) ===
    dbg = (os.getenv("DEBUG", "") or "").strip()
    if dbg:
        oa_mailto = (os.getenv("OPENALEX_MAILTO") or "").strip()
        print(f"[DEBUG] OPENALEX_API_KEY={'set' if (os.getenv('OPENALEX_API_KEY') or '').strip() else 'missing'}")
        print(f"[DEBUG] OPENALEX_MAILTO={'(missing)' if not oa_mailto else oa_mailto}")
        print(f"[DEBUG] S2_API_KEY={'set' if (os.getenv('S2_API_KEY') or '').strip() else 'missing'}")


    
    # Minimal required config
    if "timezone" not in cfg or "send_hour_local" not in cfg:
        raise RuntimeError("config.yml missing timezone or send_hour_local")

    required_env = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "TO_EMAIL"]
    for k in required_env:
        if not (os.getenv(k) or "").strip():
            raise RuntimeError(f"Missing env var: {k}")

    global OPENALEX_ENABLED
    openalex_key = (os.getenv("OPENALEX_API_KEY") or "").strip()
    if not openalex_key:
        OPENALEX_ENABLED = False
        cfg["openalex_enabled"] = False
        print("OpenAlex 已禁用：缺少 OPENALEX_API_KEY；自 2026-02-13 起 OpenAlex 将要求 API key。")
        print("请参见 README.md: OpenAlex API key")
    else:
        cfg["openalex_enabled"] = True

    profiles = cfg_raw.get("profiles", [])
    if not profiles:
        raise RuntimeError("config.yml missing profiles (list).")

    if not should_send_now(cfg):
        print("Not sending now (local hour mismatch).")
        return

    mailto = (os.getenv("OPENALEX_MAILTO", "") or "").strip()
    seen = load_seen()
    today_str = dt.date.today().isoformat()

    # 1) run all profiles (collect results + all lists for LLM)
    results: list[ProfileRunResult] = []
    all_lists_for_llm: list[list[dict]] = []

    for idx, p in enumerate(profiles, 1):
        r = run_profile(cfg, p, mailto, seen)
        if not r:
            continue
        results.append(r)

        # collect lists for LLM
        all_lists_for_llm.extend([
            r.seeds_side["reco_s2"], r.seeds_side["reco_oa"],
            r.seeds_side["graph_ref_classic"], r.seeds_side["graph_citedby_keyfollow"],
            r.track_a["pub_latest"], r.track_a["pub_classic"], r.track_a["latest"], r.track_a["classic"],
        ])
        if r.track_b:
            all_lists_for_llm.extend([r.track_b["pub_latest"], r.track_b["pub_classic"], r.track_b["latest"], r.track_b["classic"]])

        print(f"[{idx}/{len(profiles)}] prepared profile={r.profile_cfg['topic_cn']} dual={bool(r.track_b)}")

    if not results:
        print("No enabled profiles produced output; skip sending.")
        return

    # 2) Apply LLM briefs BEFORE rendering HTML
    apply_llm_briefs(cfg, all_lists_for_llm)

    strict_mode = (os.getenv("STRICT_MODE", "") or "").strip().lower() in ("1", "true", "yes")

    # 3) Render HTML blocks + mark seen
    all_profile_blocks = []
    pending_total = 0
    profile_summaries = []
    total_reasons: dict[str, int] = {}
    for r in results:
        profile_cfg = r.profile_cfg

        conflict_banner = ""
        if r.conflict and profile_cfg.get("enable_dual_track_on_conflict", True):
            try:
                conflict_banner = f"""
                <div style="margin:10px 0 0;padding:10px 12px;border-radius:12px;border:1px solid #FCD34D;
                            background:#FFFBEB;color:#92400E;font-size:13px;line-height:18px;">
                  <b>⚠️ 主题冲突疑似：</b>
                  seeds 与本 profile 的关键词/查询不一致（avg_rel={r.conflict_meta.get('avg_rel'):.2f}, hit_ratio={r.conflict_meta.get('hit_ratio'):.2f}）。
                  已启用双轨合并展示（profile query + seeds 自动 query）。
                  <div style="margin-top:6px;"><b>Seed 自动 query:</b> {r.seed_query[:240] if r.seed_query else "（生成失败）"}</div>
                </div>
                """
            except Exception:
                pass

        merged_latest = r.track_a["latest"]
        merged_classic = r.track_a["classic"]
        merged_pub_latest = r.track_a["pub_latest"]
        merged_pub_classic = r.track_a["pub_classic"]
        if r.track_b:
            merged_latest = merge_bucket(profile_cfg, "latest", r.track_a["latest"], r.track_b["latest"])
            merged_classic = merge_bucket(profile_cfg, "classic", r.track_a["classic"], r.track_b["classic"])
            merged_pub_latest = merge_bucket(profile_cfg, "pub_latest", r.track_a["pub_latest"], r.track_b["pub_latest"])
            merged_pub_classic = merge_bucket(profile_cfg, "pub_classic", r.track_a["pub_classic"], r.track_b["pub_classic"])
            print(f"[{profile_cfg['topic_cn']}] merge tracks: latest A={len(r.track_a['latest'])} B={len(r.track_b['latest'])} -> {len(merged_latest)}")
            print(f"[{profile_cfg['topic_cn']}] merge tracks: classic A={len(r.track_a['classic'])} B={len(r.track_b['classic'])} -> {len(merged_classic)}")
            print(f"[{profile_cfg['topic_cn']}] merge tracks: pub_latest A={len(r.track_a['pub_latest'])} B={len(r.track_b['pub_latest'])} -> {len(merged_pub_latest)}")
            print(f"[{profile_cfg['topic_cn']}] merge tracks: pub_classic A={len(r.track_a['pub_classic'])} B={len(r.track_b['pub_classic'])} -> {len(merged_pub_classic)}")

        lists_for_stats = [
            merged_latest, merged_classic, merged_pub_latest, merged_pub_classic,
            r.seeds_side["reco_s2"], r.seeds_side["reco_oa"],
            r.seeds_side["graph_ref_classic"], r.seeds_side["graph_citedby_keyfollow"],
        ]
        profile_ready = 0
        profile_failed = 0
        profile_fallback = 0
        profile_reasons: dict[str, int] = {}
        for lst in lists_for_stats:
            ready, failed, reasons, fallback = summarize_llm_items(lst)
            profile_ready += ready
            profile_failed += failed
            profile_fallback += fallback
            for k, v in reasons.items():
                profile_reasons[k] = profile_reasons.get(k, 0) + v
                total_reasons[k] = total_reasons.get(k, 0) + v
        pending_total += profile_failed
        profile_summaries.append({
            "profile": profile_cfg.get("topic_cn") or "",
            "ready": profile_ready,
            "failed": profile_failed,
            "fallback": profile_fallback,
            "reasons": profile_reasons,
        })

        html_a = build_html(
            profile_cfg,
            merged_latest, merged_classic,
            r.seeds_side["reco_s2"], r.seeds_side["reco_oa"],
            merged_pub_latest, merged_pub_classic,
            r.pub_map,
            r.seeds_side["graph_ref_classic"], r.seeds_side["graph_citedby_keyfollow"]
        )
        body_a = strip_email_body(html_a)

        profile_block = f"""
        <div style="margin:0 0 18px 0;">
          <div style="max-width:900px;margin:0 auto;">{conflict_banner}</div>
          {body_a}
        </div>
        """
        all_profile_blocks.append(profile_block)

        # mark seen for displayed items
        mark_seen(
            seen, today_str,
            merged_latest, merged_classic, merged_pub_latest, merged_pub_classic,
            r.seeds_side["reco_s2"], r.seeds_side["reco_oa"], r.seeds_side["graph_ref_classic"], r.seeds_side["graph_citedby_keyfollow"],
        )

    merged_body = "\n".join(all_profile_blocks)
    date_str = now_local(cfg["timezone"]).strftime("%Y-%m-%d (%a)")
    build_sha = (os.getenv("GITHUB_SHA", "") or "")[:7]
    run_id = os.getenv("GITHUB_RUN_ID", "")

    summary_lines = [
        f"profiles={len(results)} ready={sum(p['ready'] for p in profile_summaries)} "
        f"fallback={sum(p['fallback'] for p in profile_summaries)} failed={pending_total}"
    ]
    if total_reasons:
        summary_lines.append("reasons=" + ", ".join(f"{k}:{v}" for k, v in total_reasons.items()))
    summary_lines.append(
        f"openalex_req={RUN_STATS['openalex_requests']} openalex_fail={RUN_STATS['openalex_failures']} "
        f"openrouter_req={RUN_STATS['openrouter_requests']} openrouter_fail={RUN_STATS['openrouter_failures']} "
        f"parse_err={RUN_STATS['openrouter_parse_errors']}"
    )
    run_summary = " | ".join(summary_lines)
    print(f"[SUMMARY] {run_summary}")
    for ps in profile_summaries:
        print(f"[SUMMARY] profile={ps['profile']} ready={ps['ready']} fallback={ps['fallback']} failed={ps['failed']} reasons={ps['reasons']}")

    summary_html = f"""
      <div style="margin-top:10px;color:#6B7280;font-size:12.5px;line-height:18px;">
        <div><b>本期摘要：</b>{run_summary}</div>
      </div>
    """

    merged_html = f"""
    <html>
    <body style="margin:0;padding:0;background:#F5F5F4;">
      <div style="max-width:920px;margin:0 auto;padding:18px 12px;">
        <div style="padding:16px 16px;border:1px solid #E7E5E4;border-radius:16px;background:#FFFFFF;
                    box-shadow:0 1px 2px rgba(0,0,0,0.04);">
          <div style="font-size:18px;font-weight:900;color:#111827;line-height:24px;">
            多主题 · 每日科研简报
          </div>
          <div style="margin-top:6px;color:#6B7280;font-size:13px;line-height:18px;">
            {date_str} · tz={cfg["timezone"]} · sha={build_sha} · run={run_id} · topics={len(results)}
          </div>
          <div style="margin-top:10px;color:#6B7280;font-size:12.5px;line-height:18px;">
            本邮件按 profiles 分区汇总。若某主题触发冲突检测，将合并 profile query 与 seeds 自动 query 的结果展示。
          </div>
          {summary_html}
          {("<div style='margin-top:8px;color:#9A3412;font-size:12.5px;'>"
            "OpenAlex 已禁用：缺少 OPENALEX_API_KEY；自 2026-02-13 起 OpenAlex 将要求 API key。"
            " 见 README.md: OpenAlex API key</div>") if not cfg.get("openalex_enabled", True) else ""}
        </div>

        <div style="margin-top:14px;"></div>

        {merged_body}

        <div style="margin-top:16px;color:#9CA3AF;font-size:12px;line-height:18px;padding:0 2px;">
          提示：冲突检测是启发式；建议你把 seeds 放在对应 profile 下，避免跨主题混用。
          {f"<div>本期有 {pending_total} 条因生成失败已延后，下期优先补齐。</div>" if pending_total > 0 else ""}
        </div>
      </div>
    </body>
    </html>
    """

    subject = f"[每日科研简报] 多主题({len(results)}) | {now_local(cfg['timezone']).strftime('%Y-%m-%d')}"
    send_email(subject, merged_html)

    save_seen(seen)
    print(f"seen saved: {len(seen)}")
    print("Email sent.")

def mask_tail4(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "(missing)"
    # 只显示末4位，其余用 * 掩码
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]

    

if __name__ == "__main__":
    if "--selftest-http" in sys.argv:
        selftest_http_retry()
        raise SystemExit(0)
    main()
