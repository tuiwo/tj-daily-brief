import os
import re
import time
import json
import smtplib
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

# OpenAlex paging: per-page is 1..200
OPENALEX_PER_PAGE_MIN = 1
OPENALEX_PER_PAGE_MAX = 200

# OpenAlex filter OR: max 100 values in one request
OPENALEX_FILTER_OR_MAX = 100

# Polite throttling (avoid 429)
POLITE_SLEEP_SEC = 0.12


# -------------------------
# Small utils
# -------------------------
def resolve_path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (SCRIPT_DIR / p)


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

            if r.status_code in retry_on_status or (500 <= r.status_code < 600):
                if attempt < retries:
                    sleep_s = backoff_sec * (2 ** attempt)
                    print(f"{method} {url} retryable status={r.status_code}; retry in {sleep_s}s")
                    time.sleep(sleep_s)
                    continue
                r.raise_for_status()

            r.raise_for_status()
            return r.json()

        except Exception as e:
            if attempt < retries:
                sleep_s = backoff_sec * (2 ** attempt)
                print(f"{method} {url} exception: {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
            raise

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

def openalex_get(params: dict, mailto: str = "") -> dict:
    params = openalex_apply_auth(params, mailto=mailto)
    return http_request_json("GET", "https://api.openalex.org/works", params=params, timeout=60, retries=3, backoff_sec=3)


def openalex_get_work_by_id(openalex_id: str, mailto: str = "") -> Optional[dict]:
    if not openalex_id:
        return None
    oid = openalex_id.strip()
    if oid.startswith("https://openalex.org/"):
        work_id = oid.split("/")[-1]
    else:
        work_id = oid
    url = f"https://api.openalex.org/works/{work_id}"

    params = openalex_apply_auth({}, mailto=mailto)

    try:
        return http_request_json("GET", url, params=params, timeout=60, retries=2, backoff_sec=2)
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
    query = profile_cfg.get("search_query") or " ".join((profile_cfg.get("keywords") or [])[:6])

    today = dt.date.today()
    from_date = (today - dt.timedelta(days=int(profile_cfg["latest_days"]))).isoformat()

    # classic cutoff: default 2 years ago (can be adjusted if you want)
    classic_to = (today - dt.timedelta(days=365 * 2)).isoformat()

    common_filter = "type:journal-article|proceedings-article"

    per_page = clamp_int(profile_cfg.get("openalex_per_page", 200), OPENALEX_PER_PAGE_MIN, OPENALEX_PER_PAGE_MAX, 200)
    base = {"search": query, "per_page": per_page}
    if mailto:
        base["mailto"] = mailto

    latest = openalex_get({
        **base,
        "filter": f"from_publication_date:{from_date},{common_filter}",
        "sort": "publication_date:desc",
    } ,mailto=mailto).get("results", [])

    classic = openalex_get({
        **base,
        "filter": f"to_publication_date:{classic_to},{common_filter}",
        "sort": "cited_by_count:desc",
    },mailto=mailto).get("results", [])

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

    query = profile_cfg.get("search_query") or " ".join((profile_cfg.get("keywords") or [])[:6])

    today = dt.date.today()
    from_date = (today - dt.timedelta(days=int(profile_cfg["latest_days"]))).isoformat()
    classic_to = (today - dt.timedelta(days=365 * 2)).isoformat()
    common_filter = "type:journal-article|proceedings-article"

    per_page = clamp_int(profile_cfg.get("openalex_per_page", 200), OPENALEX_PER_PAGE_MIN, OPENALEX_PER_PAGE_MAX, 200)
    base = {"search": query, "per_page": per_page}
    if mailto:
        base["mailto"] = mailto

    pubs_or = "|".join(publisher_ids)

    pub_latest = openalex_get({
        **base,
        "filter": f"from_publication_date:{from_date},primary_location.source.host_organization:{pubs_or},{common_filter}",
        "sort": "cited_by_count:desc",
    }, mailto=mailto).get("results", [])

    pub_classic = openalex_get({
        **base,
        "filter": f"to_publication_date:{classic_to},primary_location.source.host_organization:{pubs_or},{common_filter}",
        "sort": "cited_by_count:desc",
    }, mailto=mailto).get("results", [])

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
# Semantic Scholar / AI4Scholar recs
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


def ai4s_headers() -> Optional[dict]:
    key = (os.getenv("AI4SCHOLAR_API_KEY") or "").strip()
    if not key:
        return None
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def fetch_ai4s_recommendations_from_seeds(profile_cfg: dict, pos_path: Path, neg_path: Path) -> Tuple[bool, list[dict]]:
    headers = ai4s_headers()
    if not headers:
        return (False, [])

    pos = load_seed_dois(pos_path)
    neg = load_seed_dois(neg_path)
    positive = [doi_to_s2_pid(d) for d in pos if doi_to_s2_pid(d)]
    negative = [doi_to_s2_pid(d) for d in neg if doi_to_s2_pid(d)]
    if not positive:
        return (True, [])

    url = "https://ai4scholar.net/recommendations/v1/papers/"
    params = {
        "fields": "title,abstract,year,citationCount,venue,externalIds,url",
        "limit": int(profile_cfg.get("s2_limit", 20)),
    }
    payload = {"positivePaperIds": positive, "negativePaperIds": negative}

    retries = int(profile_cfg.get("s2_retries", 2))
    base_backoff = int(profile_cfg.get("s2_backoff_sec", 3))

    for attempt in range(retries + 1):
        try:
            data = http_request_json(
                "POST",
                url,
                params=params,
                headers=headers,
                data=json.dumps(payload),
                timeout=60,
                retries=0,  # manual retry below
            )
            recs = data.get("recommendedPapers", None)
            if recs is None:
                recs = data.get("data", []) or []

            for p in recs:
                if isinstance(p, dict):
                    p["_via"] = "ai4scholar"
            return (True, recs)

        except requests.HTTPError as e:
            msg = str(e)
            # retry on 429/5xx handled by caller? Here we do simple backoff by status in text.
            if attempt < retries:
                sleep_s = base_backoff * (2 ** attempt)
                print(f"AI4S: http error {msg}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
            print(f"AI4S: failed; fallback to official S2. err={msg}")
            return (False, [])

        except Exception as e:
            if attempt < retries:
                sleep_s = base_backoff * (2 ** attempt)
                print(f"AI4S: exception {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
            print(f"AI4S: exception {e}; fallback to official S2.")
            return (False, [])

    return (False, [])


def fetch_s2_recommendations_from_seeds(profile_cfg: dict, pos_path: Path, neg_path: Path) -> list[dict]:
    if not profile_cfg.get("use_s2_recommendations", True):
        return []

    # Prefer AI4Scholar if key available
    ok, recs = fetch_ai4s_recommendations_from_seeds(profile_cfg, pos_path, neg_path)
    if ok:
        print(f"AI4S used, recs={len(recs)} (profile={profile_cfg.get('topic_cn')})")
        return recs

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

    for attempt in range(retries + 1):
        try:
            data = http_request_json(
                "POST",
                url,
                params=params,
                headers=s2_headers(),
                data=json.dumps(payload),
                timeout=60,
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
            print(f"S2 exception: {e}; skipping.")
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


def openrouter_chat(profile_cfg: dict, messages: list[dict]) -> str:
    headers = openrouter_headers()
    if not headers:
        raise RuntimeError("OPENROUTER_API_KEY missing")

    url = "https://openrouter.ai/api/v1/chat/completions"
    payload = {
        "model": profile_cfg.get("openrouter_model", "z-ai/glm-4.5-air:free"),
        "messages": messages,
        "temperature": float(profile_cfg.get("llm_temperature", 0.2)),
        "max_tokens": int(profile_cfg.get("llm_max_tokens", 520)),
        "stream": False,
    }

    retries = int(profile_cfg.get("llm_retries", 2))
    backoff = int(profile_cfg.get("llm_backoff_sec", 3))
    timeout = int(profile_cfg.get("llm_timeout_sec", 60))

    for attempt in range(retries + 1):
        try:
            data = http_request_json(
                "POST",
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=timeout,
                retries=0,
            )
            return (((data.get("choices") or [])[0] or {}).get("message") or {}).get("content", "").strip()
        except Exception as e:
            if attempt < retries:
                sleep_s = backoff * (2 ** attempt)
                print(f"OpenRouter exception: {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
            raise


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


def llm_cache_key(it: dict) -> str:
    return (it.get("doi") or it.get("url") or it.get("title") or "").strip()


def build_llm_prompt_cn(it: dict) -> list[dict]:
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
        "请只基于我提供的论文元数据与摘要，生成“中文科研简报”。"
        "严禁编造论文中不存在的实验、指标、结论。"
        "若摘要信息不足，请明确写“信息不足/需读全文”。"
        "风格：像人写的科研简报，讲人话但保持专业。"
    )

    user = f"""请为下列论文生成中文科研简报（不超过 180~260 中文字），格式固定为：

【一句话】……
【做了什么】……
【怎么做】……
【结果/贡献】……
【局限/注意】……
【我该怎么用】（结合“结温在线监测/估算”工程链路给一个建议）

元数据：
- 标题：{title}
- 来源/期刊/会议：{venue}
- 年份：{year}
- 引用：{cites}
- DOI：{doi}
- 主页：{url}
- PDF：{pdf if pdf else "无"}
- 分类桶：{bucket}

摘要：
{abstract if abstract else "（无摘要）"}
"""
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def llm_brief_cn(profile_cfg: dict, it: dict) -> str:
    return openrouter_chat(profile_cfg, build_llm_prompt_cn(it))


def apply_llm_briefs(global_cfg_flat: dict, lists: list[list[dict]]) -> None:
    if not global_cfg_flat.get("use_llm_brief", False):
        return

    if not openrouter_headers():
        print("LLM brief enabled but OPENROUTER_API_KEY missing; fallback to rule-based briefs.")
        return

    max_n = int(global_cfg_flat.get("llm_max_items_per_run", 18))
    cache_path = global_cfg_flat.get("llm_cache_file", "llm_cache.json")
    cache = load_llm_cache(cache_path)

    pool = []
    for lst in lists:
        for it in lst:
            k = llm_cache_key(it)
            if not k:
                continue
            if it.get("brief_cn"):
                continue
            if k in cache and (cache[k] or "").strip():
                it["brief_cn"] = cache[k]
                continue

            cites = safe_int(it.get("cited_by_count", 0), 0)
            rel = safe_int(it.get("relevance", 0), 0)
            score = (int(cites ** 0.5) * 10) + (rel * 8)
            pool.append((score, it))

    pool.sort(key=lambda x: x[0], reverse=True)
    picked = [it for _, it in pool[:max_n]]

    print(f"LLM briefs: need_generate={len(picked)} max_per_run={max_n}")

    for idx, it in enumerate(picked, 1):
        k = llm_cache_key(it)
        try:
            brief = llm_brief_cn(global_cfg_flat, it)
            if not brief.strip():
                brief = human_brief_cn(it.get("title", ""), it.get("abstract", ""))
            it["brief_cn"] = brief
            cache[k] = brief
            print(f"LLM briefs: ok {idx}/{len(picked)} key={k[:32]}")
        except Exception as e:
            print(f"LLM briefs: failed key={k[:32]} err={e}; fallback to rule-based")
            it["brief_cn"] = human_brief_cn(it.get("title", ""), it.get("abstract", ""))

        time.sleep(0.25)

    save_llm_cache(cache, cache_path)


# -------------------------
# Conflict detection (optional dual-track)
# -------------------------
STOPWORDS = {
    "the","and","or","for","with","from","into","via","using","use","based","study",
    "a","an","to","of","in","on","by","at","is","are","was","were","be","been","being",
    "method","methods","analysis","results","model","models","system","systems","paper",
    "approach","approaches","review","reviews","application","applications",
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
# HTML builder (kept similar, uses it["brief_cn"] if exists)
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
            via = it.get("via", "official_s2")
            return tag_pill("S2猜你喜欢 · AI4Scholar" if via == "ai4scholar" else "S2猜你喜欢 · 官方", "good")
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
        abstract = (it.get("abstract") or "").strip()

        brief_src = (it.get("brief_cn") or "").strip()
        if not brief_src:
            brief_src = human_brief_cn(title, abstract)

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
        header = f"""
          <div style="margin-top:18px;margin-bottom:6px;">
            <div style="font-size:15px;font-weight:800;color:#111827;line-height:20px;">{title}</div>
            <div style="margin-top:4px;color:#6B7280;font-size:13px;line-height:18px;">{desc}</div>
          </div>
        """
        body = "".join(card(x) for x in items) if items else f"""
          <div style="margin:10px 0 4px;padding:12px 14px;border:1px dashed #E5E7EB;border-radius:14px;color:#6B7280;
                      background:#FAFAFA;font-size:13px;">{empty_text}</div>
        """
        return header + body

    top_stats = [
        tag_pill(f"推荐 {len(reco_s2) + len(reco_oa)}", "good"),
        tag_pill(f"出版商精选 {len(pub_latest) + len(pub_classic)}", "good"),
        tag_pill(f"图谱 {len(graph_ref_classic) + len(graph_citedby_keyfollow)}", "warn"),
        tag_pill(f"最新 {len(latest)}", "neutral"),
        tag_pill(f"经典 {len(classic)}", "neutral"),
    ]
    top_stats_html = "".join(top_stats)

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
            <div>数据源：OpenAlex（检索/引用图谱/related_works） + Semantic Scholar（或 AI4Scholar 代理）。</div>
            <div>出版商池：按 primary_location.source.host_organization 过滤，增强 IEEE / Elsevier / Springer / Wiley 覆盖。</div>
            <div>出版商识别：{pub_status}</div>
          </div>
        </div>

        <div style="margin-top:14px;"></div>

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
                 latest,"今日未抓到足够匹配的最新条目。")}

        {section("🏛️ 经典/高影响力（全域）","全域关键词检索：用引用数主导补齐经典工作。",
                 classic,"今日未抓到足够匹配的经典条目。")}

        <div style="margin-top:16px;color:#9CA3AF;font-size:12px;line-height:18px;padding:0 2px;">
          提示：引用图谱栏目高度依赖 seeds 的质量；建议持续把你认可的“根论文/综述/标志性论文”补进 seeds_positive.txt。
        </div>
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

    # Build seed_query + conflict detection
    seed_works = fetch_seed_works_brief(mailto, pos_path, limit=int(profile_cfg.get("seeds_query_max_seeds", 10)))
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


    
    # === DEBUG (safe) ===
    dbg = (os.getenv("DEBUG", "") or "").strip()
    if dbg:
        oa_key = (os.getenv("OPENALEX_API_KEY") or "").strip()
        oa_mailto = (os.getenv("OPENALEX_MAILTO") or "").strip()
        s2_key = (os.getenv("S2_API_KEY") or "").strip()
        ai4s_key = (os.getenv("AI4SCHOLAR_API_KEY") or "").strip()

        print(f"[DEBUG] OPENALEX_API_KEY={mask_tail4(oa_key)}")
        print(f"[DEBUG] OPENALEX_MAILTO={'(missing)' if not oa_mailto else oa_mailto}")
        print(f"[DEBUG] S2_API_KEY={mask_tail4(s2_key)}")
        print(f"[DEBUG] AI4SCHOLAR_API_KEY={mask_tail4(ai4s_key)}")

        # （可选）用 /rate-limit 验证 OpenAlex key 真能用
        # OpenAlex 文档：GET /rate-limit?api_key=...  [oai_citation:1‡docs.openalex.org](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication?utm_source=chatgpt.com)
        try:
            if oa_key:
                data = http_request_json(
                    "GET",
                    "https://api.openalex.org/rate-limit",
                    params=openalex_apply_auth({}, mailto=oa_mailto),
                    timeout=20,
                    retries=1,
                    backoff_sec=1,
                )
                rl = (data or {}).get("rate_limit", {}) or {}
                print(f"[DEBUG] OpenAlex credits_remaining={rl.get('credits_remaining')} "
                      f"credits_limit={rl.get('credits_limit')} resets_in_seconds={rl.get('resets_in_seconds')}")
            else:
                print("[DEBUG] OpenAlex /rate-limit skipped (missing api key)")
        except Exception as e:
            print(f"[DEBUG] OpenAlex /rate-limit failed: {e}")


    
    # Minimal required config
    if "timezone" not in cfg or "send_hour_local" not in cfg:
        raise RuntimeError("config.yml missing timezone or send_hour_local")

    required_env = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "TO_EMAIL"]
    for k in required_env:
        if not (os.getenv(k) or "").strip():
            raise RuntimeError(f"Missing env var: {k}")

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

    # 3) Render HTML blocks + mark seen
    all_profile_blocks = []
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
                  已启用双轨展示（Track A=profile query；Track B=seeds 自动 query）。
                  <div style="margin-top:6px;"><b>Seed 自动 query:</b> {r.seed_query[:240] if r.seed_query else "（生成失败）"}</div>
                </div>
                """
            except Exception:
                pass

        html_a = build_html(
            profile_cfg,
            r.track_a["latest"], r.track_a["classic"],
            r.seeds_side["reco_s2"], r.seeds_side["reco_oa"],
            r.track_a["pub_latest"], r.track_a["pub_classic"],
            r.pub_map,
            r.seeds_side["graph_ref_classic"], r.seeds_side["graph_citedby_keyfollow"]
        )
        body_a = strip_email_body(html_a)

        body_b = ""
        if r.track_b:
            cfg_b = dict(profile_cfg)
            cfg_b["topic_cn"] = f"{profile_cfg['topic_cn']}（Track B: Seeds 自动 Query）"
            html_b = build_html(
                cfg_b,
                r.track_b["latest"], r.track_b["classic"],
                [], [],
                r.track_b["pub_latest"], r.track_b["pub_classic"],
                r.pub_map,
                [], []
            )
            body_b = strip_email_body(html_b)

        profile_block = f"""
        <div style="margin:0 0 18px 0;">
          <div style="max-width:900px;margin:0 auto;">{conflict_banner}</div>
          {body_a}
          {body_b}
        </div>
        """
        all_profile_blocks.append(profile_block)

        # mark seen for displayed items
        mark_seen(
            seen, today_str,
            r.track_a["latest"], r.track_a["classic"], r.track_a["pub_latest"], r.track_a["pub_classic"],
            r.seeds_side["reco_s2"], r.seeds_side["reco_oa"], r.seeds_side["graph_ref_classic"], r.seeds_side["graph_citedby_keyfollow"],
        )
        if r.track_b:
            mark_seen(seen, today_str, r.track_b["latest"], r.track_b["classic"], r.track_b["pub_latest"], r.track_b["pub_classic"])

    merged_body = "\n".join(all_profile_blocks)
    date_str = now_local(cfg["timezone"]).strftime("%Y-%m-%d (%a)")
    build_sha = (os.getenv("GITHUB_SHA", "") or "")[:7]
    run_id = os.getenv("GITHUB_RUN_ID", "")

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
            本邮件按 profiles 分区汇总。若某主题触发冲突检测，将展示 Track A/Track B 两套检索结果作为兜底。
          </div>
        </div>

        <div style="margin-top:14px;"></div>

        {merged_body}

        <div style="margin-top:16px;color:#9CA3AF;font-size:12px;line-height:18px;padding:0 2px;">
          提示：冲突检测是启发式；建议你把 seeds 放在对应 profile 下，避免跨主题混用。
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
    main()
