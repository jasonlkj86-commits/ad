import os
import sys
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

BASE_URL = "https://api.naver.com"
KST = timezone(timedelta(hours=9))

# 캠페인 타입 한글 매핑
TYPE_MAP = {
    "WEB_SITE":        "파워링크",   # 실제 API 값
    "POWERLINK":       "파워링크",
    "POWER_LINK":      "파워링크",
    "SHOPPING":        "쇼핑검색",
    "SHOPPING_SEARCH": "쇼핑검색",
    "PLACE_SEARCH":    "플레이스",
    "PLACE":           "플레이스",
    "BRAND_SEARCH":    "브랜드검색",
    "BRAND":           "브랜드검색",
    "DISPLAY":         "디스플레이",
}

# 기본 필드 (probe로 확장)
BASE_FIELDS = ["clkCnt", "impCnt", "salesAmt"]
OPTIONAL_FIELDS = ["convAmt", "rvImpCnt", "orderCnt", "convCnt"]


# ── 서명 ──────────────────────────────────────────────────────────────────────
def _sign(secret_key, timestamp, method, path):
    msg = f"{timestamp}.{method}.{path}"
    raw = hmac.new(secret_key.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(raw).decode()


def _headers(customer_id, access_license, secret_key, path):
    ts = str(int(time.time() * 1000))
    return {
        "X-Timestamp":  ts,
        "X-API-KEY":    access_license,
        "X-Customer":   str(customer_id),
        "X-Signature":  _sign(secret_key, ts, "GET", path),
        "Content-Type": "application/json; charset=UTF-8",
        "Accept":       "application/json",
    }


def api_get(cid, lic, sec, path, params: dict = None):
    qs  = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = BASE_URL + path + qs
    req = urllib.request.Request(url, headers=_headers(cid, lic, sec, path))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return json.loads(body)
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  ERROR {path} → HTTP {e.code}: {err[:300]}", file=sys.stderr)
        raise


# ── 유효 필드 탐색 (최초 1회) ─────────────────────────────────────────────────
def probe_fields(cid, lic, sec, sample_id, date_from, date_to):
    since = date_from.replace("-", "")
    until = date_to.replace("-", "")
    tr = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    valid = list(BASE_FIELDS)
    for f in OPTIONAL_FIELDS:
        candidate = json.dumps(valid + [f], separators=(",", ":"))
        try:
            api_get(cid, lic, sec, "/stats",
                    {"ids": sample_id, "fields": candidate,
                     "timeUnit": "total", "timeRange": tr})
            valid.append(f)
            print(f"  필드 {f} ✓")
        except Exception:
            print(f"  필드 {f} ✗")
    print(f"  확정 필드: {valid}")
    return json.dumps(valid, separators=(",", ":")), valid


# ── 통계 조회 ─────────────────────────────────────────────────────────────────
def get_stats(cid, lic, sec, obj_id, time_unit, date_from, date_to, active_fields_json):
    since = date_from.replace("-", "")
    until = date_to.replace("-", "")
    tr    = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    params = {"ids": obj_id, "fields": active_fields_json,
              "timeUnit": time_unit, "timeRange": tr}
    resp = api_get(cid, lic, sec, "/stats", params)
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict) and "data" in resp:
        return resp["data"]
    # 단일 dict인 경우 리스트로 감쌈
    if isinstance(resp, dict):
        return [resp]
    return []


# ── 목록 API ──────────────────────────────────────────────────────────────────
def get_campaigns(cid, lic, sec):
    d = api_get(cid, lic, sec, "/ncc/campaigns")
    return d if isinstance(d, list) else d.get("campaigns", d.get("items", []))


def get_adgroups(cid, lic, sec, campaign_id):
    d = api_get(cid, lic, sec, "/ncc/adgroups", {"nccCampaignId": campaign_id})
    return d if isinstance(d, list) else d.get("adGroups", d.get("items", []))


