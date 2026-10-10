# -*- coding: utf-8 -*-
"""
drawdown_backtest.py  回撤 + 过热信号回测验证看板（带时间范围与起止日期选择）

放在仓库的 drawdown/ 目录；评分直接复用 ../funds/zigzag_signal_analyzer.py 的内核，
找不到内核时退回简化版评分（仅占位）。

页面功能（全部在浏览器里按所选区间即时重算）：
  - 时间范围：1月 / 3月 / 6月 / 1年 / 全部（以数据最新日为终点，与 zigzag 看板口径一致）
  - 起止日期：下拉日历精确选择，选了日期后按钮自动取消高亮
  - 区间指标：区间涨跌幅、区间最大回撤（峰值以区间起点重新计）、当前回撤、回撤深度分位、水下天数
  - 信号回测：区间内首次进入各评分等级的"独立事件"，其后 5/20/60 日收益与最大浮亏

用法：
  python drawdown_backtest.py --demo
  python drawdown_backtest.py
  python drawdown_backtest.py                  # 默认加载项目全部16个宽基 + 基金清单
  python drawdown_backtest.py --funds-csv ../funds/funds_universe_example.csv
      基金净值优先读 ../funds/fund_nav_history.csv，并先做拆分/份额折算/大比例分红复权（内核 build_adjusted_nav）
  python drawdown_backtest.py --plotly-js inline      # 内嵌 plotly.js，离线可开
"""
import argparse
import ast
import importlib.util
import json
import os
import sys

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

HORIZONS = (5, 20, 60)
RS = 100000  # 前向收益按 1e5 取整存入 JSON，浏览器端除回
# 等级与 zigzag_signal_analyzer.get_status_action 的分界一致：20/40/60/80/90
LEVELS = [
    ("🔵冰点", 0, 20),
    ("🟦偏冷", 20, 40),
    ("🟢正常", 40, 60),
    ("🟡偏热", 60, 80),
    ("🟠高风险", 80, 90),
    ("🔴极端风险", 90, 100.0001),
]
LABELS = [l[0] for l in LEVELS]
HIGH_GROUP = "高风险及以上"
ALL_GROUP = "全样本(基准)"
GROUPS = {l: {l} for l in LABELS}
GROUPS[HIGH_GROUP] = {"🟠高风险", "🔴极端风险"}
GROUPS[ALL_GROUP] = None

INDEX_SYMBOLS = {
    "上证指数": "sh000001", "沪深300": "sh000300", "中证500": "sh000905",
    "创业板指": "sz399006", "科创50": "sh000688",
}


