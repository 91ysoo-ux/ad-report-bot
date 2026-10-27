"""Pulls Meta and TikTok ad performance and rewrites docs/index.html.

Three datasets are embedded in the page:
- SETTINGS: one row per ad set / ad group (settings + schedule), no metrics.
- DAILY: one row per ad set per day with spend and view metrics.
Month totals are computed from DAILY, so a campaign that runs across a month
boundary is split into the months it actually spent in.
- ORGANIC: one row per post with its organic (non-ad) views. Instagram comes from
  reel insights, TikTok from the post play count (scraped via Apify) minus ad plays.

Run manually for local testing (reads .env), or via GitHub Actions
(reads the same variable names from repo secrets).
"""
import json
import os
import re
from io import BytesIO
from datetime import date, datetime, timedelta, timezone

import requests
from PIL import Image

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


# ---------------- organic views ----------------

IG_USER_ID = os.environ.get("IG_USER_ID") or "17841429803805358"
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
TIKTOK_HANDLE = "goldstarbeer_official"
ORGANIC_SINCE = "2025-09-01"
ORGANIC_CACHE = os.path.join(ROOT, "data", "organic_cache.json")
# Old TikTok posts barely move; re-scraping them would only burn Apify credit.
TIKTOK_REFRESH_DAYS = 45
# Platform image URLs are signed and expire, so thumbnails are saved next to the page.
THUMB_DIR = os.path.join(ROOT, "docs", "thumbs")
_INVISIBLE = re.compile(r"[\s\u3164\u115f\u1160\u200b\ufeff]+")


def _kst_date(ts):
    """'2026-09-21T08:08:54+0000' or '...Z' -> KST calendar date."""
    ts = ts.replace("Z", "+00:00").replace("+0000", "+00:00")
    return datetime.fromisoformat(ts).astimezone(KST).date().isoformat()


def _first_line(text, limit=40):
    return next((l.strip() for l in (text or "").splitlines() if l.strip()), "")[:limit]


def _write_thumb(data, path):
    """Covers come in at several MB; a card only needs a small JPEG."""
    img = Image.open(BytesIO(data)).convert("RGB")
    img.thumbnail((360, 640))
    img.save(path, "JPEG", quality=80, optimize=True)


def _save_thumb(url, name):
    """Download once; returns the page-relative path, or None if there is no image."""
    path = os.path.join(THUMB_DIR, name)
    if not os.path.exists(path) and url:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            os.makedirs(THUMB_DIR, exist_ok=True)
            _write_thumb(r.content, path)
        except (requests.RequestException, OSError) as e:
            print(f"thumbnail {name} skipped: {e}")
    return f"thumbs/{name}" if os.path.exists(path) else None


def _caption_key(text):
    """Ad posts copy the reel's caption, so the caption ties an ad post back to its reel."""
    return _INVISIBLE.sub("", text or "")[:20]


def build_instagram_organic():
    """Own reels' insights. Meta leaves ad activity out of media insights, so these views are organic."""
    ad_keys, caption_keys = {}, {}
    for a in meta_get_all(f"{META_ACCOUNT}/ads", {
        "fields": "adset_id,creative{effective_instagram_media_id}", "limit": 200,
    }):
        mid = (a.get("creative") or {}).get("effective_instagram_media_id")
        if mid:
            ad_keys.setdefault(mid, set()).add("meta:" + a["adset_id"])
    # Ads made as separate ad posts don't point at the reel; match them by caption instead.
    for mid, keys in ad_keys.items():
        try:
            key = _caption_key(meta_get(mid, {"fields": "caption"}).get("caption"))
        except requests.HTTPError:
            continue
        if key:
            caption_keys.setdefault(key, set()).update(keys)

    rows = []
    for m in meta_get_all(f"{IG_USER_ID}/media", {
        "fields": "id,timestamp,media_product_type,permalink,caption,thumbnail_url,media_url", "limit": 100,
    }):
        day = _kst_date(m["timestamp"])
        if m.get("media_product_type") != "REELS" or day < ORGANIC_SINCE:
            continue
        try:
            insights = meta_get(f"{m['id']}/insights", {"metric": "views,likes,shares"})["data"]
        except requests.HTTPError as e:
            print(f"instagram insights skipped for {m['id']}: {e}")
            continue
        v = {i["name"]: i["values"][0]["value"] for i in insights}
        rows.append({
            "platform": "meta", "id": m["id"], "date": day, "total": None,
            "organic": int(v.get("views") or 0), "likes": int(v.get("likes") or 0),
            "shares": int(v.get("shares") or 0), "url": m.get("permalink"),
            "title": _first_line(m.get("caption")),
            "keys": sorted(ad_keys.get(m["id"], set()) | caption_keys.get(_caption_key(m.get("caption")), set())),
            "thumb": _save_thumb(m.get("thumbnail_url") or m.get("media_url"), f"ig_{m['id']}.jpg"),
        })
    return rows


def _apify_tiktok(item_ids):
    r = requests.post(
        "https://api.apify.com/v2/acts/clockworks~tiktok-scraper/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN, "timeout": 280},
        json={
            # TikTok resolves the post by id, the handle only has to be well-formed.
            "postURLs": [f"https://www.tiktok.com/@{TIKTOK_HANDLE}/video/{i}" for i in item_ids],
            "resultsPerPage": 1,
            "shouldDownloadVideos": False, "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False, "shouldDownloadSlideshowImages": False,
        },
        timeout=300,
    )
    r.raise_for_status()
    return {str(it["id"]): it for it in r.json() if it.get("id") and not it.get("error")}


