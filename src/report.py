"""Pulls Meta and TikTok ad performance and rewrites docs/index.html.

Run manually for local testing (reads .env), or via GitHub Actions
(reads the same variable names from repo secrets).
"""
import json
import os
import re
from datetime import datetime, timezone, timedelta

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_VER = "v21.0"
KST = timezone(timedelta(hours=9))

_DATE8 = re.compile(r"^(\d{8})")
_DATE6 = re.compile(r"^(\d{6})")


def _parse_date(name):
    """Campaigns/ad sets are named with a YYMMDD or YYYYMMDD prefix marking the
    shared start date both Meta and TikTok launch on for that period; this is
    the join key used to combine the two platforms numbers for the same period.
    """
    if not name:
        return None
    m = _DATE8.match(name)
    if m:
        s = m.group(1)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    m = _DATE6.match(name.strip())
    if m:
        s = m.group(1)
        return f"20{s[:2]}-{s[2:4]}-{s[4:6]}"
    return None


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

TIKTOK_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN")
TIKTOK_ADVERTISER_ID = os.environ.get("TIKTOK_ADVERTISER_ID")
TIKTOK_API_VER = "v1.3"


def meta_get(path, params):
    p = dict(params)
    p["access_token"] = META_TOKEN
    r = requests.get(f"https://graph.facebook.com/{API_VER}/{path}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def meta_get_all(path, params):
    """Follow all cursor pages; never publish a silently truncated report."""
    query = dict(params)
    rows, seen = [], set()
    while True:
        page = meta_get(path, query)
        rows.extend(page.get("data", []))
        paging = page.get("paging") or {}
        if not paging.get("next"):
            return rows
        cursor = (paging.get("cursors") or {}).get("after")
        if not cursor or cursor in seen:
            raise ValueError("Meta pagination cursor missing or repeated; report was not updated")
        seen.add(cursor)
        query["after"] = cursor


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
    data = meta_get_all(f"{META_ACCOUNT}/campaigns", {
        "fields": "id,name,status,effective_status,objective,daily_budget,lifetime_budget",
        "limit": 200,
    })
    return {row["id"]: row for row in data}


def fetch_meta_adsets():
    """adset_id -> full adset settings (budget, targeting, optimization, bid)."""
    data = meta_get_all(f"{META_ACCOUNT}/adsets", {
        "fields": (
            "id,campaign_id,name,daily_budget,lifetime_budget,bid_strategy,"
            "billing_event,optimization_goal,status,targeting"
        ),
        "limit": 200,
    })
    return {row["id"]: row for row in data}


def fetch_meta_adset_insights():
    data = meta_get_all(f"{META_ACCOUNT}/insights", {
        "level": "adset",
        "date_preset": "maximum",
        "fields": (
            "campaign_id,campaign_name,adset_id,adset_name,spend,impressions,reach,"
            "actions,video_p100_watched_actions"
        ),
        "limit": 200,
    })
    return data


def _meta_budget_from(adset, campaign):
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


def build_meta_rows():
    campaigns = fetch_meta_campaigns()
    adsets = fetch_meta_adsets()
    insights = fetch_meta_adset_insights()

    rows = []
    for row in insights:
        adset_id = row.get("adset_id")
        adset = adsets.get(adset_id, {})
        campaign = campaigns.get(row.get("campaign_id"), {})

        spend = float(row.get("spend", 0) or 0)
        views = action_value(row, "actions", "video_view")
        completion = action_value(row, "video_p100_watched_actions", "video_view")
        budget, budget_period = _meta_budget_from(adset, campaign)
        targeting = adset.get("targeting") or {}

        date = _parse_date(row.get("adset_name")) or _parse_date(row.get("campaign_name"))

        rows.append({
            "platform": "meta",
            "campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"),
            "adset_id": adset_id,
            "adset_name": row.get("adset_name"),
            "date": date,
            "status": adset.get("status") or campaign.get("effective_status"),
            "objective": campaign.get("objective"),
            "optimization_goal": adset.get("optimization_goal"),
            "bid_strategy": adset.get("bid_strategy"),
            "budget": budget,
            "budget_period": budget_period,
            "age_min": targeting.get("age_min"),
            "age_max": targeting.get("age_max"),
            "platforms": targeting.get("publisher_platforms") or ["meta"],
            "spend": spend,
            "impressions": int(row.get("impressions", 0) or 0),
            "reach": int(row.get("reach", 0) or 0),
            "ad_view_label": "3초 조회",
            "ad_view_count": int(views),
            "views": int(views),
            "completion_views": int(completion),
            "completion_rate": round(completion / views * 100, 1) if views else 0.0,
            "cpv": round(spend / views, 2) if views else None,
        })
    return rows


