import os
import sys
import json
import time
import hmac
import hashlib
import base64
from datetime import datetime, timedelta, timezone

import urllib.request
import urllib.parse
import urllib.error

BASE_URL = "https://api.naver.com"
KST = timezone(timedelta(hours=9))

STAT_FIELDS = ["clkCnt", "impCnt", "salesAmt", "ctr", "avgCpc", "rvImpCnt", "convAmt"]


# ── 서명 ──────────────────────────────────────────────────────────────────────
def _sign(secret_key: str, timestamp: str, method: str, path: str) -> str:
    message = f"{timestamp}.{method}.{path}"
    raw = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(raw).decode()


def _make_headers(customer_id, access_license, secret_key, method, path):
    ts = str(int(time.time() * 1000))
    return {
        "X-Timestamp": ts,
        "X-API-KEY": access_license,
        "X-Customer": str(customer_id),
        "X-Signature": _sign(secret_key, ts, method, path),
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
    }


# ── GET 요청 (urllib, 파라미터는 list of (key, value) 튜플) ──────────────────
def api_get(cid, lic, sec, path, pairs=None):
    """
    pairs: list of (key, value) tuples — 같은 키를 여러 번 쓸 수 있음
    """
    if pairs:
        qs = urllib.parse.urlencode(pairs)
        full_url = BASE_URL + path + "?" + qs
    else:
        full_url = BASE_URL + path

    headers = _make_headers(cid, lic, sec, "GET", path)  # 서명은 path만
    req = urllib.request.Request(full_url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            print(f"  GET {path} → {resp.status}")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"  ERROR {path} → HTTP {e.code}: {body[:300]}", file=sys.stderr)
        raise


# ── 캠페인 목록 ───────────────────────────────────────────────────────────────
def get_campaigns(cid, lic, sec):
    data = api_get(cid, lic, sec, "/ncc/campaigns")
    if isinstance(data, list):
        return data
    return data.get("campaigns", data.get("items", []))


# ── 광고그룹 목록 ─────────────────────────────────────────────────────────────
def get_ad_groups(cid, lic, sec, campaign_id):
    data = api_get(cid, lic, sec, "/ncc/adgroups",
                   [("nccCampaignId", campaign_id)])
    if isinstance(data, list):
        return data
    return data.get("adGroups", data.get("items", []))


# ── 통계 조회 ─────────────────────────────────────────────────────────────────
def get_stats(cid, lic, sec, ids: list, time_unit: str, date_from: str, date_to: str):
    """
    ids 최대 100개씩 분할 호출 후 합산
    """
    all_rows = []
    chunk_size = 100

    for i in range(0, max(len(ids), 1), chunk_size):
        chunk = ids[i:i + chunk_size] if ids else []

        # ids → 반복 파라미터: ids=id1&ids=id2&...
        pairs = [("ids", x) for x in chunk]
        # fields → 반복 파라미터: fields=clkCnt&fields=impCnt&...
        pairs += [("fields", f) for f in STAT_FIELDS]
        pairs += [
            ("timeUnit", time_unit),
            ("timeRange", json.dumps({"since": date_from, "until": date_to},
                                     separators=(",", ":"))),
        ]

        resp = api_get(cid, lic, sec, "/stats", pairs)
        rows = resp if isinstance(resp, list) else resp.get("data", [])
        all_rows.extend(rows)

    return all_rows


