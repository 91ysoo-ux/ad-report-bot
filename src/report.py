"""Pulls Meta and TikTok ad performance and rewrites docs/index.html.

Two datasets are embedded in the page:
- SETTINGS: one row per ad set / ad group (settings + schedule), no metrics.
- DAILY: one row per ad set per day with spend and view metrics.
Month totals are computed from DAILY, so a campaign that runs across a month
boundary is split into the months it actually spent in.

Run manually for local testing (reads .env), or via GitHub Actions
(reads the same variable names from repo secrets).
"""
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META_API_VER = "v21.0"
TIKTOK_API_VER = "v1.3"
KST = timezone(timedelta(hours=9))
TIKTOK_START = date(2025, 9, 30)

_DATE8 = re.compile(r"^(\d{8})")
_DATE6 = re.compile(r"^(\d{6})")
_AGE_RANGE = re.compile(r"AGE_(\d+)_(\d+)")


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


def _parse_name_date(name):
    """Names carry a YYMMDD / YYYYMMDD start-date prefix; fallback only."""
    if not name:
        return None
    name = name.strip()
    m = _DATE8.match(name)
    if m:
        s = m.group(1)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    m = _DATE6.match(name)
    if m:
        s = m.group(1)
        return f"20{s[:2]}-{s[2:4]}-{s[4:6]}"
    return None


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------- Meta ----------------

def meta_get(path, params):
    p = dict(params)
    p["access_token"] = META_TOKEN
    r = requests.get(f"https://graph.facebook.com/{META_API_VER}/{path}", params=p, timeout=60)
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


def _action(row, field, action_type="video_view"):
    for item in row.get(field) or []:
        if item.get("action_type") == action_type:
            return _num(item.get("value"))
    return 0.0


def _meta_budget(adset, campaign):
    """Ad-set budget (ABO) takes priority; falls back to the campaign budget (CBO)."""
    for src, label in ((adset, ""), (campaign, "(캠페인 예산)")):
        daily = int(_num(src.get("daily_budget")))
        if daily:
            return daily, "일" + label
        lifetime = int(_num(src.get("lifetime_budget")))
        if lifetime:
            return lifetime, "총" + label
    return None, None


def build_meta():
    campaigns = {c["id"]: c for c in meta_get_all(f"{META_ACCOUNT}/campaigns", {
        "fields": "id,name,effective_status,objective,daily_budget,lifetime_budget",
        "limit": 200,
    })}
    adsets = meta_get_all(f"{META_ACCOUNT}/adsets", {
        "fields": (
            "id,campaign_id,name,daily_budget,lifetime_budget,bid_strategy,"
            "optimization_goal,effective_status,targeting,start_time,end_time"
        ),
        "limit": 200,
    })
    daily_rows = meta_get_all(f"{META_ACCOUNT}/insights", {
        "level": "adset",
        "date_preset": "maximum",
        "time_increment": 1,
        "fields": (
            "adset_id,spend,impressions,actions,video_play_actions,"
            "video_p100_watched_actions"
        ),
        "limit": 500,
    })

    daily = []
    active_ids = set()
    for r in daily_rows:
        spend = _num(r.get("spend"))
        plays = _action(r, "video_play_actions")
        if not (spend or plays):
            continue
        active_ids.add(r["adset_id"])
        daily.append([
            "meta:" + r["adset_id"], r["date_start"], round(spend),
            int(_num(r.get("impressions"))), int(plays),
            int(_action(r, "actions")), int(_action(r, "video_p100_watched_actions")),
        ])

    settings = []
    for a in adsets:
        if a["id"] not in active_ids:
            continue
        campaign = campaigns.get(a.get("campaign_id"), {})
        targeting = a.get("targeting") or {}
        budget, budget_period = _meta_budget(a, campaign)
        settings.append({
            "key": "meta:" + a["id"],
            "platform": "meta",
            "name": a.get("name"),
            "campaign_name": campaign.get("name"),
            "start": (a.get("start_time") or "")[:10] or _parse_name_date(a.get("name")),
            "end": (a.get("end_time") or "")[:10] or None,
            "status": "ACTIVE" if a.get("effective_status") == "ACTIVE" else "PAUSED",
            "objective": campaign.get("objective"),
            "optimization_goal": a.get("optimization_goal"),
            "bid_strategy": a.get("bid_strategy"),
            "budget": budget,
            "budget_period": budget_period,
            "age_min": targeting.get("age_min"),
            "age_max": targeting.get("age_max"),
            "placements": targeting.get("publisher_platforms") or ["instagram"],
        })
    return settings, daily


# ---------------- TikTok ----------------

def tiktok_get(path, params):
    r = requests.get(
        f"https://business-api.tiktok.com/open_api/{TIKTOK_API_VER}/{path}",
        headers={"Access-Token": TIKTOK_TOKEN},
        params={k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in params.items()},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"TikTok API error on {path}: {data.get('code')} {data.get('message')}")
    return data["data"]


