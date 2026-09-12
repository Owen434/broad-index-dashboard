# -*- coding: utf-8 -*-
"""
cffex_net_position.py
中金所「期货会员成交持仓排名」-> 中信期货 / 前20会员 股指期货净持仓日报

数据源: http://www.cffex.com.cn/sj/ccpm/YYYYMM/DD/{IH,IF,IC,IM}_1.csv  (每个交易日收盘后发布)
输出(./output_cffex/):
    cffex_rank_raw.parquet   原始排名缓存(逐合约、逐名次), 增量追加
    cffex_net_daily.xlsx     日度汇总宽表
    快报_YYYYMMDD.txt        与截图同格式的文字快报
    cffex_net_position.html  单文件交互看板

用法:
    python cffex_net_position.py --start 20240101   # 首次回填
    python cffex_net_position.py                    # 之后每天增量更新
    python cffex_net_position.py --no-fetch         # 只用缓存重算、重画

口径(与截图一致):
    净变 = Σ(多单增减) - Σ(空单增减), 对该品种所有合约求和; 正=净加多单, 负=净加空单
    截图 "中信期货净空单 573" = 四个品种净变之和 = 126 - 639 + 409 - 469 = -573

开源集成说明：
    这份脚本本身已经是"抓数据+算净持仓+出单文件交互 HTML"的完整闭环，和仓库里其它
    脚本的产出形态（单文件 Plotly HTML）一致，这里只做了两处改动：
    1. 输出目录去掉个人习惯的数字前缀（原来是 "7.output_cffex"），改成 output_cffex，
       和仓库里 etf/cache_etf 这类命名风格保持一致。
    2. 首次回填（--start 20240101 起）请求量较大（4个合约 × 全部交易日），
       接入 daily_update.yml 后靠 actions/cache 把 output_cffex/cffex_rank_raw.parquet
       在每天的 CI 任务间持久化下来，之后每天只需增量抓当天的排名表，
       不会每天都从 2024 年初重新拉一遍。
    这个净持仓口径和仓库首页说的"护盘资金"是同一件事的另一个观察角度：
    ETF资金流看的是场内份额申赎，这里看的是股指期货多空持仓——两者经常互相印证。
"""
try:
    import net_patch  # noqa: F401  若有, 须在 akshare 之前导入
except ImportError:
    pass

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots

VARS = {"IH": "上证50", "IF": "沪深300", "IC": "中证500", "IM": "中证1000"}
COLORS = {"IH": "#4e79a7", "IF": "#e8871e", "IC": "#59a89c", "IM": "#9c6bb0"}
MEMBER = "中信期货"
UP, DOWN = "#d62728", "#2a9d3a"  # A股习惯: 红=多, 绿=空

# 原版用 include_plotlyjs=True，会把完整的 plotly.js（~4MB）内嵌进这一个 HTML 文件，
# 每天自动提交一次，一年下来对仓库体积不友好。这里默认改成引用 CDN（几十字节的
# <script> 标签，看板打开时联网加载），仅需要离线单文件可用时再改回 True。
EMBED_PLOTLY_JS = False
PLOTLY_CDN_URL = "https://cdn.plot.ly/plotly-2.35.2.min.js"

OUT_DIR = Path(__file__).resolve().parent / "output_cffex"
CACHE = OUT_DIR / "cffex_rank_raw.parquet"

URL = "http://www.cffex.com.cn/sj/ccpm/{ym}/{d}/{var}_1.csv"
COLS = ["date", "contract", "rank",
        "vol_name", "vol", "vol_chg",
        "long_name", "long_oi", "long_chg",
        "short_name", "short_oi", "short_chg"]
NUM_COLS = ["rank", "vol", "vol_chg", "long_oi", "long_chg", "short_oi", "short_chg"]

SESSION = requests.Session()
# 中金所是国内公开数据源，直连即可；不复用系统/环境代理，
# 避免部分本地代理把国内站点也转发出去导致连不上或超时。
SESSION.trust_env = False
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "http://www.cffex.com.cn/ccpm/",
})