def load_project_broad_catalog(here):
    """读取项目统一维护的全部宽基指数目录，避免本脚本重复维护一份清单。"""
    candidates = [
        os.path.join(here, "..", "stocks", "price_movement_patterns.py"),
        os.path.join(here, "..", "funds", "fund_correlation_report.py"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            # 只解析字面量配置，不导入整个分析脚本，避免因可选绘图库缺失而无法加载目录。
            tree = ast.parse(open(path, "r", encoding="utf-8").read(), filename=path)
            catalog = None
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "BROAD_INDICES" for t in node.targets
                ):
                    catalog = ast.literal_eval(node.value)
                    break
            if catalog:
                return catalog
        except Exception as e:
            print(f"[提示] 读取宽基目录失败 {path}: {e}")
    return {k: [k, "zh", k, "国内", "A股宽基"] for k in INDEX_SYMBOLS}


# ----------------------------------------------------------------- 数据 ---
def load_demo():
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2012-01-02", periods=3500)
    out = {}
    for i, name in enumerate(["演示指数A", "演示指数B", "演示基金C"]):
        r = rng.normal(0.0003, 0.012 + 0.003 * i, len(idx))
        out[name] = pd.Series(100 * np.exp(np.cumsum(r)), index=idx)
    return out


def load_indices(here):
    import akshare as ak
    out = {}
    meta = {}
    catalog = load_project_broad_catalog(here)
    for code, info in catalog.items():
        name, api_type, symbol, region, category = info
        try:
            # 与项目其他脚本保持一致：按接口类型抓取 A 股、美股、港股和全球指数。
            if api_type == "zh":
                df = ak.stock_zh_index_daily(symbol=symbol)
            elif api_type == "us":
                df = ak.index_us_stock_sina(symbol=symbol)
            elif api_type == "hk":
                df = ak.stock_hk_index_daily_sina(symbol=symbol)
            else:
                df = ak.index_global_hist_sina(symbol=symbol)
            date_col = "date" if "date" in df.columns else "日期"
            price_col = "close" if "close" in df.columns else "收盘"
            s = pd.Series(pd.to_numeric(df[price_col], errors="coerce").values,
                          index=pd.to_datetime(df[date_col], errors="coerce"))
            key = f"{name}({code})"
            out[key] = s.sort_index().dropna()
            meta[key] = {"section": "宽基指数", "type": category, "code": code,
                         "region": region}
        except Exception as e:  # 单个失败不影响其余
            print(f"[跳过] {name}({code}): {e}")
    return out, meta


def _read_csv_any(path, **kw):
    for enc in ("utf-8-sig", "gb18030", "gbk", "utf-8"):
        try:
            df = pd.read_csv(path, encoding=enc, **kw)
            df.columns = df.columns.str.strip()
            return df
        except Exception:
            continue
    raise RuntimeError(f"无法读取 {path}")


def _clean_code(x):
    return str(x).split(".")[0].strip().zfill(6)


def fetch_fund_akshare(code):
    """单位净值走势自带『日增长率』，复权校验要用它"""
    import akshare as ak
    h = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    h = h.rename(columns={"净值日期": "日期"})
    h["日期"] = pd.to_datetime(h["日期"], errors="coerce")
    return h.dropna(subset=["日期"])


def adjust_nav(kernel, fd, code, name):
    """净值拆分 / 份额折算 / 大比例分红复权（后复权到最新），复用 zigzag 内核同一套逻辑"""
    if kernel is None:
        print(f"[警告] 未找到内核，{code} 使用未复权净值：拆分/折算会被当成一次暴跌")
        nav = pd.to_numeric(fd["单位净值"].astype(str).str.replace(",", ""), errors="coerce")
        nav.index = fd["日期"]
        return nav.dropna(), []
    return kernel.build_adjusted_nav(fd, code, name)


def load_funds(csv_path, kernel, nav_csv=None):
    """优先读 funds/fund_nav_history.csv（fetch_fund_nav.py 的产出，与其它看板同源），
    没有再走 AKShare；统一做复权后返回 ({名称(代码): 复权净值}, {名称(代码): 复权事件})"""
    tdf = _read_csv_any(csv_path, dtype=str)
    nav_all = None
    if nav_csv and os.path.exists(nav_csv):
        nav_all = _read_csv_any(nav_csv, dtype={"基金代码": str})
        nav_all["基金代码"] = nav_all["基金代码"].map(_clean_code)
        nav_all["日期"] = pd.to_datetime(nav_all["日期"], errors="coerce")
        nav_all = nav_all.dropna(subset=["日期"])
        print(f"净值来源：{nav_csv}")
    else:
        print("未找到 fund_nav_history.csv，改用 AKShare 逐只抓取")
    out, events, meta = {}, {}, {}
    for _, row in tdf.iterrows():
        code = _clean_code(row["基金代码"])
        name = row.get("基金名称", code)
        try:
            if nav_all is not None:
                fd = nav_all[nav_all["基金代码"] == code]
                if fd.empty:
                    print(f"[跳过] {code}: 净值文件中没有该基金")
                    continue
            else:
                fd = fetch_fund_akshare(code)
            fd = fd.drop_duplicates(subset=["日期"], keep="last").sort_values("日期")
            s, ev = adjust_nav(kernel, fd, code, name)
            key = f"{name}({code})"
            out[key], events[key] = s.dropna(), ev
            meta[key] = {"section": "基金", "type": str(row.get("类型", "未分类") or "未分类"),
                         "code": code}
        except Exception as e:
            print(f"[跳过] {code}: {e}")
    return out, events, meta


# ------------------------------------------------------------ 过热评分 ---
def load_kernel():
    """导入 zigzag_signal_analyzer（它有 __main__ 保护，导入不会触发生成流程）"""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "..", "funds"), here):
        if os.path.exists(os.path.join(p, "zigzag_signal_analyzer.py")):
            sys.path.insert(0, p)
            try:
                import zigzag_signal_analyzer as z
                return z
            except Exception as e:
                print(f"[提示] 导入内核失败，改用简化评分：{e}")
    return None


def kernel_heat_score(z, close):
    ind = z.compute_eight_indicators(close)
    net, inten, _, _ = z.compute_heat_score_series(ind)
    return z.calc_score_series(net, inten)


def _roll_pct(s, win=1250, minp=250):
    f = lambda x: (x[:-1] < x[-1]).mean() * 100 if len(x) > 1 else np.nan
    return s.rolling(win, min_periods=minp).apply(f, raw=True)


