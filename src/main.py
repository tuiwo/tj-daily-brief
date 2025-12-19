import os
import re
import smtplib
import datetime as dt
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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
# OpenAlex：抽象还原
# OpenAlex 常用 abstract_inverted_index 结构，需要还原成正常字符串
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

    # 截两句“讲人话”的内容（如果摘要很短就提示）
    sents = re.split(r"(?<=[.!?])\s+", abstract.strip())
    sents = [s for s in sents if len(s) > 40]
    explain = " ".join(sents[:2]) if sents else "（摘要信息不足：建议点开链接快速判断是否与你的在线监测链路相关。）"

    return "\n".join([
        f"一句话：这篇工作围绕结温在线估算/监测给出一条可实现的技术路径。",
        f"方法线索：{(' / '.join(tags)) if tags else '未从摘要里识别到明确方法关键词'}",
        f"可量化指标：{nums if nums else '摘要未给出明确数值（或需读全文/图表）'}",
        f"拆解：{explain}",
        "建议：如果你在做 TSEP 标定/在线估算链路/误差评估，这篇优先读；否则先收藏观察。"
    ])


def fetch_latest_and_classic(cfg, mailto: str):
    # OpenAlex 推荐用 search 参数搜 works（title/abstract/fulltext 子集）
    # https://docs.openalex.org/api-entities/works/search-works  [oai_citation:7‡OpenAlex](https://docs.openalex.org/api-entities/works/search-works?utm_source=chatgpt.com)
    query = cfg.get("search_query") or " ".join(cfg["keywords"][:6])

    today = dt.date.today()
    from_date = (today - dt.timedelta(days=int(cfg["latest_days"]))).isoformat()
    classic_to = (today - dt.timedelta(days=365 * 2)).isoformat()

    common_filter = "type:journal-article|proceedings-article"

    base = {"search": query, "per_page": 50}
    if mailto:
        base["mailto"] = mailto  # polite pool（更高限额/更稳定） [oai_citation:8‡OpenAlex](https://docs.openalex.org/api-guide-for-llms?utm_source=chatgpt.com)

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


def enrich(cfg, works: list[dict]) -> list[dict]:
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
    items = sorted(items, key=lambda x: (x["relevance"], x["cited_by_count"]), reverse=True)
    return items[:n]


def build_html(cfg, latest: list[dict], classic: list[dict]) -> str:
    date_str = now_local(cfg["timezone"]).strftime("%Y-%m-%d (%a)")

    def card(it: dict) -> str:
        brief = human_brief_cn(it["title"], it["abstract"]).replace("\n", "<br>")
        return f"""
        <div style="margin:14px 0;padding:12px;border:1px solid #ddd;border-radius:10px;">
          <div style="font-size:16px;font-weight:700;">
            <a href="{it['url']}" target="_blank" rel="noreferrer">{it['title']}</a>
          </div>
          <div style="color:#555;margin-top:6px;">
            {it['venue'] or 'Unknown venue'} · {it['publication_year'] or ''} · 引用 {it['cited_by_count']} · relevance {it['relevance']}
          </div>
          <div style="margin-top:10px;line-height:1.55;">{brief}</div>
        </div>
        """

    return f"""
    <html><body style="font-family:Arial, Helvetica, sans-serif;">
      <h2>{cfg['topic_cn']} — 每日科研简报（{date_str}）</h2>
      <p style="color:#666;">
        数据源：OpenAlex（works 搜索 + 引用数）。OpenAlex 有速率限制，建议带 mailto 做 polite usage。
      </p>

      <h3>🆕 最新进展（近 {cfg['latest_days']} 天）</h3>
      {''.join(card(x) for x in latest) if latest else '<p>今天未抓到足够匹配的最新条目。</p>'}

      <h3>🏛️ 经典/高影响力（两年前及更早）</h3>
      {''.join(card(x) for x in classic) if classic else '<p>今天未抓到足够匹配的经典条目。</p>'}

      <hr>
      <p style="color:#888;font-size:12px;">
        下一阶段：加入 DOI 种子论文 + OpenAlex related_works + Semantic Scholar Recommendations（更懂你），并预留大模型摘要接口。
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
    latest_raw, classic_raw = fetch_latest_and_classic(cfg, mailto)

    latest = pick_top(dedupe(enrich(cfg, latest_raw)), int(cfg["top_latest"]))
    classic = pick_top(dedupe(enrich(cfg, classic_raw)), int(cfg["top_classic"]))

    html = build_html(cfg, latest, classic)
    subject = f"[每日科研简报] {cfg['topic_cn']} | {now_local(cfg['timezone']).strftime('%Y-%m-%d')}"

    send_email(subject, html)
    print("Email sent.")


if __name__ == "__main__":
    main()
