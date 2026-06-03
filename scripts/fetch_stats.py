import os
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

BASE_URL = "https://api.naver.com"
KST = timezone(timedelta(hours=9))


def _sign(secret_key: str, timestamp: str, method: str, path: str) -> str:
    message = f"{timestamp}.{method}.{path}"
    raw = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(raw).decode()


def _headers(customer_id: str, access_license: str, secret_key: str, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "X-Timestamp": ts,
        "X-API-KEY": access_license,
        "X-Customer": str(customer_id),
        "X-Signature": _sign(secret_key, ts, method, path),
        "Content-Type": "application/json; charset=UTF-8",
    }


def api_get(customer_id, access_license, secret_key, path, params=None):
    if params:
        path_with_qs = path + "?" + urllib.parse.urlencode(params)
    else:
        path_with_qs = path
    url = BASE_URL + path_with_qs
    headers = _headers(customer_id, access_license, secret_key, "GET", path_with_qs)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def get_campaigns(customer_id, access_license, secret_key):
    data = api_get(customer_id, access_license, secret_key, "/ncc/campaigns")
    return data if isinstance(data, list) else data.get("campaigns", [])


def get_ad_groups(customer_id, access_license, secret_key, campaign_id):
    data = api_get(customer_id, access_license, secret_key,
                   "/ncc/adgroups", {"nccCampaignId": campaign_id})
    return data if isinstance(data, list) else data.get("adGroups", [])


def get_campaign_stat(customer_id, access_license, secret_key, campaign_id, date_from, date_to):
    path = "/stats"
    params = {
        "ids": campaign_id,
        "fields": "clkCnt,impCnt,salesAmt,ctr,avg_cpc,convAmt,rvImpCnt",
        "timeRange": json.dumps({"since": date_from, "until": date_to}),
        "timeUnit": "date",
        "datePreset": "custom",
    }
    data = api_get(customer_id, access_license, secret_key, path, params)
    return data.get("data", [])


def get_adgroup_stat(customer_id, access_license, secret_key, adgroup_ids, date_from, date_to):
    if not adgroup_ids:
        return []
    path = "/stats"
    params = {
        "ids": ",".join(adgroup_ids),
        "fields": "clkCnt,impCnt,salesAmt,ctr,avg_cpc,convAmt,rvImpCnt",
        "timeRange": json.dumps({"since": date_from, "until": date_to}),
        "timeUnit": "total",
        "datePreset": "custom",
    }
    data = api_get(customer_id, access_license, secret_key, path, params)
    return data.get("data", [])


def aggregate_daily(stat_list):
    """Collapse per-campaign daily rows into cross-campaign daily totals."""
    by_date = {}
    for row in stat_list:
        d = row.get("datetime", "")[:10]
        if d not in by_date:
            by_date[d] = {"date": d, "cost": 0, "impressions": 0, "clicks": 0,
                          "conversions": 0, "conversion_amount": 0}
        s = row.get("stat", {})
        by_date[d]["cost"] += int(s.get("salesAmt", 0))
        by_date[d]["impressions"] += int(s.get("impCnt", 0))
        by_date[d]["clicks"] += int(s.get("clkCnt", 0))
        by_date[d]["conversions"] += int(s.get("rvImpCnt", 0))
        by_date[d]["conversion_amount"] += int(s.get("convAmt", 0))
    result = sorted(by_date.values(), key=lambda x: x["date"])
    for row in result:
        row["ctr"] = round(row["clicks"] / row["impressions"] * 100, 2) if row["impressions"] else 0
        row["cpc"] = round(row["cost"] / row["clicks"]) if row["clicks"] else 0
        row["roas"] = round(row["conversion_amount"] / row["cost"] * 100, 1) if row["cost"] else None
    return result


def main():
    customer_id = os.environ["NAVER_CUSTOMER_ID"]
    access_license = os.environ["NAVER_ACCESS_LICENSE"]
    secret_key = os.environ["NAVER_SECRET_KEY"]
    date_from = os.environ["DATE_FROM"]   # YYYY-MM-DD
    date_to = os.environ["DATE_TO"]       # YYYY-MM-DD

    now_kst = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    print(f"Fetching stats {date_from} ~ {date_to}")

    campaigns = get_campaigns(customer_id, access_license, secret_key)
    print(f"Found {len(campaigns)} campaigns")

    all_daily_rows = []
    adgroup_rows = []

    for campaign in campaigns:
        cid = campaign["nccCampaignId"]
        cname = campaign.get("campaignName", cid)

        # Daily stats per campaign
        daily = get_campaign_stat(customer_id, access_license, secret_key, cid, date_from, date_to)
        all_daily_rows.extend(daily)

        # Ad groups under this campaign
        adgroups = get_ad_groups(customer_id, access_license, secret_key, cid)
        if not adgroups:
            continue

        agids = [ag["nccAdgroupId"] for ag in adgroups]
        ag_stats = get_adgroup_stat(customer_id, access_license, secret_key, agids, date_from, date_to)

        # Build id→stat map
        stat_map = {row["id"]: row.get("stat", {}) for row in ag_stats}

        for ag in adgroups:
            agid = ag["nccAdgroupId"]
            agname = ag.get("adgroupName", agid)
            s = stat_map.get(agid, {})
            cost = int(s.get("salesAmt", 0))
            clicks = int(s.get("clkCnt", 0))
            imps = int(s.get("impCnt", 0))
            convs = int(s.get("rvImpCnt", 0))
            conv_amt = int(s.get("convAmt", 0))
            adgroup_rows.append({
                "campaign_name": cname,
                "adgroup_name": agname,
                "cost": cost,
                "impressions": imps,
                "clicks": clicks,
                "ctr": round(clicks / imps * 100, 2) if imps else 0,
                "cpc": round(cost / clicks) if clicks else 0,
                "conversions": convs,
                "conversion_amount": conv_amt,
                "cpa": round(cost / convs) if convs else None,
                "roas": round(conv_amt / cost * 100, 1) if cost and conv_amt else None,
            })

    # Sort ad groups by cost desc
    adgroup_rows.sort(key=lambda x: x["cost"], reverse=True)

    daily_totals = aggregate_daily(all_daily_rows)

    # Overall summary
    total_cost = sum(r["cost"] for r in adgroup_rows)
    total_imps = sum(r["impressions"] for r in adgroup_rows)
    total_clicks = sum(r["clicks"] for r in adgroup_rows)
    total_convs = sum(r["conversions"] for r in adgroup_rows)
    total_conv_amt = sum(r["conversion_amount"] for r in adgroup_rows)

    summary = {
        "cost": total_cost,
        "impressions": total_imps,
        "clicks": total_clicks,
        "ctr": round(total_clicks / total_imps * 100, 2) if total_imps else 0,
        "cpc": round(total_cost / total_clicks) if total_clicks else 0,
        "conversions": total_convs,
        "conversion_amount": total_conv_amt,
        "roas": round(total_conv_amt / total_cost * 100, 1) if total_cost and total_conv_amt else None,
    }

    output = {
        "fetched_at": now_kst,
        "date_from": date_from,
        "date_to": date_to,
        "summary": summary,
        "daily": daily_totals,
        "ad_groups": adgroup_rows,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "stats.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("Saved data/stats.json")


if __name__ == "__main__":
    main()
