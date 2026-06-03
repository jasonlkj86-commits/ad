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

# ctr, avgCpc, rvImpCnt는 API 미지원 → Python 계산 또는 제외
# convAmt는 계정 설정에 따라 지원 여부 다름 → 런타임에 확인
BASE_FIELDS  = ["clkCnt", "impCnt", "salesAmt"]
EXTRA_FIELDS = ["convAmt"]          # 전환매출액 (계정 설정에 따라 가능)


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


# ── HTTP GET ─────────────────────────────────────────────────────────────────
def api_get(cid, lic, sec, path, params: dict = None):
    """params dict → query string (서명은 path만으로 계산)"""
    qs  = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = BASE_URL + path + qs
    req = urllib.request.Request(url, headers=_headers(cid, lic, sec, path))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            print(f"  GET {path} → {r.status}")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  ERROR {path} params={params} → HTTP {e.code}: {err[:400]}", file=sys.stderr)
        raise


# ── 통계: 단일 ID, 단일 호출 ──────────────────────────────────────────────────
def probe_fields(cid, lic, sec, sample_id: str, date_from: str, date_to: str) -> str:
    """사용 가능한 필드를 한 번만 확인하고 JSON 문자열로 반환"""
    since = date_from.replace("-", "")
    until = date_to.replace("-", "")
    tr = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    valid = list(BASE_FIELDS)
    for f in EXTRA_FIELDS:
        candidate = json.dumps(valid + [f], separators=(",", ":"))
        try:
            api_get(cid, lic, sec, "/stats",
                    {"ids": sample_id, "fields": candidate,
                     "timeUnit": "total", "timeRange": tr})
            valid.append(f)
            print(f"  필드 {f} ✓")
        except Exception:
            print(f"  필드 {f} ✗ (미지원, 제외)")
    result = json.dumps(valid, separators=(",", ":"))
    print(f"  최종 사용 필드: {valid}")
    return result


def get_stat_one(cid, lic, sec, obj_id: str, time_unit: str,
                 date_from: str, date_to: str, active_fields: str):
    """ID 하나에 대한 통계 반환"""
    since = date_from.replace("-", "")
    until = date_to.replace("-", "")

    # ── 진단: 단계별로 파라미터를 늘려가며 어디서 400이 나는지 확인 ──
    tr = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    params = {"ids": obj_id, "fields": active_fields, "timeUnit": time_unit, "timeRange": tr}
    resp = api_get(cid, lic, sec, "/stats", params)
    return resp if isinstance(resp, list) else resp.get("data", [])


# ── 캠페인 목록 ───────────────────────────────────────────────────────────────
def get_campaigns(cid, lic, sec):
    d = api_get(cid, lic, sec, "/ncc/campaigns")
    return d if isinstance(d, list) else d.get("campaigns", d.get("items", []))


# ── 광고그룹 목록 ─────────────────────────────────────────────────────────────
def get_adgroups(cid, lic, sec, campaign_id):
    d = api_get(cid, lic, sec, "/ncc/adgroups", {"nccCampaignId": campaign_id})
    return d if isinstance(d, list) else d.get("adGroups", d.get("items", []))


# ── 일별 합산 ─────────────────────────────────────────────────────────────────
def aggregate_daily(rows):
    by_date = {}
    for row in rows:
        # API 응답이 flat 구조 → datetime / date / regTm 순으로 시도
        raw_d = (row.get("datetime") or row.get("date") or
                 row.get("regTm") or "")
        d = str(raw_d)[:10].replace(".", "-")  # YYYYMMDD → YYYY-MM-DD 변환
        if len(d) < 10 or not d[:4].isdigit():
            continue
        s = row.get("stat") or row  # flat or nested 모두 처리
        e = by_date.setdefault(d, {"date": d, "cost": 0, "impressions": 0,
                                   "clicks": 0, "conversions": 0, "conversion_amount": 0})
        e["cost"]              += _int(s.get("salesAmt"))
        e["impressions"]       += _int(s.get("impCnt"))
        e["clicks"]            += _int(s.get("clkCnt"))
        e["conversions"]       += _int(s.get("rvImpCnt"))
        e["conversion_amount"] += _int(s.get("convAmt"))

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
    # 첫 캠페인 구조 출력 (필드명 확인용)
    if campaigns:
        print(f"  [캠페인 샘플 키] {list(campaigns[0].keys())}")

    # 2) 유효 필드 한 번만 확인
    print("사용 가능한 통계 필드 확인 중...")
    first_cid = campaigns[0]["nccCampaignId"]
    active_fields = probe_fields(customer_id, access_license, secret_key,
                                 first_cid, date_from, date_to)

    # 3) 캠페인별 일별 통계 수집 (1개씩)
    print("일별 통계 수집 중...")
    all_daily_rows = []
    for c in campaigns:
        cid_val = c["nccCampaignId"]
        rows = get_stat_one(customer_id, access_license, secret_key,
                            cid_val, "date", date_from, date_to, active_fields)
        all_daily_rows.extend(rows)
        print(f"  캠페인 {cid_val}: {len(rows)}행")
        if rows and cid_val == campaigns[0]["nccCampaignId"]:
            print(f"  [일별 첫행] {json.dumps(rows[0], ensure_ascii=False)[:300]}")

    daily_totals = aggregate_daily(all_daily_rows)
    print(f"  → 일별 합산 {len(daily_totals)}일")

    # 3) 광고그룹별 통계
    print("광고그룹 통계 수집 중...")
    adgroup_rows = []

    for c in campaigns:
        cid_val = c["nccCampaignId"]
        cname   = c.get("name", cid_val)   # API 필드명은 'name'
        adgroups = get_adgroups(customer_id, access_license, secret_key, cid_val)
        if not adgroups:
            continue
        print(f"  [{cname}] 광고그룹 {len(adgroups)}개")

        for ag in adgroups:
            agid   = ag["nccAdgroupId"]
            agname = ag.get("name", ag.get("adgroupName", agid))  # API 필드명은 'name'
            rows   = get_stat_one(customer_id, access_license, secret_key,
                                  agid, "total", date_from, date_to, active_fields)
            s = {}
            if rows:
                s = rows[0].get("stat") or rows[0]

            cost     = _int(s.get("salesAmt"))
            clicks   = _int(s.get("clkCnt"))
            imps     = _int(s.get("impCnt"))
            convs    = 0   # rvImpCnt 미지원
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

    print(f"=== 완료: 광고그룹 {len(adgroup_rows)}개 / 일별 {len(daily_totals)}일 ===")
    print(f"총 광고비 {tc:,}원 / 전환 {tv}건 / 전환액 {ta:,}원")


if __name__ == "__main__":
    main()