def fallback_heat_score(close):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    ma, sd = close.rolling(20).mean(), close.rolling(20).std()
    parts = [_roll_pct(x) for x in (rsi, close / ma - 1, (close - (ma - 2 * sd)) / (4 * sd), close.pct_change(20))]
    return pd.concat(parts, axis=1).mean(axis=1)


def to_level(score):
    bins = [l[1] for l in LEVELS] + [LEVELS[-1][2]]
    return pd.cut(score, bins=bins, labels=LABELS, right=False)


# ------------------------------------------------------------ 前向收益 ---
def forward_arrays(close, horizons=HORIZONS):
    c = close.values.astype(float)
    n = len(c)
    ret, mfl = {}, {}
    for h in horizons:
        r = np.full(n, np.nan)
        m = np.full(n, np.nan)
        if n > h:
            r[: n - h] = c[h:] / c[: n - h] - 1
            w = sliding_window_view(c, h + 1)[:, 1:]
            m[: n - h] = w.min(axis=1) / c[: n - h] - 1  # 持有期内最大浮亏
        ret[h], mfl[h] = r, m
    return ret, mfl


def entry_indices(lv, members, cooldown):
    """首次进入该等级(组)的日子（在全历史上判定）；cooldown 内重复进入合并"""
    inn = np.array([x in members for x in lv])
    start = inn & np.r_[True, ~inn[:-1]]
    keep, last = [], -10 ** 9
    for i in np.where(start)[0]:
        if i - last >= cooldown:
            keep.append(int(i))
            last = i
    return keep


def _ints(a, scale):
    return [None if pd.isna(v) else int(round(v * scale)) for v in a]


def pack(close, score, level, cooldown, adj_events=None, meta=None):
    """压成紧凑 JSON：收益×1e5、评分×10 取整，浏览器端再还原"""
    ret, mfl = forward_arrays(close)
    lv = level.astype(object).values
    codes = [LABELS.index(x) if x in LABELS else -1 for x in lv]
    ev = {g: entry_indices(lv, m, cooldown) for g, m in GROUPS.items() if m is not None}
    return {
        "d": [d.strftime("%Y-%m-%d") for d in close.index],
        "c": [round(float(x), 4) for x in close.values],
        "s": _ints(score.values, 10),
        "l": codes,
        "r": {str(h): _ints(ret[h], RS) for h in HORIZONS},
        "m": {str(h): _ints(mfl[h], RS) for h in HORIZONS},
        "ev": ev,
        "adj": [{"date": e["date"], "kind": e["kind"], "ratio": e.get("ratio")} for e in (adj_events or [])],
        "section": (meta or {}).get("section", "未分类"),
        "type": (meta or {}).get("type", "未分类"),
        "code": (meta or {}).get("code", ""),
    }


# ---------------------------------------------------------------- 页面 ---
CSS = r"""
:root{--bg:#161616;--panel:#1e1e1e;--panel2:#252525;--border:#2a2a2a;--border2:#333;--text:#fff;
--dim:#888;--blue:#61AFEF;--gold:#E5C07B;--red:#FF3333;--green:#00CC00}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.app{max-width:1220px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:20px;margin:0 0 4px;color:var(--gold)}
.sub{font-size:12.5px;color:var(--dim);margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--border2);border-radius:12px;padding:18px 20px;
margin-bottom:18px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
.card h2{font-size:15px;margin:0 0 10px;color:var(--gold)}
.ctrl{display:flex;flex-wrap:wrap;gap:16px;align-items:center}
.ctrl label{font-size:13px;color:var(--dim)}
select,input[type=date]{background:var(--panel2);color:var(--text);border:1px solid var(--border2);
border-radius:6px;padding:6px 10px;font-size:13.5px;color-scheme:dark}
select:focus,input:focus{outline:2px solid var(--blue);outline-offset:1px}
.tgroup{display:flex;gap:4px;background:#2a2a2a;padding:3px;border-radius:6px}
.tbtn{background:transparent;border:none;color:#aaa;padding:5px 12px;font-size:12.5px;border-radius:4px;cursor:pointer}
.tbtn:hover,.tbtn.active{background:var(--blue);color:#fff;font-weight:600}
.dates{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--dim)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.m{background:var(--panel2);border:1px solid var(--border2);border-radius:8px;padding:10px 12px}
.m .k{font-size:11.5px;color:var(--dim)} .m .v{font-size:18px;font-weight:700;margin-top:3px}
.m .s{font-size:11px;color:var(--dim);margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border:1px solid var(--border2);padding:5px 8px;text-align:right}
th{background:var(--panel2);color:var(--dim);font-weight:600}
td:first-child,th:first-child{text-align:left}
tr.base td{color:var(--dim)} td.few{color:#e5a03b}
tr.pick td{background:rgba(97,175,239,.10)} tr.clk{cursor:pointer} tr.clk:hover td{background:#262c33}
.up{color:var(--red)} .dn{color:var(--green)}
.note{font-size:12px;color:var(--dim);line-height:1.7;margin:8px 0 0}
#msg{display:none;background:#3a2a10;border-left:4px solid #e5a03b;padding:8px 12px;margin:10px 0;font-size:13px}
.scroll{overflow-x:auto}
"""