_AGE_RANGE = re.compile(r"AGE_(\d+)_(\d+)")


def tiktok_get(path, params):
    p = dict(params)
    r = requests.get(
        f"https://business-api.tiktok.com/open_api/{TIKTOK_API_VER}/{path}",
        headers={"Access-Token": TIKTOK_TOKEN},
        params={k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in p.items()},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"TikTok API error on {path}: {data.get('code')} {data.get('message')}")
    return data["data"]


def tiktok_get_all(path, params, page_size=100):
    query = dict(params)
    query["page_size"] = page_size
    rows, page = [], 1
    while True:
        query["page"] = page
        data = tiktok_get(path, query)
        rows.extend(data.get("list", []))
        page_info = data.get("page_info", {})
        if page >= page_info.get("total_page", page):
            return rows
        page += 1


def _tiktok_budget_from(adgroup):
    mode = adgroup.get("budget_mode")
    budget = adgroup.get("budget") or 0
    if mode == "BUDGET_MODE_DYNAMIC_DAILY_BUDGET" and budget:
        return budget, "일"
    if mode == "BUDGET_MODE_TOTAL" and budget:
        return budget, "총"
    return None, None


def _tiktok_age_range(age_groups):
    mins, maxes = [], []
    for g in age_groups or []:
        m = _AGE_RANGE.match(g)
        if m:
            mins.append(int(m.group(1)))
            maxes.append(int(m.group(2)))
    return (min(mins), max(maxes)) if mins else (None, None)


def build_tiktok_rows():
    if not TIKTOK_TOKEN or not TIKTOK_ADVERTISER_ID:
        return []

    adgroups = {
        row["adgroup_id"]: row
        for row in tiktok_get_all("adgroup/get/", {"advertiser_id": TIKTOK_ADVERTISER_ID})
    }

    insights = tiktok_get_all("report/integrated/get/", {
        "advertiser_id": TIKTOK_ADVERTISER_ID,
        "report_type": "BASIC",
        "data_level": "AUCTION_ADGROUP",
        "dimensions": ["adgroup_id"],
        "metrics": [
            "spend", "impressions", "reach", "video_play_actions",
            "video_watched_6s", "video_views_p100", "campaign_name", "adgroup_name",
        ],
        "start_date": "2025-09-30",
        "end_date": datetime.now(KST).strftime("%Y-%m-%d"),
    })

    rows = []
    for row in insights:
        adgroup_id = row["dimensions"]["adgroup_id"]
        m = row["metrics"]
        adgroup = adgroups.get(adgroup_id, {})

        spend = float(m.get("spend", 0) or 0)
        views = float(m.get("video_watched_6s", 0) or 0)
        completion = float(m.get("video_views_p100", 0) or 0)
        budget, budget_period = _tiktok_budget_from(adgroup)
        age_min, age_max = _tiktok_age_range(adgroup.get("age_groups"))

        date = None
        if adgroup.get("schedule_start_time"):
            date = adgroup["schedule_start_time"][:10]
        if not date:
            date = _parse_date(m.get("adgroup_name")) or _parse_date(m.get("campaign_name"))

        rows.append({
            "platform": "tiktok",
            "campaign_id": adgroup.get("campaign_id"),
            "campaign_name": (m.get("campaign_name") or "").strip(),
            "adset_id": adgroup_id,
            "adset_name": (m.get("adgroup_name") or "").strip(),
            "date": date,
            "status": "ACTIVE" if adgroup.get("operation_status") == "ENABLE" else "PAUSED",
            "objective": adgroup.get("optimization_goal"),
            "optimization_goal": adgroup.get("optimization_goal"),
            "bid_strategy": adgroup.get("bid_display_mode"),
            "budget": budget,
            "budget_period": budget_period,
            "age_min": age_min,
            "age_max": age_max,
            "platforms": ["tiktok"],
            "spend": spend,
            "impressions": int(float(m.get("impressions", 0) or 0)),
            "reach": int(float(m.get("reach", 0) or 0)),
            "ad_view_label": "6초 조회",
            "ad_view_count": int(views),
            "views": int(views),
            "completion_views": int(completion),
            "completion_rate": round(completion / views * 100, 1) if views else 0.0,
            "cpv": round(spend / views, 2) if views else None,
        })
    return rows


def render(rows):
    template_path = os.path.join(ROOT, "src", "template.html")
    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    rows_json = json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c")
    updated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    html = html.replace("/*__CAMPAIGNS_JSON__*/[]/*__END__*/", rows_json)
    html = html.replace("__UPDATED_AT__", updated_at)

    out_path = os.path.join(ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out_path} ({len(rows)} ad sets, updated_at={updated_at})")


if __name__ == "__main__":
    rows = build_meta_rows() + build_tiktok_rows()
    render(rows)
