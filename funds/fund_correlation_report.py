"""基金分组聚类与相关性分析 —— 生成交互式 HTML 报告。

输入：
  fund_nav_history.csv        fetch_fund_nav.py 的输出，需含列：基金代码、日期、日增长率（也兼容“增长率”）
                              “日增长率”是百分数（0.52=0.52%），本脚本读入时统一÷100 成小数（--nav-unit 可改）
  funds_universe_example.csv  需含列：基金代码、基金名称、类型（类型列即分组依据）
输出：
  fund_correlation_report.html

说明：相关矩阵、聚类、阈值都在浏览器里按页面上的“时间范围 / 开始 / 结束”实时重算
（每一节顶部都有同一组联动控件），因此本脚本只负责把日收益率写入 HTML，不再依赖 scipy / sklearn。

报告结构：
  一、分组聚类网络图（下拉框按“类型”列切换分组，节点距离 = 相关性）
  二、聚类 / 高相关结果表（随上方分组联动）
  三、不同类型间的相关性（图 → 排行榜表格）
  四、指定基金相关性查询
  五、收益率分档关联与滚动相关（按需计算）

用法：python fund_correlation_report.py [净值csv] [目标基金csv] [输出html] [--no-index] [--refresh] [--nav-unit pct|frac|auto]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

MIN_OBS = 20       # 基金有效观测数少于该值则剔除
MIN_OVERLAP = 20   # 两只基金共同交易日少于该值则相关系数记为空
MAX_K = 10         # 候选聚类数上限
ALL_GROUP = "全部基金"
INDEX_TYPE = "大盘指数"       # 大盘指数在报告中作为一个独立“类型”
INDEX_CACHE = "index_cache"   # 指数日线缓存目录（避免每次都联网抓取）

# 名称, 接口类型, 接口symbol, 国内/国外, 细分
BROAD_INDICES = {
    # --- A股指数 ---
    "sh000001": ["上证指数", "zh", "sh000001", "国内", "A股宽基"],
    "sh000300": ["沪深300", "zh", "sh000300", "国内", "A股宽基"],
    "sz399001": ["深证成指", "zh", "sz399001", "国内", "A股宽基"],
    "sz399006": ["创业板指", "zh", "sz399006", "国内", "A股宽基"],
    "sh000016": ["上证50", "zh", "sh000016", "国内", "A股宽基"],
    "sh000905": ["中证500", "zh", "sh000905", "国内", "A股宽基"],
    "sh000688": ["科创50", "zh", "sh000688", "国内", "A股宽基"],
    "bj899050": ["北证50", "zh", "bj899050", "国内", "A股宽基"],
    "sh000010": ["上证收益", "zh", "sh000010", "国内", "A股宽基"],
    # --- 美股指数 ---
    ".IXIC": ["纳斯达克", "us", ".IXIC", "国外", "美股宽基"],
    ".INX": ["标普500", "us", ".INX", "国外", "美股宽基"],
    ".DJI": ["道琼斯", "us", ".DJI", "国外", "美股宽基"],
    # --- 港股指数 ---
    "hkHSI": ["恒生指数", "hk", "HSI", "国外", "港股宽基"],
    "hkHSTECH": ["恒生科技指数", "hk", "HSTECH", "国外", "港股宽基"],
    # --- 环球外盘指数 ---
    "jpN225": ["日经225", "global", "日经225指数", "国外", "外盘宽基"],
    "krKOSPI": ["韩国综合指数", "global", "首尔综合指数", "国外", "外盘宽基"],
}


def read_csv_safe(path, dtype=None):
    last = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return pd.read_csv(path, dtype=dtype, encoding=enc)
        except (UnicodeDecodeError, pd.errors.ParserError) as e:
            last = e
    raise ValueError(f"无法读取 {path}: {last}")


RET_COL_ALIASES = ("增长率", "日增长率")   # fetch_fund_nav.py 输出的列叫“日增长率”


def normalize_nav_cols(nav):
    """把净值表里的收益率列统一成“增长率”（兼容 fetch_fund_nav.py 的“日增长率”）。"""
    if "增长率" not in nav.columns:
        for c in RET_COL_ALIASES[1:]:
            if c in nav.columns:
                return nav.rename(columns={c: "增长率"})
    return nav


def _import_akshare():
    try:
        import net_patch  # noqa: F401  若本机有 net_patch.py（代理/域名补丁），必须先于 akshare 导入
    except ImportError:
        pass
    import akshare as ak
    return ak


def _fetch_one_index(ak, meta):
    _, api, symbol = meta[:3]
    if api == "zh":
        return ak.stock_zh_index_daily(symbol=symbol)
    if api == "us":
        return ak.index_us_stock_sina(symbol=symbol)
    if api == "hk":
        return ak.stock_hk_index_daily_sina(symbol=symbol)
    if api == "global":
        return ak.index_global_hist_sina(symbol=symbol)
    return pd.DataFrame()


def _std_index_df(df):
    """统一为 Date/Close 两列（兼容大小写及中文列名）。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["Date", "Close"])
    ren = {}
    for c in df.columns:
        cl = str(c).strip().lower()
        if cl in ("date", "日期") and "Date" not in ren.values():
            ren[c] = "Date"
        elif cl in ("close", "收盘", "收盘价", "最新价") and "Close" not in ren.values():
            ren[c] = "Close"
    df = df.rename(columns=ren)
    if "Date" not in df.columns or "Close" not in df.columns:
        return pd.DataFrame(columns=["Date", "Close"])
    out = df[["Date", "Close"]].copy()
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
    return out.dropna().drop_duplicates("Date").sort_values("Date").reset_index(drop=True)


def fetch_index_closes(end, cache_dir=INDEX_CACHE, refresh=False):
    """抓取 BROAD_INDICES 的日线收盘价（带本地缓存）。返回 ({代码: DataFrame[Date, Close]}, [(代码, 失败原因)])。"""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    ak, closes, fails = None, {}, []
    for key, meta in BROAD_INDICES.items():
        f = cache / (key.replace(".", "_") + ".csv")
        df, err = pd.DataFrame(), ""
        if f.exists() and not refresh:
            old = _std_index_df(pd.read_csv(f, encoding="utf-8-sig"))
            if len(old) and old["Date"].max() >= end - pd.Timedelta(days=7):
                df = old
        if df.empty:
            try:
                print(f"  抓取指数 {meta[0]} ({key}) ...")
                ak = ak or _import_akshare()
                df = _std_index_df(_fetch_one_index(ak, meta))
                if not df.empty:
                    df.to_csv(f, index=False, encoding="utf-8-sig")
            except Exception as e:  # 单个指数失败不影响整体
                err = f"{type(e).__name__}: {e}"[:120]
            if df.empty and f.exists():  # 抓取失败时退回旧缓存
                df = _std_index_df(pd.read_csv(f, encoding="utf-8-sig"))
        if df.empty:
            fails.append((key, err or "接口未返回数据"))
        else:
            closes[key] = df
    return closes, fails


def fetch_index_returns(start, end, cache_dir=INDEX_CACHE, refresh=False):
    """BROAD_INDICES 日收益率（小数）。返回 ({代码: 收益率Series}, [(代码, 失败原因)])。"""
    closes, fails = fetch_index_closes(end, cache_dir, refresh)
    rets = {}
    for key, df in closes.items():
        s = df.set_index("Date")["Close"].pct_change()
        s = s[(s.index >= start) & (s.index <= end)].dropna()
        if len(s):
            rets[key] = s
        else:
            fails.append((key, "基金净值日期区间内没有该指数数据"))
    return rets, fails


def code_series(s):
    return s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)