JS_ANALYTICS = r"""
// ===== ANALYTICS（纯函数，无 DOM） =====
const HORIZONS=[5,20,60];
const RS=__RS__;
const GROUP_NAMES=LABELS.concat([HIGH,ALL]);
function lowerBound(d,s){let lo=0,hi=d.length;while(lo<hi){const m=(lo+hi)>>1;if(d[m]<s)lo=m+1;else hi=m;}return lo;}
function upperIdx(d,s){let lo=0,hi=d.length;while(lo<hi){const m=(lo+hi)>>1;if(d[m]<=s)lo=m+1;else hi=m;}return lo-1;}
function addDays(s,n){const t=new Date(s+'T00:00:00Z');t.setUTCDate(t.getUTCDate()+n);return t.toISOString().slice(0,10);}
function pctile(arr,q){const a=arr.slice().sort(function(x,y){return x-y;});const n=a.length;if(!n)return null;
  const pos=(n-1)*q/100,lo=Math.floor(pos),hi=Math.ceil(pos);return a[lo]+(a[hi]-a[lo])*(pos-lo);}
function mean(a){let s=0;for(let i=0;i<a.length;i++)s+=a[i];return s/a.length;}

// 区间回撤：峰值以区间起点 i0 重新计
function ddStats(t,i0,i1){
  let peak=-Infinity,mdd=0,mddI=i0,peakI=i0;const dd=new Array(i1-i0+1);
  for(let i=i0;i<=i1;i++){const c=t.c[i];if(c>=peak){peak=c;peakI=i;}
    const x=c/peak-1;dd[i-i0]=x;if(x<mdd){mdd=x;mddI=i;}}
  const cur=dd[dd.length-1];let deeper=0;for(let k=0;k<dd.length;k++)if(dd[k]>cur)deeper++;
  return {dd:dd,mdd:mdd,mddI:mddI,cur:cur,pct:deeper/dd.length*100,uw:i1-peakI,ret:t.c[i1]/t.c[i0]-1};
}
function episodes(t,i0,i1){
  const eps=[];let peak=t.c[i0],peakI=i0,troughI=i0,trough=0;
  for(let i=i0+1;i<=i1;i++){const c=t.c[i];
    if(c>=peak){if(trough<0)eps.push({p:peakI,t:troughI,r:i,depth:trough});peak=c;peakI=i;troughI=i;trough=0;}
    else{const x=c/peak-1;if(x<trough){trough=x;troughI=i;}}}
  if(trough<0)eps.push({p:peakI,t:troughI,r:null,depth:trough});
  return eps;
}
// 区间内：首次进入各等级的独立事件（事件在全历史上判定，区间只做筛选）
function stats(t,i0,i1){
  const rows=[];
  for(const g of GROUP_NAMES){
    let idx;
    if(g===ALL){idx=[];for(let i=i0;i<=i1;i++)idx.push(i);}
    else idx=(t.ev[g]||[]).filter(function(i){return i>=i0&&i<=i1;});
    for(const h of HORIZONS){
      const r=[],m=[];
      for(const i of idx){const a=t.r[h][i];if(a!==null){r.push(a/RS);const b=t.m[h][i];if(b!==null)m.push(b/RS);}}
      if(!r.length)continue;
      let w=0;for(const x of r)if(x>0)w++;
      rows.push({g:g,h:h,n:r.length,mean:mean(r),med:pctile(r,50),win:w/r.length,p10:pctile(r,10),mfl:m.length?mean(m):null});
    }
  }
  return rows;
}
"""

