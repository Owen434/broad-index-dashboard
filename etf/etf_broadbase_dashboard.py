# -*- coding: utf-8 -*-
"""
ETF 资金流向监控面板  v3
========================
v3 相对 v2 的修改:

1. 删掉"ETF成交额/成交额分位"和"指数成交额/净申赎占比"两个子图, 连带删掉
   全部指数行情抓取代码(_index_em_kline / _index_em / _index_hist /
   _index_csindex / _normalize_amount / fetch_index_hist)与 ETF 成交额字段。
   少两条链路 = 少两个失败点, 单只 ETF 的网络请求从 4 次降到 3 次。
2. 分位子图去掉"份额变动绝对值分位"(和净申赎绝对值分位共线, 信息冗余),
   改为在 20% / 80% 两个位置画水平虚线, 直接读拥挤/清淡。
3. 新增区间按钮 1月 / 3月 / 6月 / 1年 / 全部, 默认 1年; 切区间时 y 轴按
   窗口内数据重新定标(手动框选缩放也会触发)。
4. 份额统一显示为"亿份", 金额统一显示为"亿元"。
5. 标的池从 8 只宽基扩到 45 只(宽基 + 行业/主题), 见 ETF_UNIVERSE。
6. 新增"分组合并"模式: 同一标的指数的多只 ETF(如创业板 3 只)合并成一条,
   看整条赛道的合计份额与合计净申赎。

v4 相对 v3 的修改(纯前端, 数据链路未动):

1. 区间从 5 个固定按钮改为"快捷下拉 + 日历自定义": 快捷项扩到 1月/3月/6月/
   今年以来/1年/2年/3年/全部, 另有两个 date 输入框任意指定起止; 在图上框选
   缩放会反向写回日历, 三者始终一致。
2. 新增整页截图: 用 SVG foreignObject 把整个面板画到 canvas, 支持下载 PNG 和
   直接复制到剪贴板, 不依赖 html2canvas(离线可用)。
3. 指标卡拆成两行: 第一行保留原有"最新"口径 6 项(日期/份额/当日净申赎/近5日/
   近20日/分位); 第二行是随区间联动的 9 项 —— 区间净申赎、区间净申赎份额、
   日均净申赎、区间份额变化(含百分比)、区间涨跌、净流入天数占比、单日最大
   流入/流出(带日期)、区间平均分位。

渲染方式的变化(重要):
   v2 用 fig.write_html 把 8 只 ETF × 12 条 trace 全塞进图里, 靠 updatemenus
   切 visible。扩到 45 只 + 27 个分组后这么做会有 300+ 条 trace, HTML 几十 MB
   且首屏卡死。v3 改成: Python 只输出一份紧凑 JSON(所有序列对齐到同一条日期
   轴), HTML 里用 Plotly.react 按需重画当前选中的一个标的。

依赖: akshare pandas numpy plotly requests pyarrow
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------- 代理绕行
# 必须在发起任何请求前设置
BYPASS_PROXY_FOR_CN = True

CN_DIRECT_DOMAINS = [
    "eastmoney.com", "sina.com.cn", "sinajs.cn", "sse.com.cn", "szse.cn",
    "cninfo.com.cn", "10jqka.com.cn", "csindex.com.cn", "legulegu.com",
    "hexun.com", "stcn.com", "gtimg.cn", "qq.com", "163.com",
    "localhost", "127.0.0.1",
]

if BYPASS_PROXY_FOR_CN:
    _cur = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    _merged = ",".join([p for p in ([_cur] if _cur else []) + CN_DIRECT_DOMAINS])
    os.environ["NO_PROXY"] = _merged
    os.environ["no_proxy"] = _merged

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

import akshare as ak
import numpy as np
import pandas as pd
import requests

# ============================================================ 配置
# 时间窗口滚动: END = 最新交易日(收盘前跑则退到上一交易日), START = 往前 N 年。
# 由 init_window() 在 main 里赋值, 这里只放占位, 免得别处 import 时报 NameError。
LOOKBACK_YEARS = 5         # 区间按钮里有"1年", 拉取窗口必须 >1 年
CLOSE_HHMM = 1530          # 当日 15:30 之前跑, END 退到上一交易日(当日数据还没出全)
START, END = "", ""
TRADE_DAYS: list[str] = []

CACHE_DIR = Path("cache_etf")
CACHE_DIR.mkdir(exist_ok=True)
OUT_HTML = Path("etf_flow_dashboard.html")
OUT_DATA = Path("etf_flow.parquet")

EMBED_PLOTLY_JS = True     # True=内联 plotly.min.js(约3.5MB, 离线可开); False=CDN
SPLIT_THRESHOLD = 0.5      # 单日份额变动超此比例 -> 判定为份额折算, 净申赎置空
MAX_GAP_DAYS = 7           # 份额序列断档超过该自然日数, 份额变动置空
PCT_WINDOW = 120           # 分位数滚动窗口(交易日)
PCT_MIN = 40               # 分位数最小样本
MAX_WORKERS = 6
SSE_WORKERS = 4            # 上交所份额按日拉, 并发化(单日返回全市场, 与标的数无关)
REQ_SLEEP = 0.20
RETRY_TIMES = 3
RETRY_BASE = 1.0           # 退避基数(秒): 1, 2, 4
MAX_NAV_PAGES = 80         # 净值分页上限, 防接口不返回 TotalCount 时死循环
PX_CACHE_VER = "v4"        # 行情改走新浪全量, 缓存不再按 START_END 命名
NAV_CACHE_VER = "v4"       # v4 起多存一列 日增长率(复权用), 旧缓存作废

YI = 1e8                   # 展示口径: 份额->亿份, 金额->亿元

# 货币基金默认剔除: 两家交易所的 ETF 份额表都不含货币型, 净值接口也没有
# "单位净值走势"(货币基金披露的是每万份收益/七日年化), 三条链路全取不到。
# 而且货币 ETF 的份额跟着保证金进出天天大幅波动, 跟权益资金流向没关系。
INCLUDE_MONEY_FUNDS = False
MONEY_FUND_CODES = {"511860", "016002"}

SZSE_CHUNK = "Q"           # 深市份额分季度拉, 见 fetch_szse() 注释

# ---------------------------------------------------------------- 标的池
# code: (显示名, 分组)
# 分组 = 跟踪的同一标的指数/同一赛道, "分组合并"模式按此聚合。
# 名字里带"缺份额"的是交易所份额接口取不到的, 会只有价格没有申赎;
# 带"观察"的是次新品种, 历史短, 分位数要等样本攒够才有值。
ETF_UNIVERSE: dict[str, tuple[str, str]] = {
    "510330": ("沪深300ETF华夏", "沪深300"),
    "159919": ("沪深300ETF嘉实", "沪深300"),
    "510310": ("沪深300ETF易方达", "沪深300"),
    "510300": ("沪深300ETF华泰柏瑞", "沪深300"),
    "510050": ("上证50ETF华夏", "上证50"),
    "510180": ("上证180ETF华安", "上证180"),
    "510500": ("中证500ETF南方", "中证500"),
    "512500": ("华夏中证500ETF", "中证500"),
    "159922": ("中证500ETF嘉实", "中证500"),
    "515800": ("中证800ETF汇添富", "中证800"),
    "560010": ("中证1000ETF广发", "中证1000"),
    "512100": ("中证1000ETF南方", "中证1000"),
    "159845": ("华夏中证1000ETF", "中证1000"),
    "159629": ("中证1000ETF富国", "中证1000"),
    "159915": ("创业板ETF易方达", "创业板"),
    "159952": ("创业板ETF广发", "创业板"),
    "159977": ("创业板ETF天弘", "创业板"),
    "588080": ("科创50ETF易方达", "科创50"),
    "588050": ("科创50ETF工银", "科创50"),
    "159901": ("深证100ETF易方达", "深证100"),
    "560050": ("汇添富MSCI中国A50互联互通ETF · 缺份额", "MSCI中国A50"),
    "159215": ("A500ETF平安 · 观察", "中证A500"),
    "512080": ("A500ETF中金 · 观察", "中证A500"),
    "159379": ("A500ETF融通 · 观察", "中证A500"),
    "510230": ("国泰上证180金融ETF · 缺份额", "金融地产"),
    "512640": ("嘉实中证金融地产ETF", "金融地产"),
    "159851": ("华宝中证金融科技主题ETF · 缺份额", "金融科技"),
    "516860": ("博时金融科技ETF · 缺份额", "金融科技"),
    "512660": ("国泰中证军工ETF", "军工"),
    "516110": ("国泰中证800汽车与零部件ETF", "汽车"),
    "159995": ("芯片ETF华夏", "芯片半导体"),
    "515790": ("华泰柏瑞中证光伏产业ETF", "光伏"),
    "512010": ("易方达沪深300医药ETF", "医药医疗"),
    "512170": ("医疗ETF华宝", "医药医疗"),
    "512690": ("鹏华中证酒ETF", "食品饮料"),
    "515170": ("食品饮料ETF华夏", "食品饮料"),
    "159865": ("国泰中证畜牧养殖ETF · 缺份额", "畜牧养殖"),
    "159605": ("中概互联ETF广发", "中概互联"), 
    "513050": ("中概互联网ETF易方达", "中概互联"),
    "512400": ("南方中证申万有色金属ETF · 缺份额", "有色金属"),
    "515210": ("国泰中证钢铁ETF · 缺份额", "钢铁")
}

EM_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"
EM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://fundf10.eastmoney.com/",
    "Accept": "*/*",
}

FAILURES: list[str] = []


# ============================================================ 网络工具
def _net(fn: Callable, *args, label: str = "", **kwargs):
    """统一重试包装: 指数退避 RETRY_TIMES 次, 最终失败返回 None 并登记。"""
    last = None
    for i in range(RETRY_TIMES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            if i < RETRY_TIMES - 1:
                time.sleep(RETRY_BASE * (2 ** i))
    msg = f"{label}: {type(last).__name__}: {str(last)[:120]}"
    print(f"  [FAIL] {msg}")
    FAILURES.append(msg)
    return None


def _dash(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _read_cache(path: Path, required: Optional[list[str]] = None) -> Optional[pd.DataFrame]:
    """
    读缓存, 并校验列结构。
    旧版本脚本落下的缓存 schema 可能和当前代码对不上, 这时删掉缓存回源重取,
    而不是让调用方 KeyError。
    """
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        path.unlink(missing_ok=True)
        return None

    if required:
        missing = [c for c in required if c not in df.columns]
        if missing:
            print(f"  [CACHE] {path.name} 缺列 {missing}, 失效重取")
            path.unlink(missing_ok=True)
            return None
    return df


def sweep_stale_cache() -> None:
    """
    清掉旧版命名的缓存。
    v3 之前行情/净值缓存把 START_END 写进了文件名, 窗口一滚动就永远命不中,
    只会在 cache_etf 里越堆越多。
    """
    pats = ["px_*_qfq_*.parquet", "px_*_raw_*.parquet", "nav_*_v3.parquet",
            "nav_*_2*_v*.parquet", "idx_*.parquet"]
    n = 0
    for pat in pats:
        for f in CACHE_DIR.glob(pat):
            f.unlink(missing_ok=True)
            n += 1
    if n:
        print(f"  [CACHE] 清理旧命名缓存 {n} 个")


def _mkt_prefix(code: str) -> str:
    return "sz" if code.startswith(("39", "15", "16", "18", "00", "30")) else "sh"


# ============================================================ 交易日历 / 时间窗口
def init_window() -> list[str]:
    """
    确定滚动窗口。END 取"已收盘的最新交易日":
    今天是交易日但还没到 CLOSE_HHMM 就退到上一交易日 —— 盘中跑的话当日份额
    /净值都还没披露, 硬把当天算进去只会在末端多一根空柱子。
    """
    global START, END, TRADE_DAYS
    cal = _net(ak.tool_trade_date_hist_sina, label="交易日历")
    if cal is None:
        raise RuntimeError("交易日历获取失败, 检查网络/代理设置")
    d = pd.to_datetime(cal["trade_date"]).sort_values()

    now = pd.Timestamp.now()
    cutoff = now.normalize()
    if int(now.strftime("%H%M")) < CLOSE_HHMM:
        cutoff = cutoff - pd.Timedelta(days=1)
    d = d[d <= cutoff]
    if d.empty:
        raise RuntimeError("交易日历为空, 检查系统时间")

    end = d.max()
    start = end - pd.DateOffset(years=LOOKBACK_YEARS)
    days = d[d >= start]

    END = end.strftime("%Y%m%d")
    START = days.min().strftime("%Y%m%d")
    TRADE_DAYS = days.dt.strftime("%Y%m%d").tolist()
    return TRADE_DAYS


def _last_trade_ts() -> pd.Timestamp:
    return pd.to_datetime(END)


def _last_trade_le(ts: pd.Timestamp) -> pd.Timestamp:
    """<= ts 的最后一个交易日。季度末常是周末, 拿它比对缓存新鲜度会永远不命中。"""
    key = ts.strftime("%Y%m%d")
    days = [d for d in TRADE_DAYS if d <= key]
    return pd.to_datetime(days[-1]) if days else ts


# ============================================================ ETF 行情
# 数据源顺序: 新浪 -> 东财净值走势 -> 东财行情。
#
# 为什么把东财 fund_etf_hist_em 从主源降到末位: 它走 push2his, 同一 IP 连打
# 几十次会被直接 RemoteDisconnected(实跑 45 只 × 2 次复权 = 90 请求, 前 4 只
# 成功后全灭, 79 项失败)。新浪一次返回全量历史(不分页、不限日期), 45 只只要
# 45 次请求, 且没观察到限频。
#
# 代价: 新浪只有不复权价。复权在 build_one 里用净值日增长率单独做, 见
# build_adjusted_price() —— 反正净值本来就要拉, 不额外多一次请求。
def _px_sina(code: str) -> pd.DataFrame:
    df = ak.fund_etf_hist_sina(symbol=f"{_mkt_prefix(code)}{code}")
    if df is None or df.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "日期": pd.to_datetime(df["date"], errors="coerce"),
        "收盘价": pd.to_numeric(df["close"], errors="coerce"),
    })
    out["行情来源"] = "新浪"
    return out.dropna(subset=["日期", "收盘价"])


def _px_open_fund(code: str) -> pd.DataFrame:
    """
    场外口径的单位净值走势。给新浪没收录的代码兜底, 拿到的是净值不是市价,
    所以来源要标出来, 免得事后当成成交价用。

    对货币基金无效: 该函数靠正则从东财页面抠 Data_netWorthTrend 这个 JS 变量,
    货币基金页面上根本没有(它披露的是 Data_millionCopiesIncome 每万份收益和
    Data_sevenDaysYearIncome 七日年化), 会抛 ReferenceError。
    """
    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    if df is None or df.empty or "单位净值" not in df.columns:
        return pd.DataFrame()
    out = pd.DataFrame({
        "日期": pd.to_datetime(df["净值日期"], errors="coerce"),
        "收盘价": pd.to_numeric(df["单位净值"], errors="coerce"),
    })
    out["行情来源"] = "东财净值代替"
    return out.dropna(subset=["日期", "收盘价"])


def _px_em_hist(code: str) -> pd.DataFrame:
    df = ak.fund_etf_hist_em(symbol=code, period="daily",
                             start_date=START, end_date=END, adjust="")
    if df is None or df.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "日期": pd.to_datetime(df["日期"], errors="coerce"),
        "收盘价": pd.to_numeric(df["收盘"], errors="coerce"),
    })
    out["行情来源"] = "东财行情"
    return out.dropna(subset=["日期", "收盘价"])


def fetch_price(code: str) -> pd.DataFrame:
    """
    不复权收盘价。缓存按代码存全量历史, 不再把 START_END 写进文件名 ——
    窗口每天滚动, 带日期的缓存名等于每天全部作废重拉。
    命中缓存后只看末端够不够新: 最后一根 K 线 < 最新交易日就回源重取。
    """
    cache = CACHE_DIR / f"px_{code}_{PX_CACHE_VER}.parquet"
    cached = _read_cache(cache, required=["日期", "收盘价", "行情来源"])
    if cached is not None and not cached.empty:
        if pd.to_datetime(cached["日期"]).max() >= _last_trade_ts():
            return _clip(cached)

    for fn, name in ((_px_sina, "新浪"), (_px_open_fund, "东财净值走势"),
                     (_px_em_hist, "东财行情")):
        df = _net(fn, code, label=f"PX {code} {name}")
        if df is not None and not df.empty:
            if fn is not _px_sina:
                print(f"  [PX] {code} 降级 -> {name}")
            df.sort_values("日期").to_parquet(cache, index=False)
            time.sleep(REQ_SLEEP)
            return _clip(df)

    if cached is not None and not cached.empty:   # 全失败但有旧缓存, 用旧的别断图
        print(f"  [PX] {code} 全部源失败, 沿用旧缓存(截至 "
              f"{pd.to_datetime(cached['日期']).max():%Y-%m-%d})")
        return _clip(cached)
    return pd.DataFrame()


def _clip(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["日期"] = pd.to_datetime(d["日期"], errors="coerce")
    d = d[(d["日期"] >= pd.to_datetime(START)) & (d["日期"] <= pd.to_datetime(END))]
    return d.sort_values("日期").reset_index(drop=True)


def build_adjusted_price(px: pd.Series, growth: pd.Series) -> tuple[pd.Series, str]:
    """
    用净值日增长率做前复权。

    日增长率是已扣分红/份额折算影响的真实日收益(东财 F10 口径), 拿它累乘再
    锚定到最后一个有效收盘价上, 得到的就是前复权价: 近端和市价一致, 远端把
    分红跳空抹平。缺增长率的标的直接退回不复权收盘价, 并把口径标出来。
    """
    g = pd.to_numeric(growth, errors="coerce") / 100.0
    ok = g.notna() & np.isfinite(g)
    if ok.sum() < max(20, len(px) * 0.5):
        return px.copy(), "不复权"

    chain = (1 + g.where(ok, 0.0)).cumprod()
    valid = px.notna() & chain.notna() & (chain > 0)
    if not valid.any():
        return px.copy(), "不复权"

    anchor = valid[::-1].idxmax()          # 最后一个同时有收盘价和增长率链的位置
    adj = chain / chain.loc[anchor] * px.loc[anchor]
    return adj.where(px.notna()), "净值日增长率复权"


# ============================================================ 单位净值 + 日增长率
NAV_COLS = ["日期", "单位净值", "日增长率", "净值来源"]


def _nav_from_em_api(code: str, start: str, end: str) -> pd.DataFrame:
    """直连东财 f10/lsjz, 按字段名取值, 不受返回列数变化影响。"""
    rows: list[dict] = []
    page, page_size = 1, 100
    sess = requests.Session()
    while page <= MAX_NAV_PAGES:
        params = {
            "fundCode": code, "pageIndex": page, "pageSize": page_size,
            "startDate": _dash(start), "endDate": _dash(end),
            "_": int(time.time() * 1000),
        }
        r = sess.get(EM_LSJZ_URL, params=params, headers=EM_HEADERS, timeout=12)
        r.raise_for_status()
        js = r.json()
        chunk = ((js.get("Data") or {}).get("LSJZList")) or []
        rows.extend(chunk)
        total = int(js.get("TotalCount") or 0)
        # 用"已取到的实际行数"判停, 不能用 page*page_size:
        # 东财会无视请求的 pageSize, 每页实际只返回 20 条, 按请求值算会在
        # 第 2 页就误判取完(剩下的被收盘价静默填充)
        if not chunk or (total and len(rows) >= total):
            break
        page += 1
        time.sleep(REQ_SLEEP)

    if not rows:
        return pd.DataFrame(columns=NAV_COLS)
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "日期": pd.to_datetime(df["FSRQ"], errors="coerce"),
        "单位净值": pd.to_numeric(df["DWJZ"], errors="coerce"),
        # JZZZL = 净值增长率(%), 复权就靠这一列
        "日增长率": pd.to_numeric(df.get("JZZZL"), errors="coerce"),
    })
    out["净值来源"] = "东财API"
    return out.dropna(subset=["日期", "单位净值"])


def _nav_from_open_fund(code: str, start: str, end: str) -> pd.DataFrame:
    nav = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    if nav is None or nav.empty:
        return pd.DataFrame(columns=NAV_COLS)
    out = pd.DataFrame({
        "日期": pd.to_datetime(nav["净值日期"], errors="coerce"),
        "单位净值": pd.to_numeric(nav["单位净值"], errors="coerce"),
        "日增长率": pd.to_numeric(nav.get("日增长率"), errors="coerce"),
    })
    out["净值来源"] = "东财净值走势"
    return out.dropna(subset=["日期", "单位净值"])


def _nav_from_akshare(code: str, start: str, end: str) -> pd.DataFrame:
    nav = ak.fund_etf_fund_info_em(fund=code, start_date=start, end_date=end)
    if nav is None or nav.empty:
        return pd.DataFrame(columns=NAV_COLS)
    nav = nav.rename(columns={"净值日期": "日期"})
    out = pd.DataFrame({
        "日期": pd.to_datetime(nav["日期"], errors="coerce"),
        "单位净值": pd.to_numeric(nav["单位净值"], errors="coerce"),
        "日增长率": pd.to_numeric(nav.get("日增长率"), errors="coerce"),
    })
    out["净值来源"] = "akshare"
    return out.dropna(subset=["日期", "单位净值"])


def _nav_from_price(code: str, start: str, end: str) -> pd.DataFrame:
    """兜底: 收盘价代替净值。ETF 折溢价通常 <0.5%, 估算可接受, 但没有增长率。"""
    px = fetch_price(code)
    if px.empty:
        return pd.DataFrame(columns=NAV_COLS)
    out = px[["日期", "收盘价"]].rename(columns={"收盘价": "单位净值"}).copy()
    out["日增长率"] = np.nan
    out["净值来源"] = "收盘价代替"
    return out


def fetch_nav_one(code: str) -> pd.DataFrame:
    """
    缓存按代码存, 增量补齐: 命中缓存后只拉 [缓存末日+1, END] 这一段再拼接。
    窗口天天滚动, 全量重拉 45 只 × 25 页纯属浪费。
    """
    cache = CACHE_DIR / f"nav_{code}_{NAV_CACHE_VER}.parquet"
    cached = _read_cache(cache, required=NAV_COLS + ["基金代码"])
    need_start = START

    if cached is not None and not cached.empty:
        cmax = pd.to_datetime(cached["日期"]).max()
        if cmax >= _last_trade_ts():
            return _nav_clip(cached)
        need_start = (cmax + pd.Timedelta(days=1)).strftime("%Y%m%d")

    got = None
    for fn in (_nav_from_em_api, _nav_from_open_fund, _nav_from_akshare, _nav_from_price):
        nav = _net(fn, code, need_start, END, label=f"NAV {code} {fn.__name__}")
        if nav is not None and not nav.empty:
            got = nav.copy()
            got["基金代码"] = code
            if fn is not _nav_from_em_api:
                print(f"  [NAV] {code} 降级 -> {fn.__name__}")
            break

    if got is None and cached is None:
        return pd.DataFrame(columns=NAV_COLS + ["基金代码"])

    parts = [d for d in (cached, got) if d is not None and not d.empty]
    merged = (pd.concat(parts, ignore_index=True)
                .sort_values(["日期"])
                .drop_duplicates("日期", keep="last")
                .reset_index(drop=True))
    merged.to_parquet(cache, index=False)
    return _nav_clip(merged)


def _nav_clip(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["日期"] = pd.to_datetime(d["日期"], errors="coerce")
    return d[(d["日期"] >= pd.to_datetime(START)) & (d["日期"] <= pd.to_datetime(END))]


def fetch_nav(codes: list[str]) -> pd.DataFrame:
    frames = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_nav_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if res is not None and not res.empty:
                frames.append(res)
            print(f"  [NAV] {i}/{len(codes)}", end="\r")
    print()
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ============================================================ 份额(沪深两市)
COL_ALIAS = {
    "统计日期": "日期", "交易日期": "日期", "date": "日期",
    "份额": "基金份额", "基金份额(份)": "基金份额", "当前份额": "基金份额",
    "证券代码": "基金代码", "证券简称": "基金简称",
}


def normalize_scale(df: pd.DataFrame, market: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={k: v for k, v in COL_ALIAS.items() if k in df.columns})
    need = ["日期", "基金代码", "基金份额"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        print(f"  [{market}] 缺少字段 {missing}, 实际列: {list(df.columns)}")
        return pd.DataFrame()
    if "基金简称" not in df.columns:
        df["基金简称"] = pd.NA
    out = df[["日期", "基金代码", "基金简称", "基金份额"]].copy()
    out["日期"] = pd.to_datetime(
        out["日期"].astype(str).str.replace(r"\D", "", regex=True),
        format="%Y%m%d", errors="coerce")
    out["基金代码"] = out["基金代码"].astype(str).str.zfill(6)
    out["基金份额"] = pd.to_numeric(out["基金份额"], errors="coerce")
    out["市场"] = market
    return out.dropna(subset=["日期", "基金份额"])


def fetch_sse_one_day(day: str) -> pd.DataFrame:
    cache = CACHE_DIR / f"sse_{day}.parquet"
    cached = _read_cache(cache)
    if cached is not None:
        return cached
    fn = getattr(ak, "fund_etf_scale_sse", None)
    if fn is None:
        return pd.DataFrame()
    df = _net(fn, label=f"SSE {day}", date=day)
    if df is None or df.empty:
        return pd.DataFrame()
    df.to_parquet(cache, index=False)
    time.sleep(0.3)
    return df


def fetch_sse(trade_days: list[str]) -> pd.DataFrame:
    """
    上交所份额只能按日取(单次返回当日全市场), 两年 ≈ 480 次请求。
    首跑慢, 之后全在 parquet 缓存里, 增量只补新交易日。
    """
    frames = []
    with ThreadPoolExecutor(max_workers=SSE_WORKERS) as pool:
        futures = {pool.submit(fetch_sse_one_day, d): d for d in trade_days}
        for i, fut in enumerate(as_completed(futures), 1):
            f = fut.result()
            if f is not None and not f.empty:
                frames.append(f)
            if i % 20 == 0 or i == len(trade_days):
                print(f"  [SSE] {i}/{len(trade_days)}", end="\r")
    print()
    if not frames:
        return pd.DataFrame()
    return normalize_scale(pd.concat(frames, ignore_index=True), "SH")


def _quarter_chunks(start: str, end: str) -> list[tuple[str, str, str]]:
    """按自然季度切段, 返回 [(标签, 起, 止)]。用固定日历边界而不是滚动切,
    这样已完结季度的缓存键天天不变, 每次只需重拉当前季度。"""
    s, e = pd.to_datetime(start), pd.to_datetime(end)
    out, cur = [], s.to_period("Q")
    while cur.start_time <= e:
        a, b = max(cur.start_time, s), min(cur.end_time.normalize(), e)
        out.append((str(cur), a.strftime("%Y%m%d"), b.strftime("%Y%m%d")))
        cur += 1
    return out


def fetch_szse() -> pd.DataFrame:
    """
    深市 ETF 份额。

    坑: fund_scale_daily_szse 的日期跨度不能超过 6 个月, 超了不是报错, 而是
    返回一个"带表头的空 DataFrame"(akshare 文档字符串里明写)。_net 看不出异常,
    normalize_scale 对空表直接返回, 于是整条深市链路静默贡献 0 行 ——
    表现就是所有 159xxx 都"缺份额", 而沪市 5xxxxx 一只不少。
    v3 把窗口从半年拉到 2 年正好踩中这条线, 所以 v2 能取到、v3 取不到。
    修法: 按自然季度切成 ≤3 个月的段分别请求再拼。
    """
    fn = getattr(ak, "fund_scale_daily_szse", None)
    if fn is None:
        print("  [SZSE] 当前 akshare 版本无 fund_scale_daily_szse")
        return pd.DataFrame()

    frames, empty_chunks = [], []
    for label, a, b in _quarter_chunks(START, END):
        cache = CACHE_DIR / f"szse_ETF_{label}.parquet"
        cached = _read_cache(cache)
        want = _last_trade_le(min(pd.to_datetime(b), _last_trade_ts()))
        if cached is not None and not cached.empty:
            if pd.to_datetime(cached["日期"]).max() >= want:
                frames.append(cached)
                continue

        df = _net(fn, label=f"SZSE {label}", start_date=a, end_date=b, symbol="ETF")
        if df is None or df.empty:
            empty_chunks.append(label)
            if cached is not None and not cached.empty:
                frames.append(cached)
            continue
        df.to_parquet(cache, index=False)
        frames.append(df)
        time.sleep(REQ_SLEEP)

    if empty_chunks:
        print(f"  [SZSE] 空返回段: {', '.join(empty_chunks)} "
              f"(该接口跨度>6个月会静默返回空表, 已按季度切分仍为空则是源头没数据)")
    if not frames:
        print("  [SZSE] 深市份额全部为空, 159xxx 将只有价格")
        return pd.DataFrame()
    return normalize_scale(pd.concat(frames, ignore_index=True), "SZ")


def load_manual_shares() -> pd.DataFrame:
    """可选人工补充份额: shares_manual.csv, 列 = 日期,基金代码,基金份额"""
    p = Path("shares_manual.csv")
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, dtype={"基金代码": str})
    df["日期"] = pd.to_datetime(df["日期"].astype(str).str.replace(r"\D", "", regex=True),
                               format="%Y%m%d", errors="coerce")
    df["基金代码"] = df["基金代码"].str.zfill(6)
    df["基金份额"] = pd.to_numeric(df["基金份额"], errors="coerce")
    df["市场"] = "MANUAL"
    df["基金简称"] = pd.NA
    print(f"  [MANUAL] 载入人工份额 {len(df)} 行")
    return df.dropna(subset=["日期", "基金份额"])


def build_shares(trade_days: list[str], codes: list[str]) -> pd.DataFrame:
    raw = {"SZ": fetch_szse(), "SH": fetch_sse(trade_days), "MANUAL": load_manual_shares()}
    # 分市场行数必须打出来: 某条链路静默返回空表时, 只看合计行数看不出少了谁
    print("  份额来源: " + ", ".join(
        f"{k}={0 if v is None or v.empty else len(v)}行" for k, v in raw.items()))
    parts = [d for d in raw.values() if d is not None and not d.empty]
    if not parts:
        print("  !! 未取到任何份额数据, 申赎类指标将为空")
        return pd.DataFrame(columns=["日期", "基金代码", "基金简称", "基金份额", "市场"])
    df = pd.concat(parts, ignore_index=True)
    df = df[df["基金代码"].isin(codes)]
    order = {"MANUAL": 0, "SH": 1, "SZ": 1}
    df["_pri"] = df["市场"].map(order).fillna(9)
    df = (df.sort_values(["基金代码", "日期", "_pri"])
            .drop_duplicates(["基金代码", "日期"], keep="first")
            .drop(columns="_pri"))
    return df


# ============================================================ 指标计算
def rolling_pct_rank(s: pd.Series, window: int = PCT_WINDOW,
                     min_periods: int = PCT_MIN) -> pd.Series:
    """当前值在过去 window 个样本中的分位(0~1), 剔除当前值本身。"""
    def _f(x: np.ndarray) -> float:
        cur, hist = x[-1], x[:-1]
        hist = hist[~np.isnan(hist)]
        if np.isnan(cur) or hist.size == 0:
            return np.nan
        return float((hist < cur).mean())
    return s.rolling(window, min_periods=min_periods).apply(_f, raw=True)


def build_one(code: str, shares_all: pd.DataFrame, nav_all: pd.DataFrame) -> pd.DataFrame:
    name, group = ETF_UNIVERSE[code]

    px = fetch_price(code)
    if px.empty:
        print(f"  [SKIP] {code} 无行情")
        return pd.DataFrame()

    df = px.copy()
    df["基金代码"], df["基金简称"], df["分组"] = code, name, group

    sh = shares_all[shares_all["基金代码"] == code][["日期", "基金份额"]] \
        if not shares_all.empty else pd.DataFrame()
    df = df.merge(sh, on="日期", how="left") if not sh.empty else df.assign(基金份额=np.nan)

    if not nav_all.empty:
        nv = nav_all[nav_all["基金代码"] == code][["日期", "单位净值", "日增长率", "净值来源"]]
        df = df.merge(nv, on="日期", how="left")
    else:
        df["单位净值"], df["日增长率"], df["净值来源"] = np.nan, np.nan, pd.NA

    # 用收盘价补净值时必须同步改写来源, 否则整列都写着"东财API",
    # 事后完全分不清哪些净申赎是真净值算的、哪些是代理值算的
    filled = df["单位净值"].isna() & df["收盘价"].notna()
    df.loc[filled, "净值来源"] = "收盘价代替"
    df["单位净值"] = df["单位净值"].fillna(df["收盘价"])

    df = df.sort_values("日期").reset_index(drop=True)

    # ---- 复权价: 新浪只给不复权, 用净值日增长率补出前复权口径
    df["复权价"], df["复权方式"] = build_adjusted_price(df["收盘价"], df["日增长率"])

    # ---- 份额变动 / 申赎
    df["申购赎回"] = df["基金份额"].diff()
    df["前日份额"] = df["基金份额"].shift(1)
    df["变动比例"] = df["申购赎回"] / df["前日份额"]

    obs = df["日期"].where(df["基金份额"].notna())
    gap = obs.ffill().diff().dt.days
    df.loc[gap > MAX_GAP_DAYS, ["申购赎回", "变动比例"]] = np.nan

    df["疑似折算"] = df["变动比例"].abs() > SPLIT_THRESHOLD
    df.loc[df["疑似折算"], "申购赎回"] = np.nan

    # ---- 金额口径 + 分位
    df["净申赎金额"] = df["申购赎回"] * df["单位净值"]
    df["净申赎绝对值分位"] = rolling_pct_rank(df["净申赎金额"].abs())

    return df


def build_group(gname: str, members: list[str], data: dict[str, pd.DataFrame],
                dates: pd.DatetimeIndex) -> pd.DataFrame:
    """
    同标的指数的多只 ETF 合并。

    口径说明(有取舍, 别当成精确值用):
    - 合计份额 / 合计净申赎 = 成员按日直接相加, 某成员当日缺数据按 0 处理
      (min_count=1: 只有全部成员都缺才置 NaN)。所以成员上市时间不齐或某只
      当天没报份额时, 合计值会有台阶, 看趋势可以, 看单日绝对额要留个心眼。
    - 价格用"等权收益率链"归一化(各成员日收益取均值再累乘), 而不是价格直接
      平均 —— 后者在成员起始日不同时会跳空。同组成员跟同一指数, 所以这条线
      基本就是该指数的走势。
    """
    def _col(c: str) -> pd.DataFrame:
        return pd.DataFrame({
            m: data[m].set_index("日期")[c].reindex(dates) for m in members
        })

    shares = _col("基金份额").ffill(limit=5).sum(axis=1, min_count=1)
    flow = _col("申购赎回").sum(axis=1, min_count=1)
    amount = _col("净申赎金额").sum(axis=1, min_count=1)

    ret = _col("复权价").pct_change(fill_method=None)
    avg = ret.mean(axis=1)
    price = (1 + avg.fillna(0)).cumprod()
    first = avg.first_valid_index()
    if first is not None:
        price[price.index < first] = np.nan
    else:
        price[:] = np.nan

    out = pd.DataFrame({
        "日期": dates, "复权价": price.values, "基金份额": shares.values,
        "申购赎回": flow.values, "净申赎金额": amount.values,
    })
    out["基金代码"], out["基金简称"], out["分组"] = f"G:{gname}", gname, gname
    out["净申赎绝对值分位"] = rolling_pct_rank(out["净申赎金额"].abs())
    return out


# ============================================================ 打包成 JSON
def _ser(s: pd.Series, scale: float = 1.0, nd: int = 4) -> list:
    """NaN -> None, 顺便做单位换算和四舍五入(直接决定 HTML 体积)。"""
    out = []
    for v in s.to_numpy(dtype="float64", na_value=np.nan):
        out.append(None if (v is None or math.isnan(v)) else round(float(v) / scale, nd))
    return out


def build_payload(data: dict[str, pd.DataFrame],
                  groups: dict[str, pd.DataFrame],
                  dates: pd.DatetimeIndex) -> dict:
    date_str = [d.strftime("%Y-%m-%d") for d in dates]
    items: dict[str, dict] = {}

    def _pack(key: str, name: str, group: str, kind: str,
              df: pd.DataFrame, note: str) -> None:
        d = df.set_index("日期").reindex(dates)
        items[key] = {
            "name": name, "group": group, "kind": kind, "note": note,
            "price": _ser(d["复权价"], nd=4),
            "shares": _ser(d["基金份额"], scale=YI, nd=4),
            "flow": _ser(d["申购赎回"], scale=YI, nd=4),
            "amount": _ser(d["净申赎金额"], scale=YI, nd=4),
            "pct": _ser(d["净申赎绝对值分位"], nd=3),
        }

    for code, df in data.items():
        name, group = ETF_UNIVERSE[code]
        has_flow = df["净申赎金额"].notna().any()
        bits = []
        for col, tag in (("行情来源", "行情"), ("净值来源", "净值"), ("复权方式", "价格口径")):
            if col in df.columns and df[col].notna().any():
                bits.append(tag + ":" + "/".join(sorted(set(df[col].dropna().astype(str)))))
        if not has_flow:
            bits.insert(0, "无份额数据, 仅价格")
        note = " · ".join(bits)
        _pack(code, f"{code} {name}", group, "etf", df, note)

    for gname, df in groups.items():
        n = int(df["_成员数"].iloc[0]) if "_成员数" in df.columns else 0
        _pack(f"G:{gname}", f"{gname} · 合并{n}只", gname, "group", df,
              "同组成员按日相加, 价格为等权归一化")

    return {
        "dates": date_str,
        "items": items,
        "etfOrder": [c for c in ETF_UNIVERSE if c in data],
        "groupOrder": [f"G:{g}" for g in groups],
        "start": date_str[0] if date_str else "",
        "end": date_str[-1] if date_str else "",
    }


# ============================================================ HTML 渲染
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ETF 资金流向监控面板</title>
__PLOTLY__
<style id="appStyle">
  :root{
    --bg:#f6f7f9; --panel:#fff; --line:#e3e6ea; --ink:#1c2430;
    --muted:#6b7684; --up:#d62728; --down:#2ca02c; --accent:#2f6fb0;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.5 "Segoe UI","Microsoft YaHei",-apple-system,sans-serif}
  .wrap{max-width:1420px;margin:0 auto;padding:18px 16px 40px}
  h1{font-size:19px;margin:0 0 2px;font-weight:650;letter-spacing:.3px}
  .sub{color:var(--muted);font-size:12px;margin-bottom:14px}
  .bar{background:var(--panel);border:1px solid var(--line);border-radius:8px;
       padding:12px 14px;display:flex;flex-wrap:wrap;gap:18px;align-items:center}
  .grp{display:flex;align-items:center;gap:8px}
  .lab{color:var(--muted);font-size:12px;white-space:nowrap}
  /* .asctl / .asbtn: 截图时用等价静态元素替换 select/input/button,
     因为表单控件在 foreignObject 里渲染不稳定(常渲染成空白) */
  select,input[type=date],.asctl{font:13px/1.4 inherit;padding:6px 10px;
         border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}
  select#itemSelect{min-width:290px}
  select#presetSel{min-width:112px}
  input[type=date]{min-width:140px;font-variant-numeric:tabular-nums}
  .asctl{display:inline-block;white-space:nowrap}
  .btn{font:13px/1 inherit;padding:7px 12px;border:1px solid var(--line);border-radius:6px;
       background:#fff;color:var(--ink);cursor:pointer;white-space:nowrap}
  .btn:hover{background:#eef2f6}
  .btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
  .btn.primary:hover{filter:brightness(1.08)}
  .btn[disabled]{opacity:.5;cursor:progress}
  .seg{display:flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .seg button,.seg .asbtn{font:13px/1 inherit;padding:7px 13px;border:0;background:#fff;
              color:var(--muted);cursor:pointer;border-right:1px solid var(--line)}
  .seg button:last-child,.seg .asbtn:last-child{border-right:0}
  .seg button:hover{background:#eef2f6}
  .seg button.on,.seg .asbtn.on{background:var(--accent);color:#fff}
  .seg button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
  .sect{color:var(--muted);font-size:11.5px;letter-spacing:.6px;margin:14px 0 -2px}

  .stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); 
  gap: 12px;
  margin: 12px 0 0;}
 .card, .card.wide {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 14px;
  min-width: 0; /* 删掉原有的定宽，全权交给外层网格控制 */}

  .card .k{color:var(--muted);font-size:11px;letter-spacing:.4px}
  .card .v{font-size:17px;font-weight:640;font-variant-numeric:tabular-nums;margin-top:2px}
  .card .s{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums;margin-top:1px}
  .pos{color:var(--up)} .neg{color:var(--down)}
  #note{color:var(--muted);font-size:12px;margin:10px 0 10px}
  #shotTip{color:var(--muted);font-size:12px}
  #chart{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px}
  footer{color:var(--muted);font-size:11px;margin-top:14px;line-height:1.7}
  @media (max-width:720px){ select{min-width:200px} .wrap{padding:12px 8px 30px} }
</style>
</head>
<body>
<div class="wrap">
  <h1>ETF 资金流向监控面板</h1>
  <div class="sub">份额 = 沪深交易所日度披露口径 · 净申赎金额 = 份额变动 × 单位净值 · 区间 __START__ ~ __END__</div>

  <div class="bar">
    <div class="grp">
      <span class="lab">视图</span>
      <div class="seg" id="modeSeg">
        <button data-mode="etf" class="on">单只 ETF</button>
        <button data-mode="group">同类合并</button>
      </div>
    </div>
    <div class="grp">
      <span class="lab">标的</span>
      <select id="itemSelect" aria-label="标的"></select>
    </div>
    <div class="grp">
      <span class="lab">区间</span>
      <select id="presetSel" aria-label="快捷区间">
        <option value="1m">近1月</option>
        <option value="3m">近3月</option>
        <option value="6m">近6月</option>
        <option value="ytd">今年以来</option>
        <option value="1y" selected>近1年</option>
        <option value="2y">近2年</option>
        <option value="3y">近3年</option>
        <option value="all">全部</option>
        <option value="custom">自定义</option>
      </select>
      <input type="date" id="dtFrom" aria-label="起始日期">
      <span class="lab">至</span>
      <input type="date" id="dtTo" aria-label="结束日期">
      <button class="btn" id="btnReset" title="回到近1年">重置</button>
    </div>
    <div class="grp" id="shotGrp">
      <button class="btn primary" id="btnShot">整页截图</button>
      <button class="btn" id="btnCopy">复制图片</button>
      <span id="shotTip"></span>
    </div>
  </div>

  <div class="sect">最新交易日</div>
  <div class="stats" id="stats"></div>
  <div class="sect" id="rangeSect">选定区间</div>
  <div class="stats" id="rstats"></div>
  <div id="note"></div>
  <div id="chart"></div>

  <footer>
    分位 = 当日 |净申赎金额| 在过去 __PCTWIN__ 个交易日中的位置(剔除当日自身), 样本不足 __PCTMIN__ 天不出值;
    上方 80% 虚线以上为异常放量申赎, 20% 虚线以下为清淡。<br>
    单日份额变动超过 __SPLIT__% 判定为份额折算并置空; 份额序列断档超过 __GAP__ 个自然日的首个数据点也置空, 避免把补报当成申赎。<br>
    "选定区间"一行的指标只统计区间内有效交易日: 区间净申赎 = 逐日净申赎金额求和(折算日/断档日不计入),
    区间份额变化 = 区间末有效份额 - 区间首有效份额, 两者会因为剔除折算日而对不上, 属预期。<br>
    区间可用快捷项或日历自定义, 在图上框选缩放同样会回写到日历并刷新区间指标; 双击图表还原到当前日历区间。
  </footer>
</div>

<script>
const DATA = __DATA__;
const GD = document.getElementById('chart');
const SEL = document.getElementById('itemSelect');
const PLOT_H = 980;

let MODE = 'etf';
let KEY  = DATA.etfOrder[0];
let SYNCING = false;

/* 区间状态: PRESET 是快捷项, 选 custom 时以 CUSTOM 的两个端点为准。
   手动框选缩放 / 改日历输入框都会把 PRESET 打成 custom。 */
let PRESET = '1y';
let CUSTOM = {from: null, to: null};

const X = DATA.dates;
/* 全程用 UTC 解析/格式化。混用本地时区会让 toISOString() 在东八区退一天,
   表现为区间按钮的端点整体偏移 1 个交易日 —— 不报错, 但对不齐。 */
const utc = s => new Date(String(s).slice(0, 10) + 'T00:00:00Z');
const ymd = d => d.toISOString().slice(0, 10);
const DT = X.map(utc);

/* ---------- 工具 ---------- */
const fmt = (v, n = 2) => (v === null || v === undefined || isNaN(v))
  ? '—' : v.toLocaleString('zh-CN', {minimumFractionDigits: n, maximumFractionDigits: n});

function barColor(arr){
  return arr.map(v => (v === null || v === undefined) ? 'rgba(0,0,0,0)'
                    : (v >= 0 ? 'rgba(214,39,40,.72)' : 'rgba(44,160,44,.72)'));
}

function lastValid(arr){
  for (let i = arr.length - 1; i >= 0; i--) if (arr[i] !== null) return i;
  return -1;
}

function sumTail(arr, n){
  let s = null, got = 0;
  for (let i = arr.length - 1; i >= 0 && got < n; i--){
    if (arr[i] === null) continue;
    s = (s === null ? 0 : s) + arr[i];
    got++;
  }
  return s;
}

/* 快捷区间 -> [起, 止]; 止点取该标的最后一个有价格的交易日 */
function presetRange(item){
  const li = lastValid(item.price);
  const endIdx = li < 0 ? X.length - 1 : li;
  const end = DT[endIdx], endS = X[endIdx];
  if (PRESET === 'all') return [X[0], endS];
  if (PRESET === 'ytd') return [ymd(new Date(Date.UTC(end.getUTCFullYear(), 0, 1))), endS];
  const m = {'1m':1, '3m':3, '6m':6, '1y':12, '2y':24, '3y':36}[PRESET];
  if (!m) return [X[0], endS];
  const st = new Date(end.getTime());
  st.setUTCMonth(st.getUTCMonth() - m);
  return [ymd(st), endS];
}

/* 当前生效区间。ISO 日期串可直接字典序比较, 不用转 Date */
function activeRange(item){
  let r = (PRESET === 'custom' && CUSTOM.from && CUSTOM.to)
        ? [CUSTOM.from, CUSTOM.to] : presetRange(item);
  let a = r[0], b = r[1];
  if (a > b){ const t = a; a = b; b = t; }
  if (a < X[0]) a = X[0];
  if (b > X[X.length - 1]) b = X[X.length - 1];
  if (a > b){ a = X[0]; b = X[X.length - 1]; }
  return [a, b];
}

/* 日历输入框跟着当前区间走(赋值 .value 不会触发 change, 不会递归) */
function syncPickers(xr){
  const f = document.getElementById('dtFrom'), t = document.getElementById('dtTo');
  f.min = t.min = X[0]; f.max = t.max = X[X.length - 1];
  f.value = xr[0]; t.value = xr[1];
}

function idxOf(dstr, dir){   // dir=1 取 >= 的第一个, dir=-1 取 <= 的最后一个
  const t = utc(dstr).getTime();
  if (dir === 1){ for (let i = 0; i < DT.length; i++) if (DT[i].getTime() >= t) return i; return DT.length - 1; }
  for (let i = DT.length - 1; i >= 0; i--) if (DT[i].getTime() <= t) return i; return 0;
}

/* 窗口内定标: 切区间/框选缩放后 y 轴跟着窗口走, 否则长周期一压全是平线 */
function winRange(arrs, i0, i1, opt){
  opt = opt || {};
  let lo = Infinity, hi = -Infinity;
  arrs.forEach(a => { for (let i = i0; i <= i1; i++){
    const v = a[i]; if (v === null || v === undefined || isNaN(v)) continue;
    if (v < lo) lo = v; if (v > hi) hi = v;
  }});
  if (!isFinite(lo) || !isFinite(hi)) return null;
  if (opt.zero){ lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  let pad = (hi - lo) * (opt.pad === undefined ? 0.08 : opt.pad);
  if (pad === 0) pad = Math.abs(hi) * 0.05 || 1;
  return [lo - pad, hi + pad];
}

/* ---------- 图 ---------- */
const DOM = {r1:[0.775,1.0], r2:[0.525,0.735], r3:[0.275,0.485], r4:[0.0,0.235]};

function rowTitle(text, top){
  return {text, x:0, xref:'paper', y:top + 0.017, yref:'paper',
          xanchor:'left', yanchor:'bottom', showarrow:false,
          font:{size:12.5, color:'#4a5560'}};
}

function traces(item){
  const isG = item.kind === 'group';
  const pName = isG ? '归一化价格(等权)' : '复权价';
  return [
    {type:'scatter', mode:'lines', x:X, y:item.price, name:pName,
     xaxis:'x', yaxis:'y', line:{color:'#2f6fb0', width:1.5}, connectgaps:false,
     hovertemplate:'%{y:.4f}<extra>' + pName + '</extra>'},

    {type:'scatter', mode:'lines', x:X, y:item.shares, name:'份额(亿份)',
     xaxis:'x2', yaxis:'y2', line:{color:'#7d5ba6', width:1.5}, connectgaps:false,
     hovertemplate:'%{y:,.2f} 亿份<extra>份额</extra>'},
    {type:'bar', x:X, y:item.flow, name:'净申赎份额(亿份)',
     xaxis:'x2', yaxis:'y5', marker:{color:barColor(item.flow)},
     hovertemplate:'%{y:,.3f} 亿份<extra>净申赎份额</extra>'},

    {type:'bar', x:X, y:item.amount, name:'净申赎金额(亿元)',
     xaxis:'x3', yaxis:'y3', marker:{color:barColor(item.amount)},
     hovertemplate:'%{y:,.3f} 亿元<extra>净申赎金额</extra>'},

    {type:'scatter', mode:'lines', x:X, y:item.pct, name:'净申赎绝对值分位',
     xaxis:'x4', yaxis:'y4', line:{color:'#d62728', width:1.4}, connectgaps:false,
     hovertemplate:'%{y:.0%}<extra>分位</extra>'}
  ];
}

function layout(item, xr){
  const i0 = idxOf(xr[0], 1), i1 = idxOf(xr[1], -1);
  const padDay = (d, n) => { const t = utc(d); t.setUTCDate(t.getUTCDate() + n); return ymd(t); };
  const xrPad = [padDay(xr[0], -2), padDay(xr[1], 2)];
  const yPrice  = winRange([item.price], i0, i1, {pad:0.06});
  const yShare  = winRange([item.shares], i0, i1, {pad:0.10});
  const yFlow   = winRange([item.flow], i0, i1, {zero:true, pad:0.12});
  const yAmount = winRange([item.amount], i0, i1, {zero:true, pad:0.12});

  // 只给基准 x 轴设 range, x2/x3/x4 用 matches 跟随, 免得两处 range 打架
  const ax = (dom, tick) => ({
    domain:[0,1], anchor:dom, showgrid:true, gridcolor:'rgba(0,0,0,.05)',
    showticklabels:!!tick, showspikes:true, spikemode:'across',
    spikethickness:1, spikecolor:'rgba(0,0,0,.35)', spikedash:'dot',
    type:'date'
  });

  const L = {
    height:PLOT_H, template:'plotly_white', bargap:0.12, barmode:'overlay',
    hovermode:'x unified', showlegend:false,
    margin:{l:66, r:66, t:34, b:38},
    paper_bgcolor:'#fff', plot_bgcolor:'#fff',
    xaxis:  Object.assign(ax('y', false), {range:xrPad}),
    xaxis2: Object.assign(ax('y2', false), {matches:'x'}),
    xaxis3: Object.assign(ax('y3', false), {matches:'x'}),
    xaxis4: Object.assign(ax('y4', true),  {matches:'x'}),

    yaxis: {domain:DOM.r1, gridcolor:'rgba(0,0,0,.05)', tickformat:'.3f',
            range:yPrice || undefined, automargin:true},
    yaxis2:{domain:DOM.r2, gridcolor:'rgba(0,0,0,.05)', tickformat:',.1f',
            title:{text:'亿份', font:{size:11, color:'#8a94a0'}},
            range:yShare || undefined, automargin:true},
    yaxis5:{overlaying:'y2', side:'right', showgrid:false, tickformat:',.2f',
            zeroline:true, zerolinecolor:'rgba(0,0,0,.25)',
            range:yFlow || undefined},
    yaxis3:{domain:DOM.r3, gridcolor:'rgba(0,0,0,.05)', tickformat:',.2f',
            title:{text:'亿元', font:{size:11, color:'#8a94a0'}},
            zeroline:true, zerolinecolor:'rgba(0,0,0,.25)',
            range:yAmount || undefined, automargin:true},
    yaxis4:{domain:DOM.r4, gridcolor:'rgba(0,0,0,.05)', range:[0,1],
            tickformat:'.0%', tickvals:[0,0.2,0.5,0.8,1], automargin:true},

    annotations:[
      rowTitle(item.kind === 'group' ? '归一化价格(组内等权, 首日=1)' : '复权价(净值日增长率口径)', DOM.r1[1]),
      rowTitle('份额(亿份, 左) / 净申赎份额(亿份, 右)', DOM.r2[1]),
      rowTitle('净申赎金额(亿元)', DOM.r3[1]),
      rowTitle('净申赎绝对值分位(120日滚动)', DOM.r4[1])
    ],
    shapes:[
      {type:'line', xref:'paper', x0:0, x1:1, yref:'y4', y0:0.8, y1:0.8,
       line:{color:'rgba(214,39,40,.55)', width:1, dash:'dash'}},
      {type:'line', xref:'paper', x0:0, x1:1, yref:'y4', y0:0.2, y1:0.2,
       line:{color:'rgba(44,160,44,.55)', width:1, dash:'dash'}}
    ]
  };
  return L;
}

/* ---------- 顶部数字 ---------- */
const sign  = v => (v === null || v === undefined) ? '' : (v >= 0 ? 'pos' : 'neg');
const money = v => (v === null || v === undefined) ? '—'
                 : (v >= 0 ? '+' : '') + fmt(v, 2) + ' 亿元';
const cell  = (k, v, cls, sub, wide) =>
  `<div class="card${wide ? ' wide' : ''}"><div class="k">${k}</div>` +
  `<div class="v ${cls || ''}">${v}</div>` +
  (sub ? `<div class="s">${sub}</div>` : '') + `</div>`;

/* 第一行: 与区间无关的"最新"口径, 沿用原有 6 个指标 */
function renderStats(item){
  const li = lastValid(item.amount);
  const ls = lastValid(item.shares);
  const lp = lastValid(item.price);
  const d1 = li < 0 ? null : item.amount[li];
  const d5 = sumTail(item.amount, 5);
  const d20 = sumTail(item.amount, 20);
  const pctv = item.pct[lastValid(item.pct)];

  document.getElementById('stats').innerHTML = [
    cell('最新日期', li >= 0 ? X[li] : (lp >= 0 ? X[lp] : '—')),
    cell(item.kind === 'group' ? '合计份额' : '份额',
         ls < 0 ? '—' : fmt(item.shares[ls], 2) + ' 亿份'),
    cell('当日净申赎', money(d1), sign(d1)),
    cell('近5日累计', money(d5), sign(d5)),
    cell('近20日累计', money(d20), sign(d20)),
    cell('分位', pctv === undefined || pctv === null ? '—' : (pctv * 100).toFixed(0) + '%')
  ].join('');
  document.getElementById('note').textContent = item.note || '';
}

/* 第二行: 全部按当前区间 [i0, i1] 重算, 改日历/框选缩放即刻跟着变 */
function sliceStats(item, i0, i1){
  let amt = 0, amtN = 0, fl = 0, flN = 0, pos = 0, neg = 0;
  let mx = null, mxD = '', mn = null, mnD = '', pctS = 0, pctN = 0;
  const ok = v => v !== null && v !== undefined && !isNaN(v);
  for (let i = i0; i <= i1; i++){
    const a = item.amount[i];
    if (ok(a)){
      amt += a; amtN++;
      if (a > 0) pos++; else if (a < 0) neg++;
      if (mx === null || a > mx){ mx = a; mxD = X[i]; }
      if (mn === null || a < mn){ mn = a; mnD = X[i]; }
    }
    const f = item.flow[i];  if (ok(f)){ fl += f; flN++; }
    const p = item.pct[i];   if (ok(p)){ pctS += p; pctN++; }
  }
  const first = arr => { for (let i = i0; i <= i1; i++) if (ok(arr[i])) return arr[i]; return null; };
  const last  = arr => { for (let i = i1; i >= i0; i--) if (ok(arr[i])) return arr[i]; return null; };
  return {
    amt: amtN ? amt : null, amtN,
    flow: flN ? fl : null,
    avg: amtN ? amt / amtN : null,
    pos, neg, mx, mxD, mn, mnD,
    pctAvg: pctN ? pctS / pctN : null,
    shareA: first(item.shares), shareB: last(item.shares),
    priceA: first(item.price),  priceB: last(item.price),
    days: i1 - i0 + 1
  };
}

function renderRangeStats(item, i0, i1){
  const t = sliceStats(item, i0, i1);
  const dShare = (t.shareA === null || t.shareB === null) ? null : t.shareB - t.shareA;
  const rShare = (dShare === null || !t.shareA) ? null : dShare / t.shareA * 100;
  const rPrice = (t.priceA === null || t.priceB === null || !t.priceA)
               ? null : (t.priceB / t.priceA - 1) * 100;
  const pctStr = v => (v === null) ? '—' : (v >= 0 ? '+' : '') + fmt(v, 2) + '%';
  const flowDays = t.pos + t.neg;

  document.getElementById('rangeSect').textContent =
    `选定区间  ${X[i0]} ~ ${X[i1]}  ·  ${t.days} 个交易日  ·  下列指标随区间联动`;

  document.getElementById('rstats').innerHTML = [
    cell('区间净申赎', money(t.amt), sign(t.amt),
         t.amtN ? `${t.amtN} 天有效` : '无份额数据', true),
    cell('区间净申赎份额',
         t.flow === null ? '—' : (t.flow >= 0 ? '+' : '') + fmt(t.flow, 2) + ' 亿份',
         sign(t.flow)),
    cell('日均净申赎', money(t.avg), sign(t.avg)),
    cell('区间份额变化',
         dShare === null ? '—' : (dShare >= 0 ? '+' : '') + fmt(dShare, 2) + ' 亿份',
         sign(dShare),
         (t.shareA === null || t.shareB === null) ? '' :
           `${fmt(t.shareA, 2)} → ${fmt(t.shareB, 2)}  ${pctStr(rShare)}`, true),
    cell(item.kind === 'group' ? '区间归一化涨跌' : '区间涨跌', pctStr(rPrice), sign(rPrice),
         (t.priceA === null || t.priceB === null) ? '' :
           `${fmt(t.priceA, 3)} → ${fmt(t.priceB, 3)}`, true),
    cell('净流入天数',
         flowDays ? `${t.pos} / ${flowDays}` : '—', t.pos * 2 >= flowDays ? 'pos' : 'neg',
         flowDays ? `占比 ${(t.pos / flowDays * 100).toFixed(0)}% · 流出 ${t.neg} 天` : ''),
    cell('单日最大流入', money(t.mx), 'pos', t.mxD),
    cell('单日最大流出', money(t.mn), 'neg', t.mnD),
    cell('区间平均分位', t.pctAvg === null ? '—' : (t.pctAvg * 100).toFixed(0) + '%')
  ].join('');
}

/* ---------- 渲染 ---------- */
function render(){
  const item = DATA.items[KEY];
  const xr = activeRange(item);
  syncPickers(xr);
  renderStats(item);
  renderRangeStats(item, idxOf(xr[0], 1), idxOf(xr[1], -1));
  Plotly.react(GD, traces(item), layout(item, xr),
               {responsive:true, displaylogo:false,
                modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d']});
}

/* 手动框选缩放后, y 轴按新窗口重新定标 */
function onRelayout(ev){
  if (SYNCING) return;
  const has = ('xaxis.range[0]' in ev) || ('xaxis.range' in ev) || ev['xaxis.autorange'];
  if (!has) return;
  const item = DATA.items[KEY];
  if (ev['xaxis.autorange']){ render(); return; }   // 双击还原 -> 回到当前区间按钮
  const r = GD.layout.xaxis.range;
  const xr = [String(r[0]).slice(0, 10), String(r[1]).slice(0, 10)];
  const i0 = idxOf(xr[0], 1), i1 = idxOf(xr[1], -1);

  // 框选缩放等价于自定义区间: 同步日历输入框与区间指标, 但不重绘(否则会打断缩放)
  PRESET = 'custom';
  CUSTOM = {from: X[i0], to: X[i1]};
  document.getElementById('presetSel').value = 'custom';
  syncPickers([X[i0], X[i1]]);
  renderRangeStats(DATA.items[KEY], i0, i1);

  const up = {};
  const set = (k, v) => { if (v) up[k] = v; };
  set('yaxis.range',  winRange([item.price],  i0, i1, {pad:0.06}));
  set('yaxis2.range', winRange([item.shares], i0, i1, {pad:0.10}));
  set('yaxis5.range', winRange([item.flow],   i0, i1, {zero:true, pad:0.12}));
  set('yaxis3.range', winRange([item.amount], i0, i1, {zero:true, pad:0.12}));
  if (!Object.keys(up).length) return;
  SYNCING = true;
  Plotly.relayout(GD, up).then(() => { SYNCING = false; });
}

/* ---------- 下拉 ---------- */
function fillSelect(){
  SEL.innerHTML = '';
  if (MODE === 'etf'){
    const byGroup = {};
    DATA.etfOrder.forEach(c => {
      const g = DATA.items[c].group;
      (byGroup[g] = byGroup[g] || []).push(c);
    });
    Object.keys(byGroup).forEach(g => {
      const og = document.createElement('optgroup');
      og.label = g;
      byGroup[g].forEach(c => {
        const o = document.createElement('option');
        o.value = c; o.textContent = DATA.items[c].name;
        og.appendChild(o);
      });
      SEL.appendChild(og);
    });
    if (!DATA.items[KEY] || DATA.items[KEY].kind !== 'etf') KEY = DATA.etfOrder[0];
  } else {
    DATA.groupOrder.forEach(k => {
      const o = document.createElement('option');
      o.value = k; o.textContent = DATA.items[k].name;
      SEL.appendChild(o);
    });
    if (!DATA.items[KEY] || DATA.items[KEY].kind !== 'group'){
      const cur = DATA.items[KEY] ? DATA.items[KEY].group : null;
      const hit = cur ? 'G:' + cur : null;
      KEY = (hit && DATA.items[hit]) ? hit : DATA.groupOrder[0];
    }
  }
  SEL.value = KEY;
}

/* ---------- 事件 ---------- */
SEL.addEventListener('change', () => { KEY = SEL.value; render(); });

document.getElementById('modeSeg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  MODE = b.dataset.mode;
  [...e.currentTarget.children].forEach(x => x.classList.toggle('on', x === b));
  fillSelect(); render();
});

document.getElementById('presetSel').addEventListener('change', e => {
  PRESET = e.target.value;
  if (PRESET === 'custom' && !(CUSTOM.from && CUSTOM.to)){
    const xr = presetRange(DATA.items[KEY]);   // 首次切自定义: 用当前窗口填初值
    CUSTOM = {from: xr[0], to: xr[1]};
  }
  render();
});

['dtFrom', 'dtTo'].forEach(id => {
  document.getElementById(id).addEventListener('change', () => {
    const f = document.getElementById('dtFrom').value;
    const t = document.getElementById('dtTo').value;
    if (!f || !t) return;
    CUSTOM = {from: f, to: t};
    PRESET = 'custom';
    document.getElementById('presetSel').value = 'custom';
    render();
  });
});

document.getElementById('btnReset').addEventListener('click', () => {
  PRESET = '1y';
  CUSTOM = {from: null, to: null};
  document.getElementById('presetSel').value = '1y';
  render();
});

/* ---------- 整页截图 ----------
   离线优先: 不引第三方库(html2canvas 要联网), 用 SVG foreignObject 把整个
   .wrap 塞进去再画到 canvas。图表本身先用 Plotly.toImage 转成 PNG 贴回去 ——
   Plotly 的 SVG 直接进 foreignObject 会丢样式。
   表单控件在 foreignObject 里渲染不稳定, 克隆时换成等价的静态 span。 */
const TIP = document.getElementById('shotTip');

function cloneForShot(){
  const clone = document.querySelector('.wrap').cloneNode(true);
  const g = clone.querySelector('#shotGrp'); if (g) g.remove();
  clone.querySelectorAll('select').forEach(el => {
    const real = document.getElementById(el.id);
    const sp = document.createElement('span');
    sp.className = 'asctl';
    sp.textContent = (real && real.selectedIndex >= 0)
                   ? real.options[real.selectedIndex].text : '';
    el.replaceWith(sp);
  });
  clone.querySelectorAll('input').forEach(el => {
    const real = document.getElementById(el.id);
    const sp = document.createElement('span');
    sp.className = 'asctl';
    sp.textContent = real ? real.value : '';
    el.replaceWith(sp);
  });
  clone.querySelectorAll('button').forEach(el => {
    const sp = document.createElement('span');
    sp.className = 'asbtn' + (el.classList.contains('on') ? ' on' : '')
                 + (el.classList.contains('btn') ? ' btn' : '');
    sp.textContent = el.textContent;
    el.replaceWith(sp);
  });
  return clone;
}

async function shotCanvas(){
  const wrap = document.querySelector('.wrap');
  const W = Math.round(wrap.getBoundingClientRect().width);
  const png = await Plotly.toImage(GD, {format:'png',
                                        width: GD.clientWidth || W,
                                        height: PLOT_H, scale: 2});
  const clone = cloneForShot();
  const holder = clone.querySelector('#chart');
  holder.innerHTML = '';
  const im = document.createElement('img');
  im.setAttribute('style', 'width:100%;display:block');
  im.setAttribute('src', png);
  holder.appendChild(im);

  const stage = document.createElement('div');
  stage.style.cssText = 'position:fixed;left:-10000px;top:0;width:' + W + 'px';
  stage.appendChild(clone);
  document.body.appendChild(stage);
  try { await im.decode(); } catch (e) { await new Promise(r => setTimeout(r, 200)); }
  const H = Math.ceil(clone.getBoundingClientRect().height) + 10;
  const body = new XMLSerializer().serializeToString(clone);
  document.body.removeChild(stage);

  const css = document.getElementById('appStyle').textContent;
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '">'
            + '<foreignObject x="0" y="0" width="100%" height="100%">'
            + '<div xmlns="http://www.w3.org/1999/xhtml">'
            + '<style><![CDATA[' + css + ']]></style>' + body + '</div>'
            + '</foreignObject></svg>';

  const scale = 2;
  const cv = document.createElement('canvas');
  cv.width = W * scale; cv.height = H * scale;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = getComputedStyle(document.body).backgroundColor || '#f6f7f9';
  ctx.fillRect(0, 0, cv.width, cv.height);

  const img = new Image();
  await new Promise((res, rej) => {
    img.onload = res;
    img.onerror = () => rej(new Error('foreignObject 渲染失败'));
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  });
  ctx.drawImage(img, 0, 0, cv.width, cv.height);
  return cv;
}

function shotName(){
  const item = DATA.items[KEY];
  const xr = activeRange(item);
  return ('ETF资金流向_' + item.name + '_' + xr[0] + '_' + xr[1] + '.png')
         .replace(/[\\/:*?"<>|\s]/g, '_');
}

async function withBusy(btn, text, fn){
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = text; TIP.textContent = '';
  try {
    await fn();
  } catch (err) {
    TIP.textContent = '截图失败(' + err.message + '), 已改存图表区 PNG';
    try {
      const png = await Plotly.toImage(GD, {format:'png', width: GD.clientWidth,
                                            height: PLOT_H, scale: 2});
      const a = document.createElement('a');
      a.href = png; a.download = shotName(); a.click();
    } catch (e2) { TIP.textContent = '截图失败: ' + err.message; }
  } finally {
    btn.disabled = false; btn.textContent = old;
    setTimeout(() => { TIP.textContent = ''; }, 6000);
  }
}

document.getElementById('btnShot').addEventListener('click', e => {
  withBusy(e.target, '生成中…', async () => {
    const cv = await shotCanvas();
    const blob = await new Promise(r => cv.toBlob(r, 'image/png'));
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = shotName(); a.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    TIP.textContent = '已下载';
  });
});

document.getElementById('btnCopy').addEventListener('click', e => {
  withBusy(e.target, '生成中…', async () => {
    if (!(navigator.clipboard && window.ClipboardItem))
      throw new Error('浏览器不支持剪贴板图片');
    const cv = await shotCanvas();
    const blob = await new Promise(r => cv.toBlob(r, 'image/png'));
    await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
    TIP.textContent = '已复制到剪贴板';
  });
});

fillSelect();
render();
GD.on('plotly_relayout', onRelayout);
</script>
</body>
</html>
"""