def build_tiktok_organic(tiktok_daily):
    """Spark Ads add their plays to the post itself, so organic = post play count - ad plays."""
    if not TIKTOK_TOKEN or not TIKTOK_ADVERTISER_ID:
        return []
    item_keys = {}
    for a in tiktok_get_all("ad/get/", {"advertiser_id": TIKTOK_ADVERTISER_ID}, page_size=100):
        if a.get("tiktok_item_id"):
            item_keys.setdefault(str(a["tiktok_item_id"]), set()).add("tiktok:" + a["adgroup_id"])

    ad_plays, last_day = {}, {}
    for key, day, _spend, _imp, plays, _tp, _comp in tiktok_daily:
        ad_plays[key] = ad_plays.get(key, 0) + plays
        last_day[key] = max(last_day.get(key, ""), day)

    cache = {}
    if os.path.exists(ORGANIC_CACHE):
        with open(ORGANIC_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    today = datetime.now(KST).date()
    cutoff = (today - timedelta(days=TIKTOK_REFRESH_DAYS)).isoformat()
    stale = [i for i, keys in item_keys.items()
             if i not in cache or max(last_day.get(k, "") for k in keys) >= cutoff
             or not os.path.exists(os.path.join(THUMB_DIR, f"tt_{i}.jpg"))]
    if stale and APIFY_TOKEN:
        try:
            for i, it in _apify_tiktok(stale).items():
                cache[i] = {
                    "total": int(it.get("playCount") or 0), "likes": int(it.get("diggCount") or 0),
                    "shares": int(it.get("shareCount") or 0), "date": _kst_date(it["createTimeISO"]),
                    "url": it.get("webVideoUrl"), "title": _first_line(it.get("text")),
                    "fetched": today.isoformat(),
                }
                _save_thumb((it.get("videoMeta") or {}).get("coverUrl"), f"tt_{i}.jpg")
        except requests.RequestException as e:
            print(f"apify tiktok scrape failed, using cached counts: {e}")
    elif stale:
        print("APIFY_TOKEN missing; TikTok organic views use cached counts only")
    os.makedirs(os.path.dirname(ORGANIC_CACHE), exist_ok=True)
    with open(ORGANIC_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)

    # One ad group can carry several posts (e.g. ep1+ep2), and its plays can't be split
    # between them, so posts that share ad groups are measured together as one row.
    groups = []
    for i, keys in item_keys.items():
        merged = {"ids": {i}, "keys": set(keys)}
        for g in [g for g in groups if g["keys"] & keys]:
            groups.remove(g)
            merged["ids"] |= g["ids"]
            merged["keys"] |= g["keys"]
        groups.append(merged)

    rows = []
    for g in groups:
        posts = [cache[i] for i in sorted(g["ids"]) if i in cache]
        if len(posts) != len(g["ids"]):
            continue  # a post the scraper couldn't read (deleted or private)
        total = sum(c["total"] for c in posts)
        # Ad groups that ran before TIKTOK_START have no ad plays on record, so organic is unknown.
        known = all(k in ad_plays for k in g["keys"])
        paid = sum(ad_plays.get(k, 0) for k in g["keys"])
        rows.append({
            "platform": "tiktok", "id": ",".join(sorted(g["ids"])), "date": min(c["date"] for c in posts),
            "total": total, "organic": max(total - paid, 0) if known else None,
            "likes": sum(c["likes"] for c in posts), "shares": sum(c["shares"] for c in posts),
            "url": posts[0]["url"], "title": " / ".join(c["title"] for c in posts)[:60],
            "keys": sorted(g["keys"]),
            "thumb": next((f"thumbs/tt_{i}.jpg" for i in sorted(g["ids"])
                           if os.path.exists(os.path.join(THUMB_DIR, f"tt_{i}.jpg"))), None),
        })
    return rows


def _optional(build, *args):
    """Organic views are a bonus: a missing permission must not stop the ad report itself."""
    try:
        return build(*args)
    except (requests.RequestException, RuntimeError, KeyError, ValueError) as e:
        print(f"organic views skipped ({build.__name__}): {e}")
        return []


# ---------------- render ----------------

def _embed(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def render(settings, daily, organic):
    with open(os.path.join(ROOT, "src", "template.html"), encoding="utf-8") as f:
        html = f.read()

    updated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    html = html.replace("/*__SETTINGS_JSON__*/[]", _embed(settings))
    html = html.replace("/*__DAILY_JSON__*/[]", _embed(daily))
    html = html.replace("/*__ORGANIC_JSON__*/[]", _embed(organic))
    html = html.replace("__UPDATED_AT__", updated_at)

    out_path = os.path.join(ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out_path} ({len(settings)} settings, {len(daily)} daily rows, "
          f"{len(organic)} organic posts, {updated_at})")


if __name__ == "__main__":
    meta_settings, meta_daily = build_meta()
    tiktok_settings, tiktok_daily = build_tiktok()
    organic = _optional(build_instagram_organic) + _optional(build_tiktok_organic, tiktok_daily)
    render(meta_settings + tiktok_settings, meta_daily + tiktok_daily, organic)
