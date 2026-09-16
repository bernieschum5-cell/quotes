#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为 Portfolio Performance 生成 A 股 / 港股历史报价 JSON。（v2）

输出格式（PP 的 JSON 数据源直接可用）：
    [{"date": "2026-09-14", "close": 7.71, "high": 7.8, "low": 7.6, "volume": 123456}, ...]

数据源三选一，自动降级：
    东方财富（多镜像轮换） → 腾讯 → 网易（仅 A 股）
全部为公开接口，无需 API Key。

本地自测：
    python fetch.py --test SH600004
    python fetch.py --test HK00700 -v      # -v 打印每个源的失败细节
"""

import argparse
import calendar
import csv
import io
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

OUT_DIR = os.path.join("docs", "json")
SECURITIES_FILE = "securities.csv"

TIMEOUT = 25
RETRIES = 4
BASE_WAIT = 2.5          # 重试间隔基数，逐次递增
PAUSE_BETWEEN = 1.5      # 批量模式下每个标的之间的间隔
RETRY_PAUSE = 8.0        # 第二轮重试时的间隔，拉长以避开限流

# 复权方式：0=不复权 1=前复权 2=后复权
# 默认不复权 —— 见 README，这对 PP 很重要，不要随便改成 1
ADJUST = os.environ.get("ADJUST", "0")

# 只保留这个日期之后的数据。留空则取全部历史。
START_DATE = os.environ.get("START_DATE", "2015-01-01")

VERBOSE = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 东财的多个镜像，被限流时轮换
EM_HOSTS = [
    "push2his.eastmoney.com",
    "43.push2his.eastmoney.com",
    "63.push2his.eastmoney.com",
    "7.push2his.eastmoney.com",
]

EM_PREFIX = {"SH": "1", "SZ": "0", "BJ": "0", "HK": "116"}
TX_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}
# 网易的市场前缀和东财相反：上海=0，深圳=1
WY_PREFIX = {"SH": "0", "SZ": "1"}


def log(msg):
    if VERBOSE:
        print(f"    · {msg}")


def http_get(url, referer="https://quote.eastmoney.com/", encoding="utf-8"):
    """带重试和退避的 GET。"""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": referer,
                "Connection": "close",   # 不复用连接，降低被掐断的概率
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode(encoding, errors="replace")
        except Exception as exc:                      # noqa: BLE001
            last = exc
            log(f"第 {attempt} 次失败: {type(exc).__name__}: {exc}")
            if attempt < RETRIES:
                time.sleep(BASE_WAIT * attempt + random.uniform(0, 1.5))
    raise RuntimeError(f"请求失败（重试 {RETRIES} 次）: {last}")


def _row(date, close, high, low, volume):
    return {"date": date, "close": round(float(close), 4),
            "high": round(float(high), 4), "low": round(float(low), 4),
            "volume": int(float(volume or 0))}


# ---------------------------------------------------------------- 指数识别

# A 股指数：上证系列 000xxx（在 SH 市场）、深证系列 399xxx（在 SZ 市场）
# 港股指数：HSI / HSCEI / HSTECH 等字母代码，市场写 HKI
HK_INDEX_EM = {                    # 港股指数 → 东财 secid
    "HSI": "100.HSI",              # 恒生指数
    "HSCEI": "100.HSCEI",          # 恒生中国企业指数
    "HSTECH": "100.HSTECH",        # 恒生科技指数
}


def is_index(market, code):
    """判断是不是指数。指数和个股在各数据源的代码规则不同。"""
    if market == "HKI":
        return True
    if market == "SH" and code.startswith("000"):
        return True
    if market == "SZ" and code.startswith("399"):
        return True
    return False


def em_secid(market, code):
    """东财的 secid。指数和个股规则不同。"""
    if market == "HKI":
        sec = HK_INDEX_EM.get(code.upper())
        if not sec:
            raise RuntimeError(f"未知的港股指数代码 {code}")
        return sec
    if market == "HK":
        return f"116.{code.zfill(5)}"
    if is_index(market, code):
        # 上证指数系列前缀 1，深证指数系列前缀 0 —— 和个股一致
        return f"{EM_PREFIX[market]}.{code}"
    return f"{EM_PREFIX[market]}.{code}"


# -------------------------------------------------------------------- Yahoo

YH_SUFFIX = {"SH": ".SS", "SZ": ".SZ", "BJ": ".BJ", "HK": ".HK"}


# 港股指数在 Yahoo 的符号（带 ^ 前缀）
HK_INDEX_YH = {
    "HSI": "^HSI",
    "HSCEI": "^HSCE",
    "HSTECH": "^HSTECH",
}


def yahoo_symbol(market, code):
    if market == "HKI":
        sym = HK_INDEX_YH.get(code.upper())
        if not sym:
            raise RuntimeError(f"未知的港股指数代码 {code}")
        return sym
    if market == "HK":
        return code.lstrip("0").zfill(4) + ".HK"
    return code + YH_SUFFIX[market]


def fetch_yahoo(market, code):
    """
    Yahoo chart 接口。返回 timestamp 数组 + 各价格数组，需按交易所时区还原日期。
    注意：从境外家用 IP 常被 429，但 GitHub Actions 的服务器 IP 通常没问题。
    """
    symbol = yahoo_symbol(market, code)
    # 注意：不能用 range=max —— 区间太长时 Yahoo 会把粒度自动降到月线。
    # 必须用 period1/period2 明确指定区间，再校验返回的粒度确实是日线。
    if START_DATE:
        period1 = calendar.timegm(time.strptime(START_DATE, "%Y-%m-%d"))
    else:
        period1 = 0
    params = urllib.parse.urlencode({
        "interval": "1d",
        "period1": max(period1, 0),
        "period2": int(time.time()) + 86400,
    })
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?{params}")
    payload = json.loads(http_get(url, referer="https://finance.yahoo.com/"))

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo 返回错误: {chart['error']}")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise RuntimeError("Yahoo 返回空 result")

    meta = result.get("meta") or {}
    gran = meta.get("dataGranularity")
    if gran and gran != "1d":
        raise RuntimeError(f"返回的是 {gran} 粒度而非日线，拒绝使用")

    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    vols = quote.get("volume") or []
    offset = meta.get("gmtoffset", 0) or 0

    rows = []
    for i, ts in enumerate(stamps):
        c = closes[i] if i < len(closes) else None
        if c is None:                      # 停牌日 Yahoo 给 null
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(ts + offset))
        rows.append(_row(day, c,
                         highs[i] if i < len(highs) and highs[i] is not None else c,
                         lows[i] if i < len(lows) and lows[i] is not None else c,
                         vols[i] if i < len(vols) and vols[i] is not None else 0))
    if not rows:
        raise RuntimeError("解析后为空")
    return rows


# ---------------------------------------------------------------- 东方财富

def fetch_eastmoney(market, code):
    """
    klines 每行是逗号分隔字符串：
      f51 日期, f52 开, f53 收, f54 高, f55 低, f56 量, f57 额
    即 [0]=日期 [2]=收 [3]=高 [4]=低 [5]=量
    """
    secid = em_secid(market, code)
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101",
        "fqt": ADJUST,
        "beg": "0",
        "end": "20500101",
    }
    query = urllib.parse.urlencode(params)

    errors = []
    hosts = EM_HOSTS[:]
    random.shuffle(hosts)                 # 分散压力，避免总打同一台
    for host in hosts:
        try:
            body = http_get(f"https://{host}/api/qt/stock/kline/get?{query}")
            payload = json.loads(body)
            data = payload.get("data")
            if not data or not data.get("klines"):
                raise RuntimeError("返回空数据（代码或市场可能写错）")
            rows = []
            for line in data["klines"]:
                p = line.split(",")
                if len(p) >= 6:
                    rows.append(_row(p[0], p[2], p[3], p[4], p[5]))
            if rows:
                log(f"东财命中镜像 {host}")
                return rows
            raise RuntimeError("解析后为空")
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{host}: {exc}")
            continue
    raise RuntimeError("所有镜像均失败 → " + " ; ".join(errors))


# -------------------------------------------------------------------- 腾讯

def _find_kline_list(node):
    """
    腾讯的返回结构在不同标的/不同复权下会变形，
    这里递归找出第一个「元素是列表且首项长得像日期」的列表。
    """
    if isinstance(node, list):
        if node and isinstance(node[0], list) and len(node[0]) >= 5:
            first = str(node[0][0])
            if len(first) >= 8 and first[:4].isdigit():
                return node
        return None
    if isinstance(node, dict):
        for key in ("day", "qfqday", "hfqday"):     # 优先看常见键
            if key in node:
                found = _find_kline_list(node[key])
                if found:
                    return found
        for value in node.values():
            found = _find_kline_list(value)
            if found:
                return found
    return None


def fetch_tencent(market, code):
    """腾讯日线：[日期, 开, 收, 高, 低, 量]"""
    if market == "HKI":
        raise RuntimeError("港股指数不走腾讯")
    symbol = TX_PREFIX[market] + (code.zfill(5) if market == "HK" else code)
    suffix = {"0": "", "1": "qfq", "2": "hfq"}[ADJUST]
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,,,3000,{suffix}")

    payload = json.loads(http_get(url, referer="https://gu.qq.com/"))
    series = _find_kline_list(payload.get("data"))
    if not series:
        raise RuntimeError("返回结构里找不到日线序列")

    rows = []
    for p in series:
        if len(p) >= 5:
            rows.append(_row(p[0][:10], p[2], p[3], p[4],
                             p[5] if len(p) > 5 else 0))
    if not rows:
        raise RuntimeError("解析后为空")
    return rows


# -------------------------------------------------------------------- 网易

def fetch_163(market, code):
    """
    网易财经历史数据 CSV（仅 A 股）。GBK 编码，最新的在最前面。
    列：日期, 股票代码, 名称, 收盘价, 最高价, 最低价, 成交量
    """
    if market not in WY_PREFIX:
        raise RuntimeError("网易不支持该市场（仅沪深）")

    start = (START_DATE or "1990-01-01").replace("-", "")
    end = time.strftime("%Y%m%d")
    query = urllib.parse.urlencode({
        "code": f"{WY_PREFIX[market]}{code}",
        "start": start, "end": end,
        "fields": "TCLOSE;HIGH;LOW;VOTURNOVER",
    })
    url = "https://quotes.money.163.com/service/chddata.html?" + query

    body = http_get(url, referer="https://quotes.money.163.com/",
                    encoding="gbk")
    reader = csv.reader(io.StringIO(body))
    rows = []
    for i, p in enumerate(reader):
        if i == 0 or len(p) < 7:          # 跳过表头
            continue
        try:
            close = float(p[3])
        except ValueError:
            continue
        if close <= 0:                    # 停牌日网易会给 0
            continue
        rows.append(_row(p[0], p[3], p[4], p[5], p[6]))
    if not rows:
        raise RuntimeError("解析后为空")
    return rows


# -------------------------------------------------------------------- 主流程

ALL_SOURCES = {
    "yahoo": fetch_yahoo,
    "eastmoney": fetch_eastmoney,
    "tencent": fetch_tencent,
    "163": fetch_163,
}

# 默认 Yahoo 优先：它同时覆盖沪深京港，且不受大陆源的境外 IP 风控影响。
# 想换顺序就设环境变量，例如 SOURCE_ORDER=eastmoney,yahoo
SOURCE_ORDER = [s.strip() for s in os.environ.get(
    "SOURCE_ORDER", "yahoo,eastmoney,tencent,163").split(",") if s.strip()]


def fetch(market, code):
    """按 SOURCE_ORDER 依次尝试。返回 (rows, 实际使用的源)。"""
    errors = []
    order = list(SOURCE_ORDER)
    if is_index(market, code):
        # Yahoo 对 A 股指数覆盖很差（常常只给一个数据点），指数优先走东财；
        # 但 Yahoo 保留在后面当后路——港股指数东财的 secid 不一定齐全。
        order = ["eastmoney"] + [s for s in order if s != "eastmoney"]
    for name in order:
        fn = ALL_SOURCES.get(name)
        if fn is None:
            continue
        try:
            log(f"尝试数据源 {name}")
            rows = fn(market, code)
            cleaned = clean(rows) if rows else []
            if cleaned and not looks_daily(cleaned):
                raise RuntimeError("拿到的不是日线数据（间隔过大），拒绝使用")
            # 条数过少通常意味着数据源不认这个代码。
            # 但新上市标的本来就少，用「首条日期距今不足 MIN_ROWS 个交易日」豁免。
            if cleaned and len(cleaned) < MIN_ROWS:
                first_ts = calendar.timegm(
                    time.strptime(cleaned[0]["date"], "%Y-%m-%d"))
                trading_days = (time.time() - first_ts) / 86400 * (5 / 7)
                if trading_days > MIN_ROWS * 1.5:
                    raise RuntimeError(
                        f"只返回 {len(cleaned)} 条（首条 {cleaned[0]['date']}），"
                        f"数据源很可能不认这个代码")
            if cleaned:
                return rows, name
            errors.append(f"{name}: 空结果")
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{name}: {exc}")
    raise RuntimeError("\n  ".join([""] + errors))


MIN_ROWS = int(os.environ.get("MIN_ROWS", "60"))


def looks_daily(rows):
    """粗略判断是不是日线：交易日平均间隔应在 1~4 天之间。"""
    if len(rows) < 5:
        return False                     # 太少，几乎可以肯定是坏数据
    if len(rows) < 30:
        return True                      # 新股可能确实就这么少，交给 MIN_ROWS 判
    d0 = calendar.timegm(time.strptime(rows[0]["date"], "%Y-%m-%d"))
    d1 = calendar.timegm(time.strptime(rows[-1]["date"], "%Y-%m-%d"))
    avg_gap = (d1 - d0) / 86400 / (len(rows) - 1)
    return avg_gap <= 4.0


def clean(rows):
    seen = {}
    for r in rows:
        if START_DATE and r["date"] < START_DATE:
            continue
        seen[r["date"]] = r
    return [seen[d] for d in sorted(seen)]


def read_securities(path):
    out = []
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            if enc not in ("utf-8-sig", "utf-8"):
                print(f"提示：securities.csv 是 {enc} 编码，已自动转换。"
                      f"建议改存为 UTF-8。")
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError("securities.csv 编码无法识别，请另存为 UTF-8")
    with io.StringIO(text, newline="") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("code") or "").strip()
            market = (row.get("market") or "").strip().upper()
            if not code or code.startswith("#"):
                continue
            if market not in EM_PREFIX and market != "HKI":
                print(f"  跳过 {code}：market 须为 SH/SZ/BJ/HK/HKI，收到 '{market}'")
                continue
            out.append({"code": code, "market": market,
                        "name": (row.get("name") or "").strip()})
    return out


def run_test(target):
    market, code = target[:2].upper(), target[2:]
    print(f"测试 {market} {code}  复权方式={ADJUST}")
    rows, source = fetch(market, code)
    rows = clean(rows)
    print(f"  数据源: {source}")
    print(f"  条数:   {len(rows)}")
    print(f"  区间:   {rows[0]['date']} → {rows[-1]['date']}")
    print(f"  最后一条: {json.dumps(rows[-1], ensure_ascii=False)}")
    print(f"  日线检查: {'通过' if looks_daily(rows) else '不通过（疑似月线/周线）'}")
    print("\n请把上面的 close 和行情软件里的收盘价核对一致。")
    return 0


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", metavar="SH600004",
                    help="只抓一个标的并打印结果，不写文件")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="打印每个数据源的失败细节")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if args.test:
        return run_test(args.test)

    securities = read_securities(SECURITIES_FILE)
    if not securities:
        print("securities.csv 里没有有效标的")
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)

    def process(sec, index, failures):
        """抓一个标的并写文件。成功返回 True。"""
        key = sec["market"] + sec["code"]
        label = f"{key} {sec['name']}".strip()
        try:
            rows, source = fetch(sec["market"], sec["code"])
            rows = clean(rows)
            if not rows:
                raise RuntimeError("清洗后没有数据")
            with open(os.path.join(OUT_DIR, key + ".json"), "w",
                      encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False, separators=(",", ":"))
            index.append({"key": key, "name": sec["name"], "source": source,
                          "count": len(rows), "first": rows[0]["date"],
                          "last": rows[-1]["date"]})
            print(f"OK   {label}  {len(rows)} 条  最后 {rows[-1]['date']}  [{source}]")
            return True
        except Exception as exc:                      # noqa: BLE001
            failures.append({"key": key, "error": str(exc)})
            print(f"FAIL {label}  {exc}")
            return False

    # ---- 第一轮
    index, failures = [], []
    for sec in securities:
        process(sec, index, failures)
        time.sleep(PAUSE_BETWEEN + random.uniform(0, 1.0))

    # ---- 第二轮：只重试失败的，间隔拉长。
    # 大陆数据源的失败多是随机限流，隔久一点重来通常就过了。
    if failures:
        retry_keys = {f["key"] for f in failures}
        retry_list = [s for s in securities
                      if s["market"] + s["code"] in retry_keys]
        print(f"\n第一轮失败 {len(retry_list)} 个，等待 30 秒后重试……")
        time.sleep(30)
        failures = []
        for sec in retry_list:
            process(sec, index, failures)
            time.sleep(RETRY_PAUSE + random.uniform(0, 3.0))

    with open(os.path.join(OUT_DIR, "_index.json"), "w", encoding="utf-8") as fh:
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                            time.gmtime()),
                   "adjust": ADJUST, "ok": index, "failed": failures},
                  fh, ensure_ascii=False, indent=2)

    print(f"\n完成：成功 {len(index)}，失败 {len(failures)}")
    return 0 if index else 1


if __name__ == "__main__":
    sys.exit(main())
