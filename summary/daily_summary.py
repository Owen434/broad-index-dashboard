# -*- coding: utf-8 -*-
"""
每日风险汇总卡片  daily_summary.py
=====================================
把仓库里四条流水线(宽基指数 / ETF 资金流 / 板块基金 / 黄金)的最新结果，
压成一张 PNG + 一份 Markdown + 一份 JSON，作为 GitHub Actions 每日产出的
"一眼看完"入口。

产出(全部写进 ../docs/):
    daily_summary.png    图片版汇总, 带当日日期, 分板块
    daily_summary.md     文字版(README / issue / 推送里直接贴)
    daily_summary.json   机器可读, 给以后的推送脚本用

六个板块:
    ① 宽基指数风险总览      —— 每个宽基指数的过热评分与风险等级
    ② 宽基 T+1 情景矩阵      —— 明天涨跌 X% 之后风险会跳到哪一档
    ③ ETF 分板块资金流风险   —— 只算"合并板块", 按 |净申赎| 分位定风险
    ④ 板块基金风险           —— 按 CSV 里的类型汇总, 只看最新日期
    ⑤ 黄金多周期斜率风险     —— 5/20/30/60 日斜率分位 + 综合评分
    ⑥ 数据健康度             —— 哪条链路没取到数, 免得空表被当成"没风险"

风险口径与 zigzag_signal_analyzer.get_status_action 完全一致(0~100 分):
    <20 🔵冰点  <40 🟦偏冷  <60 🟢正常  <80 🟡偏热  <90 🟠高风险  ≥90 🔴极端风险
分位数(0~1)统一乘 100 后套同一张表, 所以"分位 92%" = "🔴 极端风险"。

依赖: akshare pandas numpy matplotlib pyarrow
注意: 图片里的风险用"彩色圆点 + 中文"而不是 emoji ——
      matplotlib 不渲染彩色 emoji 字体, 直接画 emoji 会变成豆腐块。
      Markdown / JSON 里保留 emoji 原样。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ============================================================ 路径与配置
BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
DIR_ETF = ROOT / "etf"
DIR_FUNDS = ROOT / "funds"
DIR_STOCKS = ROOT / "stocks"
DIR_DOCS = ROOT / "docs"

ETF_PARQUET = DIR_ETF / "etf_flow.parquet"
FUND_NAV = DIR_FUNDS / "fund_nav_history.csv"
FUND_UNIVERSE = DIR_FUNDS / "funds_universe_example.csv"

OUT_PNG = DIR_DOCS / "daily_summary.png"
OUT_MD = DIR_DOCS / "daily_summary.md"
OUT_JSON = DIR_DOCS / "daily_summary.json"
README = ROOT / "README.md"

# README 里被自动改写的那一段的边界。两行标记之间的内容每天整段替换,
# 标记之外的手写内容一个字都不动。第一次跑时如果找不到标记, 会自动
# 插到正文第一个 "## " 标题之前, 并把标记一起写进去。
MARK_BEGIN = "<!-- DAILY_SUMMARY:BEGIN -->"
MARK_END = "<!-- DAILY_SUMMARY:END -->"
INJECT_README = True

# 图片里画几列 T+1 情景。全量 13 列(±6%)横向太宽, 默认只画 ±3%,
# Markdown / JSON 里仍然是全量。
SIM_RETURNS_FULL = [3.0, 2.0, 1.0, 0.0, -1.0, -2.0, -3.0]
SIM_RETURNS_IMG = [3.0, 2.0, 1.0, 0.0, -1.0, -2.0, -3.0]

ANALYSIS_MAX_BARS = 1200      # 指标只取最近 N 根 K 线, 与 stock_analysis_suite 一致
GOLD_WINDOWS = [5, 20, 30, 60]
GOLD_PCT_WINDOW = 252         # 斜率分位的回看窗口(交易日)
ETF_RECENT_DAYS = 5           # ETF 板块除了当日, 再给一个近 N 日累计
FUND_TOP_N = 6                # 图片里最多列几只最热的基金
FETCH_WORKERS = 6

DATA_HEALTH: list[str] = []   # 各链路的失败/缺数记录, 最后画在图上

# ============================================================ 风险等级
# (分数上界, 标签, 颜色, 纯中文标签)
RISK_TIERS = [
    (20.0, "🔵 冰点", "#3B82F6", "冰点"),
    (40.0, "🟦 偏冷", "#60A5FA", "偏冷"),
    (60.0, "🟢 正常", "#22C55E", "正常"),
    (80.0, "🟡 偏热", "#EAB308", "偏热"),
    (90.0, "🟠 高风险", "#F97316", "高风险"),
    (1e9, "🔴 极端风险", "#EF4444", "极端风险"),
]
RISK_NONE = ("⚪ 无数据", "#888888", "无数据")


def risk_from_score(score) -> tuple[str, str, str]:
    """0~100 分 -> (emoji标签, 颜色, 中文标签)"""
    if score is None or (isinstance(score, float) and (np.isnan(score))):
        return RISK_NONE
    for upper, label, color, plain in RISK_TIERS:
        if float(score) < upper:
            return label, color, plain
    return RISK_TIERS[-1][1], RISK_TIERS[-1][2], RISK_TIERS[-1][3]


def risk_from_pct(pct) -> tuple[str, str, str]:
    """分位数 0~1 -> 风险。乘 100 后与评分共用同一张阈值表。"""
    if pct is None or (isinstance(pct, float) and np.isnan(pct)):
        return RISK_NONE
    return risk_from_score(float(pct) * 100.0)


def _f(v, spec="+.2f", suffix="", dash="--"):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return dash
    return format(float(v), spec) + suffix


# ============================================================ 内核加载
def _load_module(alias: str, path: Path):
    """按路径加载脚本。两个内核的主程序都在 __name__ == '__main__' 里,
    import 只拿函数定义, 不会触发它们重新生成 HTML。"""
    if alias in sys.modules:
        return sys.modules[alias]
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:            # 缺依赖时不该拖垮整张卡片
        print(f"  [核心] 加载 {path.name} 失败: {exc}")
        sys.modules.pop(alias, None)
        return None
    return module


CORE = _load_module("zigzag_indicator_core", DIR_FUNDS / "zigzag_signal_analyzer.py")
PM = _load_module("price_movement_core", DIR_STOCKS / "price_movement_patterns.py")


def score_series_from_close(close: pd.Series) -> pd.Series:
    """收盘价/净值序列 -> 过热评分序列(与所有看板同一条链路)。"""
    ind = CORE.compute_eight_indicators(close)
    net_s, int_s, _, _ = CORE.compute_heat_score_series(ind)
    return CORE.calc_score_series(net_s, int_s)


def latest_score(close: pd.Series):
    s = pd.Series(close).dropna()
    if len(s) < CORE.MIN_ROWS:
        return None
    if ANALYSIS_MAX_BARS:
        s = s.tail(ANALYSIS_MAX_BARS)
    sc = score_series_from_close(s)
    v = sc.iloc[-1]
    return None if pd.isna(v) else float(v)


def simulate_next_day(close: pd.Series, pct: float):
    """尾部追加一个模拟收盘价再重算评分, 口径与 stock_analysis_suite 一致。"""
    s = pd.Series(close).dropna()
    if len(s) < CORE.MIN_ROWS:
        return None
    if ANALYSIS_MAX_BARS:
        s = s.tail(ANALYSIS_MAX_BARS)
    new_val = float(s.iloc[-1]) * (1.0 + pct / 100.0)
    last = s.index[-1]
    try:
        new_idx = last + pd.tseries.offsets.BDay(1)
    except Exception:
        new_idx = last + 1
    sim = pd.concat([s, pd.Series([new_val], index=[new_idx])])
    sim = sim[~sim.index.duplicated(keep="last")].sort_index()
    v = score_series_from_close(sim).iloc[-1]
    return None if pd.isna(v) else float(v)


# ============================================================ ① / ② 宽基指数
def fetch_broad_indices() -> dict[str, dict]:
    """返回 {code: {name, group, close(Series)}}。取数复用 price_movement_patterns,
    宽基池也直接用它的 BROAD_INDICES, 免得两处名单对不上。"""
    if PM is None or not hasattr(PM, "BROAD_INDICES"):
        DATA_HEALTH.append("宽基: 未找到 price_movement_patterns.py, 整块跳过")
        return {}

    universe = PM.BROAD_INDICES
    out: dict[str, dict] = {}

    def _one(code: str):
        info = universe[code]
        df = PM.get_stock_data(code, info[0])
        if df is None or df.empty or "Close" not in df.columns:
            return code, None
        close = pd.to_numeric(df["Close"], errors="coerce").dropna()
        return code, {"name": info[0], "group": info[4], "close": close}

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futs = {pool.submit(_one, c): c for c in universe}
        for fut in as_completed(futs):
            try:
                code, payload = fut.result()
            except Exception as exc:
                DATA_HEALTH.append(f"宽基 {futs[fut]} 取数异常: {type(exc).__name__}")
                continue
            if payload is None:
                DATA_HEALTH.append(f"宽基 {futs[fut]} 无行情")
                continue
            out[code] = payload

    # 保持 BROAD_INDICES 里的原始顺序(A股 -> 美股 -> 港股 -> 外盘)
    return {c: out[c] for c in universe if c in out}


def build_broad_section(indices: dict[str, dict]) -> list[dict]:
    rows = []
    for code, d in indices.items():
        close = d["close"]
        score = latest_score(close)
        label, color, plain = risk_from_score(score)
        chg = np.nan
        if len(close) >= 2 and close.iloc[-2]:
            chg = (close.iloc[-1] / close.iloc[-2] - 1) * 100
        sims = {}
        for pct in SIM_RETURNS_FULL:
            sc = simulate_next_day(close, pct)
            sl, sc_color, sc_plain = risk_from_score(sc)
            sims[pct] = {"score": sc, "risk": sl, "color": sc_color, "plain": sc_plain,
                         "delta": None if (sc is None or score is None) else sc - score}
        rows.append({
            "code": code, "name": d["name"], "group": d["group"],
            "date": close.index[-1].strftime("%Y-%m-%d") if len(close) else "",
            "close": float(close.iloc[-1]) if len(close) else None,
            "chg": None if np.isnan(chg) else float(chg),
            "score": score, "risk": label, "color": color, "plain": plain,
            "sims": sims,
        })
    # 风险从高到低, 没数据的沉底
    rows.sort(key=lambda r: (-1e9 if r["score"] is None else r["score"]), reverse=True)
    return rows


# ============================================================ ③ ETF 分板块资金流
def build_etf_section() -> list[dict]:
    """只取"合并板块"(基金代码以 G: 开头的那些行), 按 |净申赎金额| 的滚动分位定风险。

    单只 ETF 的行也在同一个 parquet 里, 这里刻意不看 —— 需求是板块级别的
    资金异动, 单只的拆分/折算噪声在合并口径下已经被平掉一部分。
    """
    if not ETF_PARQUET.exists():
        DATA_HEALTH.append(f"ETF: 未找到 {ETF_PARQUET.name}, 资金流板块跳过")
        return []
    try:
        df = pd.read_parquet(ETF_PARQUET)
    except Exception as exc:
        DATA_HEALTH.append(f"ETF: 读取 parquet 失败({type(exc).__name__})")
        return []

    g = df[df["基金代码"].astype(str).str.startswith("G:")].copy()
    if g.empty:
        DATA_HEALTH.append("ETF: parquet 里没有合并板块行")
        return []
    g["日期"] = pd.to_datetime(g["日期"])
    last = g["日期"].max()
    recent_start = g["日期"].drop_duplicates().sort_values().tail(ETF_RECENT_DAYS).min()

    YI = 1e8
    rows = []
    for name, sub in g.groupby("分组", sort=False):
        sub = sub.sort_values("日期")
        cur = sub[sub["日期"] == last]
        amount = cur["净申赎金额"].iloc[0] if len(cur) else np.nan
        pct = cur["净申赎绝对值分位"].iloc[0] if len(cur) else np.nan
        recent = sub.loc[sub["日期"] >= recent_start, "净申赎金额"].sum(min_count=1)
        label, color, plain = risk_from_pct(pct)
        rows.append({
            "group": name,
            "amount_yi": None if pd.isna(amount) else float(amount) / YI,
            "recent_yi": None if pd.isna(recent) else float(recent) / YI,
            "pct": None if pd.isna(pct) else float(pct),
            "direction": "--" if pd.isna(amount) else ("净申购" if amount > 0 else "净赎回"),
            "risk": label, "color": color, "plain": plain,
        })

    have = sum(1 for r in rows if r["pct"] is not None)
    if have == 0:
        DATA_HEALTH.append("ETF: 所有板块都没有申赎分位(份额链路没取到数)")
    elif have < len(rows):
        DATA_HEALTH.append(f"ETF: {len(rows) - have}/{len(rows)} 个板块缺份额, 仅价格")

    rows.sort(key=lambda r: (-1 if r["pct"] is None else r["pct"]), reverse=True)
    return [{"as_of": last.strftime("%Y-%m-%d"), **r} for r in rows]


# ============================================================ ④ 板块基金
def build_fund_section() -> tuple[list[dict], list[dict]]:
    """返回 (按类型汇总, 单只基金明细)。只看最新日期的评分。"""
    if not (FUND_NAV.exists() and FUND_UNIVERSE.exists()):
        DATA_HEALTH.append("基金: 缺 fund_nav_history.csv 或 funds_universe_example.csv")
        return [], []

    try:
        target = CORE.load_csv_smart(str(FUND_UNIVERSE))
        nav = CORE.load_csv_smart(str(FUND_NAV))
    except Exception as exc:
        DATA_HEALTH.append(f"基金: CSV 读取失败({type(exc).__name__})")
        return [], []

    target.columns = target.columns.str.strip()
    nav.columns = nav.columns.str.strip()
    for d in (target, nav):
        d["基金代码"] = (d["基金代码"].astype(str).str.split(".").str[0]
                       .str.strip().str.zfill(6))
    nav["单位净值"] = pd.to_numeric(
        nav["单位净值"].astype(str).str.replace(",", ""), errors="coerce")
    nav["日期"] = pd.to_datetime(nav["日期"], errors="coerce")
    nav = nav.dropna(subset=["单位净值", "日期"])
    if "类型" not in target.columns:
        target["类型"] = "未分类"
    target["类型"] = target["类型"].fillna("未分类").replace("", "未分类")

    detail = []
    for _, r in target.iterrows():
        code = str(r["基金代码"])
        name = str(r.get("基金名称") or code)
        kind = str(r["类型"])
        sub = nav[nav["基金代码"] == code].sort_values("日期")
        if len(sub) < CORE.MIN_ROWS:
            DATA_HEALTH.append(f"基金 {code} {name}: 净值不足 {CORE.MIN_ROWS} 行")
            continue
        try:
            adj, _ = CORE.build_adjusted_nav(sub, code=code, fund_name=name)
        except Exception:
            adj = pd.Series(sub["单位净值"].values, index=sub["日期"])
        score = latest_score(adj)
        label, color, plain = risk_from_score(score)
        chg = np.nan
        if len(adj) >= 2 and adj.iloc[-2]:
            chg = (adj.iloc[-1] / adj.iloc[-2] - 1) * 100
        detail.append({
            "code": code, "name": name, "type": kind,
            "date": adj.index[-1].strftime("%Y-%m-%d"),
            "score": score, "risk": label, "color": color, "plain": plain,
            "chg": None if np.isnan(chg) else float(chg),
        })

    if not detail:
        DATA_HEALTH.append("基金: 没有一只基金算出评分")
        return [], []

    groups = []
    df = pd.DataFrame(detail)
    for kind, sub in df.groupby("type", sort=False):
        valid = sub["score"].dropna()
        avg = float(valid.mean()) if len(valid) else None
        label, color, plain = risk_from_score(avg)
        hottest = sub.sort_values("score", ascending=False).iloc[0] if len(valid) else None
        groups.append({
            "type": kind, "count": int(len(sub)), "scored": int(len(valid)),
            "avg_score": avg, "risk": label, "color": color, "plain": plain,
            "hot_n": int((sub["score"] >= 80).sum()),
            "cold_n": int((sub["score"] < 40).sum()),
            "hottest": None if hottest is None else f"{hottest['name']}({hottest['score']:.0f})",
        })
    groups.sort(key=lambda g: (-1e9 if g["avg_score"] is None else g["avg_score"]), reverse=True)
    detail.sort(key=lambda d: (-1e9 if d["score"] is None else d["score"]), reverse=True)
    return groups, detail


# ============================================================ ⑤ 黄金
def build_gold_section() -> list[dict]:
    """COMEX 黄金: 各周期均线斜率的当前分位 -> 风险, 外加一个综合评分。"""
    try:
        import akshare as ak
        df = ak.futures_foreign_hist(symbol="GC")
    except Exception as exc:
        DATA_HEALTH.append(f"黄金: 取数失败({type(exc).__name__})")
        return []
    if df is None or df.empty:
        DATA_HEALTH.append("黄金: 接口返回空")
        return []

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    close = pd.to_numeric(df["close"], errors="coerce")
    close.index = df["date"]
    close = close.dropna()

    rows, pcts = [], []
    for w in GOLD_WINDOWS:
        slope = close.rolling(w).mean().diff().dropna()
        if len(slope) < 30:
            rows.append({"window": w, "slope": None, "pct": None,
                         "risk": RISK_NONE[0], "color": RISK_NONE[1], "plain": RISK_NONE[2]})
            continue
        hist = slope.tail(GOLD_PCT_WINDOW)
        cur = float(hist.iloc[-1])
        # 分位 = 当前斜率在回看窗口里的位置, 剔除当前值本身(与 ETF 分位口径一致)
        base = hist.iloc[:-1].dropna()
        pct = float((base < cur).mean()) if len(base) else np.nan
        label, color, plain = risk_from_pct(pct)
        if not np.isnan(pct):
            pcts.append(pct)
        rows.append({"window": w, "slope": cur, "pct": None if np.isnan(pct) else pct,
                     "risk": label, "color": color, "plain": plain,
                     "trend": "上行" if cur > 0 else "下行"})

    if pcts:
        avg = float(np.mean(pcts))
        label, color, plain = risk_from_pct(avg)
        rows.append({"window": "综合", "slope": None, "pct": avg,
                     "risk": label, "color": color, "plain": plain,
                     "trend": "四周期均值"})
    rows.insert(0, {"window": "价格", "slope": None, "pct": None,
                    "risk": "", "color": "#888888", "plain": "",
                    "trend": f"{close.iloc[-1]:,.1f} 美元 · {close.index[-1]:%Y-%m-%d}"})
    return rows


# ============================================================ 绘图
def setup_font() -> str:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, rcParams
    prefer = ["Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK TC",
              "Source Han Sans SC", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei",
              "Microsoft YaHei", "SimHei", "PingFang SC", "Heiti SC",
              "Arial Unicode MS"]
    have = {f.name for f in font_manager.fontManager.ttflist}
    picked = next((p for p in prefer if p in have), None)
    if picked:
        rcParams["font.sans-serif"] = [picked] + rcParams["font.sans-serif"]
    else:
        DATA_HEALTH.append("图片: 系统没有中文字体, 汉字会显示成方块 "
                           "(Actions 里 apt-get install fonts-noto-cjk 可修)")
    rcParams["axes.unicode_minus"] = False
    return picked or "(缺中文字体)"


BG = "#0F1115"
FG = "#E5E7EB"
SUB = "#9CA3AF"
LINE = "#2A2F3A"
CARD = "#161A22"


def _hex_rgba(hex_color: str, alpha: float):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (r, g, b, alpha)


class Canvas:
    """一个"从上往下堆表格"的极简排版器。

    坐标系: x 固定 0~100, y 用"行"为单位、从上往下增长(画的时候取负值)。
    先把所有行攒进 draw 列表, 最后按累计行数一次性开画布 —— 这样图片高度
    随内容自适应, 不会出现大片留白或被裁掉的表格。
    """

    ROW = 1.0
    TITLE_ROW = 1.5
    GAP = 0.6

    def __init__(self):
        self.ops = []
        self.y = 0.0

    # ---- 低层图元
    def _rect(self, x, w, h, **kw):
        self.ops.append(("rect", (x, self.y, w, h), kw))

    def _text(self, x, s, **kw):
        self.ops.append(("text", (x, self.y, s), kw))

    # ---- 高层块
    def header(self, title: str, subtitle: str):
        self.y += 0.4
        self._text(0, title, size=19, color=FG, weight="bold", va="top")
        self.y += 1.25
        self._text(0, subtitle, size=10.5, color=SUB, va="top")
        self.y += 1.1

    def section(self, title: str, note: str = ""):
        self.y += self.GAP
        self._rect(0, 100, 0.06, color=LINE)
        self.y += 0.45
        self._text(0, title, size=13.5, color=FG, weight="bold", va="top")
        if note:
            self._text(100, note, size=9.5, color=SUB, va="top", ha="right")
        self.y += 1.15

    def table(self, cols, rows, header=True):
        """cols: [(标题, 宽度, 对齐)] 宽度合计 = 100
           rows: [[cell, ...]]  cell = 字符串 或 {'text','badge','color','bold'}"""
        xs, acc = [], 0.0
        for _, w, _a in cols:
            xs.append(acc)
            acc += w
        if header:
            for (title, w, align), x in zip(cols, xs):
                self._text(self._anchor(x, w, align), title,
                           size=9.5, color=SUB, ha=align, va="center")
            self.y += 0.85
        for r in rows:
            self._rect(0, 100, self.ROW * 0.92, color=CARD, radius=0.25)
            for cell, (title, w, align), x in zip(r, cols, xs):
                if isinstance(cell, str):
                    cell = {"text": cell}
                text = cell.get("text", "")
                color = cell.get("color", FG)
                if cell.get("badge"):
                    self._badge(x, w, align, text, color)
                else:
                    self._text(self._anchor(x, w, align), text,
                               size=10, color=color, ha=align, va="center",
                               weight="bold" if cell.get("bold") else "normal")
            self.y += self.ROW
        self.y += 0.15

    @staticmethod
    def _anchor(x, w, align):
        return x if align == "left" else (x + w if align == "right" else x + w / 2)

    # 一个 size=10 的中文字大约占 1.05 个 x 单位(按默认 width_in=13 折算),
    # 用来估算徽章宽度、给居中对齐用。差一点不影响观感, 不值得为它去量真实文本框。
    CHAR_W = 1.05
    DOT_GAP = 1.6

    def _badge(self, x, w, align, text, color):
        """彩色圆点 + 中文, 代替 matplotlib 画不出来的彩色 emoji。"""
        span = self.DOT_GAP + len(text) * self.CHAR_W
        if align == "left":
            dot_x = x + 1.8
        elif align == "right":
            dot_x = x + w - span
        else:
            dot_x = self._anchor(x, w, align) - span / 2
        self.ops.append(("dot", (dot_x, self.y), {"color": color}))
        self._text(dot_x + self.DOT_GAP, text, size=10, color=color,
                   ha="left", va="center", weight="bold")

    def footer(self, lines):
        self.y += self.GAP
        self._rect(0, 100, 0.06, color=LINE)
        self.y += 0.5
        for ln in lines:
            self._text(0, ln, size=9, color=SUB, va="center")
            self.y += 0.75

    # ---- 落地
    def render(self, path: Path, width_in=13.0, row_in=0.34):
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch

        height = max(4.0, self.y * row_in + 0.9)
        fig = plt.figure(figsize=(width_in, height), dpi=160)
        fig.patch.set_facecolor(BG)
        ax = fig.add_axes([0.022, 0.015, 0.956, 0.97])
        ax.set_xlim(0, 100)
        ax.set_ylim(self.y, 0)          # y 反向, 从上往下画
        ax.axis("off")
        ax.set_facecolor(BG)

        for kind, geom, kw in self.ops:
            if kind == "rect":
                x, y, w, h = geom
                ax.add_patch(FancyBboxPatch(
                    (x, y - h / 2), w, h,
                    boxstyle=f"round,pad=0,rounding_size={kw.get('radius', 0)}",
                    linewidth=0, facecolor=kw.get("color", CARD), zorder=1))
            elif kind == "text":
                x, y, s = geom
                ax.text(x, y, s, fontsize=kw.get("size", 10),
                        color=kw.get("color", FG), ha=kw.get("ha", "left"),
                        va=kw.get("va", "center"), zorder=3,
                        fontweight=kw.get("weight", "normal"))
            elif kind == "dot":
                # 用 marker 而不是 Circle: marker 的半径以 point 计,
                # 不受 x/y 两轴数据比例影响, 永远是正圆。
                x, y = geom
                ax.plot([x], [y], marker="o", markersize=7.5,
                        color=kw["color"], zorder=3, linestyle="none")
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, facecolor=BG, bbox_inches="tight", pad_inches=0.22)
        plt.close(fig)


# ============================================================ 组装图片
def render_png(payload: dict, font_name: str) -> None:
    c = Canvas()
    d = payload
    c.header(
        f"每日风险汇总 · {d['date']}",
        "宽基指数 / ETF 分板块资金流 / 板块基金 / 黄金 —— "
        f"风险口径: 🔵冰点<20 · 🟦偏冷<40 · 🟢正常<60 · 🟡偏热<80 · 🟠高风险<90 · 🔴极端风险≥90"
        .replace("🔵", "").replace("🟦", "").replace("🟢", "")
        .replace("🟡", "").replace("🟠", "").replace("🔴", ""))

    # ---- ① 宽基风险总览
    broad = d["broad"]
    if broad:
        hot = sum(1 for r in broad if r["score"] is not None and r["score"] >= 80)
        c.section("① 宽基指数风险总览",
                  f"{len(broad)} 个指数 · 其中 {hot} 个已进入高风险及以上")
        cols = [("指数", 22, "left"), ("分类", 13, "left"), ("最新点位", 13, "right"),
                ("涨跌幅", 11, "right"), ("过热评分", 12, "right"), ("风险等级", 29, "left")]
        rows = []
        for r in broad:
            chg = r["chg"]
            chg_color = "#FF6B6B" if (chg or 0) > 0 else ("#4ADE80" if (chg or 0) < 0 else SUB)
            rows.append([
                {"text": r["name"], "bold": True},
                r["group"],
                _f(r["close"], ",.2f"),
                {"text": _f(chg, "+.2f", "%"), "color": chg_color},
                {"text": _f(r["score"], ".0f", " 分"), "color": r["color"], "bold": True},
                {"text": r["plain"], "badge": True, "color": r["color"]},
            ])
        c.table(cols, rows)

        # ---- ② T+1 情景矩阵
        c.section("② 宽基 T+1 情景模拟 · 不同涨跌幅对应的风险",
                  "假设明日收盘涨跌该幅度 → 重算八大指标 → 重算评分")
        sim_w = (100 - 22) / len(SIM_RETURNS_IMG)
        cols = [("指数 \\ 明日涨跌", 22, "left")] + [
            (f"{p:+.0f}%", sim_w, "center") for p in SIM_RETURNS_IMG]
        rows = []
        for r in broad:
            row = [{"text": r["name"], "bold": True}]
            for p in SIM_RETURNS_IMG:
                s = r["sims"].get(p) or r["sims"].get(float(p)) or {}
                row.append({"text": s.get("plain", "--"), "badge": True,
                            "color": s.get("color", SUB)})
            rows.append(row)
        c.table(cols, rows)

    # ---- ③ ETF 分板块资金流
    etf = d["etf"]
    if etf:
        c.section("③ ETF 分板块资金流风险 · 仅合并板块",
                  f"截至 {etf[0]['as_of']} · 风险 = |净申赎金额| 的滚动分位")
        cols = [("板块", 20, "left"), ("方向", 10, "left"), ("当日净申赎", 15, "right"),
                (f"近{ETF_RECENT_DAYS}日累计", 15, "right"), ("绝对值分位", 13, "right"),
                ("风险等级", 27, "left")]
        rows = []
        for r in etf:
            amt = r["amount_yi"]
            amt_color = "#FF6B6B" if (amt or 0) > 0 else ("#4ADE80" if (amt or 0) < 0 else SUB)
            rows.append([
                {"text": r["group"], "bold": True},
                {"text": r["direction"], "color": amt_color},
                {"text": _f(amt, "+,.2f", " 亿"), "color": amt_color},
                _f(r["recent_yi"], "+,.2f", " 亿"),
                _f(None if r["pct"] is None else r["pct"] * 100, ".0f", "%"),
                {"text": r["plain"], "badge": True, "color": r["color"]},
            ])
        c.table(cols, rows)

    # ---- ④ 板块基金
    fg, fd = d["fund_groups"], d["fund_detail"]
    if fg:
        c.section("④ 板块基金风险汇总 · 只看最新日期",
                  f"共 {sum(g['count'] for g in fg)} 只基金")
        cols = [("类型", 20, "left"), ("只数", 10, "right"), ("已评分", 10, "right"),
                ("平均评分", 13, "right"), ("过热/超冷", 15, "right"), ("风险等级", 32, "left")]
        rows = [[
            {"text": g["type"], "bold": True},
            str(g["count"]), str(g["scored"]),
            {"text": _f(g["avg_score"], ".0f", " 分"), "color": g["color"], "bold": True},
            f"{g['hot_n']} / {g['cold_n']}",
            {"text": g["plain"], "badge": True, "color": g["color"]},
        ] for g in fg]
        c.table(cols, rows)

        top = [x for x in fd if x["score"] is not None][:FUND_TOP_N]
        if top:
            c.section("　　最热的几只基金", "按当日过热评分降序")
            cols = [("基金", 30, "left"), ("代码", 12, "left"), ("类型", 12, "left"),
                    ("涨跌幅", 12, "right"), ("评分", 10, "right"), ("风险等级", 24, "left")]
            rows = []
            for x in top:
                chg = x["chg"]
                ccol = "#FF6B6B" if (chg or 0) > 0 else ("#4ADE80" if (chg or 0) < 0 else SUB)
                rows.append([
                    {"text": x["name"], "bold": True}, x["code"], x["type"],
                    {"text": _f(chg, "+.2f", "%"), "color": ccol},
                    {"text": _f(x["score"], ".0f"), "color": x["color"], "bold": True},
                    {"text": x["plain"], "badge": True, "color": x["color"]},
                ])
            c.table(cols, rows)

    # ---- ⑤ 黄金
    gold = d["gold"]
    if gold:
        head = next((g for g in gold if g["window"] == "价格"), None)
        c.section("⑤ COMEX 黄金 · 多周期均线斜率风险",
                  (head["trend"] if head else "") +
                  f" · 分位回看 {GOLD_PCT_WINDOW} 个交易日")
        cols = [("周期", 18, "left"), ("均线斜率", 16, "right"), ("方向", 16, "left"),
                ("斜率分位", 16, "right"), ("风险等级", 34, "left")]
        rows = []
        for g in gold:
            if g["window"] == "价格":
                continue
            w = g["window"]
            rows.append([
                {"text": f"{w} 日斜率" if isinstance(w, int) else w, "bold": True},
                _f(g.get("slope"), "+.3f"),
                {"text": "　" + g.get("trend", "--")},
                _f(None if g["pct"] is None else g["pct"] * 100, ".0f", "%"),
                {"text": g["plain"], "badge": True, "color": g["color"]},
            ])
        c.table(cols, rows)

    # ---- ⑥ 数据健康度
    notes = d["health"][:6]
    foot = [f"字体: {font_name} · 生成时间 {d['generated_at']} · "
            f"数据源 AKShare · 仅供技术交流, 不构成投资建议"]
    if notes:
        foot = [f"⚠ 数据健康度: {n}".replace("⚠", "!") for n in notes] + foot
    c.footer(foot)

    c.render(OUT_PNG)
    print(f"✅ 图片: {OUT_PNG}")


# ============================================================ Markdown / JSON
def build_md_lines(d: dict) -> list[str]:
    L = [f"# 每日风险汇总 · {d['date']}", "",
         # 同目录下的相对路径, GitHub 的 blob 页和 Pages 都认
         f"![每日风险汇总]({OUT_PNG.name})", "",
         "> 风险口径：`🔵 冰点<20 · 🟦 偏冷<40 · 🟢 正常<60 · 🟡 偏热<80 · "
         "🟠 高风险<90 · 🔴 极端风险≥90`，分位数(0~1)乘 100 后套同一张表。", ""]

    if d["broad"]:
        L += ["## ① 宽基指数风险总览", "",
              "| 指数 | 分类 | 最新点位 | 涨跌幅 | 过热评分 | 风险等级 |",
              "|---|---|---:|---:|---:|---|"]
        for r in d["broad"]:
            L.append(f"| {r['name']} | {r['group']} | {_f(r['close'], ',.2f')} | "
                     f"{_f(r['chg'], '+.2f', '%')} | {_f(r['score'], '.0f')} | {r['risk']} |")
        L += ["", "## ② 宽基 T+1 情景模拟（不同涨跌幅对应的风险）", "",
              "| 指数 \\ 明日涨跌 | " + " | ".join(f"{p:+.0f}%" for p in SIM_RETURNS_FULL) + " |",
              "|---|" + "---|" * len(SIM_RETURNS_FULL)]
        for r in d["broad"]:
            cells = []
            for p in SIM_RETURNS_FULL:
                s = r["sims"].get(p) or r["sims"].get(float(p)) or {}
                cells.append(f"{s.get('risk', '--')} {_f(s.get('score'), '.0f')}")
            L.append(f"| {r['name']} | " + " | ".join(cells) + " |")
        L.append("")

    if d["etf"]:
        L += [f"## ③ ETF 分板块资金流风险（仅合并板块，截至 {d['etf'][0]['as_of']}）", "",
              f"| 板块 | 方向 | 当日净申赎(亿元) | 近{ETF_RECENT_DAYS}日(亿元) | "
              "绝对值分位 | 风险等级 |", "|---|---|---:|---:|---:|---|"]
        for r in d["etf"]:
            L.append(f"| {r['group']} | {r['direction']} | {_f(r['amount_yi'], '+,.2f')} | "
                     f"{_f(r['recent_yi'], '+,.2f')} | "
                     f"{_f(None if r['pct'] is None else r['pct'] * 100, '.0f', '%')} | "
                     f"{r['risk']} |")
        L.append("")

    if d["fund_groups"]:
        L += ["## ④ 板块基金风险汇总（只看最新日期）", "",
              "| 类型 | 只数 | 已评分 | 平均评分 | 过热/超冷 | 最热 | 风险等级 |",
              "|---|---:|---:|---:|---:|---|---|"]
        for g in d["fund_groups"]:
            L.append(f"| {g['type']} | {g['count']} | {g['scored']} | "
                     f"{_f(g['avg_score'], '.0f')} | {g['hot_n']} / {g['cold_n']} | "
                     f"{g['hottest'] or '--'} | {g['risk']} |")
        L.append("")

    if d["gold"]:
        head = next((g for g in d["gold"] if g["window"] == "价格"), None)
        L += [f"## ⑤ COMEX 黄金 · 多周期均线斜率风险", "",
              f"最新价：{head['trend'] if head else '--'}", "",
              "| 周期 | 均线斜率 | 方向 | 斜率分位 | 风险等级 |", "|---|---:|---|---:|---|"]
        for g in d["gold"]:
            if g["window"] == "价格":
                continue
            w = g["window"]
            L.append(f"| {f'{w} 日' if isinstance(w, int) else w} | {_f(g.get('slope'), '+.3f')} | "
                     f"{g.get('trend', '--')} | "
                     f"{_f(None if g['pct'] is None else g['pct'] * 100, '.0f', '%')} | "
                     f"{g['risk']} |")
        L.append("")

    if d["health"]:
        L += ["## ⑥ 数据健康度", ""] + [f"- {h}" for h in d["health"]] + [""]

    L += ["---", f"生成时间 {d['generated_at']}　数据源 AKShare。",
          "本页为技术观察，不构成任何投资建议。"]
    return L


def render_md(d: dict) -> list[str]:
    L = build_md_lines(d)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"✅ 文字版: {OUT_MD}")
    return L


def _headline(d: dict) -> list[str]:
    """四条一句话结论, 给 README 折叠区外面那一层看。"""
    out = []
    broad = [r for r in d["broad"] if r["score"] is not None]
    if broad:
        top = broad[0]
        hot = sum(1 for r in broad if r["score"] >= 80)
        out.append(f"- **宽基指数**：{len(broad)} 个，最热 {top['name']} {top['risk']}"
                   f"（{top['score']:.0f} 分）；高风险及以上 {hot} 个")
    etf = [r for r in d["etf"] if r["pct"] is not None]
    if etf:
        t = etf[0]
        out.append(f"- **ETF 资金流**：最拥挤板块 {t['group']}（{t['direction']} "
                   f"{t['amount_yi']:+,.2f} 亿）{t['risk']}，绝对值分位 {t['pct'] * 100:.0f}%")
    fg = [g for g in d["fund_groups"] if g["avg_score"] is not None]
    if fg:
        g = fg[0]
        out.append(f"- **板块基金**：{g['type']} 平均 {g['avg_score']:.0f} 分 {g['risk']}，"
                   f"过热 {g['hot_n']} 只 / 超冷 {g['cold_n']} 只")
    gold = next((g for g in d["gold"] if g["window"] == "综合"), None)
    if gold and gold["pct"] is not None:
        out.append(f"- **COMEX 黄金**：四周期斜率均值分位 {gold['pct'] * 100:.0f}% {gold['risk']}")
    return out


def inject_readme(d: dict, md_lines: list[str]) -> None:
    """把汇总嵌进 README 的两行标记之间。

    README 里只要正文: 标题 + 四条结论 + 完整表格。图片、免责声明、
    "自动重写"那类脚注都留在 docs/daily_summary.md 里, 不往 README 搬 ——
    同一句话在一个页面上出现两遍就是噪音。
    """
    if not INJECT_README or not README.exists():
        return

    drop_prefix = ("# ", "![每日风险汇总]", "生成时间 ", "本页为技术观察")
    body = []
    for ln in md_lines:
        if ln.startswith(drop_prefix):
            continue
        if ln.strip() == "---":          # md 末尾那条分隔线连同页脚一起去掉
            break
        # 折叠区取消了, 但标题仍降一级, 免得和 README 自己的 ## 抢目录层级
        body.append("#" + ln if ln.startswith("## ") else ln)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()

    block = [MARK_BEGIN,
             f"## 📅 今日风险速览 · {d['date']}",
             "",
             *_headline(d),
             "",
             *body,
             MARK_END]
    text = README.read_text(encoding="utf-8")
    new = "\n".join(block)

    if MARK_BEGIN in text and MARK_END in text:
        head, _, rest = text.partition(MARK_BEGIN)
        _, _, tail = rest.partition(MARK_END)
        text = head + new + tail
    else:
        # 第一次跑: 插到正文第一个 "## " 标题之前(也就是居中头图那块之后)
        idx = text.find("\n## ")
        if idx < 0:
            text = text.rstrip() + "\n\n" + new + "\n"
            print("  [README] 没找到 '## ' 标题, 汇总块追加到文末")
        else:
            text = text[:idx + 1] + new + "\n\n" + text[idx + 1:]
        print("  [README] 首次写入, 已插入 DAILY_SUMMARY 标记")
    README.write_text(text, encoding="utf-8")
    print(f"✅ README: {README}")


def render_json(d: dict) -> None:
    payload = json.loads(json.dumps(d, ensure_ascii=False, default=float))
    # sims 的 key 是 float, json 会转成字符串, 这里统一成 "+2%" 更可读
    for r in payload.get("broad", []):
        r["sims"] = {f"{float(k):+.0f}%": v for k, v in r["sims"].items()}
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ JSON: {OUT_JSON}")


# ============================================================ 主流程
def main() -> dict:
    if CORE is None:
        raise SystemExit("错误：未找到 funds/zigzag_signal_analyzer.py（指标内核），无法评分。")

    font_name = setup_font()
    print("=" * 60)
    print("① 宽基指数 ...")
    indices = fetch_broad_indices()
    broad = build_broad_section(indices)
    print(f"   {len(broad)} 个指数")

    print("③ ETF 分板块资金流 ...")
    etf = build_etf_section()
    print(f"   {len(etf)} 个合并板块")

    print("④ 板块基金 ...")
    fund_groups, fund_detail = build_fund_section()
    print(f"   {len(fund_groups)} 个类型 / {len(fund_detail)} 只基金")

    print("⑤ 黄金 ...")
    gold = build_gold_section()
    print(f"   {max(0, len(gold) - 1)} 个周期")

    # 汇总日期取各链路里最新的那个交易日, 而不是运行时的系统日期
    dates = [r["date"] for r in broad if r.get("date")]
    dates += [r["as_of"] for r in etf]
    dates += [x["date"] for x in fund_detail if x.get("date")]
    payload = {
        "date": max(dates) if dates else datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "broad": broad, "etf": etf,
        "fund_groups": fund_groups, "fund_detail": fund_detail,
        "gold": gold, "health": DATA_HEALTH,
    }

    render_png(payload, font_name)
    md_lines = render_md(payload)
    render_json(payload)
    inject_readme(payload, md_lines)
    if DATA_HEALTH:
        print("\n数据健康度提示:")
        for h in DATA_HEALTH:
            print("  - " + h)
    return payload


if __name__ == "__main__":
    main()