def _plotly_script_tag() -> str:
    if EMBED_PLOTLY_JS:
        try:
            from plotly.offline import get_plotlyjs
            return "<script>" + get_plotlyjs() + "</script>"
        except Exception as exc:
            print(f"  [JS] 内联 plotly.js 失败({exc}), 回退 CDN")
    return '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'


def write_html(payload: dict) -> None:
    js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    js = js.replace("</", "<\\/")   # 防止字符串里出现 </script> 截断脚本
    html = (HTML_TEMPLATE
            .replace("__PLOTLY__", _plotly_script_tag())
            .replace("__DATA__", js)
            .replace("__START__", payload["start"])
            .replace("__END__", payload["end"])
            .replace("__PCTWIN__", str(PCT_WINDOW))
            .replace("__PCTMIN__", str(PCT_MIN))
            .replace("__SPLIT__", str(int(SPLIT_THRESHOLD * 100)))
            .replace("__GAP__", str(MAX_GAP_DAYS)))
    OUT_HTML.write_text(html, encoding="utf-8")
    size_mb = OUT_HTML.stat().st_size / 1024 / 1024
    mode = "内联plotly.js(离线可开)" if EMBED_PLOTLY_JS else "CDN(需外网)"
    print(f"\n已输出: {OUT_HTML.resolve()}  [{size_mb:.1f}MB, {mode}]")