# ── 일별 합산 ─────────────────────────────────────────────────────────────────
def aggregate_daily(rows, valid_fields):
    by_date = {}
    for row in rows:
        s = row.get("stat") or row
        # 날짜 필드 탐색
        raw_d = (row.get("datetime") or row.get("date") or
                 s.get("datetime") or s.get("date") or "")
        d = str(raw_d)[:10]
        if not d or len(d) < 8:
            continue
        # YYYYMMDD → YYYY-MM-DD
        if len(d) == 8 and "-" not in d:
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if len(d) != 10:
            continue
        e = by_date.setdefault(d, {"date": d, "cost": 0, "impressions": 0,
                                   "clicks": 0, "conversions": 0, "conversion_amount": 0})
        e["cost"]              += _int(s.get("salesAmt"))
        e["impressions"]       += _int(s.get("impCnt"))
        e["clicks"]            += _int(s.get("clkCnt"))
        # 전환수: 여러 후보 필드 시도
        e["conversions"]       += _int(s.get("rvImpCnt") or s.get("orderCnt") or s.get("convCnt"))
        e["conversion_amount"] += _int(s.get("convAmt"))

    result = sorted(by_date.values(), key=lambda x: x["date"])
    for r in result:
        r["ctr"]  = round(r["clicks"] / r["impressions"] * 100, 2) if r["impressions"] else 0
        r["cpc"]  = round(r["cost"]   / r["clicks"])               if r["clicks"]      else 0
        r["roas"] = round(r["conversion_amount"] / r["cost"] * 100, 1) \
                    if r["cost"] and r["conversion_amount"] else None
    return result


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _sum(rows, key):
    return sum(_int(r.get(key)) for r in rows)


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
    print(f"캠페인 {len(campaigns)}개:")
    for c in campaigns:
        print(f"  {c.get('name','?')} | 타입: {c.get('campaignTp','?')}")

    # 2) 유효 필드 탐색
    print("유효 필드 확인 중...")
    first_cid = campaigns[0]["nccCampaignId"]
    active_json, valid_fields = probe_fields(
        customer_id, access_license, secret_key, first_cid, date_from, date_to)

    # 3) 일별 통계 — 광고그룹 레벨로 수집 (캠페인 레벨은 datetime 없음)
    print("일별 통계 수집 중 (광고그룹 레벨)...")
    all_daily_rows = []
    first_ag_done = False
    for c in campaigns:
        cid_val  = c["nccCampaignId"]
        adgs = get_adgroups(customer_id, access_license, secret_key, cid_val)
        for ag in adgs:
            agid = ag["nccAdgroupId"]
            rows = get_stats(customer_id, access_license, secret_key,
                             agid, "date", date_from, date_to, active_json)
            all_daily_rows.extend(rows)
            if not first_ag_done and rows:
                first_ag_done = True
                print(f"  [첫 광고그룹 첫행 키] {list(rows[0].keys())}")
                print(f"  [첫 광고그룹 첫행]   {json.dumps(rows[0], ensure_ascii=False)[:300]}")

    daily_totals = aggregate_daily(all_daily_rows, valid_fields)
    print(f"  → 일별 합산 {len(daily_totals)}일")

    # 4) 광고그룹별 통계 + 캠페인 타입 분류
    print("광고그룹 통계 수집 중...")
    adgroup_rows = []

    for c in campaigns:
        cid_val  = c["nccCampaignId"]
        cname    = c.get("name", cid_val)
        ctp_raw  = c.get("campaignTp", "")
        ctype    = TYPE_MAP.get(ctp_raw, ctp_raw or "기타")

        adgroups = get_adgroups(customer_id, access_license, secret_key, cid_val)
        if not adgroups:
            continue
        print(f"  [{ctype}] {cname}: 광고그룹 {len(adgroups)}개")

        for ag in adgroups:
            agid   = ag["nccAdgroupId"]
            agname = ag.get("name", ag.get("adgroupName", agid))
            rows   = get_stats(customer_id, access_license, secret_key,
                               agid, "total", date_from, date_to, active_json)
            s = {}
            if rows:
                s = rows[0].get("stat") or rows[0]

            cost     = _int(s.get("salesAmt"))
            clicks   = _int(s.get("clkCnt"))
            imps     = _int(s.get("impCnt"))
            # 전환수: 여러 후보 시도
            convs    = _int(s.get("rvImpCnt") or s.get("orderCnt") or s.get("convCnt") or 0)
            conv_amt = _int(s.get("convAmt"))

            adgroup_rows.append({
                "campaign_type":     ctype,
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

    # 5) 전체 요약
    tc = sum(r["cost"]              for r in adgroup_rows)
    ti = sum(r["impressions"]       for r in adgroup_rows)
    tk = sum(r["clicks"]            for r in adgroup_rows)
    tv = sum(r["conversions"]       for r in adgroup_rows)
    ta = sum(r["conversion_amount"] for r in adgroup_rows)

    summary = {
        "cost":              tc,
        "impressions":       ti,
        "clicks":            tk,
        "ctr":               round(tk / ti * 100, 2) if ti else 0,
        "cpc":               round(tc / tk)          if tk else 0,
        "conversions":       tv,
        "conversion_amount": ta,
        "roas":              round(ta / tc * 100, 1) if tc and ta else None,
    }

    # 6) 타입별 소계 (한글명 키 사용)
    types = sorted(set(r["campaign_type"] for r in adgroup_rows))
    by_type = {}
    for t in types:
        rows_t = [r for r in adgroup_rows if r["campaign_type"] == t]
        tc_t = sum(r["cost"]              for r in rows_t)
        ti_t = sum(r["impressions"]       for r in rows_t)
        tk_t = sum(r["clicks"]            for r in rows_t)
        tv_t = sum(r["conversions"]       for r in rows_t)
        ta_t = sum(r["conversion_amount"] for r in rows_t)
        by_type[t] = {
            "cost":              tc_t,
            "impressions":       ti_t,
            "clicks":            tk_t,
            "ctr":               round(tk_t / ti_t * 100, 2) if ti_t else 0,
            "cpc":               round(tc_t / tk_t)          if tk_t else 0,
            "conversions":       tv_t,
            "conversion_amount": ta_t,
            "roas":              round(ta_t / tc_t * 100, 1) if tc_t and ta_t else None,
        }

    output = {
        "fetched_at": now_kst,
        "date_from":  date_from,
        "date_to":    date_to,
        "summary":    summary,
        "by_type":    by_type,
        "daily":      daily_totals,
        "ad_groups":  adgroup_rows,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "stats.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"=== 완료: 광고그룹 {len(adgroup_rows)}개 / 일별 {len(daily_totals)}일 ===")
    print(f"총 광고비 {tc:,}원 / 전환 {tv}건 / 전환액 {ta:,}원")
    for t, s in by_type.items():
        print(f"  [{t}] {s['cost']:,}원 / ROAS {s['roas']}%")


if __name__ == "__main__":
    main()