# ----------------------------------------------------------------- 抓取
def _clean(df: pd.DataFrame, date: str, var: str) -> pd.DataFrame:
    df = df[COLS].copy()
    for c in ["contract", "vol_name", "long_name", "short_name"]:
        df[c] = df[c].astype(str).str.strip()
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = date
    df["var"] = var
    return df


def fetch_var_direct(date: str, var: str) -> pd.DataFrame:
    """直接解析中金所 CSV: 只保留首列为 8 位交易日的数据行, 按位置取 12 列。"""
    r = SESSION.get(URL.format(ym=date[:6], d=date[6:], var=var), timeout=15)
    if r.status_code != 200:
        return pd.DataFrame()
    rows = []
    for line in r.content.decode("gbk", errors="ignore").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 12 and parts[0].isdigit() and len(parts[0]) == 8:
            rows.append(parts[:12])
    if not rows:
        return pd.DataFrame()
    return _clean(pd.DataFrame(rows, columns=COLS), date, var)


def fetch_var_akshare(date: str, var: str) -> pd.DataFrame:
    """备用: AKShare 的 get_cffex_rank_table, 返回 {合约: DataFrame}。"""
    import akshare as ak
    rename = {"vol_party_name": "vol_name",
              "long_party_name": "long_name", "long_open_interest": "long_oi",
              "long_open_interest_chg": "long_chg",
              "short_party_name": "short_name", "short_open_interest": "short_oi",
              "short_open_interest_chg": "short_chg"}
    frames = []
    for sym, t in ak.get_cffex_rank_table(date=date, vars_list=[var]).items():
        t = t.rename(columns=rename)
        t["contract"], t["date"] = sym, date
        frames.append(t)
    return _clean(pd.concat(frames, ignore_index=True), date, var) if frames else pd.DataFrame()


def fetch_var(date: str, var: str) -> pd.DataFrame:
    for fn in (fetch_var_direct, fetch_var_akshare):
        try:
            df = fn(date, var)
            if len(df):
                return df
        except Exception as e:  # noqa: BLE001
            print(f"    {fn.__name__} {date} {var} 失败: {e}")
    return pd.DataFrame()


def trade_dates(start: str, end: str) -> list[str]:
    try:
        import akshare as ak
        s = pd.to_datetime(ak.tool_trade_date_hist_sina()["trade_date"])
    except Exception:  # noqa: BLE001
        s = pd.Series(pd.bdate_range(start, end))
    s = s[(s >= pd.Timestamp(start)) & (s <= pd.Timestamp(end))]
    return [d.strftime("%Y%m%d") for d in s]


