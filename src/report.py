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
    """campaign_id -> {status, objective, daily_budget, lifetime_budget} (for CBO fallback)."""
    data = meta_get(f"{META_ACCOUNT}/campaigns", {
        "fields": "id,name,status,effective_status,objective,daily_budget,lifetime_budget",
        "limit": 200,
    })
    return {row["id"]: row for row in data.get("data", [])}


def fetch_meta_adsets():
    """adset_id -> full adset settings (budget, targeting, optimization, bid)."""
    data = meta_get(f"{META_ACCOUNT}/adsets", {
        "fields": (
            "id,campaign_id,name,daily_budget,lifetime_budget,bid_strategy,"
            "billing_event,optimization_goal,status,targeting"
        ),
        "limit": 200,
    })
    return {row["id"]: row for row in data.get("data", [])}


def fetch_meta_adset_insights():
    data = meta_get(f"{META_ACCOUNT}/insights", {
        "level": "adset",
        "date_preset": "maximum",
        "fields": (
            "campaign_id,campaign_name,adset_id,adset_name,spend,impressions,reach,"
            "actions,video_p100_watched_actions"
        ),
        "limit": 200,
    })
    return data.get("data", [])


def _budget_from(adset, campaign):
    """Ad-set budget (ABO) takes priority; falls back to the campaign's budget (CBO)."""
    daily = int(adset.get("daily_budget") or 0)
    if daily:
        return daily, "일"
    lifetime = int(adset.get("lifetime_budget") or 0)
    if lifetime:
        return lifetime, "총"
    c_daily = int(campaign.get("daily_budget") or 0)
    if c_daily:
        return c_daily, "일(캠페인 예산)"
    c_lifetime = int(campaign.get("lifetime_budget") or 0)
    if c_lifetime:
        return c_lifetime, "총(캠페인 예산)"
    return None, None


def build_rows():
    campaigns = fetch_meta_campaigns()
    adsets = fetch_meta_adsets()
    insights = fetch_meta_adset_insights()

    rows = []
    for row in insights:
        adset_id = row.get("adset_id")
        adset = adsets.get(adset_id, {})
        campaign = campaigns.get(row.get("campaign_id"), {})

        spend = float(row.get("spend", 0) or 0)
        ad_views_3s = action_value(row, "actions", "video_view")
        completion = action_value(row, "video_p100_watched_actions", "video_view")
        budget, budget_period = _budget_from(adset, campaign)
        targeting = adset.get("targeting") or {}

        rows.append({
            "platform": "meta",
            "campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"),
            "adset_id": adset_id,
            "adset_name": row.get("adset_name"),
            "status": adset.get("status") or campaign.get("effective_status"),
            "objective": campaign.get("objective"),
            "optimization_goal": adset.get("optimization_goal"),
            "bid_strategy": adset.get("bid_strategy"),
            "budget": budget,
            "budget_period": budget_period,
            "age_min": targeting.get("age_min"),
            "age_max": targeting.get("age_max"),
            "platforms": targeting.get("publisher_platforms") or [],
            "spend": spend,
            "impressions": int(row.get("impressions", 0) or 0),
            "reach": int(row.get("reach", 0) or 0),
            "ad_views_3s": int(ad_views_3s),
            "completion_views": int(completion),
            "completion_rate": round(completion / ad_views_3s * 100, 1) if ad_views_3s else 0.0,
            "cpv": round(spend / ad_views_3s, 2) if ad_views_3s else None,
        })
    return rows


def render(rows):
    template_path = os.path.join(ROOT, "src", "template.html")
    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    rows_json = json.dumps(rows, ensure_ascii=False)
    updated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    html = html.replace("/*__CAMPAIGNS_JSON__*/[]/*__END__*/", rows_json)
    html = html.replace("__UPDATED_AT__", updated_at)

    out_path = os.path.join(ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out_path} ({len(rows)} ad sets, updated_at={updated_at})")


if __name__ == "__main__":
    rows = build_rows()
    render(rows)