JS_UI = r"""
// ===== UI =====
const names=Object.keys(DATA);
const sections=[...new Set(names.map(function(n){return DATA[n].section||'未分类';}))];
let FIRST=null,LAST=null;
names.forEach(function(n){const d=DATA[n].d;if(FIRST===null||d[0]<FIRST)FIRST=d[0];if(LAST===null||d[d.length-1]>LAST)LAST=d[d.length-1];});
const WIN={'1M':30,'3M':90,'6M':180,'1Y':365};
const COLORS=['#3B82F6','#60A5FA','#22C55E','#EAB308','#F97316','#EF4444'];
let cur=names[0],win='1Y',start=null,end=null,section='全部',typeName='全部';
const $=function(id){return document.getElementById(id);};
function filteredNames(){return names.filter(function(n){const t=DATA[n];return (section==='全部'||t.section===section)&&(typeName==='全部'||t.type===typeName);});}
function syncSelectors(){
  const oldSection=section, oldType=typeName;
  $('section').innerHTML=['全部'].concat(sections).map(function(x){return '<option value="'+x+'">'+x+'</option>';}).join('');
  $('section').value=sections.includes(oldSection)?oldSection:'全部'; section=$('section').value;
  const types=[...new Set(names.filter(function(n){return section==='全部'||DATA[n].section===section;}).map(function(n){return DATA[n].type||'未分类';}))].sort();
  $('type').innerHTML=['全部'].concat(types).map(function(x){return '<option value="'+x+'">'+x+'</option>';}).join('');
  $('type').value=types.includes(oldType)?oldType:'全部'; typeName=$('type').value;
  const ns=filteredNames();
  $('pick').innerHTML=ns.map(function(n){return '<option value="'+n+'">'+n+'</option>';}).join('');
  if(!ns.includes(cur))cur=ns[0]||names[0];
  $('pick').value=cur;
}
function fp(x,d){return(x===null||x===undefined||isNaN(x))?'-':(x*100).toFixed(d===undefined?1:d)+'%';}
function cls(x){return x>0?'up':(x<0?'dn':'');}

function syncInputs(){$('d0').value=start;$('d1').value=end;}
function setBtns(){document.querySelectorAll('.tbtn').forEach(function(b){b.classList.toggle('active',b.dataset.win===win);});}
function preset(w){
  win=w;end=LAST;
  if(w==='ALL')start=FIRST;else{const s=addDays(LAST,-WIN[w]);start=s<FIRST?FIRST:s;}
  syncInputs();setBtns();render();
}
function clamp(s){return s<FIRST?FIRST:(s>LAST?LAST:s);}
function onDate(){
  if(!$('d0').value||!$('d1').value)return;
  let a=clamp($('d0').value),b=clamp($('d1').value);
  if(a>b){const x=a;a=b;b=x;}
  start=a;end=b;win='CUSTOM';syncInputs();setBtns();render();
}

function rowFor(name){
  const t=DATA[name],i0=lowerBound(t.d,start),i1=upperIdx(t.d,end);
  if(i1-i0<1)return null;
  const s=ddStats(t,i0,i1),st=stats(t,i0,i1);
  const hr=st.filter(function(r){return r.g===HIGH&&r.h===20;})[0];
  return {name:name,i1:i1,s:s,hr:hr,
    score:t.s[i1]===null?null:t.s[i1]/10,lvl:t.l[i1]>=0?LABELS[t.l[i1]]:'-'};
}
function renderSummary(){
  let h='<table><tr><th>标的</th><th>区间末日</th><th>区间涨跌</th><th>区间最大回撤</th><th>当前回撤</th>'+
        '<th>回撤深度分位</th><th>水下天数</th><th>末日评分</th><th>高风险及以上后20日·中位收益</th><th>·平均最大浮亏</th><th>·样本数</th></tr>';
  filteredNames().forEach(function(n){
    const r=rowFor(n);
    if(!r){h+='<tr class="clk" data-n="'+n+'"><td>'+n+'</td><td colspan="10" style="text-align:left;color:#888">区间内数据不足</td></tr>';return;}
    h+='<tr class="clk'+(n===cur?' pick':'')+'" data-n="'+n+'"><td>'+n+'</td><td>'+DATA[n].d[r.i1]+'</td>'+
      '<td class="'+cls(r.s.ret)+'">'+fp(r.s.ret)+'</td><td>'+fp(r.s.mdd)+'</td><td>'+fp(r.s.cur)+'</td>'+
      '<td>'+r.s.pct.toFixed(0)+'%</td><td>'+r.s.uw+'</td>'+
      '<td>'+(r.score===null?'-':r.score.toFixed(0)+'（'+r.lvl+'）')+'</td>'+
      '<td>'+(r.hr?fp(r.hr.med):'-')+'</td><td>'+(r.hr?fp(r.hr.mfl):'-')+'</td><td>'+(r.hr?r.hr.n:0)+'</td></tr>';
  });
  $('summary').innerHTML=h+'</table>';
  document.querySelectorAll('#summary tr.clk').forEach(function(tr){
    tr.onclick=function(){cur=tr.dataset.n;$('pick').value=cur;render();};});
}

function renderTarget(){
  const t=DATA[cur],i0=lowerBound(t.d,start),i1=upperIdx(t.d,end);
  const msg=$('msg');
  if(i1-i0<1){
    msg.textContent='所选区间内「'+cur+'」数据不足 2 个交易日，请调整起止日期。';msg.style.display='block';
    ['chart1','chart2'].forEach(function(id){Plotly.purge(id);});
    $('cards').innerHTML='';$('adj').innerHTML='';$('stats').innerHTML='';$('eps').innerHTML='';return;
  }
  msg.style.display='none';
  const s=ddStats(t,i0,i1);
  const x=t.d.slice(i0,i1+1),close=t.c.slice(i0,i1+1);
  const score=t.s.slice(i0,i1+1).map(function(v){return v===null?null:v/10;});
  const ddPct=s.dd.map(function(v){return v*100;});

  const sc=t.s[i1]===null?null:t.s[i1]/10;
  $('cards').innerHTML=
    '<div class="m"><div class="k">区间</div><div class="v" style="font-size:14px">'+t.d[i0]+' ~ '+t.d[i1]+'</div><div class="s">'+(i1-i0+1)+' 个交易日</div></div>'+
    '<div class="m"><div class="k">区间涨跌幅</div><div class="v '+cls(s.ret)+'">'+fp(s.ret)+'</div></div>'+
    '<div class="m"><div class="k">区间最大回撤</div><div class="v">'+fp(s.mdd)+'</div><div class="s">谷底 '+t.d[s.mddI]+'</div></div>'+
    '<div class="m"><div class="k">当前回撤</div><div class="v">'+fp(s.cur)+'</div><div class="s">深于区间内 '+s.pct.toFixed(0)+'% 的交易日</div></div>'+
    '<div class="m"><div class="k">水下天数</div><div class="v">'+s.uw+'</div><div class="s">距区间内最近高点</div></div>'+
    '<div class="m"><div class="k">区间末日评分</div><div class="v">'+(sc===null?'-':sc.toFixed(0))+'</div><div class="s">'+(t.l[i1]>=0?LABELS[t.l[i1]]:'-')+'</div></div>';

  const adj=t.adj||[],major=adj.filter(function(e){return e.kind!=='分红除权';}),minor=adj.length-major.length;
  $('adj').innerHTML=adj.length?('🔧 已对净值复权（后复权到最新，全历史）：'+major.map(function(e){return e.date+' '+e.kind;}).join('；')+
    (minor?((major.length?'；':'')+'另有 '+minor+' 次分红除权'):'')):'';

  // 区间内进入高风险及以上的事件
  const ev=(t.ev[HIGH]||[]).filter(function(i){return i>=i0&&i<=i1;});
  const evx=ev.map(function(i){return t.d[i];}),evy=ev.map(function(i){return t.c[i];});
  const traces=[
    {x:x,y:close,name:'价格',type:'scatter',mode:'lines',line:{width:1.4,color:'#61AFEF'},xaxis:'x',yaxis:'y'},
    {x:evx,y:evy,name:'进入高风险及以上',type:'scatter',mode:'markers',marker:{color:'#EF4444',size:9,symbol:'triangle-down'},xaxis:'x',yaxis:'y'},
    {x:x,y:ddPct,name:'回撤%',type:'scatter',mode:'lines',fill:'tozeroy',line:{width:1,color:'#e06c75'},fillcolor:'rgba(224,108,117,.35)',xaxis:'x',yaxis:'y2'},
    {x:x,y:score,name:'过热评分',type:'scatter',mode:'lines',line:{width:1.2,color:'#C678DD'},connectgaps:false,xaxis:'x',yaxis:'y3'}
  ];
  const ann=function(txt,y){return {xref:'paper',yref:'paper',x:0,y:y,text:txt,showarrow:false,xanchor:'left',yanchor:'bottom',font:{size:12,color:'#aaa'}};};
  const line=function(v,c){return {type:'line',xref:'paper',x0:0,x1:1,yref:'y3',y0:v,y1:v,line:{color:c,width:1,dash:'dot'}};};
  const ax={gridcolor:'#2a2a2a',zerolinecolor:'#333',linecolor:'#333'};
  const layout={height:720,margin:{l:55,r:20,t:20,b:35},paper_bgcolor:'#1e1e1e',plot_bgcolor:'#1e1e1e',
    font:{color:'#ccc',size:12},showlegend:false,hovermode:'x unified',
    xaxis:Object.assign({anchor:'y3',type:'date',tickformat:'%Y-%m-%d',hoverformat:'%Y-%m-%d'},ax),
    yaxis:Object.assign({domain:[0.58,1],type:'log'},ax),
    yaxis2:Object.assign({domain:[0.31,0.53],ticksuffix:'%'},ax),
    yaxis3:Object.assign({domain:[0,0.26],range:[0,100]},ax),
    annotations:[ann('价格（对数轴）与高风险信号',1.0),ann('水下曲线（相对区间内最近高点的回撤）',0.535),ann('过热评分（虚线：80 过热 / 90 极端）',0.265)],
    shapes:[line(80,'#F97316'),line(90,'#EF4444')]};
  Plotly.react('chart1',traces,layout,{displaylogo:false,responsive:true});

  const boxes=[];
  for(let k=0;k<LABELS.length;k++){
    const ys=[];for(let i=i0;i<=i1;i++)if(t.l[i]===k&&t.r['20'][i]!==null)ys.push(t.r['20'][i]/(RS/100));
    if(ys.length)boxes.push({y:ys,name:LABELS[k],type:'box',boxmean:true,marker:{color:COLORS[k]}});
  }
  Plotly.react('chart2',boxes,{height:340,margin:{l:55,r:20,t:20,b:35},paper_bgcolor:'#1e1e1e',plot_bgcolor:'#1e1e1e',
    font:{color:'#ccc',size:12},showlegend:false,yaxis:Object.assign({ticksuffix:'%'},ax),xaxis:ax},{displaylogo:false,responsive:true});

  // 回测表
  const st=stats(t,i0,i1);
  let h='<table><tr><th>等级</th><th>持有期</th><th>样本数</th><th>平均收益</th><th>中位收益</th><th>胜率</th><th>最差一成</th><th>平均最大浮亏</th></tr>';
  st.forEach(function(r){
    const few=r.n<10;
    h+='<tr'+(r.g===ALL?' class="base"':'')+'><td>'+r.g+'</td><td>'+r.h+'日</td><td'+(few?' class="few"':'')+'>'+r.n+(few?' ⚠少':'')+'</td>'+
      '<td class="'+cls(r.mean)+'">'+fp(r.mean)+'</td><td class="'+cls(r.med)+'">'+fp(r.med)+'</td><td>'+fp(r.win,0)+'</td><td>'+fp(r.p10)+'</td><td>'+fp(r.mfl)+'</td></tr>';
  });
  $('stats').innerHTML=h+'</table>';

  // 回撤明细
  const eps=episodes(t,i0,i1).sort(function(a,b){return a.depth-b.depth;}).slice(0,8);
  let e='<table><tr><th>峰值日</th><th>谷底日</th><th>修复日</th><th>最大回撤</th><th>跌至谷底(日)</th><th>修复用时(日)</th></tr>';
  eps.forEach(function(r){
    e+='<tr><td>'+t.d[r.p]+'</td><td>'+t.d[r.t]+'</td><td>'+(r.r===null?'未修复':t.d[r.r])+'</td><td>'+fp(r.depth)+'</td><td>'+(r.t-r.p)+'</td><td>'+(r.r===null?'-':(r.r-r.t))+'</td></tr>';
  });
  $('eps').innerHTML=eps.length?e+'</table>':'<p class="note">区间内无回撤记录</p>';
}
function render(){syncSelectors();renderSummary();renderTarget();}

window.addEventListener('DOMContentLoaded',function(){
  $('section').onchange=function(){section=this.value;typeName='全部';render();};
  $('type').onchange=function(){typeName=this.value;render();};
  $('pick').onchange=function(){cur=this.value;render();};
  document.querySelectorAll('.tbtn').forEach(function(b){b.onclick=function(){preset(b.dataset.win);};});
  $('d0').min=$('d1').min=FIRST;$('d0').max=$('d1').max=LAST;
  $('d0').onchange=onDate;$('d1').onchange=onDate;
  preset('1Y');
});
"""

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>回撤与过热信号回测</title>
<style>__CSS__</style>
__PLOTLY__
</head><body><div class="app">
<h1>📉 回撤 + 过热信号回测验证</h1>
<div class="sub">生成时间 __GEN__ ｜ 评分来源：__SCORER__</div>