# ============================================================ 主流程
def main() -> pd.DataFrame:
    codes = list(ETF_UNIVERSE.keys())
    if not INCLUDE_MONEY_FUNDS:
        drop = [c for c in codes if c in MONEY_FUND_CODES]
        codes = [c for c in codes if c not in MONEY_FUND_CODES]
        if drop:
            print(f"已跳过货币基金 {', '.join(drop)}: 交易所份额表不含货币型, "
                  f"净值接口也无单位净值走势(改 INCLUDE_MONEY_FUNDS=True 可强行保留)")

    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if proxy:
        state = "已绕行(NO_PROXY)" if BYPASS_PROXY_FOR_CN else "未绕行"
        print(f"检测到代理 {proxy} -> 国内数据源{state}")
    sweep_stale_cache()

    trade_days = init_window()
    print(f"窗口滚动至最新交易日: {START} ~ {END} "
          f"({len(trade_days)} 个交易日, 回溯 {LOOKBACK_YEARS} 年), 标的 {len(codes)} 只")

    print("\n--- 份额 ---")
    shares = build_shares(trade_days, codes)
    if not shares.empty:
        print(f"  份额记录 {len(shares)} 行, 覆盖 {shares['基金代码'].nunique()}/{len(codes)} 只")
        miss = [c for c in codes if c not in set(shares["基金代码"])]
        if miss:
            print(f"  无份额(仅价格): {', '.join(miss)}")

    print("\n--- 净值 ---")
    nav = fetch_nav(codes)
    if not nav.empty:
        print(nav.groupby("净值来源").size().to_string())
        cover = nav.groupby("基金代码").size()
        thin = cover[cover < len(trade_days) * 0.8]
        if not thin.empty:
            print(f"  !! 净值覆盖不足(应约 {len(trade_days)} 行/只), 缺口部分将由收盘价代替:")
            print("    " + ", ".join(f"{c}={n}行" for c, n in thin.items()))

    print("\n--- 合成 ---")
    data, frames = {}, []
    for code in codes:
        d = build_one(code, shares, nav)
        if d.empty:
            continue
        data[code] = d
        frames.append(d)

    if not data:
        raise RuntimeError("没有任何 ETF 成功构建, 检查网络/代理与 akshare 版本")

    dates = pd.DatetimeIndex(
        sorted(set(pd.concat([d["日期"] for d in data.values()])))).sort_values()

    # ---- 分组合并
    groups: dict[str, pd.DataFrame] = {}
    seen: list[str] = []
    for code in data:
        g = ETF_UNIVERSE[code][1]
        if g not in seen:
            seen.append(g)
    for g in seen:
        members = [c for c in data if ETF_UNIVERSE[c][1] == g]
        gd = build_group(g, members, data, dates)
        gd["_成员数"] = len(members)
        groups[g] = gd
        frames.append(gd.drop(columns="_成员数"))

    src = pd.concat([d[["行情来源", "复权方式"]] for d in data.values()])
    print(f"  个基 {len(data)} 只, 分组 {len(groups)} 个")
    print("  行情来源: " + ", ".join(f"{k}×{v}" for k, v in
                                  src["行情来源"].value_counts().items()))
    print("  价格口径: " + ", ".join(f"{k}×{v}" for k, v in
                                  src["复权方式"].value_counts().items()))
    for code, d in data.items():
        print(f"  {code} {ETF_UNIVERSE[code][0]}: {len(d)} 行, "
              f"净申赎有效 {int(d['净申赎金额'].notna().sum())}, "
              f"疑似折算 {int(d['疑似折算'].sum())}")

    full = pd.concat(frames, ignore_index=True)
    full.to_parquet(OUT_DATA, index=False)

    write_html(build_payload(data, groups, dates))
    print(f"已输出: {OUT_DATA.resolve()}")

    # ---- 最新交易日横截面(亿元口径)
    last = max(d["日期"].max() for d in data.values())
    snap = pd.concat([d[d["日期"] == last] for d in data.values()], ignore_index=True)
    if not snap.empty:
        view = pd.DataFrame({
            "代码": snap["基金代码"], "简称": snap["基金简称"], "分组": snap["分组"],
            "份额(亿份)": snap["基金份额"] / YI,
            "净申赎(亿份)": snap["申购赎回"] / YI,
            "净申赎(亿元)": snap["净申赎金额"] / YI,
            "分位": snap["净申赎绝对值分位"],
        }).sort_values("净申赎(亿元)", ascending=False)
        print(f"\n最新交易日 {last:%Y-%m-%d} 横截面(按净申赎金额降序):")
        print(view.to_string(index=False, float_format=lambda x: f"{x:,.3f}"))

        gsnap = pd.DataFrame([{
            "分组": g,
            "成员": int(df["_成员数"].iloc[0]),
            "合计份额(亿份)": df.loc[df["日期"] == last, "基金份额"].sum() / YI,
            "合计净申赎(亿元)": df.loc[df["日期"] == last, "净申赎金额"].sum() / YI,
        } for g, df in groups.items()]).sort_values("合计净申赎(亿元)", ascending=False)
        print(f"\n分组合并口径:")
        print(gsnap.to_string(index=False, float_format=lambda x: f"{x:,.3f}"))

    if FAILURES:
        # 按"错误类型"聚合再打印: 限频时同一条报错会刷 79 遍, 淹掉真正的问题
        buckets: dict[str, list[str]] = {}
        for m in FAILURES:
            label, _, err = m.partition(": ")
            buckets.setdefault(err.split("(")[0].strip(), []).append(label)
        print(f"\n本次失败 {len(FAILURES)} 项(成功部分已落缓存, 重跑只补失败的):")
        for err, labels in buckets.items():
            head = ", ".join(labels[:6]) + (f" ...等{len(labels)}项" if len(labels) > 6 else "")
            print(f"  - [{len(labels)}次] {err}\n      {head}")
    return full


if __name__ == "__main__":
    main()