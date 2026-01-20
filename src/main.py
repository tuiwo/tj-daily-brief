import os
import re
import time
import math
import smtplib
import datetime as dt
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json

import requests
import yaml


# -------------------------
# 基础：读取 config
# -------------------------
def load_config(path="config.yml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def now_local(tz: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(tz))


# def should_send_now(cfg) -> bool:
#     # 只在本地指定小时发信（用于配合 UTC 15/16 双跑）
#     return now_local(cfg["timezone"]).hour == int(cfg["send_hour_local"])
def should_send_now(cfg) -> bool:
    now = now_local(cfg["timezone"])
    print(f"DEBUG tz={cfg['timezone']} now={now.isoformat()} hour={now.hour} send_hour_local={cfg['send_hour_local']}")
    return now.hour == int(cfg["send_hour_local"])

# -------------------------
# OpenAlex：抽象还原（abstract_inverted_index -> string）
# -------------------------
def reconstruct_abstract(inv_idx):
    if not inv_idx:
        return ""
    pos2word = {}
    for word, poses in inv_idx.items():
        for p in poses:
            pos2word[p] = word
    return " ".join(pos2word[i] for i in sorted(pos2word))




def _safe_get_json(url: str, params: dict, timeout: int = 60, retries: int = 3, backoff_sec: int = 3) -> dict:
    """
    通用 GET JSON：遇到 429/5xx 自动重试；最终失败则抛异常或返回空（由调用方决定）。
    """
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            # 429 或 5xx：退避重试
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < retries:
                    sleep_s = backoff_sec * (2 ** attempt)
                    print(f"OpenAlex GET {url} rate-limited/server-error ({r.status_code}); retry in {sleep_s}s")
                    time.sleep(sleep_s)
                    continue
                print(f"OpenAlex GET {url} failed ({r.status_code}); give up.")
                r.raise_for_status()

            r.raise_for_status()
            return r.json()

        except Exception as e:
            if attempt < retries:
                sleep_s = backoff_sec * (2 ** attempt)
                print(f"OpenAlex GET {url} exception: {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
            print(f"OpenAlex GET {url} exception: {e}; give up.")
            raise

def openalex_get(params):
    # works 列表查询
    return _safe_get_json("https://api.openalex.org/works", params=params, timeout=60)



def openalex_get_work_by_id(openalex_id: str, mailto: str = "") -> dict | None:
    """
    openalex_id 通常长这样：
      https://openalex.org/Wxxxxxxxxx
    我们把它转换为 API：
      https://api.openalex.org/works/Wxxxxxxxxx
    """
    if not openalex_id:
        return None
    oid = openalex_id.strip()
    if oid.startswith("https://openalex.org/"):
        work_id = oid.split("/")[-1]  # Wxxxx
    else:
        work_id = oid  # 也可能直接给 Wxxxx

    url = f"https://api.openalex.org/works/{work_id}"
    params = {}
    if mailto:
        params["mailto"] = mailto
    r = requests.get(url, params=params, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def normalize_doi(doi: str) -> str:
    """
    OpenAlex 的 doi 字段一般是完整 URL 形式：https://doi.org/...
    这里把用户输入的 DOI 规范成这种形式，便于 filter=doi:...
    """
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


def openalex_find_work_by_doi(doi: str, mailto: str = "") -> dict | None:
    """
    用 filter=doi:... 找到对应 work
    """
    doi_url = normalize_doi(doi)
    if not doi_url:
        return None
    params = {"filter": f"doi:{doi_url}", "per_page": 1}
    if mailto:
        params["mailto"] = mailto
    data = openalex_get(params)
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


def normalize(s: str) -> str:
    return (s or "").lower()


# -------------------------
# 相关性与规则摘要
# -------------------------
def relevance_score(title: str, abstract: str, keywords: list[str]) -> int:
    t = normalize(title)
    a = normalize(abstract)
    score = 0
    for kw in keywords:
        k = kw.lower()
        if k in t:
            score += 3
        elif k in a:
            score += 1
    return score


def excluded(title: str, abstract: str, exclude_keywords: list[str]) -> bool:
    t = normalize(title)
    a = normalize(abstract)
    return any(k.lower() in t or k.lower() in a for k in exclude_keywords)


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

    sents = re.split(r"(?<=[.!?])\s+", abstract.strip())
    sents = [s for s in sents if len(s) > 40]
    explain = " ".join(sents[:2]) if sents else "（摘要信息不足：建议点开链接快速判断是否与你的在线监测链路相关。）"

    return "\n".join([
        "一句话：这篇工作围绕结温在线估算/监测给出一条可实现的技术路径。",
        f"方法线索：{(' / '.join(tags)) if tags else '未从摘要里识别到明确方法关键词'}",
        f"可量化指标：{nums if nums else '摘要未给出明确数值（或需读全文/图表）'}",
        f"拆解：{explain}",
        "建议：如果你在做 TSEP 标定/在线估算链路/误差评估，这篇优先读；否则先收藏观察。"
    ])


# -------------------------
# 候选获取：关键词（最新/经典）
# -------------------------
def fetch_latest_and_classic(cfg, mailto: str):
    query = cfg.get("search_query") or " ".join(cfg["keywords"][:6])

    today = dt.date.today()
    from_date = (today - dt.timedelta(days=int(cfg["latest_days"]))).isoformat()
    classic_to = (today - dt.timedelta(days=365 * 2)).isoformat()

    common_filter = "type:journal-article|proceedings-article"

    base = {"search": query, "per_page": 50}
    if mailto:
        base["mailto"] = mailto

    latest = openalex_get({
        **base,
        "filter": f"from_publication_date:{from_date},{common_filter}",
        "sort": "publication_date:desc",
    }).get("results", [])

    classic = openalex_get({
        **base,
        "filter": f"to_publication_date:{classic_to},{common_filter}",
        "sort": "cited_by_count:desc",
    }).get("results", [])

    return latest, classic


# -------------------------
# Milestone B：DOI seeds -> related_works 推荐
# -------------------------
def load_seed_dois(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
    return out



def openalex_get_url(url: str, params: dict, timeout: int = 60) -> dict:
    # cited_by_api_url 用这个
    return _safe_get_json(url, params=params, timeout=timeout)


def short_openalex_id(oid: str) -> str:
    """
    输入可能是:
      - https://openalex.org/Wxxxx
      - Wxxxx
    输出统一为 Wxxxx
    """
    s = (oid or "").strip()
    if not s:
        return ""
    if s.startswith("https://openalex.org/"):
        return s.split("/")[-1]
    return s


def fetch_works_by_openalex_ids(ids: list[str], mailto: str = "", per_page: int = 100) -> list[dict]:
    """
    批量拉 works：使用 filter=openalex:W1|W2|...
    OR 语法/限制详见 OpenAlex filter 文档：单个 filter 最多 100 个值，建议 per_page=100。 [oai_citation:3‡OpenAlex](https://docs.openalex.org/how-to-use-the-api/get-lists-of-entities/filter-entity-lists)
    """
    ids = [short_openalex_id(x) for x in ids if short_openalex_id(x)]
    if not ids:
        return []

    out = []
    chunk = 100  # OpenAlex OR 上限 100
    for i in range(0, len(ids), chunk):
        part = ids[i:i+chunk]
        params = {"filter": f"openalex:{'|'.join(part)}", "per_page": min(per_page, 100)}
        if mailto:
            params["mailto"] = mailto
        data = openalex_get(params)
        out.extend(data.get("results", []) or [])
        time.sleep(0.12)
    return out

def fetch_graph_buckets_from_seeds(cfg, mailto: str) -> tuple[list[dict], list[dict]]:
    """
    返回两个桶：
      - refs_works：seed.referenced_works（出引文，偏“根论文/经典”）
      - citedby_works：seed.cited_by_api_url（入引文，偏“关键后续”）

    Work 字段：referenced_works / cited_by_api_url。
    """
    pos = load_seed_dois("seeds_positive.txt")
    neg = set(normalize_doi(x) for x in load_seed_dois("seeds_negative.txt"))
    if not pos:
        return [], []

    max_ref = int(cfg.get("graph_max_references_per_seed", 60))
    max_citedby = int(cfg.get("graph_max_citedby_per_seed", 60))

    refs_ids: list[str] = []
    citedby_ids: list[str] = []
    seed_doi_urls = set()

    for doi in pos:
        w = openalex_find_work_by_doi(doi, mailto)
        time.sleep(0.12)
        if not w:
            continue

        doi_url = w.get("doi")
        if doi_url:
            seed_doi_urls.add(doi_url)

        # A) references（出引文）
        refs = w.get("referenced_works") or []
        if refs:
            refs_ids.extend(refs[:max_ref])

        # B) cited-by（入引文）
        cited_by_url = w.get("cited_by_api_url") or ""
        if cited_by_url:
            params = {"per_page": 100, "sort": "cited_by_count:desc"}
            if mailto:
                params["mailto"] = mailto
            try:
                data = openalex_get_url(cited_by_url, params=params)
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

    # 批量拉回 works
    refs_works = fetch_works_by_openalex_ids(
        uniq_short_ids(refs_ids),
        mailto=mailto,
        per_page=int(cfg.get("openalex_per_page", 200))
    )
    citedby_works = fetch_works_by_openalex_ids(
        uniq_short_ids(citedby_ids),
        mailto=mailto,
        per_page=int(cfg.get("openalex_per_page", 200))
    )

    # 过滤负例 DOI 和 seed 本身
    def drop_neg_and_seed(works: list[dict]) -> list[dict]:
        out = []
        for ww in works:
            d = ww.get("doi") or ""
            if d and (d in neg or d in seed_doi_urls):
                continue
            out.append(ww)
        return out

    return drop_neg_and_seed(refs_works), drop_neg_and_seed(citedby_works)




def fetch_recommendations_from_seeds(cfg, mailto: str) -> list[dict]:
    """
    对每个 seed DOI：
      DOI -> OpenAlex work
      work.related_works -> 拉回相关 works
    最后合并去重，并打上 reco_source 标记
    """
    pos = load_seed_dois("seeds_positive.txt")
    neg = set(normalize_doi(x) for x in load_seed_dois("seeds_negative.txt"))

    if not pos:
        return []

    max_related = int(cfg.get("max_related_per_seed", 25))
    all_ids: list[str] = []
    seed_doi_urls = set()

    # 1) 每个 DOI 找到对应 work，并收集 related_works ids
    for doi in pos:
        w = openalex_find_work_by_doi(doi, mailto)
        time.sleep(0.12)  # 轻微限速，减少被限流风险
        if not w:
            continue
        doi_url = w.get("doi")
        if doi_url:
            seed_doi_urls.add(doi_url)

        rel = w.get("related_works") or []
        all_ids.extend(rel[:max_related])

    # 2) 拉回 related works 详情（逐个拉，量不大更稳）
    recos = []
    seen = set()
    for oid in all_ids:
        if oid in seen:
            continue
        seen.add(oid)
        w = openalex_get_work_by_id(oid, mailto)
        time.sleep(0.12)
        if not w:
            continue
        # 排除：负例 DOI、以及种子本身
        doi_url = w.get("doi") or ""
        if doi_url and (doi_url in neg or doi_url in seed_doi_urls):
            continue
        recos.append(w)

    return recos



# -------------------------
# milestone C: wo API
# -------------------------

def s2_headers():
    # 没 key 也能尝试；有 key 会更稳（官方建议使用 key） [oai_citation:4‡Semantic Scholar](https://www.semanticscholar.org/product/api%2Ftutorial?utm_source=chatgpt.com)
    key = (os.getenv("S2_API_KEY") or "").strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["x-api-key"] = key
    return h


def doi_to_s2_pid(doi: str) -> str:
    """
    把 DOI 规范成 Semantic Scholar 推荐接口常用的 paperId 形式：DOI:10.xxxx/xxxx
    """
    d = (doi or "").strip()
    d = d.replace("doi:", "").strip()
    d = d.replace("https://doi.org/", "").strip()
    d = d.replace("http://doi.org/", "").strip()
    return f"DOI:{d}" if d else ""


def fetch_s2_recommendations_from_seeds(cfg) -> list[dict]:
    """
    Semantic Scholar Recommendations API：
      POST https://api.semanticscholar.org/recommendations/v1/papers
    官方有 Recommendations API 文档。 [oai_citation:5‡语义学者](https://api.semanticscholar.org/api-docs/recommendations?utm_source=chatgpt.com)

    无 key：更可能 429/失败，所以这里做：
      - 小 limit
      - 重试 + 指数退避
      - 失败直接返回空列表（不影响邮件）
    """
    if not cfg.get("use_s2_recommendations", True):
        return []

    # 先尝试 ai4scholar：成功就直接用它，跳过官方 S2
    ok, recs = fetch_ai4s_recommendations_from_seeds(cfg)
    if ok:
        print(f"AI4S: used, recs={len(recs)} (skip official S2)")
        return recs



    
    
    pos = load_seed_dois("seeds_positive.txt")
    neg = load_seed_dois("seeds_negative.txt")

    positive = [doi_to_s2_pid(d) for d in pos if doi_to_s2_pid(d)]
    negative = [doi_to_s2_pid(d) for d in neg if doi_to_s2_pid(d)]

    if not positive:
        return []

    url = "https://api.semanticscholar.org/recommendations/v1/papers"
    params = {
        "fields": "title,abstract,year,citationCount,venue,externalIds,url",
        "limit": int(cfg.get("s2_limit", 20)),
    }

    payload = {"positivePaperIds": positive, "negativePaperIds": negative}

    retries = int(cfg.get("s2_retries", 2))
    base_backoff = int(cfg.get("s2_backoff_sec", 3))

    for attempt in range(retries + 1):
        try:
            print(f"S2: start, positive={len(positive)}, negative={len(negative)}, limit={params['limit']}, has_key={bool((os.getenv('S2_API_KEY') or '').strip())}")
            r = requests.post(
                url,
                headers=s2_headers(),
                params=params,
                data=json.dumps(payload),
                timeout=60,
            )

            # 429/5xx：重试（无 key 时更常见） [oai_citation:6‡Semantic Scholar](https://www.semanticscholar.org/product/api%2Ftutorial?utm_source=chatgpt.com)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < retries:
                    sleep_s = base_backoff * (2 ** attempt)
                    print(f"S2 rate-limited or server error ({r.status_code}); retry in {sleep_s}s")
                    time.sleep(sleep_s)
                    continue
                print(f"S2 failed with status={r.status_code}; skipping.")
                return []

            r.raise_for_status()
            data = r.json()
            recs = data.get("recommendedPapers", []) or []
            print(f"S2: ok, status={r.status_code}, recs={len(recs)}")
            return recs
            return data.get("recommendedPapers", []) or []

        except Exception as e:
            if attempt < retries:
                sleep_s = base_backoff * (2 ** attempt)
                print(f"S2 exception: {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
            print(f"S2 exception: {e}; skipping.")
            return []

    return []





def bare_doi(doi_or_url: str) -> str:
    """
    输入可能是：
      - https://doi.org/10.xxx/yyy  （OpenAlex 常见）
      - DOI:10.xxx/yyy
      - 10.xxx/yyy
    输出统一为：10.xxx/yyy
    """
    s = (doi_or_url or "").strip()
    if not s:
        return ""
    s = s.lower().replace("doi:", "").strip()
    s = s.replace("https://doi.org/", "").replace("http://doi.org/", "")
    return s.strip()


def unpaywall_lookup(doi_or_url: str, email: str, timeout: int = 20) -> dict | None:
    """
    Unpaywall v2: https://api.unpaywall.org/v2/{DOI}?email=...   [oai_citation:4‡pubfetcher.readthedocs.io](https://pubfetcher.readthedocs.io/en/stable/fetcher.html?utm_source=chatgpt.com)
    """
    doi = bare_doi(doi_or_url)
    if not doi or not email:
        return None

    url = f"https://api.unpaywall.org/v2/{doi}"
    r = requests.get(url, params={"email": email}, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def attach_fulltext_links(cfg, items: list[dict]) -> list[dict]:
    """
    给每条记录补：
      - pdf_url（若有）
      - oa_status / license / version（可选显示）
    只对“已经入选要发邮件的条目”做查询，控制调用量（建议≤10万/天/用户）。 [oai_citation:5‡docs.ropensci.org](https://docs.ropensci.org/roadoi/reference/oadoi_fetch.html)
    """
    email = (os.getenv("UNPAYWALL_EMAIL") or "").strip()
    if not email:
        print("Unpaywall: UNPAYWALL_EMAIL missing; skip fulltext enrichment.")
        return items

    cache: dict[str, dict] = {}
    for it in items:
        d = bare_doi(it.get("doi") or "")
        if not d:
            continue

        if d in cache:
            data = cache[d]
        else:
            try:
                data = unpaywall_lookup(d, email, timeout=int(cfg.get("unpaywall_timeout", 20)))
            except Exception as e:
                print(f"Unpaywall error for DOI {d}: {e}")
                data = None
            cache[d] = data or {}
            time.sleep(0.12)  # 轻微限速，礼貌一点

        if not data:
            continue

        best = data.get("best_oa_location") or {}
        pdf = best.get("url_for_pdf") or ""   # 字段名在 Unpaywall schema/支持文档里列出  [oai_citation:6‡Unpaywall](https://support.unpaywall.org/support/solutions/articles/44002142311-what-do-the-fields-in-the-api-response-and-snapshot-records-mean-)
        landing = best.get("url_for_landing_page") or ""
        it["pdf_url"] = pdf or ""
        it["oa_status"] = data.get("oa_status") or ""
        it["oa_license"] = best.get("license") or ""
        it["oa_version"] = best.get("version") or ""
        it["oa_landing"] = landing or ""

    return items




# -------------------------
# ai4scholar API
# -------------------------
def ai4s_headers():
    key = (os.getenv("AI4SCHOLAR_API_KEY") or "").strip()
    if not key:
        return None
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def fetch_ai4s_recommendations_from_seeds(cfg) -> tuple[bool, list[dict]]:
    """
    ai4scholar 优先入口：
    - 成功（HTTP 200）=> 返回 (True, recs)，并“跳过”官方 S2
    - 失败/异常 => 返回 (False, [])，让外层 fallback 到官方 S2

    说明：
    ai4scholar 文档示例显示用 Authorization: Bearer 方式访问 /graph/v1/...  [oai_citation:2‡Awesomely](https://ai4scholar.net/docs/code-examples)
    我们这里按 Semantic Scholar Recommendations 的路径去尝试：/recommendations/v1/papers(/)
    """
    headers = ai4s_headers()
    if not headers:
        return (False, [])

    pos = load_seed_dois("seeds_positive.txt")
    neg = load_seed_dois("seeds_negative.txt")
    positive = [doi_to_s2_pid(d) for d in pos if doi_to_s2_pid(d)]
    negative = [doi_to_s2_pid(d) for d in neg if doi_to_s2_pid(d)]
    if not positive:
        return (True, [])  # 有 key 但没有正例：视为“成功但无输出”，跳过官方 S2

    base = "https://ai4scholar.net"
    url = f"{base}/recommendations/v1/papers/"  # 尾斜杠更稳
    params = {
        "fields": "title,abstract,year,citationCount,venue,externalIds,url",
        "limit": int(cfg.get("s2_limit", 20)),
    }
    payload = {"positivePaperIds": positive, "negativePaperIds": negative}

    retries = int(cfg.get("s2_retries", 2))
    base_backoff = int(cfg.get("s2_backoff_sec", 3))

    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, params=params, data=json.dumps(payload), timeout=60)

            # 打印积分信息（ai4scholar 示例里提到这些 headers） [oai_citation:3‡Awesomely](https://ai4scholar.net/docs/code-examples)
            if r.status_code == 200:
                rem = r.headers.get("X-Credits-Remaining")
                charged = r.headers.get("X-Credits-Charged")
                print(f"AI4S: ok, remaining={rem}, charged={charged}")

                data = r.json()
                # 兼容两种可能的返回结构：recommendedPapers（S2风格） 或 data（ai4s风格）
                recs = data.get("recommendedPapers", None)
                if recs is None:
                    recs = data.get("data", []) or []

                # 标记来源，便于你在邮件里显示“via ai4scholar”
                for p in recs:
                    if isinstance(p, dict):
                        p["_via"] = "ai4scholar"

                return (True, recs)

            # 429/5xx：重试
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < retries:
                    sleep_s = base_backoff * (2 ** attempt)
                    print(f"AI4S: {r.status_code}; retry in {sleep_s}s")
                    time.sleep(sleep_s)
                    continue
                print(f"AI4S: failed status={r.status_code}; fallback to official S2.")
                return (False, [])

            # 401/402/403 等：直接 fallback（401/402 在 ai4scholar 文档示例里有提到） [oai_citation:4‡Awesomely](https://ai4scholar.net/docs/code-examples)
            print(f"AI4S: failed status={r.status_code}; fallback to official S2.")
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


# -------------------------
# LLM Brief via OpenRouter
# -------------------------

def openrouter_headers() -> dict | None:
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return None
    h = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    site = (os.getenv("OPENROUTER_SITE_URL") or "").strip()
    app = (os.getenv("OPENROUTER_APP_NAME") or "").strip()
    # OpenRouter docs recommend these optional headers
    if site:
        h["HTTP-Referer"] = site
    if app:
        h["X-Title"] = app
    return h


def openrouter_chat(cfg, messages: list[dict]) -> str:
    """
    POST /api/v1/chat/completions (OpenAI-compatible)
    Docs: https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request
    """
    headers = openrouter_headers()
    if not headers:
        raise RuntimeError("OPENROUTER_API_KEY missing")

    url = "https://openrouter.ai/api/v1/chat/completions"
    payload = {
        "model": cfg.get("openrouter_model", "openai/gpt-4.1-mini"),
        "messages": messages,
        "temperature": float(cfg.get("llm_temperature", 0.2)),
        "max_tokens": int(cfg.get("llm_max_tokens", 520)),
        "stream": False,
    }

    retries = int(cfg.get("llm_retries", 2))
    backoff = int(cfg.get("llm_backoff_sec", 3))
    timeout = int(cfg.get("llm_timeout_sec", 60))

    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)

            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < retries:
                    sleep_s = backoff * (2 ** attempt)
                    print(f"OpenRouter rate-limited/server error ({r.status_code}); retry in {sleep_s}s")
                    time.sleep(sleep_s)
                    continue
                raise RuntimeError(f"OpenRouter failed status={r.status_code}: {r.text[:200]}")

            r.raise_for_status()
            data = r.json()
            # OpenAI-style
            return (((data.get("choices") or [])[0] or {}).get("message") or {}).get("content", "").strip()

        except Exception as e:
            if attempt < retries:
                sleep_s = backoff * (2 ** attempt)
                print(f"OpenRouter exception: {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue
            raise


def load_llm_cache(path: str) -> dict:
    if not path:
        return {}
    if not os.path.exists(path):
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
    # 优先 DOI，其次 url，其次 title
    return (it.get("doi") or it.get("url") or it.get("title") or "").strip()


def build_llm_prompt_cn(it: dict) -> list[dict]:
    """
    生成“中文科研简报”，禁止臆测：只能基于给定元数据与摘要。
    """
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

    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


def llm_brief_cn(cfg, it: dict) -> str:
    msgs = build_llm_prompt_cn(it)
    return openrouter_chat(cfg, msgs)


def apply_llm_briefs(cfg, lists: list[list[dict]]) -> None:
    """
    对多个列表中的条目，挑选 Top-N 做 LLM 简报，并写回 it["brief_cn"]。
    有缓存：llm_cache.json
    """
    if not cfg.get("use_llm_brief", False):
        return

    headers = openrouter_headers()
    if not headers:
        print("LLM brief enabled but OPENROUTER_API_KEY missing; fallback to rule-based briefs.")
        return

    max_n = int(cfg.get("llm_max_items_per_run", 18))
    cache_path = cfg.get("llm_cache_file", "llm_cache.json")
    cache = load_llm_cache(cache_path)

    # 合并候选并按“已有的排序信号”选 Top-N：优先引用多、相关性高
    pool = []
    for lst in lists:
        for it in lst:
            k = llm_cache_key(it)
            if not k:
                continue
            # 已经有 brief 就跳过
            if it.get("brief_cn"):
                continue
            # cache 命中则直接写回
            if k in cache and (cache[k] or "").strip():
                it["brief_cn"] = cache[k]
                continue

            # 评分：引用数开方 + relevance
            cites = int(it.get("cited_by_count", 0) or 0)
            rel = int(it.get("relevance", 0) or 0)
            score = (int(cites ** 0.5) * 10) + (rel * 8)
            pool.append((score, it))

    pool.sort(key=lambda x: x[0], reverse=True)
    picked = [it for _, it in pool[:max_n]]

    print(f"LLM briefs: need_generate={len(picked)} max_per_run={max_n}")

    for idx, it in enumerate(picked, 1):
        k = llm_cache_key(it)
        try:
            brief = llm_brief_cn(cfg, it)
            # 简单兜底：空就回退
            if not brief.strip():
                brief = human_brief_cn(it.get("title",""), it.get("abstract",""))
            it["brief_cn"] = brief
            cache[k] = brief
            print(f"LLM briefs: ok {idx}/{len(picked)} key={k[:32]}")
        except Exception as e:
            print(f"LLM briefs: failed key={k[:32]} err={e}; fallback to rule-based")
            it["brief_cn"] = human_brief_cn(it.get("title",""), it.get("abstract",""))

        # 小睡避免触发限流
        time.sleep(0.25)

    save_llm_cache(cache, cache_path)




# -------------------------
# enrich / 去重 / 排序
# -------------------------
def enrich(cfg, works: list[dict], tag: str = "", publisher_id_set: set[str] | None = None) -> list[dict]:
    publisher_id_set = publisher_id_set or set()

    out = []
    for w in works:
        title = w.get("title") or ""
        abstract = reconstruct_abstract(w.get("abstract_inverted_index"))
        if excluded(title, abstract, cfg.get("exclude_keywords", [])):
            continue

        src = ((w.get("primary_location") or {}).get("source") or {})
        host_org = src.get("host_organization") or ""   # e.g. "https://openalex.org/P...."
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
            "relevance": relevance_score(title, abstract, cfg["keywords"]),
            "bucket": tag,
            "host_org": host_org,
            "is_in_doaj": is_in_doaj,
            "publisher_hit": (host_org in publisher_id_set) if host_org else False,
            "via": w.get("_via", "openalex"),
        })
    return out


def dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
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


def filter_seen(cfg, items: list[dict], seen: dict) -> list[dict]:
    keep_days = int(cfg.get("seen_days_keep", 30))
    today = dt.date.today()

    # 清理过期记录
    cleaned = {}
    for k, v in seen.items():
        try:
            d = dt.date.fromisoformat(v)
            if (today - d).days <= keep_days:
                cleaned[k] = v
        except Exception:
            pass
    seen.clear()
    seen.update(cleaned)

    out = []
    for it in items:
        key = it.get("doi") or it.get("url") or it.get("title")
        if not key:
            continue
        if key in seen:
            continue
        out.append(it)
    return out


def rank_score(cfg, it: dict) -> int:
    score = int(it.get("relevance", 0)) * 10
    score += int(it.get("cited_by_count", 0) ** 0.5) * 3  # 引用数做“开方”避免极端值碾压

    if it.get("publisher_hit"):
        score += int(cfg.get("publisher_boost", 6)) * 10

    if it.get("is_in_doaj"):
        score -= int(cfg.get("doaj_penalty", 2)) * 10

    return score

def pick_top(cfg, items: list[dict], n: int) -> list[dict]:
    items = sorted(items, key=lambda x: rank_score(cfg, x), reverse=True)
    return items[:n]


def pick_top_cited(items: list[dict], n: int) -> list[dict]:
    return sorted(items, key=lambda x: x.get("cited_by_count", 0), reverse=True)[:n]


def enrich_s2(cfg, papers: list[dict], tag: str = "reco_s2") -> list[dict]:
    out = []
    for p in papers:
        title = p.get("title") or ""
        abstract = p.get("abstract") or ""

        if excluded(title, abstract, cfg.get("exclude_keywords", [])):
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
            "relevance": relevance_score(title, abstract, cfg["keywords"]),
            "bucket": tag,  # reco_s2
            "via": p.get("_via", "official_s2"),
        })
    return out


PUBLISHER_CACHE = "publisher_ids.json"


def openalex_get_json(url: str, params: dict, timeout: int = 60) -> dict:
    # 你原来 publishers 用这个
    return _safe_get_json(url, params=params, timeout=timeout)

def resolve_publishers_openalex_ids(names: list[str], mailto: str = "") -> dict[str, str]:
    """
    返回：{ "Elsevier": "https://openalex.org/Pxxxx", ... }
    结果会写入 publisher_ids.json 缓存，减少每天查询次数。
    """
    # 读缓存
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

    for name in names:
        if name in out and out[name].startswith("https://openalex.org/P"):
            continue

        params = {"search": name, "per_page": 5}
        if mailto:
            params["mailto"] = mailto

        data = openalex_get_json("https://api.openalex.org/publishers", params=params)
        results = data.get("results", []) or []
        # 选最像的一个（简单策略：第一个）
        if results:
            out[name] = results[0].get("id", "")
            changed = True
        else:
            out[name] = ""
            changed = True

        time.sleep(0.12)

    if changed:
        with open(PUBLISHER_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    return out

def fetch_publisher_pools(cfg, mailto: str, publisher_ids: list[str]):
    """
    返回：pub_latest_raw, pub_classic_raw
    """
    if not publisher_ids:
        return [], []

    query = cfg.get("search_query") or " ".join(cfg["keywords"][:6])

    today = dt.date.today()
    from_date = (today - dt.timedelta(days=int(cfg["latest_days"]))).isoformat()
    classic_to = (today - dt.timedelta(days=365 * 2)).isoformat()

    common_filter = "type:journal-article|proceedings-article"

    per_page = int(cfg.get("openalex_per_page", 200))
    base = {"search": query, "per_page": per_page}
    if mailto:
        base["mailto"] = mailto

    # OR 语法用 | 连接（OpenAlex 常用）
    pubs_or = "|".join(publisher_ids)

    pub_latest = openalex_get({
        **base,
        "filter": f"from_publication_date:{from_date},primary_location.source.host_organization:{pubs_or},{common_filter}",
        "sort": "cited_by_count:desc",
    }).get("results", [])

    pub_classic = openalex_get({
        **base,
        "filter": f"to_publication_date:{classic_to},primary_location.source.host_organization:{pubs_or},{common_filter}",
        "sort": "cited_by_count:desc",
    }).get("results", [])

    return pub_latest, pub_classic



def build_html(
    cfg,
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
    date_str = now_local(cfg["timezone"]).strftime("%Y-%m-%d (%a)")
    build_sha = (os.getenv("GITHUB_SHA", "") or "")[:7]
    run_id = os.getenv("GITHUB_RUN_ID", "")

    pub_lines = []
    for name, pid in (pub_map or {}).items():
        pub_lines.append(f"{name} ✓" if pid else f"{name} ✗")
    pub_status = " / ".join(pub_lines) if pub_lines else "（未配置 preferred_publishers）"

    def card(it: dict) -> str:
        title = it.get("title", "")
        abstract = it.get("abstract", "")

        # ✅ 优先用 LLM 生成的科研简报；没有则回退规则摘要
        brief_src = (it.get("brief_cn") or "").strip()
        if not brief_src:
            brief_src = human_brief_cn(title, abstract)
        brief = brief_src.replace("\n", "<br>")

        bucket = it.get("bucket")
        if bucket == "reco_s2":
            via = it.get("via", "official_s2")
            source_label = "S2猜你喜欢(ai4scholar)" if via == "ai4scholar" else "S2猜你喜欢(官方)"
        elif bucket == "reco_oa":
            source_label = "OpenAlex相关"
        elif bucket == "pub_latest":
            source_label = "出版商精选-最新"
        elif bucket == "pub_classic":
            source_label = "出版商精选-经典"
        elif bucket == "graph_ref_classic":
            source_label = "引用图谱-经典根论文"
        elif bucket == "graph_citedby_keyfollow":
            source_label = "引用图谱-关键后续"
        elif bucket == "latest":
            source_label = "最新"
        elif bucket == "classic":
            source_label = "经典"
        else:
            source_label = bucket or "未知来源"

        doi_url = it.get("url") or ""
        pdf_url = it.get("pdf_url") or ""

        pdf_btn = ""
        if pdf_url:
            pdf_btn = f"""
              <a href="{pdf_url}" target="_blank" rel="noreferrer"
                 style="display:inline-block;margin-left:8px;padding:2px 10px;border:1px solid #888;border-radius:999px;text-decoration:none;font-weight:600;">
                PDF
              </a>
            """

        venue = it.get("venue") or "Unknown venue"
        year = it.get("publication_year") or ""
        cites = it.get("cited_by_count", 0) or 0
        rel = it.get("relevance", 0) or 0

        return f"""
        <div style="margin:14px 0;padding:12px;border:1px solid #ddd;border-radius:10px;">
          <div style="font-size:16px;font-weight:700;">
            <a href="{doi_url}" target="_blank" rel="noreferrer">{title}</a>
            {pdf_btn}
          </div>
          <div style="color:#555;margin-top:6px;">
            {venue} · {year} · 引用 {cites} · relevance {rel} · 来源 {source_label} · 全文 {"PDF" if pdf_url else "无"}
          </div>
          <div style="margin-top:10px;line-height:1.55;">{brief}</div>
        </div>
        """

    def section(title: str, items: list[dict], empty_html: str) -> str:
        return f"""
        <h3>{title}</h3>
        {''.join(card(x) for x in items) if items else empty_html}
        """

    return f"""
    <html><body style="font-family:Arial, Helvetica, sans-serif;">
      <h2>{cfg['topic_cn']} — 每日科研简报（{date_str}）</h2>
      <p style="color:#666;">
        数据源：OpenAlex（works 搜索 / 引用图谱 / related_works）+ Semantic Scholar（或 ai4scholar）。<br>
        出版商池：按 primary_location.source.host_organization 过滤（Publisher级），提升 IEEE/Elsevier/Springer/Wiley 覆盖。<br>
        出版商识别：{pub_status}<br>
        构建标识：sha={build_sha} run={run_id}
      </p>

      {section("⭐ S2猜你喜欢（更像“你可能也喜欢”）", reco_s2,
               "<p>S2 今天没有产出（或被跳过），不影响其他内容。</p>")}

      {section("🧭 OpenAlex脉络（沿你的种子论文 related_works 扩展）", reco_oa,
               "<p>OpenAlex related_works 今天为空：检查 seeds_positive.txt DOI 是否有效。</p>")}

      {section("🏷️ 出版商精选-最新（IEEE / Elsevier / Springer / Wiley）", pub_latest,
               "<p>出版商池“最新”今天为空：可能是 publisher 解析失败、或关键词过窄、或当天返回不足。</p>")}

      {section("🏷️ 出版商精选-经典（IEEE / Elsevier / Springer / Wiley）", pub_classic,
               "<p>出版商池“经典”今天为空：可能是 publisher 解析失败、或 classic 条件过严、或引用阈值设置过高。</p>")}

      {section("📚 引用图谱-经典根论文（references：更像“这方向的地基”）", graph_ref_classic,
               "<p>引用图谱-根论文今天为空：可能是 seeds 数量不足、阈值过严（年限/最低引用），或图谱抓取失败。</p>")}

      {section("🛰️ 引用图谱-关键后续（cited-by：更像“重要延展/路线分叉”）", graph_citedby_keyfollow,
               "<p>引用图谱-关键后续今天为空：可能是 follow_years 太短、最低引用过高，或 seeds 覆盖不足。</p>")}

      {section(f"🆕 最新进展（全域，近 {cfg['latest_days']} 天）", latest,
               "<p>今天未抓到足够匹配的最新条目。</p>")}

      {section("🏛️ 经典/高影响力（全域，两年前及更早）", classic,
               "<p>今天未抓到足够匹配的经典条目。</p>")}

      <hr>
      <p style="color:#888;font-size:12px;">
        提示：引用图谱栏目强依赖 seeds 的质量；建议把你认可的“根论文/综述/标志性论文”逐步补充进 seeds_positive.txt。
      </p>
    </body></html>
    """





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


def main():
    cfg = load_config()

    # ---- config 必要项检查（一次性）----
    required_cfg = ["topic_cn", "timezone", "send_hour_local", "keywords", "latest_days", "top_latest", "top_classic"]
    for k in required_cfg:
        if k not in cfg:
            raise RuntimeError(f"config.yml missing key: {k}")

    # ---- env 必要项检查（一次性）----
    required_env = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "TO_EMAIL"]
    for k in required_env:
        if not (os.getenv(k) or "").strip():
            raise RuntimeError(f"Missing env var: {k}")

    # ---- 是否到点发送 ----
    if not should_send_now(cfg):
        print("Not sending now (local hour mismatch).")
        return

    mailto = os.getenv("OPENALEX_MAILTO", "")
    seen = load_seen()
    print(f"DEBUG seen loaded: {len(seen)}")

    # --------
    # 兼容层：有些函数你可能已经改了签名（比如 enrich/pick_top）
    # --------
    def call_enrich(works, tag, publisher_id_set=None):
        try:
            return enrich(cfg, works, tag, publisher_id_set=publisher_id_set)
        except TypeError:
            return enrich(cfg, works, tag)

    def call_pick_top(items, n):
        try:
            return pick_top(cfg, items, n)  # 你当前版本是 pick_top(cfg, items, n)
        except TypeError:
            return pick_top(items, n)       # 兼容旧版 pick_top(items, n)

    # --------
    # A) 出版商ID解析 + 出版商池子抓取
    # --------
    pub_names = cfg.get("preferred_publishers", [])
    pub_map = resolve_publishers_openalex_ids(pub_names, mailto)
    pub_ids = [v for v in pub_map.values() if v and v.startswith("https://openalex.org/P")]
    pub_id_set = set(pub_ids)
    print(f"DEBUG publishers: {pub_map}")

    pub_latest_raw, pub_classic_raw = ([], [])
    if cfg.get("enable_publisher_pools", True) and pub_ids:
        pub_latest_raw, pub_classic_raw = fetch_publisher_pools(cfg, mailto, pub_ids)

    # --------
    # B) 全域关键词：最新 + 经典
    # --------
    latest_raw, classic_raw = fetch_latest_and_classic(cfg, mailto)

    latest_items = filter_seen(cfg, dedupe(call_enrich(latest_raw, "latest", publisher_id_set=pub_id_set)), seen)
    classic_items = filter_seen(cfg, dedupe(call_enrich(classic_raw, "classic", publisher_id_set=pub_id_set)), seen)

    latest = call_pick_top(latest_items, int(cfg["top_latest"]))
    classic = call_pick_top(classic_items, int(cfg["top_classic"]))

    # --------
    # C) 推荐：OpenAlex related_works + S2 / ai4scholar
    # --------
    reco_oa_raw = fetch_recommendations_from_seeds(cfg, mailto)
    reco_oa = dedupe(call_enrich(reco_oa_raw, "reco_oa", publisher_id_set=pub_id_set))
    reco_oa = filter_seen(cfg, reco_oa, seen)
    reco_oa = pick_top_cited(reco_oa, int(cfg.get("top_reco_oa", 10)))

    reco_s2_raw = fetch_s2_recommendations_from_seeds(cfg)  # ai4scholar 成功会在内部 skip 官方S2
    reco_s2 = dedupe(enrich_s2(cfg, reco_s2_raw, "reco_s2"))
    reco_s2 = filter_seen(cfg, reco_s2, seen)
    reco_s2 = pick_top_cited(reco_s2, int(cfg.get("top_reco_s2", 10)))

    # --------
    # D) 出版商精选
    # --------
    pub_latest_items = filter_seen(cfg, dedupe(call_enrich(pub_latest_raw, "pub_latest", publisher_id_set=pub_id_set)), seen)
    pub_classic_items = filter_seen(cfg, dedupe(call_enrich(pub_classic_raw, "pub_classic", publisher_id_set=pub_id_set)), seen)

    pub_latest = call_pick_top(pub_latest_items, int(cfg.get("top_pub_latest", 8)))
    pub_classic = pick_top_cited(pub_classic_items, int(cfg.get("top_pub_classic", 8)))

    # --------
    # C.5) 引用图谱分桶：
    #   - graph_ref_classic（根论文/经典）
    #   - graph_citedby_keyfollow（关键后续/影响扩展）
    # --------
    refs_raw, citedby_raw = fetch_graph_buckets_from_seeds(cfg, mailto)

    # A) 根论文/经典（references）
    ref_items = dedupe(call_enrich(refs_raw, "graph_ref_classic", publisher_id_set=pub_id_set))
    ref_items = filter_seen(cfg, ref_items, seen)

    years_ago = int(cfg.get("graph_ref_classic_years_ago", 5))
    year_cut = dt.date.today().year - years_ago
    ref_items = [x for x in ref_items if (x.get("publication_year") or 9999) <= year_cut]

    min_cites = int(cfg.get("graph_ref_min_cited_by", 30))
    ref_items = [x for x in ref_items if int(x.get("cited_by_count", 0) or 0) >= min_cites]

    graph_ref_classic = pick_top_cited(ref_items, int(cfg.get("top_graph_ref_classic", 10)))

    # B) 关键后续（cited-by）
    cited_items = dedupe(call_enrich(citedby_raw, "graph_citedby_keyfollow", publisher_id_set=pub_id_set))
    cited_items = filter_seen(cfg, cited_items, seen)

    follow_years = int(cfg.get("graph_follow_years", 3))
    follow_cut = dt.date.today().year - follow_years
    cited_items = [x for x in cited_items if (x.get("publication_year") or 0) >= follow_cut]

    follow_min_cites = int(cfg.get("graph_follow_min_cited_by", 10))
    cited_items = [x for x in cited_items if int(x.get("cited_by_count", 0) or 0) >= follow_min_cites]

    graph_citedby_keyfollow = call_pick_top(cited_items, int(cfg.get("top_graph_citedby_keyfollow", 10)))

    # --------
    # E) Unpaywall 全文链接
    # --------
    latest = attach_fulltext_links(cfg, latest)
    classic = attach_fulltext_links(cfg, classic)
    reco_s2 = attach_fulltext_links(cfg, reco_s2)
    reco_oa = attach_fulltext_links(cfg, reco_oa)
    pub_latest = attach_fulltext_links(cfg, pub_latest)
    pub_classic = attach_fulltext_links(cfg, pub_classic)
    graph_ref_classic = attach_fulltext_links(cfg, graph_ref_classic)
    graph_citedby_keyfollow = attach_fulltext_links(cfg, graph_citedby_keyfollow)

    # ✅ 删除：graph_classic 未定义，会 NameError
    # graph_classic = attach_fulltext_links(cfg, graph_classic)


    # --------
    # LLM 简报（OpenRouter）
    # 只对“入选发邮件的条目”做 Top-N 调用，控制成本，并带缓存
    # --------
    apply_llm_briefs(cfg, [
        reco_s2, reco_oa,
        pub_latest, pub_classic,
        graph_ref_classic, graph_citedby_keyfollow,
        latest, classic
    ])


    # --------
    # F) 生成HTML并发送
    # --------

    html = build_html(
        cfg, latest, classic, reco_s2, reco_oa,
        pub_latest, pub_classic, pub_map,
        graph_ref_classic, graph_citedby_keyfollow
    )
    subject = f"[每日科研简报] {cfg['topic_cn']} | {now_local(cfg['timezone']).strftime('%Y-%m-%d')}"
    send_email(subject, html)

    # --------
    # G) 写回 seen（把今天发过的都记住）
    # --------
    today_str = dt.date.today().isoformat()
    for lst in [latest, classic, reco_s2, reco_oa, pub_latest, pub_classic, graph_ref_classic, graph_citedby_keyfollow]:
        for it in lst:
            k = it.get("doi") or it.get("url") or it.get("title")
            if k:
                seen[k] = today_str
    save_seen(seen)

    print(f"DEBUG seen saved: {len(seen)}")
    print("Email sent.")


        

if __name__ == "__main__":
    main()