<div class="card"><div class="ctrl">
  <div><label>板块 </label><select id="section"></select></div>
  <div><label>类型 </label><select id="type"></select></div>
  <div><label>标的 </label><select id="pick"></select></div>
  <div class="tgroup">
    <button class="tbtn" data-win="1M">1月</button>
    <button class="tbtn" data-win="3M">3月</button>
    <button class="tbtn" data-win="6M">6月</button>
    <button class="tbtn active" data-win="1Y">1年</button>
    <button class="tbtn" data-win="ALL">全部</button>
  </div>
  <div class="dates"><label>起</label><input type="date" id="d0"><label>止</label><input type="date" id="d1"></div>
</div>
<p class="note">所有数字按所选区间重算：区间最大回撤的峰值从区间起点重新计；信号表只统计<b>区间内</b>首次进入各等级的独立事件（冷却 __COOLDOWN__ 个交易日），
其后收益可延伸到区间终点之后。区间越短样本越少，⚠ 表示样本不足 10，仅供参考。</p></div>

<div id="msg"></div>
<div class="card"><h2>汇总（点击行切换标的）</h2><div class="scroll" id="summary"></div></div>
<div class="card"><div class="cards" id="cards"></div><p class="note" id="adj"></p></div>
<div class="card"><div id="chart1"></div></div>
<div class="card"><h2>过热信号回测</h2><div class="scroll" id="stats"></div>
<div id="chart2"></div>
<p class="note">"最大浮亏"＝入场后持有期内相对入场价的最低点；箱线图为区间内各等级当日之后 20 日收益（全部天数，含重叠，仅看形状）。以上均为历史统计，不构成投资建议。</p></div>
<div class="card"><h2>最深的几次回撤（区间内）</h2><div class="scroll" id="eps"></div></div>
</div>
<script>
const DATA=__DATA__;
const LABELS=__LABELS__;
const HIGH=__HIGH__;
const ALL=__ALL__;
__JS__
</script></body></html>
"""


def build_html(data, plotly_js, cooldown, scorer):
    if plotly_js == "inline":
        from plotly.offline import get_plotlyjs
        plotly_tag = "<script>" + get_plotlyjs() + "</script>"
    else:
        plotly_tag = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'
    js = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = HTML
    for k, v in {
        "__CSS__": CSS, "__PLOTLY__": plotly_tag, "__GEN__": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "__SCORER__": scorer, "__COOLDOWN__": str(cooldown), "__DATA__": js(data),
        "__LABELS__": js(LABELS), "__HIGH__": js(HIGH_GROUP), "__ALL__": js(ALL_GROUP),
        "__JS__": (JS_ANALYTICS + JS_UI).replace("__RS__", str(RS)),
    }.items():
        html = html.replace(k, v)
    return html


# ---------------------------------------------------------------- 主程序 ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--funds-csv", help="基金清单CSV；省略时默认读取 ../funds/funds_universe_example.csv")
    ap.add_argument("--nav-csv", help="fetch_fund_nav.py 产出的 fund_nav_history.csv，默认 ../funds/fund_nav_history.csv")
    ap.add_argument("--out", default="drawdown_dashboard.html")
    ap.add_argument("--cooldown", type=int, default=20)
    ap.add_argument("--plotly-js", choices=["cdn", "inline"], default="cdn")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    kernel = load_kernel()
    scorer = ("zigzag_signal_analyzer 内核评分（八大指标过热评分）" if kernel else
              "简化版评分（RSI/BIAS/BOLL%b/20日涨幅的滚动分位均值，仅占位）")
    print("评分来源：", scorer)

    if a.demo:
        data = load_demo()
        meta = {name: {"section": "演示", "type": "演示数据", "code": ""} for name in data}
    else:
        data, meta = load_indices(here)
    adj = {}
    # 默认自动接入项目完整基金清单；仍可通过 --funds-csv 指定自定义基金池。
    funds_csv = a.funds_csv or os.path.join(here, "..", "funds", "funds_universe_example.csv")
    nav_csv = a.nav_csv or os.path.join(here, "..", "funds", "fund_nav_history.csv")
    if os.path.exists(funds_csv):
        fdata, adj, fmeta = load_funds(funds_csv, kernel, nav_csv)
        data.update(fdata)
        meta.update(fmeta)
    elif not a.demo:
        print(f"[提示] 未找到基金清单，已跳过基金板块：{funds_csv}")
    if not data:
        sys.exit("没有取到任何数据")

    packed = {}
    for name, close in data.items():
        close = close[~close.index.duplicated()].sort_index().dropna()
        if len(close) < 400:
            print(f"[跳过] {name}: 数据不足")
            continue
        score = kernel_heat_score(kernel, close) if kernel else fallback_heat_score(close)
        packed[name] = pack(close, score, to_level(score), a.cooldown, adj.get(name), meta.get(name))
        print(f"[完成] {name}")

    with open(a.out, "w", encoding="utf-8") as f:
        f.write(build_html(packed, a.plotly_js, a.cooldown, scorer))
    print(f"已生成 {a.out}")


if __name__ == "__main__":
    main()