def update_cache(raw: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    have = set(raw["date"]) if len(raw) else set()
    todo = [d for d in trade_dates(start, end) if d not in have]
    print(f"待抓取 {len(todo)} 个交易日")
    new = []
    for i, d in enumerate(todo, 1):
        frames = [f for f in (fetch_var(d, v) for v in VARS) if len(f)]
        got = ",".join(f["var"].iloc[0] for f in frames) or "无数据(未发布?)"
        print(f"  [{i}/{len(todo)}] {d}: {got}")
        if frames:
            new.append(pd.concat(frames, ignore_index=True))
        time.sleep(0.3)
    if new:
        raw = pd.concat(([raw] if len(raw) else []) + new, ignore_index=True)
        raw = raw.drop_duplicates(["date", "var", "contract", "rank"], keep="last")
        raw.to_parquet(CACHE, index=False)
    return raw


# ----------------------------------------------------------------- 计算
def summarize(raw: pd.DataFrame) -> pd.DataFrame:
    """每个 (日期, 品种): 中信与前20合计的多/空持仓及增减, 已对全部合约求和。"""
    x = raw.copy()
    is_l = x["long_name"].str.contains(MEMBER, na=False)
    is_s = x["short_name"].str.contains(MEMBER, na=False)
    x["c_long"], x["c_long_chg"] = x["long_oi"].where(is_l, 0), x["long_chg"].where(is_l, 0)
    x["c_short"], x["c_short_chg"] = x["short_oi"].where(is_s, 0), x["short_chg"].where(is_s, 0)
    g = x.groupby(["date", "var"])[
        ["c_long", "c_short", "c_long_chg", "c_short_chg",
         "long_oi", "short_oi", "long_chg", "short_chg"]].sum()
    g["citic_net"] = g["c_long"] - g["c_short"]
    g["citic_net_chg"] = g["c_long_chg"] - g["c_short_chg"]
    g["top20_net"] = g["long_oi"] - g["short_oi"]
    g["top20_net_chg"] = g["long_chg"] - g["short_chg"]
    return g.reset_index().sort_values(["date", "var"])


METRICS = [("citic_net_chg", "中信净变"), ("citic_net", "中信净持仓"),
           ("top20_net_chg", "前20净变"), ("top20_net", "前20净持仓")]


def to_wide(s: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for key, label in METRICS:
        p = s.pivot(index="date", columns="var", values=key).reindex(columns=list(VARS))
        p.columns = [f"{label}_{VARS[v]}" for v in p.columns]
        p[f"{label}_合计"] = p.sum(axis=1)
        parts.append(p)
    w = pd.concat(parts, axis=1)
    w.index.name = "日期"
    return w


def text_report(s: pd.DataFrame, date: str) -> str:
    d = s[s["date"] == date].set_index("var")
    side = lambda v: "多" if v > 0 else "空"  # noqa: E731
    c, t = d["citic_net_chg"].sum(), d["top20_net_chg"].sum()
    lines = [f"{date[:4]}-{date[4:6]}-{date[6:]}",
             f"中信期货净{side(c)}单 {abs(c):.0f}",
             f"前20机构净{side(t)}单 {abs(t):.0f}",
             "中信期货 净持仓变化："]
    for v, name in VARS.items():
        if v not in d.index:
            continue
        x = d.at[v, "citic_net_chg"]
        lines.append(f"({name})净加{abs(x):.0f}手{side(x)}单；" if x else f"({name})持平；")
    lines[-1] = lines[-1].rstrip("；") + "。"
    return "\n".join(lines)


# ----------------------------------------------------------------- 输出
def _fmt_cell(v) -> str:
    if pd.isna(v):
        return "<td></td>"
    color = UP if v > 0 else DOWN if v < 0 else "inherit"
    return f'<td style="color:{color}">{v:+,.0f}</td>'


def recent_table(wide: pd.DataFrame, n: int = 15) -> str:
    cols = [f"中信净变_{VARS[v]}" for v in VARS] + ["中信净变_合计", "前20净变_合计",
                                                  "中信净持仓_合计", "前20净持仓_合计"]
    heads = [VARS[v] for v in VARS] + ["中信合计", "前20合计", "中信净持仓", "前20净持仓"]
    body = []
    for date, row in wide[cols].tail(n).iloc[::-1].iterrows():
        body.append(f"<tr><td>{date[4:6]}-{date[6:]}</td>" + "".join(_fmt_cell(row[c]) for c in cols) + "</tr>")
    head = "<tr><th>日期</th>" + "".join(f"<th>{h}</th>" for h in heads) + "</tr>"
    return (f'<table><thead><tr><th></th><th colspan="6">中信期货 当日净变(手)</th>'
            f'<th>前20 当日净变</th><th colspan="2">净持仓水平(多−空)</th></tr>{head}</thead>'
            f'<tbody>{"".join(body)}</tbody></table>')


def build_figure(s: pd.DataFrame, wide: pd.DataFrame, window: int = 120) -> go.Figure:
    dates = sorted(s["date"].unique())
    xs = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        subplot_titles=("中信期货 每日净持仓变化(手) 正=净加多 负=净加空",
                                        "中信期货 净持仓(多−空, 手)",
                                        "前20会员 净持仓(多−空, 手)"))
    for v, name in VARS.items():
        sub = s[s["var"] == v].set_index("date").reindex(dates)
        fig.add_bar(x=xs, y=sub["citic_net_chg"], name=name, legendgroup=v,
                    marker_color=COLORS[v], row=1, col=1)
        for row, key in ((2, "citic_net"), (3, "top20_net")):
            fig.add_scatter(x=xs, y=sub[key], name=name, legendgroup=v, showlegend=False,
                            mode="lines", line=dict(color=COLORS[v], width=1.8), row=row, col=1)
    fig.add_scatter(x=xs, y=wide["中信净变_合计"].reindex(dates), name="四品种合计",
                    mode="lines+markers", line=dict(color="#222", width=1.2),
                    marker=dict(size=4), row=1, col=1)
    fig.update_layout(barmode="relative", height=1050, hovermode="x unified",
                      template="plotly_white", margin=dict(l=60, r=30, t=70, b=40),
                      font=dict(family="Microsoft YaHei, PingFang SC, sans-serif", size=12),
                      legend=dict(orientation="h", y=1.06, x=0))
    fig.update_xaxes(type="category", nticks=14)
    if len(xs) > window:
        fig.update_xaxes(range=[len(xs) - window - 0.5, len(xs) - 0.5])
    fig.update_yaxes(zeroline=True, zerolinecolor="#999", tickformat=",")
    return fig


def build_html(s, wide, report, path: Path):
    fig_html = build_figure(s, wide).to_html(full_html=False, include_plotlyjs=EMBED_PLOTLY_JS,
                                             config={"displaylogo": False})
    plotly_script_tag = "" if EMBED_PLOTLY_JS else f'<script src="{PLOTLY_CDN_URL}"></script>'
    css = """
    body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;margin:0;background:#fafaf7;color:#222}
    main{max-width:1180px;margin:0 auto;padding:24px 20px 48px}
    h1{font-size:22px;font-weight:600;margin:0 0 4px}
    .sub{color:#777;font-size:13px;margin-bottom:20px}
    .top{display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start}
    .report{white-space:pre-line;font-size:17px;line-height:1.75;background:#fff;
            border-left:4px solid #b8860b;padding:14px 22px;min-width:280px}
    table{border-collapse:collapse;font-size:13px;background:#fff}
    th,td{padding:4px 10px;text-align:right;border-bottom:1px solid #eee;white-space:nowrap}
    th{background:#f1efe8;font-weight:600}
    td:first-child{text-align:left;color:#555}
    .chart{margin-top:24px;background:#fff}
    @media (max-width:640px){
        main{padding:16px 12px 32px}
        h1{font-size:18px}
        .top{gap:16px}
        .report{font-size:15px;padding:12px 16px;min-width:0;width:100%}
        table{font-size:11px}
        th,td{padding:3px 6px}
    }
    """
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>股指期货 会员净持仓</title><style>{css}</style>{plotly_script_tag}</head><body><main>
<h1>股指期货会员净持仓：中信期货与前20会员</h1>
<div class="sub">数据：中金所成交持仓排名，全部合约合计；更新至 {s['date'].max()}，
生成于 {dt.datetime.now():%Y-%m-%d %H:%M}</div>
<div class="top"><div class="report">{report}</div><div style="overflow-x:auto;max-width:100%">{recent_table(wide)}</div></div>
<div class="chart">{fig_html}</div></main></body></html>"""
    path.write_text(html, encoding="utf-8")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="回填起始日 YYYYMMDD(默认从缓存最后一天续抓)")
    ap.add_argument("--end", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    raw = pd.read_parquet(CACHE) if CACHE.exists() else pd.DataFrame()
    if not a.no_fetch:
        start = a.start or (raw["date"].max() if len(raw) else "20240101")
        raw = update_cache(raw, start, a.end)
    if raw.empty:
        sys.exit("没有数据：检查网络(cffex.com.cn 是否直连)或日期范围")

    s = summarize(raw)
    wide = to_wide(s)
    last = s["date"].max()
    report = text_report(s, last)
    print("\n" + report)

    wide.to_excel(OUT_DIR / "cffex_net_daily.xlsx")
    (OUT_DIR / f"快报_{last}.txt").write_text(report, encoding="utf-8")
    build_html(s, wide, report, OUT_DIR / "cffex_net_position.html")
    print(f"\n输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