def json_ready(x):
    if isinstance(x, dict):
        return {str(k): json_ready(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_ready(v) for v in x]
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return None if not np.isfinite(x) else float(x)
    if x is None:
        return None
    return x


def generate_correlation_html(nav_file, target_file, output_file, include_indices=True, refresh=False,
                              cache_dir=INDEX_CACHE, unit="pct"):
    nav = normalize_nav_cols(read_csv_safe(nav_file, {"基金代码": str}))
    target = read_csv_safe(target_file, {"基金代码": str})
    for df, cols, p in [(nav, ["基金代码", "日期", "增长率"], nav_file),
                        (target, ["基金代码", "基金名称", "类型"], target_file)]:
        miss = [c for c in cols if c not in df.columns]
        if miss:
            raise ValueError(f"{p} 缺少字段：{', '.join(miss)}")
    nav, target = nav.copy(), target.copy()
    nav["基金代码"], target["基金代码"] = code_series(nav["基金代码"]), code_series(target["基金代码"])
    target["基金名称"] = target["基金名称"].fillna("").astype(str).str.strip()
    target["类型"] = target["类型"].fillna("未分类").astype(str).str.strip().replace("", "未分类")
    target = target.drop_duplicates("基金代码")

    nav["日期"] = pd.to_datetime(nav["日期"], errors="coerce")
    # 收益率统一成“小数”（0.01 = 1%），与指数的 pct_change() 以及页面里 ±0.01 的分档阈值同口径。
    # fetch_fund_nav.py 输出的“日增长率”是不带 % 号的百分数（0.52 表示 0.52%），必须除以 100；
    # 带 % 号的字符串无论 unit 取值，一律按百分数处理。
    raw = nav["增长率"].astype(str).str.strip()
    has_pct = raw.str.contains("%", regex=False)
    val = pd.to_numeric(raw.str.replace("%", "", regex=False).str.replace(",", "", regex=False),
                        errors="coerce")
    plain = val[~has_pct].dropna()
    med = float(plain.abs().median()) if len(plain) else float("nan")
    as_pct = {"pct": True, "frac": False}.get(unit, bool(len(plain)) and med > 0.05)   # auto：中位绝对值>0.05 视为百分数
    nav["增长率"] = np.where(has_pct | as_pct, val / 100.0, val)
    print(f"净值文件“增长率”按{'百分数（已÷100）' if as_pct else '小数'}解读"
          + (f"（自动识别：无%号数值的中位绝对值={med:.4f}）" if unit == "auto" and len(plain) else ""))
    nav = nav.dropna(subset=["日期", "增长率"])
    pivot = nav.pivot_table(index="日期", columns="基金代码", values="增长率", aggfunc="mean").sort_index()

    names = dict(zip(target["基金代码"], target["基金名称"]))
    types = dict(zip(target["基金代码"], target["类型"]))
    subtype, region, idx_keys, idx_fail = {}, {}, [], []
    if include_indices:
        rets, idx_fail = fetch_index_returns(pivot.index.min(), pivot.index.max(), cache_dir, refresh)
        if rets:
            pivot = pd.concat([pivot, pd.DataFrame({k: v.reindex(pivot.index) for k, v in rets.items()})], axis=1)
        for key in rets:
            names[key], types[key] = BROAD_INDICES[key][0], INDEX_TYPE
            subtype[key], region[key] = BROAD_INDICES[key][4], BROAD_INDICES[key][3]
            idx_keys.append(key)
    obs = pivot.notna().sum()
    codes, missing = [], []
    for c in target["基金代码"]:
        if c not in pivot.columns:
            missing.append({"code": c, "name": names[c], "type": types[c], "reason": "净值文件中没有该基金代码"})
        elif obs[c] < MIN_OBS:
            missing.append({"code": c, "name": names[c], "type": types[c], "reason": f"有效观测仅 {int(obs[c])} 个（<{MIN_OBS}）"})
        else:
            codes.append(c)
    for key in idx_keys:
        if obs[key] < MIN_OBS:
            missing.append({"code": key, "name": names[key], "type": INDEX_TYPE, "reason": f"有效观测仅 {int(obs[key])} 个（<{MIN_OBS}）"})
        else:
            codes.append(key)
    for key, why in idx_fail:
        missing.append({"code": key, "name": BROAD_INDICES[key][0], "type": INDEX_TYPE, "reason": "指数抓取失败：" + why})
    if len(codes) < 2:
        raise ValueError("可用基金不足 2 只，无法计算相关性")

    pivot = pivot[codes]
    type_names = list(dict.fromkeys(types[c] for c in codes))   # 保持 funds_universe_example.csv 中出现的顺序
    dates = [d.strftime("%Y-%m-%d") for d in pivot.index]
    ret = [[round(float(v), 6) if np.isfinite(v) else None for v in pivot[c].to_numpy(float)] for c in codes]

    data = {
        "funds": [{"code": c, "name": names.get(c, ""), "type": types[c],
                   "sub": subtype.get(c, ""), "region": region.get(c, "")} for c in codes],
        "types": type_names, "dates": dates, "ret": ret,        # ret[i][t]：第 i 只基金在 dates[t] 的日收益率（小数）
        "missing": missing, "n_target": int(len(target)) + (len(BROAD_INDICES) if include_indices else 0),
        "index_type": INDEX_TYPE if idx_keys else "", "all_group": ALL_GROUP,
        "min_obs": MIN_OBS, "min_overlap": MIN_OVERLAP, "max_k": MAX_K,
    }
    html = render_html(json.dumps(json_ready(data), ensure_ascii=False, separators=(",", ":")))
    Path(output_file).write_text(html, encoding="utf-8")
    print(f"成功！有效 {len(codes)} 只（其中大盘指数 {len(idx_keys)} 个；未纳入 {len(missing)} 个），HTML 已保存至：{output_file}")


def _plotly_script_tag():
    for filename in ("plotly.min.js", "plotly-4.1.1.min.js", "plotly-2.35.2.min.js"):
        if (Path.cwd() / filename).exists():
            return f'<script src="{filename}"></script>'
    if (Path(__file__).resolve().parent.parent / "docs" / "plotly.min.js").exists():
        return '<script src="plotly.min.js"></script>'   # 产物会被拷到 docs/，与 plotly.min.js 同目录
    return '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'


def render_html(data_json):
    return HTML.replace("__DATA__", data_json).replace("__PLOTLY_TAG__", _plotly_script_tag())


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>基金分组聚类与相关性分析</title>__PLOTLY_TAG__<style>
:root{--ink:#172033;--muted:#667085;--line:#e5e7eb;--bg:#f5f7fb;--blue:#2563eb;--green:#078b67;--red:#dc4c64}*{box-sizing:border-box}body{margin:0;font-family:"Microsoft YaHei","PingFang SC",Arial;color:var(--ink);background:var(--bg)}.wrap{max-width:1500px;margin:auto;padding:28px 22px 60px}h1{margin:0 0 8px;font-size:28px}h2{margin:0 0 6px;font-size:19px}h3{margin:22px 0 10px;font-size:15px}.subtitle,.hint{color:var(--muted)}.subtitle{margin-bottom:12px;font-size:14px}.hint{font-size:12px;line-height:1.7}
.cards{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:12px;margin:14px 0}.card,.panel{background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 2px 8px #1018280a}.card{padding:13px 15px}.card .label{color:var(--muted);font-size:12px}.card .value{font-size:18px;font-weight:700;margin-top:4px}.card .sub{font-size:11px;color:var(--muted);margin-top:2px}.panel{padding:20px;margin-top:16px}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:10px 0 12px}label{color:var(--muted);font-size:13px}select,input,button{font:inherit;border:1px solid #d0d5dd;border-radius:7px;padding:7px 10px;background:#fff;color:var(--ink)}input[type=range]{padding:0;vertical-align:middle;width:150px}input[type=checkbox]{vertical-align:middle}button{background:var(--blue);color:#fff;border-color:var(--blue);cursor:pointer}button.secondary{background:#fff;color:var(--ink)}
.chart{min-height:420px}.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}.netwrap{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:14px}#net{width:100%;aspect-ratio:1000/680;background:#fbfcfe;border:1px solid var(--line);border-radius:10px;touch-action:none;display:block}.side{border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:13px;line-height:1.7;max-height:690px;overflow:auto}.side b{font-size:14px}.side ol{margin:4px 0 8px;padding-left:20px}.side li{margin:2px 0}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:12px;color:var(--muted)}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.table-wrap{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;vertical-align:top}td.wrapc{white-space:normal;min-width:320px}th{position:sticky;top:0;background:#f8fafc;z-index:1}tr:hover td{background:#f7faff}.pd{color:#b91c1c;font-weight:700}.pl{color:#e07a7a;font-weight:600}.nd{color:#15803d;font-weight:700}.nl{color:#4fae6d;font-weight:600}.lg{cursor:pointer}.lg:hover{color:var(--ink);text-decoration:underline}#macro{width:100%;aspect-ratio:860/520;border:1px solid var(--line);border-radius:10px;display:block}#macro path,#macro g{transition:opacity .2s}.gbar{display:inline-block;width:64px;height:8px;border-radius:4px;vertical-align:middle;margin-right:4px}.empty{text-align:center;color:var(--muted);padding:28px 8px}.badge{padding:3px 7px;border-radius:999px;background:#eef4ff;color:#2458b8;font-size:12px}.chip{display:inline-block;margin:2px 4px 2px 0;padding:1px 7px;border:1px solid #d0d5dd;border-radius:999px;font-size:12px;background:#fff}
details.note{background:#fff8e6;border:1px solid #f5d98a;border-radius:8px;padding:8px 12px;font-size:13px;margin:8px 0}.tip{position:fixed;pointer-events:none;background:#101828;color:#fff;padding:6px 9px;border-radius:6px;font-size:12px;line-height:1.5;display:none;z-index:99;max-width:300px}
.btn-grp{display:inline-flex;vertical-align:middle;margin-left:6px}.btn-grp button{padding:5px 10px;font-size:12px;background:#fff;color:var(--ink);border:1px solid #d0d5dd;border-radius:0;margin:0 0 0 -1px}.btn-grp button:first-child{border-radius:6px 0 0 6px;margin-left:0}.btn-grp button:last-child{border-radius:0 6px 6px 0}.btn-grp button.active{background:var(--blue);color:#fff;border-color:var(--blue)}.winbar{display:contents}.wInfo{font-size:12px;color:var(--muted)}
@media(max-width:1000px){.cards{grid-template-columns:repeat(2,1fr)}.grid2,.netwrap{grid-template-columns:1fr}.wrap{padding:18px 12px}}
</style></head><body><main class="wrap"><h1>基金分组聚类与相关性分析</h1><div class="subtitle" id="subtitle"></div><details class="note" id="missingBox" hidden><summary id="missingSum"></summary><div id="missingList"></div></details><section class="cards" id="cards"></section>

<section class="panel"><h2>一、总览（总）：各类型之间的关系</h2><div class="controls" style="margin:6px 0 4px"><span class="winbar"></span></div><div class="hint">先看全局：每个节点是一个类型（大小 = 基金数，形状 = 类型，外圈 = 类型内部平均相关），连线上的数字 = 类型间平均相关系数，颜色越深、线越粗相关越强（红 = 正相关，绿 = 负相关）。鼠标悬停高亮某类型的连线；点击类型节点或表中的类型名，进入下方“分”视图查看该类型内部的聚类。</div><div class="grid2" style="margin-top:12px"><div><svg id="macro" viewBox="0 0 860 520"></svg><div class="legend" id="macroLegend"></div></div><div id="typeSummary"></div></div></section>
<section class="panel" id="netSec"><h2>二、分组聚类网络图（分）</h2><div class="controls" style="margin:6px 0 4px"><span class="winbar"></span></div><div class="hint">先按 funds_universe_example.csv 的“类型”列分组：用下拉框切换分组，观察组内哪些基金聚在一起、哪些基金相关度高。节点距离由相关系数决定（越近越相关），连线表示相关系数达到阈值；虚线色块为聚类范围。可拖动节点、点击基金查看其相关基金。“全部基金”是总视图（点击图例中的类型可进入该类型的分视图），其余为各类型的分视图；颜色越深相关越强。</div>
<div class="controls"><label>分组（类型）<select id="grp"></select></label><label>聚类数 <select id="kSel"></select></label><label>着色 <select id="colorBy"><option value="cluster">按聚类结果</option><option value="type">按基金类型</option><option value="sub">按细分（指数市场等）</option></select></label><label>形状 <select id="shapeBy"><option value="auto">自动</option><option value="type">按类型</option><option value="cluster">按聚类</option><option value="sub">按细分</option></select></label><label>连线 <select id="edgeMode"><option value="pos">正相关 ≥ 阈值</option><option value="abs">|相关| ≥ 阈值</option></select></label><label>阈值 <input id="thr" type="range" min="0" max="0.99" step="0.01" value="0.7"> <b id="thrV"></b></label><label><input id="showLbl" type="checkbox"> 显示名称</label><label><input id="showHull" type="checkbox" checked> 聚类范围</label><button id="relayout" class="secondary">重新布局</button></div>
<div class="netwrap"><div><svg id="net" viewBox="0 0 1000 680"><g id="vp"><g id="hullG"></g><g id="edgeG"></g><g id="nodeG"></g><g id="lblG"></g></g></svg><div class="legend" id="legend"></div><div class="hint" id="netHint"></div></div><div class="side" id="side"></div></div></section>

<section class="panel"><h2>三、聚类与高相关结果表 <span class="badge" id="grpBadge"></span></h2><div class="controls" style="margin:6px 0 4px"><span class="winbar"></span></div><div class="hint">以下图表随上方“分组”“聚类数”联动，用来检验聚类效果与组内相关度。</div><div class="cards" id="gcards" style="grid-template-columns:repeat(6,minmax(110px,1fr))"></div>
<div class="grid2"><div><h3>组内相关系数热力图（按聚类顺序排列，黑框 = 聚类块）</h3><div id="heatG" class="chart"></div></div><div><h3>聚类质量：各聚类数的轮廓系数</h3><div id="silG" class="chart" style="min-height:300px"></div><div class="hint">轮廓系数越接近 1，说明同一聚类内更紧密、聚类之间更分离；可在上方切换聚类数对比。</div></div></div>
<h3>聚类成员表</h3><div id="clusterTable" class="table-wrap"></div>
<h3>组内高相关基金对排行</h3><div class="controls"><label>排序 <select id="pairMode"><option value="high">相关性最高</option><option value="low">相关性最低</option></select></label><label>范围 <select id="pairScope"><option value="all">全部基金对</option><option value="same">仅同一聚类</option><option value="diff">仅跨聚类</option></select></label><label>显示数量 <input id="pairN" type="number" value="20" min="1" max="200" style="width:75px"></label></div><div id="pairTable" class="table-wrap"></div>
<h3>基金中心度（谁是核心、谁是离群）</h3><div class="controls"><label>排序 <select id="centMode"><option value="high">组内平均相关由高到低（核心基金）</option><option value="low">组内平均相关由低到高（离群基金）</option></select></label></div><div id="centTable" class="table-wrap"></div></section>

<section class="panel"><h2>四、不同类型间的相关性</h2><div class="controls" style="margin:6px 0 4px"><span class="winbar"></span></div><div class="hint">先看类型整体关系（热力图、分布），再选择“源类型 → 目标类型”，查看源类型的每只基金与目标类型中哪些基金最相关（例如 持有 → 板块），最后是各类排行榜。</div>
<div class="grid2"><div><h3>类型 × 类型 平均相关系数（对角线 = 类型内部）</h3><div id="typeHeat" class="chart"></div></div><div><h3>类型对基金相关系数分布（箱线图）</h3><div id="typeBox" class="chart"></div></div></div>
<div class="controls" style="margin-top:20px"><label>源类型 <select id="srcT"></select></label><span>→</span><label>目标类型 <select id="dstT"></select></label></div>
<div class="grid2"><div><h3 id="crossHeatTitle"></h3><div id="crossHeat" class="chart"></div></div><div><h3 id="bestBarTitle"></h3><div id="bestBar" class="chart"></div></div></div>
<h3>类型对相关性排行榜</h3><div class="controls"><label>排名口径 <select id="typeRankMode"><option value="mean">平均相关系数</option><option value="abs_mean">平均绝对相关系数</option><option value="median">中位数相关系数</option><option value="hi">高相关（≥0.7）占比</option></select></label></div><div id="typeRankTable" class="table-wrap"></div>
<div id="idxSec"><h3>大盘指数 × 基金类型：各指数与各类基金的平均相关</h3><div id="idxHeat" class="chart" style="min-height:360px"></div><h3>各大盘指数最相关的基金</h3><div id="idxTable" class="table-wrap"></div></div>
<h3 id="mapTitle"></h3><div class="controls"><label>每只基金显示最相关的目标基金数 <select id="mapN"><option>1</option><option selected>3</option><option>5</option><option>10</option></select></label></div><div class="hint" id="mapHint" style="margin-bottom:6px"></div><div id="mapTable" class="table-wrap"></div>
<h3 id="crossPairTitle"></h3><div class="controls"><label>排序 <select id="crossMode"><option value="high">相关性最高</option><option value="low">相关性最低</option></select></label><label>显示数量 <input id="crossN" type="number" value="20" min="1" max="200" style="width:75px"></label></div><div id="crossPairTable" class="table-wrap"></div></section>

<section class="panel"><h2>五、指定基金相关性查询</h2><div class="controls" style="margin:6px 0 4px"><span class="winbar"></span></div><div class="controls"><label>基金 <input id="fundSearch" list="fundOptions" placeholder="输入代码或名称" style="min-width:260px"></label><datalist id="fundOptions"></datalist><label>范围 <select id="qScope"><option value="all">全部基金</option><option value="same">仅同类型</option><option value="cross">仅跨类型</option></select></label><label>排序 <select id="corrMode"><option value="high">相关性最高</option><option value="low">相关性最低</option></select></label><label>显示数量 <input id="corrTopN" type="number" value="10" min="1" max="100" style="width:75px"></label><button id="queryBtn">查询</button><button id="clearBtn" class="secondary">清空</button></div><div id="querySummary" class="hint"></div><div class="grid2"><div id="queryTable" class="table-wrap"><div class="empty">请输入基金代码或名称后查询。</div></div><div id="queryChart" class="chart" style="min-height:300px"></div></div><div class="hint" style="margin-top:8px">表格中的 n 为共同交易日数；β按“查询基金 A → 表格中的基金 B”回归；95% CI 为 Pearson 相关系数的 Fisher 近似置信区间；p 值用于检验 Pearson 相关是否为 0。收益率存在时间依赖时，请结合滚动相关和分档分析判断。批量查看 p 值时需注意多重检验问题。</div></section>

<section class="panel"><h2>六、收益率状态关联（按需计算）</h2><div class="controls" style="margin:6px 0 4px"><span class="winbar"></span></div><div class="hint">把两只基金的日收益率按区间分档，使用列联表检验它们的涨跌状态是否独立。计算只在点击按钮后执行，避免影响页面首次打开速度。建议先使用3档或5档；7档需要更多共同交易日。</div><div class="controls"><label>基金 A <input id="stateFundA" list="fundOptions" placeholder="代码或名称" style="min-width:220px"></label><label>基金 B <input id="stateFundB" list="fundOptions" placeholder="代码或名称" style="min-width:220px"></label><label>分档 <select id="stateBins"><option value="3">3档：跌 / 震荡 / 涨</option><option value="5" selected>5档：±1%、±2%</option><option value="7">7档：±1%、±2%、±3%</option></select></label><button id="stateBtn">计算分档关联</button></div><div id="stateSummary" class="hint"></div><div class="grid2"><div><h3>联合频数 / 行条件概率</h3><div id="stateTable" class="table-wrap"><div class="empty">请选择两只基金后计算。</div></div></div><div><h3>分档关联热力图</h3><div id="stateHeat" class="chart" style="min-height:360px"></div></div></div><div class="controls" style="margin-top:18px"><label>滚动窗口 <select id="rollingWindow"><option value="60">60日</option><option value="120" selected>120日</option><option value="250">250日</option></select></label><button id="rollingBtn" class="secondary">刷新滚动相关</button></div><div id="rollingSummary" class="hint"></div><div id="rollingChart" class="chart" style="min-height:320px"></div></section></main><div class="tip" id="tip"></div>
<script>
const DATA=__DATA__;const F=DATA.funds,$=id=>document.getElementById(id);let M=[];
const ALLTYPES=DATA.types.slice(),D=DATA.dates,ND=D.length,MIN_OBS=DATA.min_obs,MIN_OV=DATA.min_overlap,MAX_K=DATA.max_k;
const RET=DATA.ret.map(a=>Float64Array.from(a,v=>v==null?NaN:v));delete DATA.ret;
let winStart=D[0],winEnd=D[ND-1],winPreset="all",ACT=[],OBS=[];
const fmt=(v,d=3)=>v==null||!Number.isFinite(+v)?"—":(+v).toFixed(d);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const cls=v=>{v=Number(v);return v>=0?(v>=DEEP?"pd":"pl"):(v<=-DEEP?"nd":"nl")};
const PAL=["#2563eb","#f59e0b","#8b5cf6","#0891b2","#a16207","#c026d3","#475569","#4f46e5","#0369a1","#9333ea","#d97706","#334155"];
const DEEP=.7,POS_D="#b91c1c",POS_L="#f19a9a",NEG_D="#15803d",NEG_L="#86d19c";
const corrCol=(r,d=DEEP)=>r>=0?(r>=d?POS_D:POS_L):(r<=-d?NEG_D:NEG_L);
const NSH=12,TYPE_COL={},TYPE_SHAPE={},SUBS=[],SUB_COL={},SUB_SHAPE={};ALLTYPES.forEach((t,i)=>{TYPE_COL[t]=PAL[i%PAL.length];TYPE_SHAPE[t]=i%NSH});
F.forEach(f=>{const k=f.sub||f.type;if(!(k in SUB_SHAPE)){SUB_SHAPE[k]=SUBS.length%NSH;SUB_COL[k]=PAL[SUBS.length%PAL.length];SUBS.push(k)}});
const short=(s,n=8)=>s.length>n?s.slice(0,n)+"…":s;const mean=a=>a.length?a.reduce((s,x)=>s+x,0)/a.length:null;
const median=a=>{if(!a.length)return null;const b=[...a].sort((x,y)=>x-y),m=b.length>>1;return b.length%2?b[m]:(b[m-1]+b[m])/2};
const fl=i=>`${F[i].code} ${F[i].name}`;const hasP=typeof Plotly!=="undefined";
const statsCache=new Map(),overlapCache=new Map();
const cacheKey=(i,j)=>{const a=Math.min(i,j),b=Math.max(i,j);return `${a}|${b}|${w0()}|${w1()}`};
function rankVals(a){const out=new Array(a.length),idx=a.map((v,i)=>i).sort((i,j)=>a[i]-a[j]);for(let p=0;p<idx.length;){let q=p+1;while(q<idx.length&&a[idx[q]]===a[idx[p]])q++;const r=(p+q-1)/2+1;for(let k=p;k<q;k++)out[idx[k]]=r;p=q}return out}
function logGamma(z){const p=[676.5203681218851,-1259.1392167224028,771.32342877765313,-176.61502916214059,12.507343278686905,-.13857109526572012,9.984369578019572e-6,1.5056327351493116e-7];if(z<.5)return Math.log(Math.PI)-Math.log(Math.sin(Math.PI*z))-logGamma(1-z);z-=1;let x=.99999999999980993;for(let i=0;i<p.length;i++)x+=p[i]/(z+i+1);const t=z+p.length-.5;return .5*Math.log(2*Math.PI)+(z+.5)*Math.log(t)-t+Math.log(x)}
function betaCF(a,b,x){let qab=a+b,qap=a+1,qam=a-1,c=1,d=1-qab*x/qap;if(Math.abs(d)<3e-30)d=3e-30;d=1/d;let h=d;for(let m=1;m<=120;m++){const m2=2*m;let aa=m*(b-m)*x/((qam+m2)*(a+m2));d=1+aa*d;if(Math.abs(d)<3e-30)d=3e-30;c=1+aa/c;if(Math.abs(c)<3e-30)c=3e-30;d=1/d;h*=d*c;aa=-(a+m)*(qab+m)*x/((a+m2)*(qap+m2));d=1+aa*d;if(Math.abs(d)<3e-30)d=3e-30;c=1+aa/c;if(Math.abs(c)<3e-30)c=3e-30;d=1/d;const del=d*c;h*=del;if(Math.abs(del-1)<3e-8)break}return h}
function regBeta(x,a,b){if(x<=0)return 0;if(x>=1)return 1;const bt=Math.exp(logGamma(a+b)-logGamma(a)-logGamma(b)+a*Math.log(x)+b*Math.log(1-x));return x<(a+1)/(a+b+2)?bt*betaCF(a,b,x)/a:1-bt*betaCF(b,a,1-x)/b}
function regGammaQ(a,x){if(x<0||a<=0)return NaN;if(x===0)return 1;if(x<a+1){let sum=1/a,term=sum;for(let n=1;n<200;n++){term*=x/(a+n);sum+=term;if(Math.abs(term)<Math.abs(sum)*3e-8)break}return 1-Math.exp(-x+a*Math.log(x)-logGamma(a))*sum}let b=x+1-a,c=1/3e-30,d=1/b,h=d;for(let i=1;i<=200;i++){const an=-i*(i-a),bb=b+2*i;d=an*d+bb;if(Math.abs(d)<3e-30)d=3e-30;c=bb+an/c;if(Math.abs(c)<3e-30)c=3e-30;d=1/d;const del=d*c;h*=del;if(Math.abs(del-1)<3e-8)break}return Math.exp(-x+a*Math.log(x)-logGamma(a))*h}
const chi2P=(x,df)=>df>0?Math.max(0,Math.min(1,regGammaQ(df/2,x/2))):null;
function pairN(i,j){const key=cacheKey(i,j);if(overlapCache.has(key))return overlapCache.get(key);let n=0;const a=RET[i],b=RET[j],w=w0(),we=w1();for(let t=w;t<we;t++)if(a[t]===a[t]&&b[t]===b[t])n++;overlapCache.set(key,n);return n}
function pairStats(i,j){const key=cacheKey(i,j);if(statsCache.has(key))return statsCache.get(key);const a=RET[i],b=RET[j],w=w0(),we=w1(),x=[],y=[];for(let t=w;t<we;t++)if(a[t]===a[t]&&b[t]===b[t]){x.push(a[t]);y.push(b[t])}const n=x.length;if(n<2){const z={n,pearson:null,spearman:null,beta:null,r2:null,same:null,ciLow:null,ciHigh:null,p:null};statsCache.set(key,z);return z}const mx=mean(x),my=mean(y);let sxx=0,syy=0,sxy=0,same=0;for(let k=0;k<n;k++){const dx=x[k]-mx,dy=y[k]-my;sxx+=dx*dx;syy+=dy*dy;sxy+=dx*dy;if((x[k]>0&&y[k]>0)||(x[k]<0&&y[k]<0)||(x[k]===0&&y[k]===0))same++}const r=sxx>0&&syy>0?sxy/Math.sqrt(sxx*syy):null;const rx=rankVals(x),ry=rankVals(y),mrx=mean(rx),mry=mean(ry);let qx=0,qy=0,qxy=0;for(let k=0;k<n;k++){const dx=rx[k]-mrx,dy=ry[k]-mry;qx+=dx*dx;qy+=dy*dy;qxy+=dx*dy}const sr=qx>0&&qy>0?qxy/Math.sqrt(qx*qy):null;const beta=sxx>0?sxy/sxx:null,ci=r==null||n<4?{low:null,high:null}:{low:Math.tanh(Math.atanh(Math.max(-.999999,Math.min(.999999,r)))-1.96/Math.sqrt(n-3)),high:Math.tanh(Math.atanh(Math.max(-.999999,Math.min(.999999,r)))+1.96/Math.sqrt(n-3))};let p=null;if(r!=null&&n>2){const t=Math.abs(r)*Math.sqrt((n-2)/Math.max(1e-15,1-r*r));p=regBeta((n-2)/((n-2)+t*t),(n-2)/2,.5)}const z={n,pearson:r,spearman:sr,beta,r2:r==null?null:r*r,same:n?same/n:null,ciLow:ci.low,ciHigh:ci.high,p};statsCache.set(key,z);return z}
const pairHTML=p=>p?`${esc(fl(p.i))} ↔ ${esc(fl(p.j))} <span class="${cls(p.r)}">(${fmt(p.r)})</span>`:"—";
function plot(id,data,layout){if(!hasP){$(id).innerHTML='<div class="empty">图表库 Plotly 未加载（需联网，或把 plotly.min.js 放在运行目录）。网络图和表格不受影响。</div>';return}
 Plotly.react(id,data,Object.assign({margin:{l:60,r:20,t:15,b:60},paper_bgcolor:"#fff",plot_bgcolor:"#fff",font:{family:"Microsoft YaHei,PingFang SC,Arial",size:12}},layout),{responsive:true,displaylogo:false})}
function heatScale(vals){const mn=Math.min(...vals.filter(v=>v!=null));return mn>=0?{zmin:0,zmax:1,colorscale:[[0,"#ffffff"],[.5,"#f19a9a"],[1,"#b91c1c"]]}:{zmin:-1,zmax:1,colorscale:[[0,"#15803d"],[.25,"#86d19c"],[.5,"#ffffff"],[.75,"#f19a9a"],[1,"#b91c1c"]]}}
const G=()=>DATA.groups[$("grp").value],curK=()=>Number($("kSel").value);
function curLabels(){const g=G(),r=g.results.find(x=>x.k==curK())||g.results[0],m={};g.idx.forEach((gi,li)=>m[gi]=r.labels[li]);return m}
function posMap(g){const p={};g.order.forEach((li,k)=>p[g.idx[li]]=k);return p}
function groupPairs(g){const A=g.idx,o=[];for(let x=0;x<A.length;x++)for(let y=x+1;y<A.length;y++){const r=M[A[x]][A[y]];if(r!=null)o.push({i:A[x],j:A[y],r})}return o}
function pairsBetween(a,b){const A=DATA.groups[a].idx,B=DATA.groups[b].idx,o=[];if(a===b)return groupPairs(DATA.groups[a]);for(const i of A)for(const j of B){const r=M[i][j];if(r!=null)o.push({i,j,r})}return o}
const ordIdx=t=>{const g=DATA.groups[t];return g.order.map(li=>g.idx[li])};

/* ---------- 时间窗口 + 相关/聚类引擎（浏览器内按所选区间重算） ---------- */
const w0=()=>{let i=0;while(i<ND&&D[i]<winStart)i++;return i},w1=()=>{let i=ND;while(i>0&&D[i-1]>winEnd)i--;return i};
function pcorr(a,b,w,we){let n=0,sx=0,sy=0,sxx=0,syy=0,sxy=0;for(let t=w;t<we;t++){const x=a[t],y=b[t];if(x===x&&y===y){n++;sx+=x;sy+=y;sxx+=x*x;syy+=y*y;sxy+=x*y}}
if(n<MIN_OV)return null;const vx=sxx-sx*sx/n,vy=syy-sy*sy/n;if(!(vx>1e-18)||!(vy>1e-18))return null;return Math.max(-1,Math.min(1,(sxy-sx*sy/n)/Math.sqrt(vx*vy)))}
function quantile(a,q){const p=(a.length-1)*q,lo=Math.floor(p),hi=Math.ceil(p);return a[lo]+(a[hi]-a[lo])*(p-lo)}
function defaultThr(sub){const n=sub.length,v=[];for(let i=0;i<n;i++)for(let j=i+1;j<n;j++){const x=sub[i][j];if(x!=null&&Number.isFinite(x))v.push(x)}
if(!v.length)return .5;v.sort((a,b)=>a-b);const tg=Math.min(Math.max(2.5*n,10),.12*v.length),q=1-Math.max(tg,1)/v.length;return Math.min(Math.max(Math.floor(quantile(v,Math.max(q,0))*20)/20,0),.95)}
function clusterGroup(sub){const n=sub.length,d=sub.map((row,i)=>row.map((r,j)=>i===j?0:Math.min(2,Math.max(0,1-(r==null?0:r)))));
for(let i=0;i<n;i++)for(let j=i+1;j<n;j++){const v=(d[i][j]+d[j][i])/2;d[i][j]=d[j][i]=v}
if(n<3)return{order:[...Array(n).keys()],best_k:1,results:[{k:1,silhouette:null,labels:new Array(n).fill(0)}]};
const Q=d.map(r=>Float64Array.from(r)),act=new Array(n).fill(true),sz=new Array(n).fill(1),node=[...Array(n).keys()],mg=[];
for(let s=0;s<n-1;s++){let bi=-1,bj=-1,bv=Infinity;for(let i=0;i<n;i++){if(!act[i])continue;const row=Q[i];for(let j=i+1;j<n;j++)if(act[j]&&row[j]<bv){bv=row[j];bi=i;bj=j}}
for(let k=0;k<n;k++){if(!act[k]||k===bi||k===bj)continue;const v=(sz[bi]*Q[bi][k]+sz[bj]*Q[bj][k])/(sz[bi]+sz[bj]);Q[bi][k]=Q[k][bi]=v}
sz[bi]+=sz[bj];act[bj]=false;mg.push([bi,bj]);node[bi]=[node[bi],node[bj]]}
const order=[];(function walk(t){if(typeof t==="number")order.push(t);else{walk(t[0]);walk(t[1])}})(node[mg[mg.length-1][0]]);
const labelsFor=k=>{const p=[...Array(n).keys()],f=x=>{while(p[x]!==x){p[x]=p[p[x]];x=p[x]}return x};for(let s=0;s<n-k;s++)p[f(mg[s][1])]=f(mg[s][0]);const m=new Map(),lab=[];for(let i=0;i<n;i++){const r=f(i);if(!m.has(r))m.set(r,m.size);lab.push(m.get(r))}return lab};
const sil=lab=>{const k=Math.max(...lab)+1,cnt=new Array(k).fill(0);lab.forEach(l=>cnt[l]++);let tot=0;for(let i=0;i<n;i++){if(cnt[lab[i]]<=1)continue;const sm=new Array(k).fill(0);for(let j=0;j<n;j++)if(j!==i)sm[lab[j]]+=d[i][j];
const a=sm[lab[i]]/(cnt[lab[i]]-1);let b=Infinity;for(let c=0;c<k;c++)if(c!==lab[i]&&cnt[c]>0)b=Math.min(b,sm[c]/cnt[c]);const den=Math.max(a,b);tot+=den>0?(b-a)/den:0}return tot/n};
const results=[];for(let k=2;k<=Math.min(MAX_K,n-1);k++){const lab=labelsFor(k),u=new Set(lab).size;if(u<2||u>=n)continue;results.push({k,silhouette:sil(lab),labels:lab})}
if(!results.length)results.push({k:1,silhouette:null,labels:new Array(n).fill(0)});
let best=results[0];results.forEach(r=>{if(r.silhouette!=null&&(best.silhouette==null||r.silhouette>best.silhouette))best=r});
return{order,best_k:best.k,results}}
function computeAll(w,we){const n=F.length,obs=new Array(n).fill(0);
for(let i=0;i<n;i++){const a=RET[i];let c=0;for(let t=w;t<we;t++)if(a[t]===a[t])c++;obs[i]=c}
const act=[];for(let i=0;i<n;i++)if(obs[i]>=MIN_OBS)act.push(i);if(act.length<2)return null;
const Mx=Array.from({length:n},()=>new Array(n).fill(null));
for(let x=0;x<act.length;x++){const i=act[x];Mx[i][i]=1;for(let y=x+1;y<act.length;y++){const j=act[y],r=pcorr(RET[i],RET[j],w,we);Mx[i][j]=Mx[j][i]=r==null?null:Math.round(r*1e4)/1e4}}
const types=ALLTYPES.filter(t=>act.some(i=>F[i].type===t)),groups={};
for(const g of [DATA.all_group,...types]){const idx=g===DATA.all_group?act.slice():act.filter(i=>F[i].type===g),sub=idx.map(i=>idx.map(j=>Mx[i][j])),info=clusterGroup(sub);info.idx=idx;info.default_thr=defaultThr(sub);groups[g]=info}
return{M:Mx,obs,act,types,groups,w,we}}
function applyResult(r){M=r.M;OBS=r.obs;ACT=r.act;DATA.types=r.types;DATA.group_names=[DATA.all_group,...r.types];DATA.groups=r.groups;DATA.date_range={start:D[r.w],end:D[r.we-1],days:r.we-r.w};statsCache.clear();overlapCache.clear()}
function syncBars(msg){document.querySelectorAll(".wS").forEach(x=>x.value=winStart);document.querySelectorAll(".wE").forEach(x=>x.value=winEnd);
document.querySelectorAll(".winbar [data-months]").forEach(b=>b.classList.toggle("active",b.dataset.months===String(winPreset)));
const a=w0(),b=w1();document.querySelectorAll(".wInfo").forEach(x=>{x.textContent=msg||(b>a?`${D[a]} ~ ${D[b-1]} · ${b-a} 个交易日`:"");x.style.color=msg?"#b42318":""})}
function setWindow(s,e,preset){if(s>e){const t=s;s=e;e=t}if(s<D[0])s=D[0];if(e>D[ND-1])e=D[ND-1];const os=winStart,oe=winEnd;winStart=s;winEnd=e;const w=w0(),we=w1();
if(we-w<MIN_OV){winStart=os;winEnd=oe;syncBars(`⚠️ 所选区间只有 ${Math.max(0,we-w)} 个交易日，少于 ${MIN_OV} 个，无法计算相关性，已保持原区间`);return}
const res=computeAll(w,we);if(!res){winStart=os;winEnd=oe;syncBars("⚠️ 该区间内有效基金不足 2 只，已保持原区间");return}
winPreset=preset;applyResult(res);syncBars("");renderAll(false)}
function setPreset(m){const end=D[ND-1];let s;if(m==="all")s=D[0];else{const d=new Date(end+"T00:00:00Z");d.setUTCMonth(d.getUTCMonth()-(+m));s=d.toISOString().slice(0,10)}setWindow(s,end,m)}
function initBars(){const P=[["3","3月"],["6","6月"],["12","1年"],["36","3年"],["60","5年"],["all","全部"]],
h=`<label>时间范围<span class="btn-grp">${P.map(([m,t])=>`<button type="button" data-months="${m}">${t}</button>`).join("")}</span></label><label>开始 <input type="date" class="wS" min="${D[0]}" max="${D[ND-1]}" style="width:140px"></label><label>结束 <input type="date" class="wE" min="${D[0]}" max="${D[ND-1]}" style="width:140px"></label><span class="wInfo"></span>`;
document.querySelectorAll(".winbar").forEach(b=>b.innerHTML=h);
document.addEventListener("click",e=>{const b=e.target.closest(".winbar [data-months]");if(b)setPreset(b.dataset.months)});
document.addEventListener("change",e=>{if(e.target.matches&&e.target.matches(".wS,.wE")){const bar=e.target.closest(".winbar"),s=bar.querySelector(".wS").value,en=bar.querySelector(".wE").value;if(s&&en)setWindow(s,en,"")}})}
const AF=()=>ACT.map(i=>F[i]);
function renderMissing(){const st=DATA.missing||[],ac=new Set(ACT),dyn=F.map((f,i)=>i).filter(i=>!ac.has(i)),n=st.length+dyn.length;$("missingBox").hidden=!n;if(!n)return;
$("missingSum").textContent=`有 ${n} 只目标基金未纳入分析${dyn.length?`（其中 ${dyn.length} 只因所选时间范围内有效观测不足 ${MIN_OBS} 个）`:""}（点击展开）`;
$("missingList").innerHTML=st.map(m=>`<div>${esc(m.code)} ${esc(m.name)}（${esc(m.type)}）：${esc(m.reason)}</div>`).join("")+dyn.map(i=>`<div>${esc(F[i].code)} ${esc(F[i].name)}（${esc(F[i].type)}）：所选时间范围内有效观测仅 ${OBS[i]} 个（<${MIN_OBS}）</div>`).join("")}
function fillSelects(first){const g0=$("grp").value,s0=$("srcT").value,d0=$("dstT").value,names=DATA.group_names,T=DATA.types;
$("grp").innerHTML=names.map(g=>`<option value="${esc(g)}">${esc(g)}（${g===DATA.all_group?"总":"分"} · ${DATA.groups[g].idx.length}）</option>`).join("");$("grp").value=names.includes(g0)?g0:DATA.all_group;
const ts=T.map(t=>`<option value="${esc(t)}">${esc(t)}</option>`).join("");$("srcT").innerHTML=ts;$("dstT").innerHTML=ts;
const hold=T.includes("持有")?"持有":T[0],sect=T.includes("板块")?"板块":(T.find(t=>t!==hold)||hold);
$("srcT").value=!first&&T.includes(s0)?s0:hold;$("dstT").value=!first&&T.includes(d0)?d0:sect;
$("fundOptions").innerHTML=AF().map(f=>`<option value="${esc(f.code)}">${esc(f.name)}（${esc(f.type)}）</option>`).join("")}
function renderAll(first){summary();fillSelects(first);buildTP();renderMacro();renderTypeSummary();loadGroup();renderGroupSection();typeCharts();renderCross();renderTypeRank();renderIndexSection();if(!first&&$("fundSearch").value)query();if(!first&&$("stateFundA").value&&$("stateFundB").value)renderState()}

/* ---------- 顶部概览 ---------- */
function summary(){const cnt={};AF().forEach(f=>cnt[f.type]=(cnt[f.type]||0)+1);const all=groupPairs(DATA.groups[DATA.all_group]).map(p=>p.r);
 const within=[],cross=[];groupPairs(DATA.groups[DATA.all_group]).forEach(p=>(F[p.i].type===F[p.j].type?within:cross).push(p.r));
 const v=[["有效基金",`${ACT.length} / ${DATA.n_target}`,"匹配成功 / 目标总数（含大盘指数）"],["基金类型",DATA.types.length,DATA.types.map(t=>`${t} ${cnt[t]}`).join(" · ")],["数据区间",DATA.date_range.start.slice(2)+" ~ "+DATA.date_range.end.slice(2),`${DATA.date_range.days} 个日期`],["全部基金平均相关",fmt(mean(all)),"所有两两基金对"],["类型内部平均相关",fmt(mean(within)),"同类型基金对"],["跨类型平均相关",fmt(mean(cross)),"不同类型基金对"]];
 $("cards").innerHTML=v.map(x=>`<div class="card"><div class="label">${esc(x[0])}</div><div class="value">${esc(x[1])}</div><div class="sub">${esc(x[2])}</div></div>`).join("");
 $("subtitle").textContent=`相关系数 = 基金日增长率的 Pearson 相关系数（两只基金共同交易日不足 20 个则不计）；分组依据为 funds_universe_example.csv 的“类型”列。当前统计区间 ${DATA.date_range.start} ~ ${DATA.date_range.end}（${DATA.date_range.days} 个交易日），可在每一节顶部的“时间范围 / 开始 / 结束”调整，全报告联动。`;
 renderMissing()}

/* ---------- 一、网络图 ---------- */
const NS="http://www.w3.org/2000/svg",net={nodes:[],edges:[],hulls:[],iter:0,max:300,raf:0,sel:null,drag:null,fit:true,W:1000,H:680,tf:{s:1,tx:0,ty:0}};net.S=Math.min(net.W,net.H)*.3;
const mk=(t,a,p)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e};
function poly(n,r,rot){let d="";for(let i=0;i<n;i++){const a=rot+2*Math.PI*i/n;d+=(i?"L":"M")+(r*Math.cos(a)).toFixed(1)+","+(r*Math.sin(a)).toFixed(1)}return d+"Z"}
function starP(n,r,r2,rot){let d="";for(let i=0;i<2*n;i++){const a=rot+Math.PI*i/n,q=i%2?r2:r;d+=(i?"L":"M")+(q*Math.cos(a)).toFixed(1)+","+(q*Math.sin(a)).toFixed(1)}return d+"Z"}
function plusP(r,rot){const t=.36*r,P=[[-t,-r],[t,-r],[t,-t],[r,-t],[r,t],[t,t],[t,r],[-t,r],[-t,t],[-r,t],[-r,-t],[-t,-t]],c=Math.cos(rot),s=Math.sin(rot);return"M"+P.map(p=>(p[0]*c-p[1]*s).toFixed(1)+","+(p[0]*s+p[1]*c).toFixed(1)).join("L")+"Z"}
function shapePath(s,r){s%=NSH;const q=r*1.12;switch(s){case 0:return`M${-r},0a${r},${r} 0 1,0 ${2*r},0a${r},${r} 0 1,0 ${-2*r},0`;case 1:return poly(4,q*1.1,Math.PI/4);case 2:return poly(4,q*1.25,0);case 3:return poly(3,q*1.2,-Math.PI/2);case 4:return poly(3,q*1.2,Math.PI/2);case 5:return poly(5,q*1.1,-Math.PI/2);case 6:return poly(6,q*1.1,0);case 7:return starP(5,q*1.45,q*.62,-Math.PI/2);case 8:return plusP(q*1.25,0);case 9:return plusP(q*1.25,Math.PI/4);case 10:return starP(4,q*1.45,q*.5,0);default:return poly(8,q*1.1,Math.PI/8)}}
const icon=(s,c)=>`<svg width="16" height="16" viewBox="-9 -9 18 18" style="vertical-align:-3px;margin-right:3px"><path d="${shapePath(s,5.5)}" fill="${c}"/></svg>`;
function shapeMode(){const m=$("shapeBy").value;return m==="auto"?($("grp").value===DATA.all_group?"type":"cluster"):m}
function shapeOf(n,lab,sm){const f=F[n.id];return sm==="type"?TYPE_SHAPE[f.type]:sm==="sub"?SUB_SHAPE[f.sub||f.type]:lab[n.id]%NSH}
function hull(P){P=P.slice().sort((a,b)=>a[0]-b[0]||a[1]-b[1]);if(P.length<3)return P;const cr=(o,a,b)=>(a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0]);const lo=[];for(const p of P){while(lo.length>=2&&cr(lo[lo.length-2],lo[lo.length-1],p)<=0)lo.pop();lo.push(p)}const up=[];for(let i=P.length-1;i>=0;i--){const p=P[i];while(up.length>=2&&cr(up[up.length-2],up[up.length-1],p)<=0)up.pop();up.push(p)}up.pop();lo.pop();return lo.concat(up)}
function loadGroup(){const g=G();$("kSel").innerHTML=g.results.map(r=>`<option value="${r.k}" ${r.k==g.best_k?"selected":""}>${r.k} 类${r.silhouette==null?"":`（轮廓 ${fmt(r.silhouette)}）`}${r.k==g.best_k&&g.results.length>1?" ★":""}</option>`).join("");
 $("thr").value=g.default_thr;$("showLbl").checked=g.idx.length<=30;net.sel=null;$("colorBy").value=$("grp").value===DATA.all_group?"type":"cluster";$("shapeBy").value="auto";initPos()}
function mdsInit(ids){const n=ids.length;if(n<3)return null;const D=ids.map(i=>ids.map(j=>{let r=M[i][j];if(r==null)r=0;return 2*Math.max(0,1-r)})),rm=D.map(mean),gm=mean(rm),B=D.map((row,i)=>row.map((d,j)=>-.5*(d-rm[i]-rm[j]+gm))),vs=[];
 for(let c=0;c<2;c++){let v=Array.from({length:n},(_,i)=>Math.sin(i*1.7+c*2.3)+.1*Math.cos(i*.9)),lam=0;for(let it=0;it<80;it++){vs.forEach(([u])=>{const d=v.reduce((s,x,k)=>s+x*u[k],0);v=v.map((x,k)=>x-d*u[k])});const w=B.map(row=>row.reduce((s,x,k)=>s+x*v[k],0)),nm=Math.hypot(...w)||1;lam=nm;v=w.map(x=>x/nm)}vs.push([v,lam])}
 return ids.map((_,k)=>[vs[0][0][k]*Math.sqrt(Math.max(vs[0][1],0)),vs[1][0][k]*Math.sqrt(Math.max(vs[1][1],0))])}
function initPos(){const g=G(),n=g.idx.length,pos=posMap(g),R=Math.min(net.W,net.H)*.33,m=mdsInit(g.idx);
 net.nodes=g.idx.map((gi,k)=>{const j=(Math.random()-.5)*12;if(m)return{id:gi,x:net.W/2+m[k][0]*net.S+j,y:net.H/2+m[k][1]*net.S+j,fixed:false};const a=2*Math.PI*pos[gi]/Math.max(n,1);return{id:gi,x:net.W/2+R*Math.cos(a),y:net.H/2+R*Math.sin(a),fixed:false}});
 net.iter=0;net.max=Math.min(420,160+n*3);net.fit=true;net.tf={s:1,tx:0,ty:0};relax(40);fitView(true);build();kick()}
function relax(m){const ns=net.nodes,n=ns.length,S=net.S;if(n<2){if(n==1){ns[0].x=net.W/2;ns[0].y=net.H/2}return}
 for(let it=0;it<m;it++)for(let a=0;a<n;a++){const p=ns[a];if(p.fixed)continue;let sx=0,sy=0,sw=0;const row=M[p.id];
  for(let b=0;b<n;b++){if(a===b)continue;const q=ns[b];let r=row[q.id];if(r==null)r=0;const t=Math.max(24,Math.sqrt(Math.max(0,2*(1-r)))*S),w=(S*S)/(t*t);
   const dx=p.x-q.x,dy=p.y-q.y,d=Math.hypot(dx,dy)||.01;sx+=w*(q.x+t*dx/d);sy+=w*(q.y+t*dy/d);sw+=w}
  p.x+=(sx/sw-p.x)*.55;p.y+=(sy/sw-p.y)*.55}}
function fitView(now){const ns=net.nodes;if(!ns.length)return;let x0=1e9,x1=-1e9,y0=1e9,y1=-1e9;ns.forEach(n=>{x0=Math.min(x0,n.x);x1=Math.max(x1,n.x);y0=Math.min(y0,n.y);y1=Math.max(y1,n.y)});
 const bw=Math.max(x1-x0,60),bh=Math.max(y1-y0,60),s=Math.min((net.W-110)/bw,(net.H-110)/bh,1.5),tx=net.W/2-s*(x0+x1)/2,ty=net.H/2-s*(y0+y1)/2,k=now?1:.25,t=net.tf;t.s+=(s-t.s)*k;t.tx+=(tx-t.tx)*k;t.ty+=(ty-t.ty)*k}
function kick(){net.iter=Math.min(net.iter,net.max-100);if(!net.raf)net.raf=requestAnimationFrame(tick)}
function tick(){net.raf=0;relax(3);net.iter+=3;if(net.fit&&!net.drag){fitView(false);if(net.iter>=net.max-60)net.fit=false}draw();if(net.iter<net.max||net.drag)net.raf=requestAnimationFrame(tick)}
function draw(){const t=net.tf;$("vp").setAttribute("transform",`translate(${t.tx},${t.ty}) scale(${t.s})`);
 net.nodes.forEach(n=>{n.el.setAttribute("transform",`translate(${n.x},${n.y})`);if(n.lb){n.lb.setAttribute("x",n.x);n.lb.setAttribute("y",n.y+n.rad+13)}});
 net.edges.forEach(e=>{e.el.setAttribute("x1",e.a.x);e.el.setAttribute("y1",e.a.y);e.el.setAttribute("x2",e.b.x);e.el.setAttribute("y2",e.b.y)});
 net.hulls.forEach(h=>{const H=hull(h.m.map(n=>[n.x,n.y]));h.el.setAttribute("d",H.length?"M"+H.map(p=>p[0]+","+p[1]).join("L")+"Z":"")})}
function colorOf(n,lab,cby){const f=F[n.id];return cby==="cluster"?PAL[lab[n.id]%PAL.length]:cby==="sub"?SUB_COL[f.sub||f.type]:TYPE_COL[f.type]}
function build(){const g=G(),lab=curLabels(),thr=Number($("thr").value),mode=$("edgeMode").value,cby=$("colorBy").value,sm=shapeMode(),ns=net.nodes,n=ns.length,E=[];net.lab=lab;$("thrV").textContent=thr.toFixed(2);
 for(let a=0;a<n;a++)for(let b=a+1;b<n;b++){const r=M[ns[a].id][ns[b].id];if(r==null)continue;if(mode==="pos"?r>=thr:Math.abs(r)>=thr)E.push({a:ns[a],b:ns[b],r})}
 E.sort((x,y)=>Math.abs(y.r)-Math.abs(x.r));const total=E.length;net.edges=E.slice(0,1500);ns.forEach(x=>x.deg=0);net.edges.forEach(e=>{e.a.deg++;e.b.deg++});
 ["hullG","edgeG","nodeG","lblG"].forEach(id=>$(id).textContent="");net.hulls=[];
 if($("showHull").checked){const cl={};ns.forEach(x=>(cl[lab[x.id]]??=[]).push(x));Object.entries(cl).forEach(([c,m])=>{const col=cby==="cluster"?PAL[c%PAL.length]:"#94a3b8";net.hulls.push({m,el:mk("path",{fill:col,"fill-opacity":.13,stroke:col,"stroke-opacity":.13,"stroke-width":30,"stroke-linejoin":"round","stroke-linecap":"round"},$("hullG"))})})}
 net.edges.forEach(e=>{const w=Math.abs(e.r);e.el=mk("line",{stroke:corrCol(e.r),"stroke-opacity":.35+.55*w,"stroke-width":.7+2.4*w},$("edgeG"))});
 ns.forEach(nd=>{const f=F[nd.id];nd.rad=5.5+Math.min(6,Math.sqrt(nd.deg)*1.25);nd.el=mk("g",{cursor:"pointer"},$("nodeG"));mk("path",{d:shapePath(shapeOf(nd,lab,sm),nd.rad),fill:colorOf(nd,lab,cby),stroke:"#fff","stroke-width":1.5},nd.el);
  nd.lb=mk("text",{"font-size":11,"text-anchor":"middle",fill:"#344054","pointer-events":"none"},$("lblG"));nd.lb.textContent=short(f.name);nd.lb.style.display=$("showLbl").checked?"":"none";
  nd.el.addEventListener("pointerenter",ev=>{const t=$("tip");t.style.display="block";t.innerHTML=`<b>${esc(f.code)} ${esc(f.name)}</b><br>类型：${esc(f.type)}${f.sub?`（${esc(f.sub)}·${esc(f.region)}）`:""}　聚类：${lab[nd.id]+1}<br>组内平均相关：${fmt(nodeMean(nd.id))}<br>连线数：${nd.deg}`});
  nd.el.addEventListener("pointermove",ev=>{const t=$("tip");t.style.left=ev.clientX+14+"px";t.style.top=ev.clientY+14+"px"});nd.el.addEventListener("pointerleave",()=>$("tip").style.display="none");
  nd.el.addEventListener("pointerdown",ev=>{ev.preventDefault();ev.stopPropagation();net.drag={n:nd,sx:ev.clientX,sy:ev.clientY,moved:false};nd.fixed=true;net.fit=false;kick()})});
 const lg=[],has=f=>ns.some(x=>f(F[x.id]));
 if(cby==="cluster"){[...new Set(Object.values(lab))].sort((a,b)=>a-b).forEach(k=>lg.push(`<span><span class="dot" style="background:${PAL[k%PAL.length]}"></span>聚类 ${k+1}（${ns.filter(x=>lab[x.id]==k).length}）</span>`))}
 else if(cby==="sub")SUBS.filter(k=>has(f=>(f.sub||f.type)===k)).forEach(k=>lg.push(`<span><span class="dot" style="background:${SUB_COL[k]}"></span>${esc(k)}</span>`));
 else DATA.types.filter(t=>has(f=>f.type===t)).forEach(t=>lg.push(`<span class="lg" data-t="${esc(t)}" title="点击进入该类型"><span class="dot" style="background:${TYPE_COL[t]}"></span>${esc(t)}</span>`));
 const sk=new Map();ns.forEach(x=>{const f=F[x.id],l=sm==="type"?f.type:sm==="sub"?(f.sub||f.type):"聚类 "+(lab[x.id]+1);if(!sk.has(l))sk.set(l,shapeOf(x,lab,sm))});
 lg.push("｜形状（"+(sm==="type"?"类型":sm==="sub"?"细分":"聚类")+"）："+[...sk].sort((a,b)=>a[1]-b[1]).map(([l,k])=>icon(k,"#64748b")+esc(l)).join("　"));
 lg.push(`｜连线：<span style="color:${POS_D}">━</span>深红 强正相关(≥${DEEP}) <span style="color:${POS_L}">━</span>浅红 正相关 <span style="color:${NEG_L}">━</span>浅绿 负相关 <span style="color:${NEG_D}">━</span>深绿 强负相关(≤-${DEEP})`);$("legend").innerHTML=lg.join("");
 $("netHint").textContent=`当前显示 ${net.edges.length} 条连线${total>net.edges.length?`（共 ${total} 条，仅显示相关度最高的 ${net.edges.length} 条，请调高阈值）`:""}。`;net.total=total;const ec=$("edgeCnt");if(ec)ec.textContent=total;applySel();draw()}
function nodeMean(id){const g=G(),rs=g.idx.filter(j=>j!==id).map(j=>M[id][j]).filter(v=>v!=null);return mean(rs)}
function applySel(){const s=net.sel;const nb=new Set();if(s!=null){net.edges.forEach(e=>{if(e.a.id===s)nb.add(e.b.id);if(e.b.id===s)nb.add(e.a.id)});nb.add(s)}
 net.edges.forEach(e=>e.el.style.opacity=s==null||e.a.id===s||e.b.id===s?1:.04);net.nodes.forEach(n=>{const o=s==null||nb.has(n.id)?1:.22;n.el.style.opacity=o;n.lb.style.opacity=o;n.el.querySelector("path").setAttribute("stroke",n.id===s?"#111827":"#fff")});renderSide()}
function renderSide(){const g=G(),s=net.sel,box=$("side");if(s==null){box.innerHTML=`<b>使用说明</b><ul style="padding-left:18px;margin:6px 0"><li>用“分组”下拉框切换类型</li><li>点击节点：高亮并查看其最相关基金</li><li>拖动节点：其余节点会重新弹性排布</li><li>调阈值：只显示更高相关的连线</li></ul><div class="hint">距离由相关系数决定：同一簇内的基金相关度高，簇与簇之间距离大。</div>`;return}
 const f=F[s],rs=g.idx.filter(j=>j!==s&&M[s][j]!=null).map(j=>({j,r:M[s][j]})).sort((a,b)=>b.r-a.r),li=x=>`<li>${esc(fl(x.j))}<br><span class="${cls(x.r)}">${fmt(x.r)}</span> <span class="hint">${esc(F[x.j].type)}</span></li>`;
 box.innerHTML=`<b>${esc(f.code)} ${esc(f.name)}</b><div><span class="badge">${esc(f.type)}</span> 聚类 ${net.lab[s]+1}</div><div class="hint">组内平均相关 ${fmt(nodeMean(s))}；窗口内有效观测 ${OBS[s]}</div><div style="margin-top:8px"><b>最相关 Top 5</b></div><ol>${rs.slice(0,5).map(li).join("")}</ol><div><b>最不相关 Bottom 5</b></div><ol>${rs.slice(Math.max(5,rs.length-5)).reverse().map(li).join("")}</ol>`}
window.addEventListener("pointermove",ev=>{const d=net.drag;if(!d)return;if(Math.hypot(ev.clientX-d.sx,ev.clientY-d.sy)>3)d.moved=true;const r=$("net").getBoundingClientRect(),k=net.W/r.width,t=net.tf;d.n.x=((ev.clientX-r.left)*k-t.tx)/t.s;d.n.y=((ev.clientY-r.top)*k-t.ty)/t.s});
window.addEventListener("pointerup",()=>{const d=net.drag;if(!d)return;d.n.fixed=false;net.drag=null;if(!d.moved){net.sel=net.sel===d.n.id?null:d.n.id;applySel()}else kick()});
$("net").addEventListener("pointerdown",ev=>{if(ev.target.id==="net"||ev.target.closest("#hullG")){net.sel=null;applySel()}});

/* ---------- 二、结果表 ---------- */
function renderGroupSection(){const g=G(),lab=curLabels(),k=curK(),pos=posMap(g),res=g.results.find(x=>x.k==k)||g.results[0],ps=groupPairs(g);$("grpBadge").textContent=`${$("grp").value} · ${g.idx.length} 只 · ${k} 类`;
 const best=ps.length?ps.reduce((m,p)=>p.r>m.r?p:m):null,cards=[["组内基金数",g.idx.length],["组内平均相关",fmt(mean(ps.map(p=>p.r)))],["组内相关中位数",fmt(median(ps.map(p=>p.r)))],["当前聚类轮廓系数",fmt(res.silhouette)],["阈值内连线数",`<span id="edgeCnt">${net.total??0}</span>`],["组内最高相关",best?fmt(best.r):"—"]];
 $("gcards").innerHTML=cards.map(c=>`<div class="card"><div class="label">${c[0]}</div><div class="value">${c[1]}</div></div>`).join("");
 /* 热力图 */
 const ord=g.order.map(li=>g.idx[li]),lbl=ord.map(i=>F[i].code+" "+short(F[i].name,7)),z=ord.map(i=>ord.map(j=>i===j?1:M[i][j])),sc=heatScale(z.flat()),shapes=[];let st=0;
 for(let p=1;p<=ord.length;p++)if(p===ord.length||lab[ord[p]]!==lab[ord[st]]){shapes.push({type:"rect",xref:"x",yref:"y",x0:st-.5,x1:p-.5,y0:st-.5,y1:p-.5,line:{color:"#111827",width:1.6}});st=p}
 plot("heatG",[{z,x:lbl,y:lbl,type:"heatmap",...sc,hovertemplate:"%{y}<br>%{x}<br>相关系数：%{z:.3f}<extra></extra>"}],{shapes,margin:{l:20,r:10,t:10,b:20},xaxis:{showticklabels:ord.length<=40,tickangle:-60,type:"category",automargin:true},yaxis:{showticklabels:ord.length<=40,autorange:"reversed",type:"category",automargin:true},height:520});
 /* 轮廓系数 */
 plot("silG",[{x:g.results.map(r=>r.k),y:g.results.map(r=>r.silhouette),type:"bar",marker:{color:g.results.map(r=>r.k==k?"#2563eb":r.k==g.best_k?"#10b981":"#cbd5e1")},hovertemplate:"%{x} 类<br>轮廓系数 %{y:.3f}<extra></extra>"}],{xaxis:{title:"聚类数",dtick:1},yaxis:{title:"轮廓系数"},height:300,margin:{l:60,r:20,t:10,b:50}});
 renderClusterTable(lab,pos);renderPairs(lab);renderCent(lab)}
function renderClusterTable(lab,pos){const g=G(),gr={};g.idx.forEach(i=>(gr[lab[i]]??=[]).push(i));
 const rows=Object.entries(gr).map(([c,m])=>{m.sort((a,b)=>pos[a]-pos[b]);const intra=[],inter=[];for(let a=0;a<m.length;a++)for(let b=a+1;b<m.length;b++){const r=M[m[a]][m[b]];if(r!=null)intra.push(r)}
  g.idx.forEach(i=>{if(lab[i]!=c)m.forEach(j=>{const r=M[i][j];if(r!=null)inter.push(r)})});let core=null,bm=-9;if(m.length>1)m.forEach(i=>{const mu=mean(m.filter(j=>j!==i).map(j=>M[i][j]).filter(v=>v!=null));if(mu!=null&&mu>bm){bm=mu;core=i}});
  const tc={};m.forEach(i=>tc[F[i].type]=(tc[F[i].type]||0)+1);return{c:+c,m,n:m.length,intra:mean(intra),inter:mean(inter),core,tc}}).sort((a,b)=>b.n-a.n);
 $("clusterTable").innerHTML=`<table><thead><tr><th>聚类</th><th>基金数</th><th>簇内平均相关</th><th>与其他簇平均相关</th><th>区分度（内−外）</th><th>核心基金</th><th>类型构成</th><th>成员</th></tr></thead><tbody>${rows.map(r=>`<tr><td><span class="dot" style="background:${PAL[r.c%PAL.length]}"></span>聚类 ${r.c+1}</td><td>${r.n}</td><td class="${cls(r.intra)}">${fmt(r.intra)}</td><td>${fmt(r.inter)}</td><td>${r.intra!=null&&r.inter!=null?fmt(r.intra-r.inter):"—"}</td><td>${r.core!=null?esc(fl(r.core)):"—"}</td><td>${Object.entries(r.tc).map(([t,n])=>`${esc(t)} ${n}`).join(" · ")}</td><td class="wrapc">${r.m.map(i=>`<span class="chip" title="${esc(fl(i))}">${esc(F[i].code)} ${esc(short(F[i].name,10))}${$("grp").value===DATA.all_group?` · ${esc(F[i].type)}`:""}</span>`).join("")}</td></tr>`).join("")}</tbody></table>`}
function renderPairs(lab){lab=lab||curLabels();const g=G(),low=$("pairMode").value==="low",sc=$("pairScope").value,n=Math.max(1,Math.min(200,Number($("pairN").value)||20));
 let a=groupPairs(g);if(sc==="same")a=a.filter(p=>lab[p.i]===lab[p.j]);if(sc==="diff")a=a.filter(p=>lab[p.i]!==lab[p.j]);a.sort((x,y)=>low?x.r-y.r:y.r-x.r);a=a.slice(0,n);
 $("pairTable").innerHTML=a.length?`<table><thead><tr><th>排名</th><th>基金 A</th><th>类型</th><th>基金 B</th><th>类型</th><th>相关系数</th><th>共同天数 n</th><th>是否同聚类</th></tr></thead><tbody>${a.map((p,k)=>`<tr><td>${k+1}</td><td>${esc(fl(p.i))}</td><td>${esc(F[p.i].type)}</td><td>${esc(fl(p.j))}</td><td>${esc(F[p.j].type)}</td><td class="${cls(p.r)}">${fmt(p.r)}</td><td>${pairN(p.i,p.j)}</td><td>${lab[p.i]===lab[p.j]?"是（聚类 "+(lab[p.i]+1)+"）":"否"}</td></tr>`).join("")}</tbody></table>`:'<div class="empty">该范围内没有基金对。</div>'}
function renderCent(lab){lab=lab||curLabels();const g=G(),low=$("centMode").value==="low";
 const rows=g.idx.map(i=>{const rs=g.idx.filter(j=>j!==i&&M[i][j]!=null).map(j=>({j,r:M[i][j]})).sort((a,b)=>b.r-a.r);return{i,mu:mean(rs.map(x=>x.r)),hi:rs[0],lo:rs[rs.length-1]}}).sort((a,b)=>low?a.mu-b.mu:b.mu-a.mu);
 $("centTable").innerHTML=`<table><thead><tr><th>排名</th><th>基金</th><th>类型</th><th>聚类</th><th>组内平均相关</th><th>最相关基金</th><th>最低相关基金</th></tr></thead><tbody>${rows.map((r,k)=>`<tr><td>${k+1}</td><td>${esc(fl(r.i))}</td><td>${esc(F[r.i].type)}</td><td>${lab[r.i]+1}</td><td class="${cls(r.mu)}">${fmt(r.mu)}</td><td>${r.hi?`${esc(fl(r.hi.j))} <span class="${cls(r.hi.r)}">(${fmt(r.hi.r)})</span>`:"—"}</td><td>${r.lo?`${esc(fl(r.lo.j))} <span class="${cls(r.lo.r)}">(${fmt(r.lo.r)})</span>`:"—"}</td></tr>`).join("")}</tbody></table>`}

/* ---------- 三、跨类型 ---------- */
const TP=[];function buildTP(){TP.length=0;DATA.types.forEach((a,i)=>DATA.types.forEach((b,j)=>{if(j<i)return;const ps=pairsBetween(a,b);if(!ps.length)return;const rs=ps.map(p=>p.r);TP.push({a,b,within:a===b,n:ps.length,mean:mean(rs),median:median(rs),abs_mean:mean(rs.map(Math.abs)),hi:rs.filter(r=>r>=.7).length/rs.length,rs,max:ps.reduce((m,p)=>p.r>m.r?p:m),min:ps.reduce((m,p)=>p.r<m.r?p:m)})}))}
function typeCharts(){const T=DATA.types,mat=T.map(a=>T.map(b=>{const t=TP.find(x=>(x.a===a&&x.b===b)||(x.a===b&&x.b===a));return t?t.mean:null})),sc=heatScale(mat.flat()),ann=[];T.forEach((a,i)=>T.forEach((b,j)=>{const v=mat[i][j];if(v!=null)ann.push({x:b,y:a,text:fmt(v,2),showarrow:false,font:{color:v>.55?"#fff":"#111827",size:14}})}));
 plot("typeHeat",[{z:mat,x:T,y:T,type:"heatmap",...sc,hovertemplate:"%{y} × %{x}<br>平均相关：%{z:.3f}<extra></extra>"}],{annotations:ann,yaxis:{autorange:"reversed"},margin:{l:80,r:20,t:10,b:70}});
 plot("typeBox",TP.map(t=>({y:t.rs,name:t.within?`${t.a}（内部）`:`${t.a} × ${t.b}`,type:"box",boxmean:true,boxpoints:false,marker:{color:t.within?"#64748b":"#f97316"}})),{showlegend:false,yaxis:{title:"相关系数",zeroline:true},margin:{l:60,r:20,t:10,b:80}})}
function renderCross(){const a=$("srcT").value,b=$("dstT").value,A=ordIdx(a),B=ordIdx(b),same=a===b,z=A.map(i=>B.map(j=>i===j?null:M[i][j])),sc=heatScale(z.flat());
 $("crossHeatTitle").textContent=`${a}（行）× ${b}（列）基金相关系数热力图`;$("bestBarTitle").textContent=`${a} 每只基金与 ${b} 的最高相关 / 平均相关`;$("mapTitle").textContent=`${a} → ${b}：每只${a}基金最相关的${b}基金`;$("crossPairTitle").textContent=`${a} × ${b} 基金对排行榜`;
 plot("crossHeat",[{z,x:B.map(i=>F[i].code+" "+short(F[i].name,6)),y:A.map(i=>F[i].code+" "+short(F[i].name,6)),type:"heatmap",...sc,hovertemplate:"%{y}<br>%{x}<br>相关系数：%{z:.3f}<extra></extra>"}],{height:520,margin:{l:20,r:10,t:10,b:20},xaxis:{type:"category",showticklabels:B.length<=30,tickangle:-60,automargin:true},yaxis:{type:"category",showticklabels:A.length<=30,autorange:"reversed",automargin:true}});
 const rows=A.map(i=>{const rs=B.filter(j=>j!==i&&M[i][j]!=null).map(j=>({j,r:M[i][j]})).sort((x,y)=>y.r-x.r);return{i,rs,mu:mean(rs.map(x=>x.r))}}).filter(r=>r.rs.length).sort((x,y)=>y.rs[0].r-x.rs[0].r);
 plot("bestBar",[{x:rows.map(r=>F[r.i].code+" "+short(F[r.i].name,6)),y:rows.map(r=>r.rs[0].r),type:"bar",name:"最高相关",marker:{color:"#2563eb"},customdata:rows.map(r=>fl(r.rs[0].j)),hovertemplate:"%{x}<br>最高相关 %{y:.3f}<br>匹配：%{customdata}<extra></extra>"},{x:rows.map(r=>F[r.i].code+" "+short(F[r.i].name,6)),y:rows.map(r=>r.mu),type:"scatter",mode:"markers",name:"平均相关",marker:{color:"#f97316",size:7},hovertemplate:"%{x}<br>平均相关 %{y:.3f}<extra></extra>"}],{height:520,margin:{l:50,r:10,t:10,b:150},xaxis:{tickangle:-60,type:"category"},yaxis:{title:"相关系数"},legend:{orientation:"h",y:1.06}});
 renderMap(rows);renderCrossPairs()}
function renderMap(rows){const a=$("srcT").value,b=$("dstT").value,m=Number($("mapN").value),li=x=>`${esc(fl(x.j))} <span class="${cls(x.r)}">(${fmt(x.r)})</span>`,
pick=r=>({top:r.rs.slice(0,m),bot:r.rs.slice(Math.max(m,r.rs.length-m)).reverse()}),few=rows.filter(r=>r.rs.length<2*m).length;
$("mapHint").textContent=`每只${a}基金的“最相关”与“最不相关”各显示 ${m} 只（切换选项时两列始终一致；最不相关从相关性最低开始排列，且与最相关不重复）。`+(few?`有 ${few} 只基金的可比${b}基金不足 ${2*m} 只，其“最不相关”一列相应少于 ${m} 只。`:"");
$("mapTable").innerHTML=rows.length?`<table><thead><tr><th>排名</th><th>${esc(a)}基金</th><th>最高相关系数</th><th>最相关的${esc(b)}基金 Top ${m}</th><th>${esc(b)}平均相关</th><th>最不相关的${esc(b)}基金 Bottom ${m}</th></tr></thead><tbody>${rows.map((r,k)=>{const p=pick(r);return`<tr><td>${k+1}</td><td>${esc(fl(r.i))}</td><td class="${cls(r.rs[0].r)}">${fmt(r.rs[0].r)}</td><td class="wrapc">${p.top.map(li).join("<br>")}</td><td>${fmt(r.mu)}</td><td class="wrapc">${p.bot.length?p.bot.map(li).join("<br>"):"—"}</td></tr>`}).join("")}</tbody></table>`:'<div class="empty">没有可用的基金对。</div>'}
function renderCrossPairs(){const a=$("srcT").value,b=$("dstT").value,low=$("crossMode").value==="low",n=Math.max(1,Math.min(200,Number($("crossN").value)||20)),ps=pairsBetween(a,b).sort((x,y)=>low?x.r-y.r:y.r-x.r).slice(0,n);
 $("crossPairTable").innerHTML=ps.length?`<table><thead><tr><th>排名</th><th>${esc(a)}基金</th><th>${esc(b)}基金</th><th>相关系数</th><th>共同天数 n</th></tr></thead><tbody>${ps.map((p,k)=>{const[x,y]=F[p.i].type===a?[p.i,p.j]:[p.j,p.i];return`<tr><td>${k+1}</td><td>${esc(fl(x))}</td><td>${esc(fl(y))}</td><td class="${cls(p.r)}">${fmt(p.r)}</td><td>${pairN(p.i,p.j)}</td></tr>`}).join("")}</tbody></table>`:'<div class="empty">没有可用的基金对。</div>'}
function renderTypeRank(){const m=$("typeRankMode").value,a=[...TP].sort((x,y)=>y[m]-x[m]);
 $("typeRankTable").innerHTML=`<table><thead><tr><th>排名</th><th>类型 A</th><th>类型 B</th><th>范围</th><th>平均相关</th><th>中位数</th><th>平均绝对相关</th><th>≥0.7 占比</th><th>基金对数</th><th>最高相关基金对</th><th>最低相关基金对</th></tr></thead><tbody>${a.map((r,i)=>`<tr><td>${i+1}</td><td>${esc(r.a)}</td><td>${esc(r.b)}</td><td>${r.within?"类型内部":"跨类型"}</td><td class="${cls(r.mean)}">${fmt(r.mean)}</td><td>${fmt(r.median)}</td><td>${fmt(r.abs_mean)}</td><td>${(r.hi*100).toFixed(1)}%</td><td>${r.n}</td><td>${pairHTML(r.max)}</td><td>${pairHTML(r.min)}</td></tr>`).join("")}</tbody></table>`}

/* ---------- 四、指定基金查询 ---------- */
function query(){const q=$("fundSearch").value.trim().toLowerCase(),f=AF().find(x=>x.code.toLowerCase()==q)||AF().find(x=>x.name.toLowerCase()==q)||AF().find(x=>x.code.toLowerCase().includes(q)||x.name.toLowerCase().includes(q)),box=$("queryTable");
 if(!q||!f){$("querySummary").textContent="没有找到匹配的基金，请检查代码或名称（或该基金在所选时间范围内有效观测不足，未纳入）。";box.innerHTML='<div class="empty">无查询结果。</div>';$("queryChart").innerHTML="";return}
 const i=F.indexOf(f),low=$("corrMode").value==="low",sc=$("qScope").value,n=Math.max(1,Math.min(100,Number($("corrTopN").value)||10));
 let a=F.map((o,j)=>({j,r:M[i][j]})).filter(x=>x.j!==i&&x.r!=null);if(sc==="same")a=a.filter(x=>F[x.j].type===f.type);if(sc==="cross")a=a.filter(x=>F[x.j].type!==f.type);a.sort((x,y)=>low?x.r-y.r:y.r-x.r);a=a.slice(0,n).map(x=>Object.assign(x,{s:pairStats(i,x.j)}));
 $("querySummary").innerHTML=`已选择：<span class="badge">${esc(f.code)}</span> ${esc(f.name)}；类型：${esc(f.type)}；显示相关性${low?"最低":"最高"}的 ${a.length} 只基金。`;
 box.innerHTML=`<table><thead><tr><th>排名</th><th>基金代码</th><th>基金名称</th><th>类型</th><th>Pearson r</th><th>Spearman ρ</th><th>β</th><th>R²</th><th>同向率</th><th>共同天数 n</th><th>95% CI</th><th>p 值</th></tr></thead><tbody>${a.map((x,k)=>`<tr><td>${k+1}</td><td>${esc(F[x.j].code)}</td><td>${esc(F[x.j].name)}</td><td>${esc(F[x.j].type)}</td><td class="${cls(x.r)}">${fmt(x.r)}</td><td class="${cls(x.s.spearman)}">${fmt(x.s.spearman)}</td><td>${fmt(x.s.beta)}</td><td>${fmt(x.s.r2)}</td><td>${x.s.same==null?"—":(x.s.same*100).toFixed(1)+"%"}</td><td>${x.s.n}</td><td>${x.s.ciLow==null?"—":`[${fmt(x.s.ciLow)}, ${fmt(x.s.ciHigh)}]`}</td><td>${fmt(x.s.p,4)}</td></tr>`).join("")}</tbody></table>`;
 plot("queryChart",[{y:a.map(x=>short(F[x.j].name,12)+" "+F[x.j].code),x:a.map(x=>x.r),type:"bar",orientation:"h",marker:{color:a.map(x=>TYPE_COL[F[x.j].type])},customdata:a.map(x=>[x.s.n,x.s.spearman,x.s.same]),hovertemplate:"%{y}<br>Pearson %{x:.3f}<br>共同天数 %{customdata[0]}<br>Spearman %{customdata[1]:.3f}<br>同向率 %{customdata[2]:.1%}<extra></extra>"}],{height:Math.max(300,a.length*24+60),yaxis:{autorange:"reversed",type:"category"},xaxis:{title:"Pearson 相关系数"},margin:{l:170,r:20,t:10,b:45}})}

/* ---------- 六、收益率分档关联与滚动相关（按需计算） ---------- */
function inputFund(id){const q=$(id).value.trim().toLowerCase();return AF().find(x=>x.code.toLowerCase()===q)||AF().find(x=>x.name.toLowerCase()===q)||AF().find(x=>x.code.toLowerCase().includes(q)||x.name.toLowerCase().includes(q))}
function binSpec(k){if(Number(k)===3)return{edges:[-Infinity,-.01,.01,Infinity],labels:["跌幅 ≥ 1%","小幅波动（−1%～1%）","涨幅 ≥ 1%"]};if(Number(k)===7)return{edges:[-Infinity,-.03,-.02,-.01,.01,.02,.03,Infinity],labels:["跌幅 ≥ 3%","跌幅 2%～3%","跌幅 1%～2%","小幅波动（−1%～1%）","涨幅 1%～2%","涨幅 2%～3%","涨幅 ≥ 3%"]};return{edges:[-Infinity,-.02,-.01,.01,.02,Infinity],labels:["跌幅 ≥ 2%","跌幅 1%～2%","小幅波动（−1%～1%）","涨幅 1%～2%","涨幅 ≥ 2%"]}}
function binIndex(v,edges){for(let k=0;k<edges.length-1;k++)if(v>=edges[k]&&v<edges[k+1])return k;return edges.length-2}
function stateAnalysis(i,j,k){const spec=binSpec(k),r=spec.labels.length,counts=Array.from({length:r},()=>new Array(r).fill(0)),a=RET[i],b=RET[j],w=w0(),we=w1();let n=0;for(let t=w;t<we;t++)if(a[t]===a[t]&&b[t]===b[t]){counts[binIndex(a[t],spec.edges)][binIndex(b[t],spec.edges)]++;n++}const rows=counts.map(row=>row.reduce((s,v)=>s+v,0)),cols=spec.labels.map((_,c)=>counts.reduce((s,row)=>s+row[c],0));let chi=0,minExp=Infinity;for(let x=0;x<r;x++)for(let y=0;y<r;y++){const e=n?rows[x]*cols[y]/n:0;if(e>0){chi+=(counts[x][y]-e)**2/e;minExp=Math.min(minExp,e)}}const df=(r-1)*(r-1),p=n?chi2P(chi,df):null,v=n&&r>1?Math.sqrt(chi/(n*(r-1))):null;return{spec,counts,rows,cols,n,chi,df,p,v,minExp}}
function renderState(){const fa=inputFund("stateFundA"),fb=inputFund("stateFundB"),box=$("stateTable");if(!fa||!fb||fa.code===fb.code){$("stateSummary").textContent="请输入两只不同的有效基金代码或名称。";box.innerHTML='<div class="empty">请选择两只不同的基金后计算。</div>';$('stateHeat').innerHTML="";return}const i=F.indexOf(fa),j=F.indexOf(fb),z=stateAnalysis(i,j,$("stateBins").value),s=pairStats(i,j);if(!z.n){$("stateSummary").textContent="所选区间没有共同有效收益率。";box.innerHTML='<div class="empty">没有可用于分档的共同交易日。</div>';return}const pct=z.counts.map((row,x)=>row.map(v=>z.rows[x]?v/z.rows[x]:0));box.innerHTML=`<table><thead><tr><th>${esc(fa.name)} ＼ ${esc(fb.name)}</th>${z.spec.labels.map(x=>`<th>${esc(x)}</th>`).join("")}<th>行合计</th></tr></thead><tbody>${z.counts.map((row,x)=>`<tr><th>${esc(z.spec.labels[x])}</th>${row.map((v,y)=>`<td>${v}<br><span class="hint">${(pct[x][y]*100).toFixed(1)}%</span></td>`).join("")}<td>${z.rows[x]}</td></tr>`).join("")}<tr><th>列合计</th>${z.cols.map(v=>`<td>${v}</td>`).join("")}<td>${z.n}</td></tr></tbody></table>`;$("stateSummary").innerHTML=`${esc(fa.name)} × ${esc(fb.name)}：共同天数 n=${z.n}；Pearson r=${fmt(s.pearson)}，Spearman ρ=${fmt(s.spearman)}，β=${fmt(s.beta)}，同向率=${s.same==null?"—":(s.same*100).toFixed(1)+"%"}；χ²=${fmt(z.chi,2)}，df=${z.df}，p=${fmt(z.p,4)}，Cramér’s V=${fmt(z.v)}。${z.minExp<5?` <span style="color:#b42318">提示：最小期望频数 ${fmt(z.minExp,1)}&lt;5，建议合并档位或使用蒙特卡洛/精确方法。</span>`:""}`;plot("stateHeat",[{z:pct,x:z.spec.labels,y:z.spec.labels,type:"heatmap",zmin:0,zmax:1,colorscale:[[0,"#fff"],[.25,"#dbeafe"],[.6,"#60a5fa"],[1,"#1d4ed8"]],hovertemplate:"A：%{y}<br>B：%{x}<br>行条件概率：%{z:.1%}<extra></extra>"}],{height:Math.max(360,z.spec.labels.length*55+140),margin:{l:140,r:20,t:15,b:130},xaxis:{tickangle:-35},yaxis:{autorange:"reversed"}});renderRolling()}
function rollingValues(i,j,win){const w=w0(),we=w1(),span=we-w,step=Math.max(1,Math.floor(span/180)),out=[];if(win<2||span<win)return out;for(let end=w+win;end<=we;end+=step){const r=pcorr(RET[i],RET[j],end-win,end);if(r!=null)out.push({d:D[end-1],r})}return out}
function renderRolling(){const fa=inputFund("stateFundA"),fb=inputFund("stateFundB");if(!fa||!fb||fa.code===fb.code){$("rollingSummary").textContent="";$("rollingChart").innerHTML="";return}const win=Number($("rollingWindow").value),v=rollingValues(F.indexOf(fa),F.indexOf(fb),win);if(!v.length){$("rollingSummary").textContent=`当前区间不足 ${win} 个交易日，无法计算滚动相关。`;$("rollingChart").innerHTML="";return}$("rollingSummary").textContent=`${fa.name} × ${fb.name}：${win}日滚动相关，共 ${v.length} 个窗口；曲线用于观察关系稳定性，不替代全区间检验。`;plot("rollingChart",[{x:v.map(x=>x.d),y:v.map(x=>x.r),type:"scatter",mode:"lines",line:{color:"#2563eb",width:2},hovertemplate:"%{x}<br>滚动相关 %{y:.3f}<extra></extra>"}],{yaxis:{title:"相关系数",range:[-1,1]},xaxis:{title:"窗口结束日期"},margin:{l:55,r:20,t:15,b:50}})}

/* ---------- 一、总览（总） ---------- */
function drill(t){$("grp").value=t;$("grp").onchange();$("netSec").scrollIntoView({behavior:"smooth",block:"start"})}
const lerpC=(a,b,t)=>{const A=[1,3,5].map(i=>parseInt(a.slice(i,i+2),16)),B=[1,3,5].map(i=>parseInt(b.slice(i,i+2),16));return"#"+A.map((x,i)=>Math.round(x+(B[i]-x)*t).toString(16).padStart(2,"0")).join("")};
const mcol=(v,t)=>v>=0?lerpC("#f4b6b6","#b91c1c",t):lerpC("#a3dbb3","#15803d",t);
function renderMacro(){const T=DATA.types,svg=$("macro"),W=860,H=520,cx=W/2,cy=H/2+5,n=T.length,pos={},R={},X=TP.filter(x=>!x.within);svg.textContent="";
 const defs=mk("defs",{},svg),flt=mk("filter",{id:"mshadow",x:"-30%",y:"-30%",width:"160%",height:"160%"},defs);mk("feDropShadow",{dx:0,dy:3,stdDeviation:4,"flood-color":"#101828","flood-opacity":.18},flt);
 const bg=mk("radialGradient",{id:"mbg"},defs);mk("stop",{offset:"0%","stop-color":"#ffffff"},bg);mk("stop",{offset:"100%","stop-color":"#edf1f9"},bg);
 mk("rect",{width:W,height:H,rx:10,fill:"url(#mbg)"},svg);mk("ellipse",{cx,cy,rx:250,ry:150,fill:"none",stroke:"#dbe2ef","stroke-dasharray":"4 7"},svg);
 T.forEach((t,i)=>{const a=-Math.PI/2+2*Math.PI*i/Math.max(n,1);pos[t]=n===1?[cx,cy]:[cx+250*Math.cos(a),cy+150*Math.sin(a)];R[t]=22+2.4*Math.sqrt(DATA.groups[t].idx.length)});
 const av=X.map(x=>Math.abs(x.mean)),mn=Math.min(...av),mx=Math.max(...av),nt=v=>mx>mn?(Math.abs(v)-mn)/(mx-mn):.6,edges=[];let opp=0;
 X.forEach(x=>{const p=pos[x.a],q=pos[x.b],m=[(p[0]+q[0])/2,(p[1]+q[1])/2],dx=q[0]-p[0],dy=q[1]-p[1],L=Math.hypot(dx,dy)||1;let c;
  if(Math.hypot(m[0]-cx,m[1]-cy)<30){const sg=opp++%2?-1:1;c=[m[0]-dy/L*100*sg,m[1]+dx/L*100*sg]}else c=[m[0]+(cx-m[0])*.3,m[1]+(cy-m[1])*.3];
  const tr=(a,o)=>{const ux=c[0]-a[0],uy=c[1]-a[1],l=Math.hypot(ux,uy)||1,r=R[o]+19;return[a[0]+ux/l*r,a[1]+uy/l*r]},s=tr(p,x.a),e=tr(q,x.b),t=nt(x.mean),col=mcol(x.mean,t);
  edges.push({x,s,e,c,col,t,path:mk("path",{d:`M${s[0]},${s[1]}Q${c[0]},${c[1]} ${e[0]},${e[1]}`,fill:"none",stroke:col,"stroke-width":3+9*t,"stroke-linecap":"round","stroke-opacity":.85},svg)})});
 const setHi=t=>edges.forEach(E=>{const on=t==null||E.x.a===t||E.x.b===t;E.path.style.opacity=on?1:.12;E.pill.style.opacity=on?1:.12});
 const lab=[];
 T.forEach(t=>{const [x,y]=pos[t],g=DATA.groups[t],w=TP.find(z=>z.within&&z.a===t),r=R[t],v=w&&w.mean!=null?w.mean:0,f=Math.max(0,Math.min(1,v)),rho=r+9,C=2*Math.PI*rho,el=mk("g",{transform:`translate(${x},${y})`,cursor:"pointer",filter:"url(#mshadow)"},svg);
  mk("title",{},el).textContent=`${t}：${g.idx.length} 只；内部平均相关 ${fmt(w?w.mean:null)}；点击进入分视图`;
  mk("circle",{r:r+14,fill:"#fff"},el);mk("circle",{r:rho,fill:"none",stroke:"#e8ecf4","stroke-width":6},el);
  mk("circle",{r:rho,fill:"none",stroke:TYPE_COL[t],"stroke-width":6,"stroke-linecap":"round","stroke-dasharray":`${f*C} ${C}`,transform:"rotate(-90)"},el);
  mk("circle",{r:r,fill:TYPE_COL[t],"fill-opacity":.13},el);mk("path",{d:shapePath(TYPE_SHAPE[t],r*.55),fill:TYPE_COL[t]},el);
  el.addEventListener("pointerenter",()=>setHi(t));el.addEventListener("pointerleave",()=>setHi(null));el.addEventListener("click",()=>drill(t));
  const dx=x-cx,dy=y-cy,dl=Math.hypot(dx,dy)||1,ux=n===1?0:dx/dl,uy=n===1?1:dy/dl,px=x+ux*(r+26),py=y+uy*(r+26),side=Math.abs(ux)>.35,anc=side?(ux>0?"start":"end"):"middle",ny=side?y-2:(uy<0?py-18:py+16);
  lab.push([px,ny,anc,t,`${g.idx.length} 只 · 内部 ${fmt(w?w.mean:null,2)}`])});
 lab.forEach(([px,ny,anc,t,sub])=>{const halo={"text-anchor":anc,stroke:"#f4f7fc","stroke-width":4,"paint-order":"stroke"},a=mk("text",{x:px,y:ny,"font-size":16,"font-weight":700,fill:"#172033",...halo},svg),b=mk("text",{x:px,y:ny+17,"font-size":12,fill:"#667085",...halo},svg);a.textContent=t;b.textContent=sub});
 edges.forEach(E=>{const bx=.25*E.s[0]+.5*E.c[0]+.25*E.e[0],by=.25*E.s[1]+.5*E.c[1]+.25*E.e[1],g=mk("g",{},svg);E.pill=g;
  mk("title",{},g).textContent=`${E.x.a} × ${E.x.b}：平均相关 ${fmt(E.x.mean)}（${E.x.n} 对基金）`;mk("rect",{x:bx-24,y:by-12,width:48,height:24,rx:12,fill:"#fff",stroke:E.col,"stroke-width":2},g);
  mk("text",{x:bx,y:by+4.5,"text-anchor":"middle","font-size":13,"font-weight":700,fill:E.x.mean>=0?"#991b1b":"#166534"},g).textContent=fmt(E.x.mean,2)});
 $("macroLegend").innerHTML=`<span><span class="gbar" style="background:linear-gradient(90deg,#f4b6b6,#b91c1c)"></span>浅红→深红：正相关由弱到强</span><span><span class="gbar" style="background:linear-gradient(90deg,#a3dbb3,#15803d)"></span>浅绿→深绿：负相关由弱到强</span>${X.length?`<span>当前类型间范围 ${fmt(Math.min(...X.map(x=>x.mean)),2)} ~ ${fmt(Math.max(...X.map(x=>x.mean)),2)}</span>`:""}<span>外圈 = 类型内部平均相关</span>`}
const bar=v=>Math.round(Math.max(0,Math.min(1,v||0))*100);
function renderTypeSummary(){const rows=[];
 DATA.types.forEach(t=>{const g=DATA.groups[t],w=TP.find(z=>z.within&&z.a===t),cr=TP.filter(z=>!z.within&&(z.a===t||z.b===t)).sort((a,b)=>b.mean-a.mean)[0];
  rows.push(`<tr><td><span class="lg" data-t="${esc(t)}"><span class="dot" style="background:${TYPE_COL[t]}"></span><b>${esc(t)}</b></span></td><td>${g.idx.length}</td><td class="${cls(w?w.mean:null)}" style="background:linear-gradient(90deg,#fbdcdc ${bar(w?w.mean:0)}%,transparent ${bar(w?w.mean:0)}%)">${fmt(w?w.mean:null)}</td><td>${cr?`${esc(cr.a===t?cr.b:cr.a)} <span class="${cls(cr.mean)}">(${fmt(cr.mean)})</span>`:"—"}</td></tr>`);
  const sm={};g.idx.forEach(i=>{const k=F[i].sub;if(k)(sm[k]??=[]).push(i)});
  if(Object.keys(sm).length>1)Object.entries(sm).forEach(([k,m])=>{const rs=[];for(let a=0;a<m.length;a++)for(let b=a+1;b<m.length;b++){const r=M[m[a]][m[b]];if(r!=null)rs.push(r)}const mu=mean(rs);rows.push(`<tr><td style="padding-left:26px">└ ${esc(k)}</td><td>${m.length}</td><td class="${cls(mu)}" style="background:linear-gradient(90deg,#fbdcdc ${bar(mu)}%,transparent ${bar(mu)}%)">${fmt(mu)}</td><td>—</td></tr>`)})});
 $("typeSummary").innerHTML=`<div class="table-wrap"><table><thead><tr><th>类型 / 细分</th><th>基金数</th><th>内部平均相关</th><th>相关最高的其他类型</th></tr></thead><tbody>${rows.join("")}</tbody></table></div><div class="hint" style="margin-top:8px">点击类型名称进入该类型的分视图。</div>`}
function renderIndexSection(){const IT=DATA.index_type,box=$("idxSec");if(!IT||!DATA.groups[IT]){box.hidden=true;return}box.hidden=false;
 const ix=DATA.groups[IT].idx,FT=DATA.types.filter(t=>t!==IT);if(!FT.length){box.hidden=true;return}
 const z=ix.map(i=>FT.map(t=>mean(DATA.groups[t].idx.map(j=>M[i][j]).filter(v=>v!=null)))),ann=[];ix.forEach((i,a)=>FT.forEach((t,b)=>{const v=z[a][b];if(v!=null)ann.push({x:t,y:F[i].name,text:fmt(v,2),showarrow:false,font:{size:12,color:v>.65?"#fff":"#111827"}})}));
 plot("idxHeat",[{z,x:FT,y:ix.map(i=>F[i].name),type:"heatmap",...heatScale(z.flat()),hovertemplate:"%{y} × %{x}<br>平均相关：%{z:.3f}<extra></extra>"}],{annotations:ann,height:Math.max(360,ix.length*26+90),yaxis:{autorange:"reversed",type:"category"},margin:{l:110,r:20,t:10,b:50}});
 const cell=(i,t)=>DATA.groups[t].idx.filter(j=>j!==i&&M[i][j]!=null).sort((p,q)=>M[i][q]-M[i][p]).slice(0,3).map(j=>`${esc(fl(j))} <span class="${cls(M[i][j])}">(${fmt(M[i][j])})</span>`).join("<br>")||"—";
 $("idxTable").style.maxHeight="none";
 $("idxTable").innerHTML=`<table><thead><tr><th>细分</th><th>地区</th><th>指数</th>${FT.map(t=>`<th>${esc(t)}平均</th>`).join("")}${FT.map(t=>`<th>最相关「${esc(t)}」基金 Top 3</th>`).join("")}</tr></thead><tbody>${ix.map((i,a)=>`<tr><td>${esc(F[i].sub)}</td><td>${esc(F[i].region)}</td><td>${esc(F[i].name)}</td>${z[a].map(v=>`<td class="${cls(v)}">${fmt(v)}</td>`).join("")}${FT.map(t=>`<td class="wrapc" style="min-width:250px">${cell(i,t)}</td>`).join("")}</tr>`).join("")}</tbody></table>`}

/* ---------- 初始化 ---------- */
function init(){
  $("grp").onchange=()=>{loadGroup();renderGroupSection()};$("kSel").onchange=()=>{build();renderGroupSection()};
 ["colorBy","shapeBy","edgeMode","showLbl","showHull"].forEach(id=>$(id).onchange=build);$("thr").oninput=build;$("relayout").onclick=initPos;
 ["pairMode","pairScope","pairN"].forEach(id=>$(id).onchange=()=>renderPairs());$("centMode").onchange=()=>renderCent();
 $("srcT").onchange=$("dstT").onchange=renderCross;$("mapN").onchange=renderCross;["crossMode","crossN"].forEach(id=>$(id).onchange=renderCrossPairs);$("typeRankMode").onchange=renderTypeRank;
 $("queryBtn").onclick=query;$("fundSearch").onkeydown=e=>{if(e.key=="Enter")query()};["qScope","corrMode","corrTopN"].forEach(id=>$(id).onchange=()=>{if($("fundSearch").value)query()});
 $("clearBtn").onclick=()=>{$("fundSearch").value="";$("querySummary").textContent="";$("queryTable").innerHTML='<div class="empty">请输入基金代码或名称后查询。</div>';$("queryChart").innerHTML=""};
 $("stateBtn").onclick=renderState;$("rollingBtn").onclick=renderRolling;$("rollingWindow").onchange=renderRolling;
 $("legend").onclick=e=>{const t=e.target.closest(".lg");if(t)drill(t.dataset.t)};$("typeSummary").onclick=e=>{const t=e.target.closest(".lg");if(t)drill(t.dataset.t)};
 initBars();applyResult(computeAll(0,ND));syncBars("");renderAll(true)}
init();
</script></body></html>'''


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="生成基金分组聚类与相关性分析 HTML")
    ap.add_argument("nav", nargs="?", default="fund_nav_history.csv")
    ap.add_argument("target", nargs="?", default="funds_universe_example.csv")
    ap.add_argument("output", nargs="?", default="fund_correlation_report.html")
    ap.add_argument("--no-index", action="store_true", help="不加入大盘指数")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，重新抓取指数")
    ap.add_argument("--cache-dir", default=INDEX_CACHE)
    ap.add_argument("--nav-unit", choices=["pct", "frac", "auto"], default="pct",
                    help="净值文件“增长率”的单位：pct=百分数（fetch_fund_nav.py 的输出，默认），frac=小数，auto=按数值大小猜")
    a = ap.parse_args()
    generate_correlation_html(a.nav, a.target, a.output, not a.no_index, a.refresh, a.cache_dir, a.nav_unit)
