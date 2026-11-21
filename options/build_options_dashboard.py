"""
美股期权微观结构与流动性踩踏监控仪表盘（复刻 Google Sites 原版 + 真实数据驱动）

原版：https://sites.google.com/view/fupeng715/full-page-embed
     备份于 options/reference/（作为渲染模板，1:1 保留原页面交互与判定逻辑）

数据源（全部免费公开）：
  1. Yahoo Finance chart API  ：VIX / VIX9D / VIX3M(=VXV) / VVIX / SKEW / DSPX / VIXEQ / NVDA / SPX / IRX
  2. Cboe 官方每日统计页       ：个股 / 全市场 / 指数 Put/Call 比率
  3. yfinance ^SPX 期权链      ：按行权价自算 BS Greeks → GEX / 近月净 Gamma / Net Delta Flow

做市商三项指标口径（自算，页面上有标注）：
  - spxGex      ：≤90DTE 净 GEX（$/1% 变动，put 记负）—— 量级与原页面刻度一致，阈值沿用原设计
  - dealerGamma ：0-9DTE 近月净 GEX × 0.4 校准系数
                  shortcut: 系数是把自算近月净GEX映射到原页面(SpotGamma聚合)±10B刻度的经验值,
                  历史快照≥20天后应改为自身分位数重新校准
  - netDelta    ：客户侧期权净 Delta（百万股）的日环比；负值 = 做市商机械卖出对冲。首日无前日对比记 0

产出：options/options_microstructure_dashboard.html（单文件，Tailwind/Chart.js 已内嵌，离线可看）
历史：options/data/metrics_history.json（每日指标快照，CI 随页面一起提交）

用法：python options/build_options_dashboard.py（在仓库根目录或本目录均可）
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

warnings.filterwarnings("ignore")

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
TEMPLATE = BASE / "reference" / "original_dashboard.html"
OUT_HTML = BASE / "options_microstructure_dashboard.html"
HISTORY = BASE / "data" / "metrics_history.json"
VENDOR = BASE / "vendor"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
ET = ZoneInfo("America/New_York")

YAHOO_SYMBOLS = {  # 指标名 -> Yahoo 符号
    "vix": "^VIX", "vix9d": "^VIX9D", "vix3m": "^VIX3M", "vvix": "^VVIX",
    "skew": "^SKEW", "dspx": "^DSPX", "vixeq": "^VIXEQ", "nvda": "NVDA",
    "spx": "^GSPC", "irx": "^IRX",
}
CBOE_DAILY_URL = "https://www.cboe.com/markets/us/options/market-statistics/daily"
VENDOR_JS = {
    "tailwind": ("https://cdn.tailwindcss.com", "tailwind_play.js"),
    "chartjs": ("https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js", "chart_umd_4.4.3.js"),
}
SQRT2PI = math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------- utilities
def log(msg: str) -> None:
    print(f"[options] {msg}", flush=True)


def http_get(url: str, timeout: int = 25, retries: int = 3) -> str:
    last: Exception | None = None
    for i in range(retries):
        try:
            resp = requests.get(url, headers=UA, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"GET 失败({i + 1}/{retries}) {url}: {e}")
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET 最终失败 {url}: {last}")


def get_json(url: str) -> dict:
    try:
        return json.loads(http_get(url))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"返回非 JSON: {url}") from e


# ---------------------------------------------------------------- data: Yahoo
def fetch_yahoo_quotes() -> tuple[dict, datetime]:
    """返回 {指标名: 价格} 与数据时间（最新 regularMarketTime）。"""
    out, latest = {}, datetime.now(UTC)
    for key, sym in YAHOO_SYMBOLS.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=1d"
        meta = get_json(url)["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        # ^VXV 等停更符号会返回 0；NaN 价格（NaN <= 0 为 False）也必须拦下
        if price is None or not math.isfinite(price) or price <= 0:
            raise RuntimeError(f"{sym} 无有效报价: {price}")
        out[key] = float(price)
        t = meta.get("regularMarketTime")
        if t:
            latest = max(latest, datetime.fromtimestamp(t, UTC))
    return out, latest


# ---------------------------------------------------------------- data: Cboe P/C
def fetch_cboe_pc() -> dict:
    """解析 Cboe 每日统计页服务端渲染的 P/C 比率。"""
    html = http_get(CBOE_DAILY_URL)
    found = {}
    for name in ("EQUITY", "TOTAL", "INDEX"):
        m = re.search(
            r'\\"name\\":\\"' + name + r' PUT/CALL RATIO\\",\\"value\\":\\"([\d.]+)\\', html)
        if not m:
            raise RuntimeError(f"Cboe 统计页未找到 {name} PUT/CALL RATIO")
        found[name] = float(m.group(1))
    return {"pcEquity": found["EQUITY"], "pcTotal": found["TOTAL"], "pcIndex": found["INDEX"]}


# ---------------------------------------------------------------- data: SPX chain
def _valid_row(k, oi, iv) -> bool:
    """行级有效性：NaN 会穿透 not oi / iv < 0.01 这类比较（结果为 False），必须显式
    isfinite；周末拉链时会有大量 NaN OI 行（无持仓报价）。"""
    if k is None or oi is None or iv is None:
        return False
    return (math.isfinite(k) and math.isfinite(oi) and math.isfinite(iv)
            and k > 0 and oi > 0 and iv >= 0.01)


def fetch_dealer_metrics(spot: float, rate: float) -> tuple[dict, dict]:
    """抓 ^SPX 期权链（≤90DTE），自算 BS Greeks 聚合。

    返回 (dashboard 三指标, 原始聚合快照供历史积累)。
    客户流假设（SqueezeMetrics 惯例）：客户买 put / 卖 call，故 put 的 gamma 记负号。
    """
    import yfinance as yf

    tk = yf.Ticker("^SPX")
    exps = list(tk.options)
    if not exps:
        raise RuntimeError("^SPX 期权链为空")

    now = datetime.now(UTC)
    per_dte: dict[int, dict] = {}
    for e in exps:
        dte = (datetime.strptime(e, "%Y-%m-%d").replace(tzinfo=UTC) - now).days
        if dte < 0 or dte > 90:
            continue
        ch = tk.option_chain(e)
        T = max(dte, 0.5) / 365.0
        cg = pg = cd = pd = 0.0  # gamma×OI×100 / delta×OI×100（客户侧口径）
        for df, is_call in ((ch.calls, True), (ch.puts, False)):
            for k, oi, iv in zip(df.strike, df.openInterest, df.impliedVolatility, strict=True):
                if not _valid_row(k, oi, iv):
                    continue
                d1 = (math.log(spot / k) + (rate + iv * iv / 2.0) * T) / (iv * math.sqrt(T))
                gamma = math.exp(-d1 * d1 / 2.0) / SQRT2PI / (spot * iv * math.sqrt(T))
                delta = 0.5 * (1.0 + math.erf(d1 / SQRT2PI))
                if not is_call:
                    delta -= 1.0
                if is_call:
                    cg += gamma * oi * 100
                    cd += delta * oi * 100
                else:
                    pg += gamma * oi * 100
                    pd += delta * oi * 100
        per_dte[dte] = {"cg": cg, "pg": pg, "cd": cd, "pd": pd, "exp": e}

    if not per_dte:
        raise RuntimeError("期权链聚合为空")

    def net_gex_b(dtes: list[int]) -> float:
        c = sum(per_dte[d]["cg"] for d in dtes if d in per_dte)
        p = sum(per_dte[d]["pg"] for d in dtes if d in per_dte)
        return (c - p) * spot * spot * 0.01 / 1e9

    all_dtes = sorted(per_dte)
    near = [d for d in all_dtes if d <= 9]
    gex_90 = net_gex_b(all_dtes)
    gex_9 = net_gex_b(near)
    d_shares = sum(per_dte[d]["cd"] + per_dte[d]["pd"] for d in all_dtes)  # 客户侧净 delta（股）

    metrics = {
        "spxGex": round(gex_90, 1),
        "dealerGamma": round(gex_9 * 0.4, 1),  # 校准系数见模块 docstring
        "netDelta": 0.0,  # 占位，由 main() 用历史日环比填充
    }
    raw = {
        "gex90_b": round(gex_90, 2),
        "gex9_b": round(gex_9, 2),
        "delta_shares_m": round(d_shares / 1e6, 1),
        "n_expirations": len(all_dtes),
    }
    return metrics, raw


# ---------------------------------------------------------------- history
def load_history() -> dict:
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log(f"历史文件损坏，忽略: {e}")
    return {}


def save_history(hist: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")


def _latest_before(hist: dict, data_date: str) -> dict | None:
    keys = sorted(k for k in hist if k < data_date)
    return hist[keys[-1]] if keys else None


# ---------------------------------------------------------------- render
def load_vendor_js() -> dict:
    """vendor 脚本：本地缓存优先，缺失则下载（~600KB，提交进仓库供离线构建）。"""
    out = {}
    for key, (url, fname) in VENDOR_JS.items():
        path = VENDOR / fname
        if not path.exists() or path.stat().st_size < 10000:
            VENDOR.mkdir(parents=True, exist_ok=True)
            log(f"下载 vendor 脚本: {url}")
            path.write_text(http_get(url, timeout=60), encoding="utf-8")
        out[key] = path.read_text(encoding="utf-8")
    return out


def patch_template(template: str, values: dict, meta: dict, vendor: dict) -> str:
    """对原版模板做最小替换：真实数据 + 数据溯源 + 内嵌 vendor JS。"""
    n_applied = 0

    def rep(old: str, new: str) -> None:
        nonlocal n_applied, template
        found = template.count(old)
        if found != 1:
            raise RuntimeError(f"模板补丁定位失败（期望 1 处，实际 {found} 处）: {old[:60]}")
        template = template.replace(old, new)
        n_applied += 1

    # 1. 内嵌 vendor JS（离线可用）
    rep('<script src="https://cdn.tailwindcss.com"></script>',
        f"<script>/* Tailwind Play CDN v3 (vendored) */\n{vendor['tailwind']}\n</script>")
    rep('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>',
        f"<script>/* Chart.js v4.4.3 UMD (vendored) */\n{vendor['chartjs']}\n</script>")

    # 2. 生成信息注释
    rep("<!DOCTYPE html>\n",
        f"<!DOCTYPE html>\n<!-- 由 options/build_options_dashboard.py 生成 · "
        f"数据截至 {meta['as_of']} · 复刻自 Google Sites 原版(备份于 options/reference/) -->\n")

    # 3. 数据基准 / 数据源行
    rep("数据基准：<strong class=\"text-slate-700 font-medium\">2026-11-18/19（月度 OPEX 交割日前）</strong>",
        f"数据基准：<strong class=\"text-slate-700 font-medium\">{meta['date_label']}</strong>")
    rep("口径参考：LSEG Datastream / CBOE / SpotGamma / Squeezemetrics",
        f"数据：Yahoo Finance（VIX 家族/DSPX/VIXEQ/SKEW/NVDA） · Cboe 官方统计（P/C） · "
        f"SPX 期权链自算（BS Greeks） · 更新于 {meta['updated_ts']} UTC{meta['data_note']}")

    # 4. 当前实盘情景 = 真实数据
    vals_js = ",\n          ".join(f"{k}: {v}" for k, v in values.items())
    current_re = re.compile(r"current: \{[\s\S]*?\},\n      stage1: \{")
    if not current_re.search(template):
        raise RuntimeError("模板补丁定位失败: SCENARIOS.current 块")
    template = current_re.sub(
        f"current: {{\n        label: '{meta['scenario_label']}',\n        values: {{\n"
        f"          {vals_js}\n        }}\n      }},\n      stage1: {{", template)
    n_applied += 1
    rep("当前实盘 (11/19 OPEX前)", meta["scenario_label"])  # 顶部按钮文案
    rep("重置为 11/19 实盘数据", f"重置为 {meta['date_short']} 实盘数据")

    # 5. 自算口径标注（做市商模块）
    rep("Dealer Agg Gamma (-8.6B 底线) / SPX GEX / Net Delta",
        "近月净Gamma / SPX GEX / Net Delta（自算口径：SPX 期权链 + BS Greeks）")
    rep("thresholdText: '零轴翻转（当前极值 -$97B~-$104B）'",
        "thresholdText: '零轴翻转（自算：≤90DTE 净GEX $B/1%）'")
    rep("// 首次渲染（默认加载 11/19 OPEX 前实盘数据）",
        "// 首次渲染（真实数据，见顶部数据基准行）")

    assert n_applied == 11, f"补丁数量异常: {n_applied}"
    return template


# ---------------------------------------------------------------- self-check
PLAUSIBLE = {  # (min, max) 合理区间
    "vixeqVix": (1.0, 4.0), "dspx": (10, 80), "nvda": (50, 300),
    "vix": (8, 90), "vvix": (60, 200), "vixVxv": (0.7, 1.6), "vix9dVix": (0.7, 1.6),
    "skew": (110, 200), "pcEquity": (0.3, 1.5), "pcTotal": (0.5, 1.8), "pcIndex": (0.7, 2.0),
    "spxGex": (-200, 200), "dealerGamma": (-30, 30), "netDelta": (-500, 500),
}


def stress_index_py(s: dict) -> int:
    """computeStressIndex 的 Python 移植，用于交叉自检（JS 端为运行时真源）。"""
    def clamp(v, lo, hi):
        return min(max(v, lo), hi)

    total = (
        clamp((s["vix"] - 14) / 22 * 15, 0, 15) + clamp((s["vvix"] - 85) / 50 * 10, 0, 10)
        + clamp((s["vixVxv"] - 0.88) / 0.24 * 9, 0, 9) + clamp((s["vix9dVix"] - 0.88) / 0.24 * 6, 0, 6)
        + clamp((20 - s["spxGex"]) / 140 * 12, 0, 12) + clamp((1 - s["dealerGamma"]) / 10 * 10, 0, 10)
        + clamp((20 - s["netDelta"]) / 160 * 8, 0, 8) + clamp((2.5 - s["vixeqVix"]) / 1.15 * 15, 0, 15)
        + clamp((s["pcEquity"] - 0.5) / 0.4 * 6, 0, 6) + clamp((s["pcTotal"] - 0.8) / 0.55 * 5, 0, 5)
        + clamp((s["pcIndex"] - 1.0) / 0.4 * 4, 0, 4))
    return round(clamp(total, 0, 100))


def self_check(values: dict, out_html: str, meta: dict) -> None:
    for k, (lo, hi) in PLAUSIBLE.items():
        v = values.get(k)
        assert v is not None and math.isfinite(v), f"指标缺失或非有限值: {k}={v}"
        assert lo <= v <= hi, f"指标超出合理区间: {k}={v} 不在 [{lo},{hi}]"
    score = stress_index_py(values)
    assert 0 <= score <= 100, f"压力指数越界: {score}"
    log(f"自检通过：14 项指标在合理区间，压力指数(Python 交叉计算) = {score}")
    payload = json.dumps(values)
    assert "NaN" not in payload and "Infinity" not in payload
    for banned in ("cdn.tailwindcss.com", "cdn.jsdelivr.net"):
        assert f'src="https://{banned}' not in out_html, f"产出仍引用 CDN: {banned}"
    assert meta["scenario_label"] in out_html and "SCENARIOS" in out_html


# ---------------------------------------------------------------- main
def _monthly_opex_note(data_date: str) -> str:
    """月度 OPEX = 每月第三个周五；数据日为当日或其前后一天时给标签。"""
    y, m = int(data_date[:4]), int(data_date[5:7])
    d = datetime.strptime(data_date, "%Y-%m-%d")
    fridays = []
    for day in range(1, 32):
        try:
            dt = datetime(y, m, day)
        except ValueError:
            break
        if dt.weekday() == 4:
            fridays.append(dt)
    if len(fridays) >= 3:
        opex = fridays[2]
        if d == opex:
            return "月度 OPEX 交割日 · "
        if abs((d - opex).days) == 1:
            return "月度 OPEX 前后 · "
    return ""


def _chain_fallback(hist: dict, data_date: str, raw: dict) -> tuple[dict, dict | None, str]:
    """链退化时沿用最近快照（含同日）的 raw。返回 (raw, 兑底 dealer 或 None, 页面注记)。

    周末数据源大面积缺失 put 侧 IV（IV<0.01 被过滤），聚合近乎归零；真实市场即使
    Gamma 翻零，delta 聚合仍是 ±千万股量级——两者同时归零只可能是链退化。"""
    if abs(raw["gex90_b"]) >= 1.0 or abs(raw["delta_shares_m"]) >= 5.0:
        return raw, None, ""
    if data_date in hist:
        cand_date, candidate = data_date, hist[data_date]
    else:
        keys = sorted(k for k in hist if k < data_date)
        cand_date = keys[-1] if keys else None
        candidate = hist[cand_date] if cand_date is not None else None
    if candidate is None or candidate.get("raw", {}).get("gex90_b") is None:
        raise RuntimeError("期权链数据退化且无历史快照可兑底（疑似周末数据源未更新）")
    fb_raw = dict(candidate["raw"])
    dealer = {
        "spxGex": round(fb_raw["gex90_b"], 1),
        "dealerGamma": round(fb_raw["gex9_b"] * 0.4, 1),
        "netDelta": candidate.get("values", {}).get("netDelta", 0.0),
    }
    note = f" · 做市商指标沿用 {cand_date} 快照（本日链数据退化：疑似周末 IV 未更新）"
    log(f"链数据退化，做市商指标沿用 {cand_date} 快照: {fb_raw}")
    return fb_raw, dealer, note


def main() -> int:
    quotes, quoted_at = fetch_yahoo_quotes()
    log(f"Yahoo 报价 OK: VIX={quotes['vix']} VIXEQ={quotes['vixeq']} DSPX={quotes['dspx']} "
        f"VVIX={quotes['vvix']} SKEW={quotes['skew']} NVDA={quotes['nvda']}")

    pc = fetch_cboe_pc()
    log(f"Cboe P/C OK: equity={pc['pcEquity']} total={pc['pcTotal']} index={pc['pcIndex']}")

    rate = quotes.get("irx", 40.0) / 100.0  # 13 周 T-Bill 折扣收益率近似
    dealer, raw = fetch_dealer_metrics(quotes["spx"], rate)
    log(f"期权链自算 OK: {raw}")

    # 数据日期：以最新报价时间（ET 日期）为准；周末运行时个别符号的时间戳来自隔夜期货
    # 会话（落在周六/周日），回退到最近的周五——数据本体仍是上一交易日的收盘值
    q_et = quoted_at.astimezone(ET)
    data_date = q_et.strftime("%Y-%m-%d")
    d = datetime.strptime(data_date, "%Y-%m-%d")
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    data_date = d.strftime("%Y-%m-%d")
    date_short = data_date[5:].replace("-", "/")
    # 会话已收盘 = 数据定版：运行时刻已过了数据日的 16:00 ET，或已是其后某天
    now_et = datetime.now(ET)
    is_final = (now_et.date() > d.date()) or (now_et.date() == d.date() and now_et.hour >= 16)

    hist = load_history()
    raw, fb_dealer, chain_note = _chain_fallback(hist, data_date, raw)
    if fb_dealer is not None:
        dealer = fb_dealer

    values = {
        "vixeqVix": round(quotes["vixeq"] / quotes["vix"], 2),
        "dspx": round(quotes["dspx"], 1),
        "nvda": round(quotes["nvda"], 1),
        "vix": round(quotes["vix"], 1),
        "vvix": round(quotes["vvix"], 1),
        "vixVxv": round(quotes["vix"] / quotes["vix3m"], 2),
        "vix9dVix": round(quotes["vix9d"] / quotes["vix"], 2),
        "skew": round(quotes["skew"], 1),
        "pcEquity": round(pc["pcEquity"], 2),
        "pcTotal": round(pc["pcTotal"], 2),
        "pcIndex": round(pc["pcIndex"], 2),
        "spxGex": dealer["spxGex"],
        "dealerGamma": dealer["dealerGamma"],
        "netDelta": dealer["netDelta"],
    }

    # netDelta 日环比（客户侧净 delta 变化，负 = 做市商机械卖出对冲）；
    # 链退化兑底时沿用快照值，不重算日环比
    prev = _latest_before(hist, data_date)
    data_note = chain_note
    if not chain_note and prev is not None and prev.get("raw", {}).get("delta_shares_m") is not None:
        values["netDelta"] = round(raw["delta_shares_m"] - prev["raw"]["delta_shares_m"], 0)
    elif not chain_note:
        data_note += " · Net Delta 首日基线（无前日对比）"

    meta = {
        "date_label": f"{data_date}（{_monthly_opex_note(data_date)}"
                      f"{'收盘定版' if is_final else '盘中快照'}）",
        "updated_ts": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "data_note": data_note,
        "scenario_label": f"当前实盘 ({date_short})",
        "date_short": date_short,
        "as_of": quoted_at.strftime("%Y-%m-%d %H:%M UTC"),
    }

    template = TEMPLATE.read_text(encoding="utf-8")
    vendor = load_vendor_js()
    out_html = patch_template(template, values, meta, vendor)
    self_check(values, out_html, meta)

    OUT_HTML.write_text(out_html, encoding="utf-8")
    log(f"已生成 {OUT_HTML.relative_to(REPO)}（{OUT_HTML.stat().st_size / 1024:.0f} KB）")

    # 历史快照（保留最近 250 个交易日）
    hist[data_date] = {
        "values": values, "raw": raw,
        "as_of": quoted_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "final": is_final,
        "chain_fallback": bool(chain_note),
    }
    if len(hist) > 250:
        hist = dict(sorted(hist.items())[-250:])
    save_history(hist)
    log(f"历史快照已更新：{data_date}（共 {len(hist)} 天）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