def tiktok_get_all(path, params, page_size=500):
    query = dict(params, page_size=page_size)
    rows, page = [], 1
    while True:
        query["page"] = page
        data = tiktok_get(path, query)
        rows.extend(data.get("list", []))
        if page >= (data.get("page_info") or {}).get("total_page", page):
            return rows
        page += 1


def _tiktok_budget(adgroup):
    budget = int(_num(adgroup.get("budget")))
    mode = adgroup.get("budget_mode")
    if budget and mode == "BUDGET_MODE_DYNAMIC_DAILY_BUDGET":
        return budget, "일"
    if budget and mode == "BUDGET_MODE_TOTAL":
        return budget, "총"
    return None, None


def _tiktok_age(age_groups):
    mins, maxes = [], []
    for g in age_groups or []:
        m = _AGE_RANGE.match(g)
        if m:
            mins.append(int(m.group(1)))
            maxes.append(int(m.group(2)))
    return (min(mins), max(maxes)) if mins else (None, None)


def build_tiktok():
    if not TIKTOK_TOKEN or not TIKTOK_ADVERTISER_ID:
        return [], []

    adgroups = tiktok_get_all("adgroup/get/", {"advertiser_id": TIKTOK_ADVERTISER_ID}, page_size=100)
    campaigns = {c["campaign_id"]: c for c in tiktok_get_all(
        "campaign/get/", {"advertiser_id": TIKTOK_ADVERTISER_ID}, page_size=100)}

    # Day-level reports are capped at 30 days per request.
    daily = []
    active_ids = set()
    today = datetime.now(KST).date()
    chunk_start = TIKTOK_START
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=29), today)
        for r in tiktok_get_all("report/integrated/get/", {
            "advertiser_id": TIKTOK_ADVERTISER_ID,
            "report_type": "BASIC",
            "data_level": "AUCTION_ADGROUP",
            "dimensions": ["adgroup_id", "stat_time_day"],
            "metrics": ["spend", "impressions", "video_play_actions",
                        "video_watched_6s", "video_views_p100"],
            "start_date": chunk_start.isoformat(),
            "end_date": chunk_end.isoformat(),
        }):
            m = r["metrics"]
            spend = _num(m.get("spend"))
            plays = _num(m.get("video_play_actions"))
            if not (spend or plays):
                continue
            gid = r["dimensions"]["adgroup_id"]
            active_ids.add(gid)
            daily.append([
                "tiktok:" + gid, r["dimensions"]["stat_time_day"][:10], round(spend),
                int(_num(m.get("impressions"))), int(plays),
                int(_num(m.get("video_watched_6s"))), int(_num(m.get("video_views_p100"))),
            ])
        chunk_start = chunk_end + timedelta(days=1)

    settings = []
    for g in adgroups:
        if g["adgroup_id"] not in active_ids:
            continue
        budget, budget_period = _tiktok_budget(g)
        age_min, age_max = _tiktok_age(g.get("age_groups"))
        campaign = campaigns.get(g.get("campaign_id"), {})
        settings.append({
            "key": "tiktok:" + g["adgroup_id"],
            "platform": "tiktok",
            "name": (g.get("adgroup_name") or "").strip(),
            "campaign_name": (g.get("campaign_name") or campaign.get("campaign_name") or "").strip(),
            "start": (g.get("schedule_start_time") or "")[:10] or _parse_name_date(g.get("adgroup_name")),
            "end": (g.get("schedule_end_time") or "")[:10] or None,
            "status": "ACTIVE" if g.get("operation_status") == "ENABLE"
                      and campaign.get("operation_status") == "ENABLE" else "PAUSED",
            "objective": campaign.get("objective_type"),
            "optimization_goal": g.get("optimization_goal"),
            "bid_strategy": g.get("bid_display_mode"),
            "budget": budget,
            "budget_period": budget_period,
            "age_min": age_min,
            "age_max": age_max,
            "placements": ["tiktok"],
        })
    return settings, daily


# ---------------- render ----------------

def _embed(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def render(settings, daily):
    with open(os.path.join(ROOT, "src", "template.html"), encoding="utf-8") as f:
        html = f.read()

    updated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    html = html.replace("/*__SETTINGS_JSON__*/[]", _embed(settings))
    html = html.replace("/*__DAILY_JSON__*/[]", _embed(daily))
    html = html.replace("__UPDATED_AT__", updated_at)

    out_path = os.path.join(ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out_path} ({len(settings)} settings, {len(daily)} daily rows, {updated_at})")


if __name__ == "__main__":
    meta_settings, meta_daily = build_meta()
    tiktok_settings, tiktok_daily = build_tiktok()
    render(meta_settings + tiktok_settings, meta_daily + tiktok_daily)
