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


def should_send_now(cfg) -> bool:
    # 只在本地指定小时发信（用于配合 UTC 15/16 双跑）
    return now_local(cfg["timezone"]).hour == int(cfg["send_hour_local"])


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


def openalex_get(params):
    r = requests.get("https://api.openalex.org/works", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


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






# -------------------------
# enrich / 去重 / 排序
# -------------------------
def enrich(cfg, works: list[dict], tag: str = "") -> list[dict]:
    out = []
    for w in works:
        title = w.get("title") or ""
        abstract = reconstruct_abstract(w.get("abstract_inverted_index"))
        if excluded(title, abstract, cfg.get("exclude_keywords", [])):
            continue

        out.append({
            "title": title,
            "abstract": abstract,
            "publication_year": w.get("publication_year"),
            "publication_date": w.get("publication_date"),
            "cited_by_count": w.get("cited_by_count", 0) or 0,
            "venue": (((w.get("primary_location") or {}).get("source") or {}).get("display_name")) or "",
            "doi": w.get("doi"),
            "url": pick_best_url(w),
            "relevance": relevance_score(title, abstract, cfg["keywords"]),
            "bucket": tag,  # latest / classic / reco
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


def pick_top(items: list[dict], n: int) -> list[dict]:
    # 简单可用：相关性优先，再看引用数
    items = sorted(items, key=lambda x: (x["relevance"], x["cited_by_count"]), reverse=True)
    return items[:n]


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
        })
    return out



# -------------------------
# 邮件 HTML
# -------------------------
def build_html(cfg, latest: list[dict], classic: list[dict], reco: list[dict]) -> str:
    date_str = now_local(cfg["timezone"]).strftime("%Y-%m-%d (%a)")

    def card(it: dict) -> str:
        brief = human_brief_cn(it["title"], it["abstract"]).replace("\n", "<br>")
        source_label = "关键词"
        if it.get("bucket") == "reco_s2":
            source_label = "S2猜你喜欢"
        elif it.get("bucket") == "reco_oa":
            source_label = "OpenAlex相关"
        elif it.get("bucket") == "reco":
            source_label = "推荐"
        elif it.get("bucket") == "latest":
            source_label = "最新"
        elif it.get("bucket") == "classic":
            source_label = "经典"
        return f"""
        <div style="margin:14px 0;padding:12px;border:1px solid #ddd;border-radius:10px;">
          <div style="font-size:16px;font-weight:700;">
            <a href="{it['url']}" target="_blank" rel="noreferrer">{it['title']}</a>
          </div>
          <div style="color:#555;margin-top:6px;">
            {it['venue'] or 'Unknown venue'} · {it['publication_year'] or ''} · 引用 {it['cited_by_count']} · relevance {it['relevance']} · 来源 {source_label}
          </div>
          <div style="margin-top:10px;line-height:1.55;">{brief}</div>
        </div>
        """

    reco_days = ""
    return f"""
    <html><body style="font-family:Arial, Helvetica, sans-serif;">
      <h2>{cfg['topic_cn']} — 每日科研简报（{date_str}）</h2>
      <p style="color:#666;">
        数据源：OpenAlex（works 搜索 + 引用数 + related_works 推荐）。建议带 OPENALEX_MAILTO 做 polite usage。
      </p>

      <h3>⭐ 为你推荐（基于你收藏的 DOI 种子论文）</h3>
      {''.join(card(x) for x in reco) if reco else '<p>今天“为你推荐”为空：请在 seeds_positive.txt 添加 3–10 篇你认可的 DOI。</p>'}

      <h3>🆕 最新进展（近 {cfg['latest_days']} 天）</h3>
      {''.join(card(x) for x in latest) if latest else '<p>今天未抓到足够匹配的最新条目。</p>'}

      <h3>🏛️ 经典/高影响力（两年前及更早）</h3>
      {''.join(card(x) for x in classic) if classic else '<p>今天未抓到足够匹配的经典条目。</p>'}

      <hr>
      <p style="color:#888;font-size:12px;">
        下一阶段：接入 Semantic Scholar Recommendations（支持正/负例更懂你），并把摘要升级为“可选大模型生成（只对 Top-N 调用，控制 token 成本）”。
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
    if not should_send_now(cfg):
        print("Not sending now (local hour mismatch).")
        return

    mailto = os.getenv("OPENALEX_MAILTO", "")

    # 1) 关键词：最新 + 经典
    latest_raw, classic_raw = fetch_latest_and_classic(cfg, mailto)
    latest = pick_top(dedupe(enrich(cfg, latest_raw, "latest")), int(cfg["top_latest"]))
    classic = pick_top(dedupe(enrich(cfg, classic_raw, "classic")), int(cfg["top_classic"]))

    # 2) Milestone B：DOI seeds -> related_works 推荐
    # OpenAlex 推荐（你已完成）
    reco_oa_raw = fetch_recommendations_from_seeds(cfg, mailto)
    reco_oa = enrich(cfg, reco_oa_raw, "reco_oa")
    
    # S2 推荐（无 key 也尝试；失败会自动跳过）
    reco_s2_raw = fetch_s2_recommendations_from_seeds(cfg)
    reco_s2 = enrich_s2(cfg, reco_s2_raw, "reco_s2")
    
    # 合并去重
    reco_all = dedupe(reco_oa + reco_s2)
    
    # 轻微偏向 S2（因为更像“猜你喜欢”）；无 S2 数据也不影响
    for it in reco_all:
        if it.get("bucket") == "reco_s2":
            it["relevance"] += 2
    
    reco = pick_top(reco_all, int(cfg.get("top_reco", 3)))

    html = build_html(cfg, latest, classic, reco)
    subject = f"[每日科研简报] {cfg['topic_cn']} | {now_local(cfg['timezone']).strftime('%Y-%m-%d')}"

    send_email(subject, html)
    print("Email sent.")


if __name__ == "__main__":
    main()
