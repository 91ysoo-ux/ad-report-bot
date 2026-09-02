"""Pulls Meta (and later TikTok) ad performance and rewrites docs/index.html.

Run manually for local testing (reads .env), or via GitHub Actions
(reads the same variable names from repo secrets).
"""
import json
import os
from datetime import datetime, timezone, timedelta

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_VER = "v21.0"
KST = timezone(timedelta(hours=9))


def load_local_env(path):
    """Only used for local runs; GitHub Actions injects real env vars directly."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_local_env(os.path.join(ROOT, ".env"))

META_TOKEN = os.environ["META_ACCESS_TOKEN"]
META_ACCOUNT = os.environ["META_AD_ACCOUNT_ID"]

OBJ_LABEL = {
    "OUTCOME_ENGAGEMENT": "참여",
    "OUTCOME_AWARENESS": "인지도",
    "OUTCOME_TRAFFIC": "트래픽",
}


def meta_get(path, params):
    p = dict(params)
    p["access_token"] = META_TOKEN
    r = requests.get(f"https://graph.facebook.com/{API_VER}/{path}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def action_value(row, field, action_type="video_view"):
    for item in row.get(field, []) or []:
        if item.get("action_type") == action_type:
            try:
                return float(item["value"])
            except (KeyError, ValueError):
                return 0.0
    return 0.0


def fetch_meta_campaigns():
    meta = {}
    data = meta_get(f"{META_ACCOUNT}/campaigns", {
        "fields": "id,name,status,effective_status,objective",
        "limit": 200,
    })
    for row in data.get("data", []):
        meta[row["id"]] = {
            "status": row.get("effective_status"),
            "objective": row.get("objective"),
        }
    return meta


def fetch_meta_insights():
    data = meta_get(f"{META_ACCOUNT}/insights", {
        "level": "campaign",
        "date_preset": "maximum",
        "fields": (
            "campaign_id,campaign_name,spend,impressions,reach,actions,"
            "video_p100_watched_actions"
        ),
        "limit": 200,
    })
    return data.get("data", [])


def build_campaigns():
    meta_info = fetch_meta_campaigns()
    insights = fetch_meta_insights()
    rows = []
    for row in insights:
        spend = float(row.get("spend", 0) or 0)
        ad_views_3s = action_value(row, "actions", "video_view")
        completion = action_value(row, "video_p100_watched_actions", "video_view")
        info = meta_info.get(row.get("campaign_id"), {})
        rows.append({
            "platform": "meta",
            "campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"),
            "status": info.get("status"),
            "objective": info.get("objective"),
            "spend": spend,
            "impressions": int(row.get("impressions", 0) or 0),
            "reach": int(row.get("reach", 0) or 0),
            "ad_views_3s": int(ad_views_3s),
            "completion_views": int(completion),
            "completion_rate": round(completion / ad_views_3s * 100, 1) if ad_views_3s else 0.0,
            "cpv": round(spend / ad_views_3s, 2) if ad_views_3s else None,
        })
    return rows


def render(campaigns):
    template_path = os.path.join(ROOT, "src", "template.html")
    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    campaigns_json = json.dumps(campaigns, ensure_ascii=False)
    updated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    html = html.replace("/*__CAMPAIGNS_JSON__*/[]/*__END__*/", campaigns_json)
    html = html.replace("__UPDATED_AT__", updated_at)

    out_path = os.path.join(ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out_path} ({len(campaigns)} campaigns, updated_at={updated_at})")


if __name__ == "__main__":
    campaigns = build_campaigns()
    render(campaigns)
