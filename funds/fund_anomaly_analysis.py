"""基金 + 大盘指数 日收益率异常分析 —— 只分析、不处理（不修改、不剔除、不缩尾任何原始数据）。

数据来源：
  fund_nav_history.csv        列：基金代码、日期、日增长率（也兼容“增长率”）
  funds_universe_example.csv  列：基金代码、基金名称、类型
  大盘指数                    fund_correlation_report.py 中的 BROAD_INDICES（akshare 抓取 + 本地缓存）

异常判定方法（共 8 种；前 6 种来自教材，后 2 种为补充）：
  教材：箱线法 1.5×IQR / 箱线法 3×IQR（极端）/ 偏度调整箱线（MedCouple）/ 固定比例法 1%·99% / 均值±3σ / MAD 法
  补充：修正 Z 分数（Iglewicz-Hoaglin）/ EWMA 波动率法（动态阈值，可预判下一交易日）
输出：
  fund_anomaly_analysis.html   （总览 → 近期窗口 → 单只序列 → 阈值总表 → 方法说明）
  “时间范围 + 开始/结束日期”出现在 一、总览 / 二、近期窗口 / 三、单只序列，三处联动；
  四、阈值总表随当前时间范围重新估计并联动更新。

用法：python fund_anomaly_analysis.py [净值csv] [目标基金csv] [输出html] [--no-index] [--refresh] [--nav-unit auto|pct|frac]
需要与 fund_correlation_report.py 放在同一目录（复用其读取函数和指数抓取）。
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

try:
    import fund_correlation_report as cor
except ImportError as e:  # pragma: no cover
    raise SystemExit("找不到 fund_correlation_report.py，请把本脚本和它放在同一个目录再运行") from e

MIN_N = 30            # 序列有效观测数少于该值则不分析
PCT_TAIL = 1.0        # 固定比例法：上下各 1%
K_SIGMA, K_MAD, K_MZ = 3.0, 3.0, 3.5
EWMA_LAMBDA, EWMA_K, EWMA_BURN = 0.94, 3.0, 20
SUSPECT_LIMIT = {"index": 25.0, "fund": 40.0}   # 单日涨跌幅绝对值超过此值 → 疑似数据错误（%）

METHODS = [
    {"key": "box15", "name": "箱线法（1.5×IQR）", "short": "箱线1.5", "voter": True, "book": True,
     "formula": "下界 Q1−1.5×IQR；上界 Q3+1.5×IQR",
     "note": "Tukey 经典箱线图。对极值不敏感，但收益率常有尖峰厚尾，会判出较多异常。"},
    {"key": "box3", "name": "箱线法（3×IQR，极端）", "short": "箱线3", "voter": False, "book": True,
     "formula": "下界 Q1−3×IQR；上界 Q3+3×IQR",
     "note": "教材 Boxplot 法：区间外为极端异常。只作极端标记，不计入票数（避免与 1.5×IQR 重复计票）。"},
    {"key": "adjbox", "name": "偏度调整箱线（MedCouple）", "short": "调整箱线", "voter": True, "book": True,
     "formula": "mc≥0：L=Q1−1.5·e^(−3.5mc)·IQR，U=Q3+1.5·e^(4mc)·IQR；mc<0：L=Q1−1.5·e^(−4mc)·IQR，U=Q3+1.5·e^(3.5mc)·IQR",
     "note": "Hubert & Vandervieren(2007)。mc 为 MedCouple 偏度，数据右偏时放宽上界、左偏时放宽下界。"},
    {"key": "pct", "name": "固定比例法（1%/99%）", "short": "固定比例", "voter": True, "book": True,
     "formula": "下界 = 1% 分位数；上界 = 99% 分位数",
     "note": "按位置而非按显著性判定：无论数据怎样，总有约 2% 的点落在区间外，适合作“相对极端”参考。"},
    {"key": "sigma", "name": "均值标准差法（3σ）", "short": "3σ", "voter": True, "book": True,
     "formula": "μ ± 3σ（样本均值与样本标准差）",
     "note": "思路来自正态分布；均值和标准差受极值影响大，且收益率通常非正态，阈值偏宽。"},
    {"key": "mad", "name": "MAD 法（3×MAD）", "short": "MAD", "voter": True, "book": True,
     "formula": "中位数 ± 3×MAD，MAD=median(|x−中位数|)",
     "note": "按教材使用未缩放的 MAD（约等于 0.6745σ），比 3σ 更严格、更稳健。MAD=0 时用平均绝对偏差×0.8453 代替。"},
    {"key": "mz", "name": "修正 Z 分数（|M|>3.5）", "short": "修正Z", "voter": True, "book": False,
     "formula": "M=0.6745×(x−中位数)/MAD，|M|>3.5 即 x 偏离中位数超过约 5.19×MAD",
     "note": "Iglewicz & Hoaglin 推荐的稳健 Z 分数，比 3×MAD 宽松，介于 MAD 法与 3σ 之间。"},
    {"key": "ewma", "name": "EWMA 波动率法（±3σt，动态）", "short": "EWMA", "voter": True, "book": False,
     "formula": "σt² = λσ²t−1 + (1−λ)r²t−1（λ=0.94，零均值），|rt| > 3σt 为异常；下一交易日区间 = ±3σT+1",
     "note": "考虑波动聚集：行情平静时阈值收窄、剧烈时放宽，可直接预判下一交易日的异常区间。前 20 个观测为预热期不判定。"},
]
VOTERS = [m["key"] for m in METHODS if m["voter"]]


def medcouple(x, cap=3000):
    """MedCouple 偏度（Brys 等 2004）。中位数处的并列点贡献记为空（对结果影响可忽略）。"""
    x = np.sort(np.asarray(x, float)[np.isfinite(x)])
    if len(x) > cap:
        x = x[np.linspace(0, len(x) - 1, cap).astype(int)]
    md = np.median(x)
    xp, xm = x[x >= md], x[x <= md]
    num = (xp[:, None] - md) - (md - xm[None, :])
    den = xp[:, None] - xm[None, :]
    h = num[den != 0] / den[den != 0]
    return float(np.median(h)) if h.size else 0.0


def ewma_sigma(x, lam=EWMA_LAMBDA, burn=EWMA_BURN):
    """返回长度 n+1 的 σ：sig[t] 为用 t 之前信息对第 t 个观测的预测，sig[n] 为下一期预测。"""
    n = len(x)
    s2 = np.empty(n + 1)
    s2[0] = np.var(x[:burn]) if n >= burn else np.var(x)
    for t in range(n):
        s2[t + 1] = lam * s2[t] + (1 - lam) * x[t] ** 2
    return np.sqrt(s2)


def find_suspects(r, kind):
    """识别疑似数据/接口错误的点（例如收盘价为 0 导致的 -100%）。只标记，不修改；这些点不参与阈值估计与票数。"""
    lim, out = SUSPECT_LIMIT[kind], {}
    for i, v in enumerate(r):
        if not np.isfinite(v):
            continue
        if v <= -90:
            out[i] = f"单日涨跌幅 {v:.1f}% ≈ -100%：收盘价为 0/缺失，疑似接口返回错误（并非真实下跌）"
        elif abs(v) >= lim:
            out[i] = f"单日涨跌幅 {v:+.1f}% 超出合理范围（{'宽基指数' if kind == 'index' else '基金'}阈值 ±{lim:.0f}%），疑似数据错误"
        elif abs(v) >= 10 and i + 1 < len(r) and np.isfinite(r[i + 1]) and r[i + 1] * v < 0 and abs(r[i + 1]) >= 0.8 * abs(v):
            out[i] = f"单日 {v:+.1f}% 后次日反向 {r[i + 1]:+.1f}% 回补，疑似单点错误"
    return out


def static_bounds(x):
    q1, q3 = np.percentile(x, [25, 75])
    iqr = q3 - q1
    md = float(np.median(x))
    mad = float(np.median(np.abs(x - md)))
    if mad == 0:                      # 一半以上收益相同（如净值不变）时的退化处理
        mad = 0.8453 * float(np.mean(np.abs(x - md)))
    mc = medcouple(x)
    b = {}
    if iqr > 0:
        b["box15"] = (q1 - 1.5 * iqr, q3 + 1.5 * iqr)
        b["box3"] = (q1 - 3 * iqr, q3 + 3 * iqr)
        lo = q1 - 1.5 * np.exp(-3.5 * mc) * iqr if mc >= 0 else q1 - 1.5 * np.exp(-4 * mc) * iqr
        hi = q3 + 1.5 * np.exp(4 * mc) * iqr if mc >= 0 else q3 + 1.5 * np.exp(3.5 * mc) * iqr
        b["adjbox"] = (lo, hi)
    b["pct"] = (np.percentile(x, PCT_TAIL), np.percentile(x, 100 - PCT_TAIL))
    sd = x.std(ddof=1)
    if sd > 0:
        b["sigma"] = (x.mean() - K_SIGMA * sd, x.mean() + K_SIGMA * sd)
    if mad > 0:
        b["mad"] = (md - K_MAD * mad, md + K_MAD * mad)
        b["mz"] = (md - K_MZ * mad / 0.6745, md + K_MZ * mad / 0.6745)
    return b, mc, md, mad


def analyze(r, sus):
    """r：与全局日期对齐的日收益率(%)，缺失为 NaN；sus：疑似数据错误的位置集合（不参与阈值估计与票数）。"""
    N = len(r)
    ok = np.isfinite(r).copy()
    for i in sus:
        ok[i] = False
    pos = np.flatnonzero(ok)
    x = r[pos]
    if len(x) < MIN_N:
        return None
    b, mc, md, mad = static_bounds(x)
    sig = ewma_sigma(x)
    sg = np.full(N, np.nan)
    sg[pos] = sig[:-1]
    b["ewma"] = (-EWMA_K * sig[-1], EWMA_K * sig[-1])      # 下一交易日的预判区间
    mk, votes = np.zeros(N, int), np.zeros(N, int)
    for bit, m in enumerate(METHODS):
        k = m["key"]
        f = np.zeros(N, bool)
        if k == "ewma":
            idx = pos[EWMA_BURN:]
            f[idx] = np.abs(r[idx]) > EWMA_K * sg[idx]
        elif k in b:
            f[pos] = (x < b[k][0]) | (x > b[k][1])
        else:
            continue
        mk |= f.astype(int) << bit
        if m["voter"]:
            votes += f
    app = [k for k in VOTERS if k in b]
    abn = max(2, int(round(0.4 * len(app))))
    sev = max(abn + 1, int(round(0.7 * len(app))))
    lv = np.where(votes >= sev, 3, np.where(votes >= abn, 2, np.where(votes >= 1, 1, 0)))
    lv = np.where(np.isfinite(r), lv, 0)
    # 疑似数据错误点：等级归零、收益率置空（不在前端显示为独立等级）
    for i in sus:
        lv[i] = 0
    # mk 稀疏化：只存非零项的索引和值，大幅减小 HTML 体积
    mk_idx = np.flatnonzero(mk).tolist()
    mk_val = [int(mk[i]) for i in mk_idx]
    his = sorted(b[k][1] for k in app)
    los = sorted((b[k][0] for k in app), reverse=True)
    ks = [1, abn, sev]
    rd3 = lambda v: None if v is None or not np.isfinite(v) else round(float(v), 3)
    rd2 = lambda v: None if v is None or not np.isfinite(v) else round(float(v), 2)
    # r 数组：疑似错误点置 None，其余 2 位小数
    r_out = [None if i in sus else rd2(v) for i, v in enumerate(r)]
    return {
        "n": int(len(x)), "mean": rd3(x.mean()), "std": rd3(x.std(ddof=1)), "skew": rd3(stats.skew(x, bias=False)),
        "kurt": rd3(stats.kurtosis(x)), "jb": float(stats.jarque_bera(x).pvalue), "mc": rd3(mc), "med": rd3(md), "mad": rd3(mad),
        "b": {k: [rd3(v[0]), rd3(v[1])] for k, v in b.items()},
        "zones": {"up": [rd3(his[j - 1]) for j in ks], "dn": [rd3(los[j - 1]) for j in ks]}, "k": [abn, sev], "napp": len(app),
        "r": r_out, "lv": lv.astype(int).tolist(),
        "mk_idx": mk_idx, "mk_val": mk_val,
        "sg": [rd2(v) for v in sg], "last": int(np.flatnonzero(np.isfinite(r) & ~np.isin(np.arange(N), list(sus)))[-1]),
    }


def load_returns(nav_file, target_file, unit, include_index, refresh, cache_dir):
    nav = cor.normalize_nav_cols(cor.read_csv_safe(nav_file, {"基金代码": str}))
    target = cor.read_csv_safe(target_file, {"基金代码": str})
    for df, cols, p in [(nav, ["基金代码", "日期", "增长率"], nav_file), (target, ["基金代码", "基金名称", "类型"], target_file)]:
        miss = [c for c in cols if c not in df.columns]
        if miss:
            raise ValueError(f"{p} 缺少字段：{', '.join(miss)}")
    nav, target = nav.copy(), target.copy()
    nav["基金代码"], target["基金代码"] = cor.code_series(nav["基金代码"]), cor.code_series(target["基金代码"])
    target["基金名称"] = target["基金名称"].fillna("").astype(str).str.strip()
    target["类型"] = target["类型"].fillna("未分类").astype(str).str.strip().replace("", "未分类")
    target = target.drop_duplicates("基金代码")
    nav["日期"] = pd.to_datetime(nav["日期"], errors="coerce")
    raw = nav["增长率"].astype(str).str.strip()
    pct = raw.str.contains("%", regex=False)
    val = pd.to_numeric(raw.str.replace("%", "", regex=False).str.replace(",", "", regex=False), errors="coerce")
    plain = val[~pct].dropna()
    med = float(plain.abs().median()) if len(plain) else float("nan")
    as_pct = {"pct": True, "frac": False}.get(unit, bool(len(plain)) and med > 0.05)
    nav["ret"] = val.where(pct, val if as_pct else val * 100)
    unit_note = (f"净值文件“增长率”按{'百分数' if as_pct else '小数（已×100）'}解读"
                 + (f"（自动识别：无%号数值的中位绝对值={med:.4f}）" if unit == "auto" and len(plain) else "") + "，全部结果以 % 显示")
    nav = nav.dropna(subset=["日期", "ret"])
    pivot = nav.pivot_table(index="日期", columns="基金代码", values="ret", aggfunc="mean").sort_index()
    meta = {}
    for c in target["基金代码"]:
        if c in pivot.columns:
            t = target.loc[target["基金代码"] == c].iloc[0]
            meta[c] = {"name": t["基金名称"], "type": t["类型"], "sub": "", "region": "", "kind": "fund"}
    pivot = pivot[list(meta)]
    fails = []
    if include_index:
        closes, fails = cor.fetch_index_closes(pd.Timestamp.today().normalize(), cache_dir, refresh)
        start, cols = pivot.index.min() - pd.Timedelta(days=10), {}
        for key, df in closes.items():
            c = df.set_index("Date")["Close"].sort_index()
            c = c[c.index >= start]
            bad = ~(c > 0)                                 # 收盘价 <=0：接口返回 0 的典型情形
            cc = c.where(~bad)
            ret = (cc / cc.shift(1) - 1) * 100
            prev_ok = cc.shift(1).notna()
            ret[bad & prev_ok] = -100.0                   # 收盘价为 0：记 -100%，交给 find_suspects 标记为疑似接口错误
            ret[bad & ~prev_ok] = np.nan
            cols[key] = ret[ret.index >= pivot.index.min()]
            m = cor.BROAD_INDICES[key]
            meta[key] = {"name": m[0], "type": cor.INDEX_TYPE, "sub": m[4], "region": m[3], "kind": "index"}
        if cols:
            pivot = pivot.join(pd.DataFrame(cols), how="outer").sort_index()
    return pivot, meta, unit_note, fails


def generate(nav_file, target_file, output_file, unit="auto", include_index=True, refresh=False, cache_dir=cor.INDEX_CACHE):
    R, meta, unit_note, fails = load_returns(nav_file, target_file, unit, include_index, refresh, cache_dir)
    dates = [d.strftime("%Y-%m-%d") for d in R.index]
    sus_all = {k: find_suspects(R[k].to_numpy(float), meta[k]["kind"]) for k in R.columns}
    series, skipped = [], []
    for k in R.columns:
        res = analyze(R[k].to_numpy(float), set(sus_all[k]))
        if res is None:
            skipped.append(f"{meta[k]['name']}（有效观测不足 {MIN_N} 个）")
            continue
        series.append({"key": k, "code": k, "name": meta[k]["name"], "type": meta[k]["type"], "sub": meta[k]["sub"],
                       "region": meta[k]["region"], **res})
    for k, why in fails:
        skipped.append(f"{cor.BROAD_INDICES[k][0]}（指数抓取失败：{why}）")
    types = list(dict.fromkeys(s["type"] for s in series))
    data = {"series": series, "dates": dates, "types": types, "skipped": skipped, "unit_note": unit_note,
            "methods": METHODS, "voters": VOTERS,
            "params": {"pct": PCT_TAIL, "sigma": K_SIGMA, "mad": K_MAD, "mz": K_MZ, "lam": EWMA_LAMBDA, "ewma_k": EWMA_K,
                       "burn": EWMA_BURN, "min_n": MIN_N, "limit": SUSPECT_LIMIT}}
    html = HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":"))).replace("__PLOTLY_TAG__", cor._plotly_script_tag())
    Path(output_file).write_text(html, encoding="utf-8")
    print(f"成功！分析 {len(series)} 条序列（含指数 {sum(s['type'] == cor.INDEX_TYPE for s in series)} 个），"
          f"未纳入 {len(skipped)} 个；HTML 已保存至：{output_file}")


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>基金与大盘指数 收益率异常分析</title>__PLOTLY_TAG__<style>
:root{--ink:#172033;--muted:#667085;--line:#e5e7eb;--bg:#f5f7fb;--blue:#2563eb}*{box-sizing:border-box}body{margin:0;font-family:"Microsoft YaHei","PingFang SC",Arial;color:var(--ink);background:var(--bg)}.wrap{max-width:1500px;margin:auto;padding:28px 22px 60px}h1{margin:0 0 8px;font-size:28px}h2{margin:0 0 6px;font-size:19px}h3{margin:22px 0 10px;font-size:15px}.subtitle,.hint{color:var(--muted)}.subtitle{font-size:14px;margin-bottom:10px}.hint{font-size:12px;line-height:1.7}
.cards{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:12px;margin:14px 0}.card,.panel{background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 2px 8px #1018280a}.card{padding:13px 15px}.card .label{color:var(--muted);font-size:12px}.card .value{font-size:18px;font-weight:700;margin-top:4px}.card .sub{font-size:11px;color:var(--muted);margin-top:2px}.panel{padding:20px;margin-top:16px}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:10px 0 12px}label{color:var(--muted);font-size:13px}select,input,button{font:inherit;border:1px solid #d0d5dd;border-radius:7px;padding:7px 10px;background:#fff;color:var(--ink)}input[type=checkbox]{vertical-align:middle}.chart{min-height:340px}.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
.btn-grp{display:inline-flex;gap:2px;vertical-align:middle}.btn-grp button{padding:5px 10px;font-size:12px;cursor:pointer;border:1px solid #d0d5dd;background:#fff;border-radius:6px;margin:0}.btn-grp button.active{background:var(--blue);color:#fff;border-color:var(--blue)}.btn-grp button:first-child{border-radius:6px 0 0 6px}.btn-grp button:last-child{border-radius:0 6px 6px 0}.btn-grp button:only-child{border-radius:6px}
.lv-btn{padding:4px 12px;font-size:12px;cursor:pointer;border-radius:14px;border:1px solid;background:#fff;margin-right:6px}.lv-btn.lv1{border-color:#e0a800;color:#e0a800}.lv-btn.lv1.active{background:#e0a800;color:#fff}.lv-btn.lv2{border-color:#f08a24;color:#f08a24}.lv-btn.lv2.active{background:#f08a24;color:#fff}.lv-btn.lv3{border-color:#d92d20;color:#d92d20}.lv-btn.lv3.active{background:#d92d20;color:#fff}
.table-wrap{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;vertical-align:top}td.w{white-space:normal;min-width:220px}th{position:sticky;top:0;background:#f8fafc;z-index:1}tr:hover td{background:#f7faff}.up{color:#c0392b;font-weight:600}.dn{color:#15803d;font-weight:600}.empty{text-align:center;color:var(--muted);padding:26px 8px}.lk{cursor:pointer;color:var(--blue)}.lk:hover{text-decoration:underline}
.lv{display:inline-block;padding:1px 8px;border-radius:9px;color:#fff;font-size:12px}
details.note{background:#fff8e6;border:1px solid #f5d98a;border-radius:8px;padding:8px 12px;font-size:13px;margin:8px 0}
.winbar{display:contents}.wInfo{font-size:12px;color:var(--muted)}
@media(max-width:1000px){.cards{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}.wrap{padding:18px 12px}}
</style></head><body><main class="wrap"><h1>基金与大盘指数 收益率异常分析</h1><div class="subtitle" id="subtitle"></div><details class="note" id="skipBox" hidden><summary id="skipSum"></summary><div id="skipList"></div></details>

<section class="panel"><h2>一、总览（总）</h2><div class="hint">只分析、不处理：不修改任何原始数据。8 种方法各自给出"正常区间"，一个点被几种方法同时判为异常，就是几票：1~2 票=关注，3~4 票=异常，≥5 票=严重（具体票数门槛随可用方法数略有调整）。疑似接口错误的点已排除在阈值估计之外，不参与异常判定。</div>
<div class="controls"><span class="winbar"></span><label>类型 <select id="typeF"></select></label></div>
<div class="cards" id="cards"></div>
<h3>异常日历：窗口内每天各等级异常的序列数（堆叠；高柱 = 全市场性事件）</h3>
<div class="controls" id="calLvBtns"><button class="lv-btn lv1 active" data-lv="1">关注</button><button class="lv-btn lv2 active" data-lv="2">异常</button><button class="lv-btn lv3 active" data-lv="3">严重</button></div>
<div id="cal" class="chart"></div>
<h3>最新一个交易日的异常榜</h3><div class="controls"><label><input id="onlyAbn" type="checkbox" checked> 仅显示有异常的序列</label></div><div id="latestTable" class="table-wrap"></div></section>

<section class="panel"><h2>二、近期窗口内的异常（分）</h2><div class="controls" style="margin:6px 0 2px"><span class="winbar"></span></div><div class="hint">窗口由"时间范围"决定（一、二、三节的时间范围相互联动）。热力图：行 = 序列（按异常数排序，最多 80 条），列 = 交易日；颜色 = 异常等级。</div><div id="winHeat" class="chart" style="min-height:300px"></div>
<h3>窗口内异常排名</h3><div class="hint">"波动放大倍数" = 窗口内收益率标准差 ÷ 全历史标准差，>1.5 说明近期明显比平时更剧烈。点击序列名称可跳转到下方单只分析。</div><div id="rankTable" class="table-wrap"></div>
<h3>窗口内异常明细</h3><div class="controls"><label>最低等级 <select id="minLv"><option value="1">关注及以上</option><option value="2" selected>异常及以上</option><option value="3">仅严重</option></select></label></div><div id="detailTable" class="table-wrap"></div></section>

<section class="panel" id="sSec"><h2>三、单只序列：异常位置与预判区间（分）</h2><div class="hint">看一只基金/指数在哪个涨跌幅位置开始异常，以及下一个交易日涨跌幅落在什么区间会被判为异常。图表横轴、“窗口内命中”和异常日列表随上方时间范围变化；阈值（正常区间）与分布直方图均基于当前时间范围估计。</div>
<div class="controls"><span class="winbar"></span><label>类型 <select id="ssType"></select></label><label>序列 <select id="ssSel" style="min-width:260px"></select></label><label>阈值线 <select id="lineMode"></select></label></div>
<div class="cards" id="sCards"></div><div id="sChart" class="chart" style="min-height:440px"></div>
<div class="grid2"><div><h3>涨跌幅分布与阈值（当前窗口）</h3><div id="sHist" class="chart"></div></div><div><h3>各方法的正常区间</h3><div id="mTable" class="table-wrap" style="max-height:none"></div></div></div>
<div class="grid2"><div><h3>下一交易日预判：涨跌幅超过多少算异常</h3><div id="zTable"></div><div class="controls"><label>假设下一交易日涨跌幅 <input id="hyp" type="number" step="0.1" placeholder="例如 3.5" style="width:110px"> %</label></div><div id="hypOut" class="hint"></div></div><div><h3>该序列窗口内的异常日</h3><div id="sAnoms" class="table-wrap"></div></div></div></section>

<section class="panel"><h2>四、全部序列的异常阈值总表</h2><div class="hint">每个单元格 = 当前时间范围内该方法给出的正常区间（涨跌幅 %），超出即异常。"预警区间"= 至少 1 种方法判异常的边界，"严重区间"= 至少 5 种方法判异常的边界。EWMA 列为当前窗口结束后的下一交易日预判区间。</div>
<div class="controls"><label>搜索 <input id="thrQ" placeholder="代码或名称" style="width:200px"></label></div><div id="thrTable" class="table-wrap" style="max-height:640px"></div></section>

<section class="panel"><h2>五、方法说明</h2><div id="methodTable" class="table-wrap" style="max-height:none"></div><div class="hint" style="margin-top:8px" id="paramNote"></div></section></main>
<script>
const DATA=__DATA__,S=DATA.series,D=DATA.dates,N=D.length,MT=DATA.methods,$=id=>document.getElementById(id),hasP=typeof Plotly!=="undefined";
const fmt=(v,d=3)=>v==null||!Number.isFinite(+v)?"—":(+v).toFixed(d),esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pc=(v,d=2)=>v==null||!Number.isFinite(+v)?"—":(v>0?"+":"")+(+v).toFixed(d)+"%",cu=v=>v>=0?"up":"dn";
const LV=["正常","关注","异常","严重"],LC=["#94a3b8","#e0a800","#f08a24","#d92d20"];
const lvT=l=>`<span class="lv" style="background:${LC[l]}">${LV[l]}</span>`,popc=m=>{let c=0;while(m){c+=m&1;m>>=1}return c},mnames=m=>MT.filter((x,i)=>m>>i&1).map(x=>x.short).join("、")||"—";
const TCOL={};DATA.types.forEach((t,i)=>TCOL[t]=["#2563eb","#f59e0b","#8b5cf6","#0891b2","#a16207","#c026d3"][i%6]);S.forEach((s,i)=>s.i=i);
/* mk 稀疏数组还原为完整数组 */
S.forEach(s=>{const mk=new Array(N).fill(0);if(s.mk_idx)s.mk_idx.forEach((idx,j)=>{mk[idx]=s.mk_val[j]});s.mk=mk;delete s.mk_idx;delete s.mk_val});
/* 每次改变时间范围后，按当前窗口重新估计阈值并重新计票。 */
let calcCache={key:"",map:new Map()};
const VOTER_KEYS=DATA.voters, MIN_N=DATA.params.min_n;
function qtile(a,p){if(!a.length)return null;const z=[...a].sort((x,y)=>x-y),h=(z.length-1)*p,i=Math.floor(h),f=h-i;return z[i]+(z[i+1]??z[i]-z[i])*f}
function avg(a){return a.length?a.reduce((x,y)=>x+y,0)/a.length:0}
function sdA(a){if(a.length<2)return 0;const m=avg(a);return Math.sqrt(a.reduce((x,y)=>x+(y-m)**2,0)/(a.length-1))}
function medA(a){return qtile(a,.5)}
function mcA(a){const z=[...a].sort((x,y)=>x-y);if(z.length<3)return 0;const md=medA(z),xp=z.filter(x=>x>=md),xm=z.filter(x=>x<=md),h=[];for(const p of xp)for(const m of xm){const den=p-m;if(Math.abs(den)>1e-12)h.push(((p-md)-(md-m))/den)}return h.length?medA(h):0}
function calcFor(s,w,we){const pos=[],x=[];for(let i=w;i<we;i++)if(s.r[i]!=null&&Number.isFinite(+s.r[i])){pos.push(i);x.push(+s.r[i])}
 const out={n:x.length,x,pos,last:pos.length?pos[pos.length-1]:null,b:{},mk:new Array(N).fill(0),lv:new Array(N).fill(0),sg:new Array(N).fill(null),zones:{up:[],dn:[]},k:[0,0],napp:0,mean:null,std:null,skew:null,kurt:null,jb:null};
 if(x.length<MIN_N)return out;
 const q1=qtile(x,.25),q3=qtile(x,.75),iqr=q3-q1,md=medA(x);let mad=medA(x.map(v=>Math.abs(v-md)));if(mad===0)mad=.8453*avg(x.map(v=>Math.abs(v-md)));const mc=mcA(x);
 if(iqr>0){out.b.box15=[q1-1.5*iqr,q3+1.5*iqr];out.b.box3=[q1-3*iqr,q3+3*iqr];out.b.adjbox=mc>=0?[q1-1.5*Math.exp(-3.5*mc)*iqr,q3+1.5*Math.exp(4*mc)*iqr]:[q1-1.5*Math.exp(-4*mc)*iqr,q3+1.5*Math.exp(3.5*mc)*iqr]}
 out.b.pct=[qtile(x,DATA.params.pct/100),qtile(x,1-DATA.params.pct/100)];const sd=sdA(x);if(sd>0)out.b.sigma=[avg(x)-DATA.params.sigma*sd,avg(x)+DATA.params.sigma*sd];if(mad>0){out.b.mad=[md-DATA.params.mad*mad,md+DATA.params.mad*mad];out.b.mz=[md-DATA.params.mz*mad/.6745,md+DATA.params.mz*mad/.6745]}
 let s2=sdA(x.slice(0,Math.min(DATA.params.burn,x.length)))**2;if(!Number.isFinite(s2))s2=sd*sd;const sig=new Array(x.length+1);sig[0]=Math.sqrt(Math.max(0,s2));for(let t=0;t<x.length;t++)sig[t+1]=Math.sqrt(DATA.params.lam*sig[t]**2+(1-DATA.params.lam)*x[t]**2);out.b.ewma=[-DATA.params.ewma_k*sig[x.length],DATA.params.ewma_k*sig[x.length]];pos.forEach((i,t)=>out.sg[i]=sig[t]);
 const votes=new Array(N).fill(0),app=[];MT.forEach((m,bit)=>{const f=new Array(N).fill(false),b=out.b[m.key];if(m.key==='ewma'){app.push(m.key);if(x.length>DATA.params.burn)for(let t=DATA.params.burn;t<x.length;t++)f[pos[t]]=Math.abs(x[t])>DATA.params.ewma_k*sig[t]}else if(b){if(m.voter)app.push(m.key);pos.forEach((i,t)=>{f[i]=x[t]<b[0]||x[t]>b[1]})}pos.forEach(i=>{if(f[i]){out.mk[i]|=1<<bit;if(m.voter)votes[i]++}})});
 out.napp=app.length;const abn=Math.max(2,Math.round(.4*app.length)),sev=Math.max(abn+1,Math.round(.7*app.length));out.k=[abn,sev];pos.forEach(i=>out.lv[i]=votes[i]>=sev?3:votes[i]>=abn?2:votes[i]>=1?1:0);
 const ups=app.map(k=>out.b[k][1]).sort((a,b)=>a-b),dns=app.map(k=>out.b[k][0]).sort((a,b)=>b-a);[1,abn,sev].forEach(j=>{out.zones.up.push(ups[j-1]??null);out.zones.dn.push(dns[j-1]??null)});
 out.mean=avg(x);out.std=sd; if(x.length>2&&sd>0){const m=out.mean,ss=x.reduce((z,v)=>z+(v-m)**2,0),s3=x.reduce((z,v)=>z+(v-m)**3,0),s4=x.reduce((z,v)=>z+(v-m)**4,0);out.skew=x.length/((x.length-1)*(x.length-2))*s3/(sd**3);if(x.length>3){out.kurt=(x.length*(x.length+1)/((x.length-1)*(x.length-2)*(x.length-3)))*(s4/(sd**4))-3*(x.length-1)**2/((x.length-2)*(x.length-3));const jbstat=x.length/6*(out.skew**2+out.kurt**2/4);out.jb=Math.exp(-jbstat/2);}}
 return out}
function C(s){const key=`${w0()}:${w1()}`,id=s.i;if(calcCache.key!==key){calcCache={key,map:new Map()}}if(!calcCache.map.has(id))calcCache.map.set(id,calcFor(s,w0(),w1()));const z=calcCache.map.get(id);if(s._fullStd==null)s._fullStd=s.std;s._calc=z;s.b=z.b;s.zones=z.zones;s.k=z.k;s.napp=z.napp;s.n=z.n;s.mean=z.mean;s.std=z.std;s.skew=z.skew;s.kurt=z.kurt;s.mc=z.mc;s.jb=z.jb;s.mk=z.mk;s.lv=z.lv;s.sg=z.sg;s.last=z.last;return z}
function rankC(c,x){const a=[...c.x].sort((u,v)=>u-v);let lo=0,hi=a.length;while(lo<hi){const m=(lo+hi)>>1;a[m]<x?lo=m+1:hi=m}return a.length?100*lo/a.length:null}
/* 日期范围窗口 */
let winStart=D[0],winEnd=D[N-1],winPreset="12";
function w0(){let i=0;while(i<N&&D[i]<winStart)i++;return i}
function w1(){let i=N;while(i>0&&D[i-1]>winEnd)i--;return i}
function syncBars(msg){document.querySelectorAll(".wS").forEach(x=>x.value=winStart);document.querySelectorAll(".wE").forEach(x=>x.value=winEnd);
document.querySelectorAll(".winbar [data-months]").forEach(b=>b.classList.toggle("active",b.dataset.months===String(winPreset)));
const a=w0(),b=w1();document.querySelectorAll(".wInfo").forEach(x=>{x.textContent=msg||(b>a?`${D[a]} ~ ${D[b-1]} · ${b-a} 个交易日`:"");x.style.color=msg?"#b42318":""})}
function setWin(s,e,preset){if(s>e){const t=s;s=e;e=t}if(s<D[0])s=D[0];if(e>D[N-1])e=D[N-1];const os=winStart,oe=winEnd;winStart=s;winEnd=e;
if(w1()<=w0()){winStart=os;winEnd=oe;syncBars("⚠️ 所选区间内没有交易日，已保持原区间");return}
winPreset=preset;calcCache={key:"",map:new Map()};syncBars("");refreshAll()}
function setPreset(m){const end=D[N-1];let s;if(m==="all")s=D[0];else{const d=new Date(end+"T00:00:00Z");d.setUTCMonth(d.getUTCMonth()-(+m));s=d.toISOString().slice(0,10)}setWin(s,end,m)}
function refreshAll(){renderOverview();renderWindow();renderSeries()}
function initBars(){const P=[["1","1月"],["3","3月"],["6","6月"],["12","1年"],["36","3年"],["all","全部"]],
h=`<label>时间范围<span class="btn-grp">${P.map(([m,t])=>`<button type="button" data-months="${m}">${t}</button>`).join("")}</span></label><label>开始 <input type="date" class="wS" min="${D[0]}" max="${D[N-1]}" style="width:140px"></label><label>结束 <input type="date" class="wE" min="${D[0]}" max="${D[N-1]}" style="width:140px"></label><span class="wInfo"></span>`;
document.querySelectorAll(".winbar").forEach(b=>b.innerHTML=h);
document.addEventListener("click",e=>{const b=e.target.closest(".winbar [data-months]");if(b)setPreset(b.dataset.months)});
document.addEventListener("change",e=>{if(e.target.matches&&e.target.matches(".wS,.wE")){const bar=e.target.closest(".winbar"),s=bar.querySelector(".wS").value,en=bar.querySelector(".wE").value;if(s&&en)setWin(s,en,"")}})}
const okT=s=>$("typeF").value==="all"||s.type===$("typeF").value;
const sd_=a=>{if(a.length<2)return null;const m=a.reduce((x,y)=>x+y,0)/a.length;return Math.sqrt(a.reduce((x,y)=>x+(y-m)**2,0)/(a.length-1))};
const srt=s=>s._calc?[...s._calc.x].sort((a,b)=>a-b):(s._s??=s.r.filter(v=>v!=null).sort((a,b)=>a-b));
function prank(s,x){const a=srt(s);let lo=0,hi=a.length;while(lo<hi){const m=(lo+hi)>>1;a[m]<x?lo=m+1:hi=m}return a.length?100*lo/a.length:null}
const tbl=(h,rows)=>rows.length?`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`:'<div class="empty">没有符合条件的数据。</div>';
const nameLk=s=>`<span class="lk" data-i="${s.i}">${esc(s.name)}</span> <span class="hint">${esc(s.code)}</span>`;
function plot(id,data,layout){if(!hasP){$(id).innerHTML='<div class="empty">图表库 Plotly 未加载（需联网，或把 plotly.min.js 放在运行目录）。表格不受影响。</div>';return}Plotly.react(id,data,Object.assign({margin:{l:55,r:20,t:15,b:50},paper_bgcolor:"#fff",plot_bgcolor:"#fff",font:{family:"Microsoft YaHei,PingFang SC,Arial",size:12}},layout),{responsive:true,displaylogo:false})}

/* ===== 一、总览 ===== */
let calShowLv={1:true,2:true,3:true};
function renderOverview(){const w=w0(),we=w1(),L=S.filter(okT),c=[0,0,0,0],dayByLv={1:new Array(N).fill(0),2:new Array(N).fill(0),3:new Array(N).fill(0)};
 S.forEach(s=>{const z=C(s);for(let i=w;i<we;i++){if(s.r[i]==null)continue;if(okT(s))c[z.lv[i]]++;if(z.lv[i]>=1&&z.lv[i]<=3)dayByLv[z.lv[i]][i]++}});
 let bi=w;for(let i=w;i<we;i++){const tot=dayByLv[1][i]+dayByLv[2][i]+dayByLv[3][i];if(tot>(dayByLv[1][bi]+dayByLv[2][bi]+dayByLv[3][bi]))bi=i}
 let abnSeq=0;S.forEach(s=>{const z=C(s);if(okT(s)){for(let i=w;i<we;i++){if(s.r[i]!=null&&z.lv[i]>=1){abnSeq++;break}}}});
 const v=[["序列数（当前筛选）",L.length,`共 ${S.length} 条序列`],["分析窗口",`${D[w]} ~ ${D[we-1]}`,`${we-w} 个交易日`],["窗口内 严重 / 异常 / 关注",`${c[3]} / ${c[2]} / ${c[1]}`,"出现次数（序列×日）"],["窗口内有异常的序列数",abnSeq,`占筛选序列 ${L.length?(100*abnSeq/L.length).toFixed(0):0}%`],["窗口内异常最集中的一天",D[bi],`${dayByLv[1][bi]+dayByLv[2][bi]+dayByLv[3][bi]} 条序列同时 异常/严重`],["最新数据日期",D[N-1],"全部序列中最晚的一天"]];
 $("cards").innerHTML=v.map(x=>`<div class="card"><div class="label">${x[0]}</div><div class="value">${esc(x[1])}</div><div class="sub">${esc(x[2])}</div></div>`).join("");
 renderCal();renderLatest()}
function renderCal(){const w=w0(),we=w1(),xs=D.slice(w,we),traces=[];
 [{lv:1,name:"关注",color:LC[1]},{lv:2,name:"异常",color:LC[2]},{lv:3,name:"严重",color:LC[3]}].forEach(o=>{
  if(!calShowLv[o.lv])return;
  const cnt=new Array(N).fill(0);S.forEach(s=>{if(!okT(s))return;const z=C(s);for(let i=w;i<we;i++){if(s.r[i]!=null&&z.lv[i]===o.lv)cnt[i]++}});
  traces.push({x:xs,y:cnt.slice(w,we),type:"bar",name:o.name,marker:{color:o.color},hovertemplate:"%{x}<br>"+o.name+"：%{y} 条序列<extra></extra>"});
 });
 plot("cal",traces,{barmode:"stack",yaxis:{title:"异常序列数"},legend:{orientation:"h",y:1.12},height:340,margin:{l:55,r:20,t:15,b:75},xaxis:{type:"date",tickformat:"%Y-%m-%d",tickangle:-45}})}
function renderLatest(){const only=$("onlyAbn").checked,rows=S.filter(okT).map(s=>{const z=C(s),i=z.last;return {s,z,i,l:i==null?0:z.lv[i],r:i==null?null:s.r[i]}}).filter(x=>x.i!=null&&(!only||x.l>=1)).sort((a,b)=>b.l-a.l||Math.abs(b.r)-Math.abs(a.r));
 $("latestTable").innerHTML=tbl(["序列","类型","日期","涨跌幅","窗口分位","票数","命中方法","等级"],rows.slice(0,300).map(x=>`<tr><td>${nameLk(x.s)}</td><td>${esc(x.s.type)}</td><td>${D[x.i]}</td><td class="${cu(x.r)}">${pc(x.r)}</td><td>${fmt(rankC(x.z,x.r),1)+"%"}</td><td>${popc(x.z.mk[x.i])+" / "+x.z.napp}</td><td class="w">${esc(mnames(x.z.mk[x.i]))}</td><td>${lvT(x.l)}</td></tr>`))}

/* ===== 二、窗口 ===== */
let rankCache=[];
function winRows(){const w=w0(),we=w1();return S.filter(okT).map(s=>{const z=C(s),c=[0,0,0,0],v=[];let n=0,up=null,dn=null,last=null;for(let i=w;i<we;i++){if(s.r[i]==null)continue;n++;c[z.lv[i]]++;const x=s.r[i];v.push(x);if(up==null||x>up)up=x;if(dn==null||x<dn)dn=x;if(z.lv[i]>=1)last=i}const sd=sd_(v);return{s,z,n,c,up,dn,last,vr:sd!=null&&s._fullStd?sd/s._fullStd:null}}).sort((a,b)=>b.c[3]-a.c[3]||b.c[2]-a.c[2]||b.c[1]-a.c[1])}
function renderWindow(){const w=w0(),we=w1(),rows=winRows();rankCache=rows;
 const top=rows.filter(r=>r.c[1]+r.c[2]+r.c[3]>0).slice(0,80),xs=D.slice(w,we);
 plot("winHeat",[{z:top.map(r=>r.z.lv.slice(w,we).map((l,k)=>r.s.r[w+k]==null?null:l)),x:xs,y:top.map(r=>r.s.name+" "+r.s.code),type:"heatmap",zmin:0,zmax:3,showscale:false,xgap:1,ygap:1,
  colorscale:[[0,"#eef1f6"],[.25,"#eef1f6"],[.25,"#f6c453"],[.5,"#f6c453"],[.5,"#f08a24"],[.75,"#f08a24"],[.75,"#d92d20"],[1,"#d92d20"]],
  customdata:top.map(r=>r.s.r.slice(w,we)),hovertemplate:"%{y}<br>%{x}<br>涨跌幅 %{customdata:.2f}%<br>等级：%{z}（0正常 1关注 2异常 3严重）<extra></extra>"}],{height:Math.max(240,top.length*15+90),margin:{l:190,r:25,t:10,b:80},yaxis:{autorange:"reversed",automargin:true,tickfont:{size:10}},xaxis:{type:"date",tickformat:"%Y-%m-%d",tickangle:-45,nticks:Math.min(24,Math.ceil(xs.length/20))}});
 $("rankTable").innerHTML=tbl(["序列","类型","窗口样本","严重","异常","关注","异常占比","最大涨幅","最大跌幅","波动放大倍数","最近异常日"],rows.map(r=>`<tr><td>${nameLk(r.s)}</td><td>${esc(r.s.type)}</td><td>${r.n}</td><td>${r.c[3]}</td><td>${r.c[2]}</td><td>${r.c[1]}</td><td>${r.n?(100*(r.c[1]+r.c[2]+r.c[3])/r.n).toFixed(0)+"%":"—"}</td><td class="up">${pc(r.up)}</td><td class="dn">${pc(r.dn)}</td><td>${r.vr==null?"—":(r.vr>1.5?`<b class="up">${r.vr.toFixed(2)}</b>`:r.vr.toFixed(2))}</td><td>${r.last==null?"—":D[r.last]}</td></tr>`));
 renderDetail()}
function renderDetail(){const w=w0(),we=w1(),m=+$("minLv").value,rows=[];S.filter(okT).forEach(s=>{const z=C(s);for(let i=w;i<we;i++){const l=z.lv[i];if(s.r[i]!=null&&l>=m&&l<=3)rows.push({s,z,i,l})}});
 rows.sort((a,b)=>b.i-a.i||b.l-a.l);$("detailTable").innerHTML=tbl(["日期","序列","类型","涨跌幅","窗口分位","票数","命中方法","等级"],rows.slice(0,500).map(x=>`<tr><td>${D[x.i]}</td><td>${nameLk(x.s)}</td><td>${esc(x.s.type)}</td><td class="${cu(x.s.r[x.i])}">${pc(x.s.r[x.i])}</td><td>${fmt(rankC(x.z,x.s.r[x.i]),1)+"%"}</td><td>${popc(x.z.mk[x.i])+" / "+x.z.napp}</td><td class="w">${esc(mnames(x.z.mk[x.i]))}</td><td>${lvT(x.l)}</td></tr>`))+(rows.length>500?`<div class="hint">共 ${rows.length} 条，仅显示最近 500 条。</div>`:"")}


/* ===== 三、单只 ===== */
const curS=()=>S[+$("ssSel").value],kcol={box15:"#2563eb",box3:"#0891b2",adjbox:"#8b5cf6",pct:"#a16207",sigma:"#c026d3",mad:"#475569",mz:"#0d9488",ewma:"#f59e0b"};
function fillSel(){const t=$("ssType").value,L=S.filter(s=>t==="all"||s.type===t);$("ssSel").innerHTML=L.map(s=>`<option value="${s.i}">${esc(s.name)}（${esc(s.code)}）</option>`).join("")}
function pickSeries(i){$("ssType").value="all";fillSel();$("ssSel").value=i;renderSeries();$("sSec").scrollIntoView({behavior:"smooth"})}
function renderSeries(){const s=curS();if(!s)return;C(s);const w=w0(),we=w1(),lm=$("lineMode").value,Z=s.zones,tiers=[["关注线（≥1 种方法）","#e0a800","dot"],["异常线（≥"+s.k[0]+" 种）","#f08a24","dash"],["严重线（≥"+s.k[1]+" 种）","#d92d20","solid"]];
 const last=s.last,l=s.lv[last];$("sCards").innerHTML=[["样本数",s.n,`${s.type}${s.sub?" · "+s.sub:""}`],["均值 / 标准差",`${pc(s.mean,3)} / ${fmt(s.std,3)}%`,"日涨跌幅"],["偏度 / 超额峰度",`${fmt(s.skew,2)} / ${fmt(s.kurt,2)}`,"厚尾：峰度远大于 0"],["JB 正态检验 p 值",s.jb==null?"—":(s.jb<1e-4?"<0.0001":fmt(s.jb,4)),s.jb!=null&&s.jb<.05?"非正态：3σ 法可信度低，优先看 MAD/调整箱线":"当前窗口"],["MedCouple 偏度",fmt(s.mc,3),s.mc>.1?"右偏":s.mc<-.1?"左偏":"基本对称"],["最新一日",last==null?"—":`${pc(s.r[last])} ${D[last].slice(2)}`,last==null?"当前窗口无有效数据":LV[l]]].map(x=>`<div class="card"><div class="label">${x[0]}</div><div class="value" style="font-size:16px">${x[1]}</div><div class="sub">${x[2]}</div></div>`).join("");
 const fd=ms=>new Date(ms).toISOString().slice(0,19).replace("T"," "),pad=864e5*.6,xRange=we>w?[fd(Date.parse(D[w])-pad),fd(Date.parse(D[we-1])+pad)]:undefined;
 const xs=D,traces=[{x:xs,y:s.r,type:"bar",name:"日涨跌幅",marker:{color:s.lv.map(q=>LC[q])},customdata:s.lv.map((q,i)=>[LV[q],mnames(s.mk[i])]),hovertemplate:"%{x}<br>%{y:.2f}%<br>%{customdata[0]}：%{customdata[1]}<extra></extra>"}];
 const vals=srt(s);let ext=0;for(let i=w;i<we;i++)if(s.r[i]!=null)ext=Math.max(ext,Math.abs(s.r[i]));const lim=Math.max(ext,...Z.up.map(Math.abs),...Z.dn.map(Math.abs))*1.15;
 const hl=(y,c,dash,name,grp,show)=>({x:[D[0],D[N-1]],y:[y,y],mode:"lines",type:"scatter",line:{color:c,dash,width:1.5},name,legendgroup:grp,showlegend:show,hoverinfo:"y+name"});
 const band=()=>{const e=DATA.params.ewma_k;traces.push({x:xs,y:s.sg.map(v=>v==null?null:e*v),mode:"lines",type:"scatter",line:{width:0},showlegend:false,hoverinfo:"skip"},{x:xs,y:s.sg.map(v=>v==null?null:-e*v),mode:"lines",type:"scatter",line:{width:0},fill:"tonexty",fillcolor:"rgba(245,158,11,.14)",name:"EWMA ±3σt 动态带",hoverinfo:"skip"})};
 const vlines=[];
 if(lm==="zones")tiers.forEach((t,k)=>{traces.push(hl(Z.up[k],t[1],t[2],t[0]+" 下一日预判","z"+k,true),hl(Z.dn[k],t[1],t[2],t[0],"z"+k,false));vlines.push(Z.up[k],Z.dn[k])});
 else{const ks=lm==="all"?MT.map(m=>m.key):[lm];ks.forEach(k=>{if(k==="ewma"){if(lm!=="all")band();return}const b=s.b[k];if(!b)return;const nm=MT.find(m=>m.key===k).short;traces.push(hl(b[1],kcol[k],"dash",nm,k,true),hl(b[0],kcol[k],"dash",nm,k,false));vlines.push(b[0],b[1])});if(lm==="all")band()}
 const shapes=[];
 plot("sChart",traces,{shapes,height:440,yaxis:{title:"日涨跌幅（%）",range:[-lim,lim]},legend:{orientation:"h",y:1.14},hovermode:"closest",xaxis:{type:"date",tickformat:"%Y-%m-%d",range:xRange},bargap:.2});
 const hs=[{x:vals,type:"histogram",nbinsx:70,marker:{color:"#94a3b8"},name:"收益率",hovertemplate:"%{x:.2f}%<br>%{y} 天<extra></extra>"}];
 plot("sHist",hs,{height:340,shapes:vlines.map((v,k)=>({type:"line",xref:"x",yref:"paper",x0:v,x1:v,y0:0,y1:1,line:{color:lm==="zones"?tiers[Math.floor(k/2)][1]:kcol[lm]||"#475569",dash:"dash",width:1.5}})),xaxis:{title:"日涨跌幅（%）"},yaxis:{title:"天数"},margin:{l:55,r:15,t:10,b:50}});
 const lastI=s.last;$("mTable").innerHTML=tbl(["方法","下界","上界","窗口命中","最新一日"],MT.map((m,i)=>{const b=s.b[m.key];let ww=0;if(b)s.mk.forEach((x,j)=>{if(x>>i&1)ww++});return`<tr><td>${esc(m.name)}</td><td class="dn">${b?pc(b[0]):"不适用"}</td><td class="up">${b?pc(b[1]):"不适用"}</td><td>${b?ww:"—"}</td><td>${b&&lastI!=null?((s.mk[lastI]>>i&1)?"命中":"—"):"—"}</td></tr>`}));
 $("zTable").innerHTML=tbl(["档位","上涨超过","下跌低于","含义"],tiers.map((t,k)=>`<tr><td>${t[0].split("（")[0]}</td><td class="up">${pc(Z.up[k])}</td><td class="dn">${pc(Z.dn[k])}</td><td class="w">${["至少 1 种方法判异常","≥"+s.k[0]+" 种方法同时判异常","≥"+s.k[1]+" 种方法同时判异常"][k]}（共 ${s.napp} 种计票方法）</td></tr>`))+`<div class="hint" style="margin-top:6px">基于当前窗口重新计算的静态阈值 + EWMA 下一交易日波动预测。</div>`;
 const rows=[];for(let i=we-1;i>=w;i--)if(s.r[i]!=null&&s.lv[i]>=1)rows.push(`<tr><td>${D[i]}</td><td class="${cu(s.r[i])}">${pc(s.r[i])}</td><td>${fmt(prank(s,s.r[i]),1)+"%"}</td><td>${popc(s.mk[i])+" / "+s.napp}</td><td class="w">${esc(mnames(s.mk[i]))}</td><td>${lvT(s.lv[i])}</td></tr>`);
 $("sAnoms").innerHTML=tbl(["日期","涨跌幅","历史分位","票数","命中方法","等级"],rows);renderHyp()}
function renderHyp(){const s=curS(),v=$("hyp").value;if(!s||v===""){$("hypOut").textContent="";return}const x=+v;let votes=0;const hit=[];MT.forEach(m=>{const b=s.b[m.key];if(b&&(x<b[0]||x>b[1])){hit.push(m.short);if(m.voter)votes++}});const l=votes>=s.k[1]?3:votes>=s.k[0]?2:votes>=1?1:0;
 $("hypOut").innerHTML=`涨跌幅 <b class="${cu(x)}">${pc(x)}</b>：窗口分位 ${fmt(prank(s,x),1)}%；被 ${votes} / ${s.napp} 种方法判为异常（${hit.join("、")||"无"}）→ ${lvT(l)}`}

/* ===== 四、阈值总表 / 五、方法 ===== */
function renderThr(){const q=$("thrQ").value.trim().toLowerCase(),L=S.filter(okT).filter(s=>!q||s.name.toLowerCase().includes(q)||s.code.toLowerCase().includes(q));L.forEach(C);const iv=b=>b?`<span class="dn">${pc(b[0])}</span> ~ <span class="up">${pc(b[1])}</span>`:"—";
 $("thrTable").innerHTML=tbl(["序列","类型","窗口样本","偏度","峰度","JB p",...MT.map(m=>m.short+(m.key==="ewma"?"（下一日）":"")),"预警区间（≥1 种）","严重区间"],L.map(s=>`<tr><td>${nameLk(s)}</td><td>${esc(s.type)}</td><td>${s.n}</td><td>${fmt(s.skew,2)}</td><td>${fmt(s.kurt,1)}</td><td class="${s.jb!=null&&s.jb<.05?"up":""}">${s.jb==null?"—":(s.jb<1e-4?"<0.0001":fmt(s.jb,3))}</td>${MT.map(m=>`<td>${iv(s.b[m.key])}</td>`).join("")}<td>${iv([s.zones.dn[0],s.zones.up[0]])}</td><td>${iv([s.zones.dn[2],s.zones.up[2]])}</td></tr>`))}
function renderMethods(){$("methodTable").innerHTML=tbl(["#","方法","计票","判定公式","说明"],MT.map((m,i)=>`<tr><td>${i+1}</td><td><b>${esc(m.name)}</b></td><td>${m.voter?"是":"否（极端标记）"}</td><td class="w">${esc(m.formula)}</td><td class="w">${esc(m.note)}</td></tr>`));
 const p=DATA.params;$("paramNote").innerHTML=`参数：固定比例 ${p.pct}%/${100-p.pct}%；σ 倍数 ${p.sigma}；MAD 倍数 ${p.mad}；修正 Z 阈值 ${p.mz}；EWMA λ=${p.lam}、倍数 ${p.ewma_k}、预热 ${p.burn} 个观测；最少样本 ${p.min_n}。等级：票数 ≥1 关注，≥ 异常门槛 异常，≥ 严重门槛 严重（可用方法 7 种时为 3 票 / 5 票）。<br><b>牛熊与日期范围影响：</b>箱线/百分位/3σ/MAD/修正Z/EWMA 的阈值、方法命中和异常等级均按<b>当前时间窗口</b>重新计算；切换时间范围后，结果会随窗口内样本重新变化。窗口有效样本少于最少样本数时，该序列不提供可靠阈值。`}

function init(){$("subtitle").textContent=`${DATA.unit_note}；日期范围 ${D[0]} ~ ${D[N-1]}，共 ${S.length} 条序列。`;
 if(DATA.skipped.length){$("skipBox").hidden=false;$("skipSum").textContent=`有 ${DATA.skipped.length} 条序列未纳入分析（点击展开）`;$("skipList").innerHTML=DATA.skipped.map(x=>`<div>${esc(x)}</div>`).join("")}
 const ts=`<option value="all">全部类型</option>`+DATA.types.map(t=>`<option>${esc(t)}</option>`).join("");$("typeF").innerHTML=ts;$("ssType").innerHTML=ts;
 $("lineMode").innerHTML=`<option value="zones">共识三档（下一日预判）</option><option value="all">全部方法</option>`+MT.map(m=>`<option value="${m.key}">${esc(m.name)}</option>`).join("");
 /* 默认窗口：最近一年 */
 initBars();{const d=new Date(D[N-1]+"T00:00:00Z");d.setUTCFullYear(d.getUTCFullYear()-1);const s=d.toISOString().slice(0,10);winStart=s<D[0]?D[0]:s;winEnd=D[N-1];winPreset="12";syncBars("")}
 fillSel();renderOverview();renderWindow();renderThr();renderMethods();
 const best=rankCache.length?rankCache[0].s.i:0;$("ssSel").value=best;renderSeries();
 /* 事件绑定 */
 
 
 $("typeF").onchange=()=>{renderOverview();renderWindow();renderThr()};$("onlyAbn").onchange=renderLatest;$("minLv").onchange=renderDetail;
 document.querySelectorAll("#calLvBtns .lv-btn").forEach(b=>b.onclick=()=>{b.classList.toggle("active");calShowLv[+b.dataset.lv]=b.classList.contains("active");renderCal()});
 $("ssType").onchange=()=>{fillSel();renderSeries()};$("ssSel").onchange=renderSeries;$("lineMode").onchange=renderSeries;$("hyp").oninput=renderHyp;$("thrQ").oninput=renderThr;
 document.addEventListener("click",e=>{const t=e.target.closest(".lk");if(t)pickSeries(+t.dataset.i)})}
init();
</script></body></html>'''


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="基金 + 大盘指数 日收益率异常分析（只分析不处理）")
    ap.add_argument("nav", nargs="?", default="fund_nav_history.csv")
    ap.add_argument("target", nargs="?", default="funds_universe_example.csv")
    ap.add_argument("output", nargs="?", default="fund_anomaly_analysis.html")
    ap.add_argument("--no-index", action="store_true", help="不加入大盘指数")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，重新抓取指数")
    ap.add_argument("--cache-dir", default=cor.INDEX_CACHE)
    ap.add_argument("--nav-unit", choices=["auto", "pct", "frac"], default="auto", help='净值"增长率"无%号时的单位：auto 自动识别 / pct 百分数 / frac 小数')
    a = ap.parse_args()
    generate(a.nav, a.target, a.output, a.nav_unit, not a.no_index, a.refresh, a.cache_dir)