# ── 일별 집계 ─────────────────────────────────────────────────────────────────
def aggregate_daily(stat_rows):
    by_date = {}
    for row in stat_rows:
        d = (row.get("datetime") or row.get("date") or "")[:10]
        if not d:
            continue
        s = row.get("stat") or row
        if d not in by_date:
            by_date[d] = {"date": d, "cost": 0, "impressions": 0,
                          "clicks": 0, "conversions": 0, "conversion_amount": 0}
        by_date[d]["cost"]              += _int(s.get("salesAmt"))
        by_date[d]["impressions"]       += _int(s.get("impCnt"))
        by_date[d]["clicks"]            += _int(s.get("clkCnt"))
        by_date[d]["conversions"]       += _int(s.get("rvImpCnt"))
        by_date[d]["conversion_amount"] += _int(s.get("convAmt"))

    result = sorted(by_date.values(), key=lambda x: x["date"])
    for r in result:
        r["ctr"]  = round(r["clicks"] / r["impressions"] * 100, 2) if r["impressions"] else 0
        r["cpc"]  = round(r["cost"] / r["clicks"])                  if r["clicks"]      else 0
        r["roas"] = round(r["conversion_amount"] / r["cost"] * 100, 1) \
                    if r["cost"] and r["conversion_amount"] else None
    return result


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    customer_id    = os.environ["NAVER_CUSTOMER_ID"]
    access_license = os.environ["NAVER_ACCESS_LICENSE"]
    secret_key     = os.environ["NAVER_SECRET_KEY"]
    date_from      = os.environ["DATE_FROM"]
    date_to        = os.environ["DATE_TO"]
    now_kst = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    print(f"=== 수집 시작 {date_from} ~ {date_to} ===")

    # 1) 캠페인 목록
    campaigns = get_campaigns(customer_id, access_license, secret_key)
    print(f"캠페인 {len(campaigns)}개")
    campaign_name_map = {c["nccCampaignId"]: c.get("campaignName", c["nccCampaignId"])
                         for c in campaigns}
    all_campaign_ids = list(campaign_name_map.keys())

    # 2) 일별 통계 (차트용)
    print("일별 통계 수집 중...")
    daily_rows = get_stats(customer_id, access_license, secret_key,
                           all_campaign_ids, "date", date_from, date_to)
    print(f"  → {len(daily_rows)}행")
    daily_totals = aggregate_daily(daily_rows)

    # 3) 광고그룹별 합계
    print("광고그룹 통계 수집 중...")
    adgroup_rows = []

    for campaign in campaigns:
        cid_val = campaign["nccCampaignId"]
        cname   = campaign_name_map[cid_val]
        adgroups = get_ad_groups(customer_id, access_license, secret_key, cid_val)
        if not adgroups:
            continue
        print(f"  [{cname}] 광고그룹 {len(adgroups)}개")

        agids = [ag["nccAdgroupId"] for ag in adgroups]
        ag_stats = get_stats(customer_id, access_license, secret_key,
                             agids, "total", date_from, date_to)
        stat_map = {}
        for row in ag_stats:
            rid = row.get("id") or row.get("nccAdgroupId")
            stat_map[rid] = row.get("stat") or row

        for ag in adgroups:
            agid   = ag["nccAdgroupId"]
            agname = ag.get("adgroupName", agid)
            s      = stat_map.get(agid, {})
            cost     = _int(s.get("salesAmt"))
            clicks   = _int(s.get("clkCnt"))
            imps     = _int(s.get("impCnt"))
            convs    = _int(s.get("rvImpCnt"))
            conv_amt = _int(s.get("convAmt"))
            adgroup_rows.append({
                "campaign_name":     cname,
                "adgroup_name":      agname,
                "cost":              cost,
                "impressions":       imps,
                "clicks":            clicks,
                "ctr":               round(clicks / imps * 100, 2) if imps   else 0,
                "cpc":               round(cost / clicks)          if clicks  else 0,
                "conversions":       convs,
                "conversion_amount": conv_amt,
                "cpa":               round(cost / convs)           if convs   else None,
                "roas":              round(conv_amt / cost * 100, 1)
                                     if cost and conv_amt          else None,
            })

    adgroup_rows.sort(key=lambda x: x["cost"], reverse=True)

    # 4) 전체 요약
    total_cost     = sum(r["cost"]              for r in adgroup_rows)
    total_imps     = sum(r["impressions"]       for r in adgroup_rows)
    total_clicks   = sum(r["clicks"]            for r in adgroup_rows)
    total_convs    = sum(r["conversions"]       for r in adgroup_rows)
    total_conv_amt = sum(r["conversion_amount"] for r in adgroup_rows)

    summary = {
        "cost":              total_cost,
        "impressions":       total_imps,
        "clicks":            total_clicks,
        "ctr":               round(total_clicks / total_imps * 100, 2) if total_imps   else 0,
        "cpc":               round(total_cost / total_clicks)          if total_clicks  else 0,
        "conversions":       total_convs,
        "conversion_amount": total_conv_amt,
        "roas":              round(total_conv_amt / total_cost * 100, 1)
                             if total_cost and total_conv_amt else None,
    }

    output = {
        "fetched_at": now_kst,
        "date_from":  date_from,
        "date_to":    date_to,
        "summary":    summary,
        "daily":      daily_totals,
        "ad_groups":  adgroup_rows,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "stats.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"=== 완료: 광고그룹 {len(adgroup_rows)}개, 일별 {len(daily_totals)}일 ===")
    print(f"총 광고비 {total_cost:,}원 / 전환 {total_convs}건 / 전환액 {total_conv_amt:,}원")


if __name__ == "__main__":
    main()
