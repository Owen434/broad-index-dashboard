# -*- coding: utf-8 -*-
"""
基金风险排名 + 历史分位定位  (v2)
=================================================================
回答两个问题：
  1) 【横截面】这只基金在同类基金里排第几危险？过去一段时间排名在爬升还是下滑？
  2) 【时序】它的评分落在自己过去 LOOKBACK_DAYS 日评分分布的什么位置？

v3 相对 v1 的改动：
  1) 排名去掉「/总数」后缀，只显示名次；总数改在汇总条与导出图抬头里给出。
  2) 排名随类型联动。切换类型后，比较范围就是该类型内部，名次、名次变化、
     走势图、汇总统计全部重排。
  3) 日期可选。可回看最近 DATE_CHOICES 个交易日中的任意一天，届时全表
     （评分、分位、排名、位置条、走势）都按那一天重新计算。
  4) 导出图片弃用 html2canvas，改为自绘 SVG。html2canvas 1.4.1 在这张表上
     （sticky 表头 + 横向滚动容器）会稳定产出全透明画布，且依赖 CDN。现在
     由脚本直接拼原生 SVG 再转 PNG：零外部依赖、不联网、矢量级清晰，
     导出内容与当前的类型/日期/筛选状态完全一致。PNG 转换若被浏览器
     拦截会自动降级为 .svg 下载。

为支撑 2)、3)，Python 端不再算死一份排名，而是下发一个紧凑的评分矩阵，
排名与统计在浏览器端实时重算。评分与指标口径仍 100% 调用
zigzag_signal_analyzer.py 的函数，绝不重写。
"""
import os
import sys
import json
import importlib.util
import warnings
from datetime import datetime

import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')

# ==========================================
# 可调参数
# ==========================================
LOOKBACK_DAYS = 250      # 「自身历史分位」的基准窗口（约一年交易日）
DATE_CHOICES = 60        # 日期下拉框可回看多少个交易日
TREND_DAYS = 60          # 排名走势迷你图的长度
DELTA_DAYS = 5           # 排名变化 / 分位变化的对比间隔
FFILL_LIMIT = 3          # 净值披露滞后时，允许沿用前值参与排名的天数
HOT_THRESHOLD = 70       # 「连续偏热」的评分阈值
COLD_THRESHOLD = 30      # 「连续偏冷」的评分阈值
MIN_PCT_SAMPLES = 30     # 算分位所需的最少样本，不足则显示 --

OUTPUT_HTML = 'fund_riskrank_percentile.html'
OUTPUT_CSV = 'fund_riskrank_percentile.csv'
EXPORT_CSV = True

# 下发到前端的交易日数 = 可选日期数 + 走势回溯长度
# 这两个值直接决定 HTML 体积，嫌大就往下调
SHIP_DAYS = DATE_CHOICES + TREND_DAYS

# ==========================================
# 复用 18 号脚本的指标内核
# ==========================================
CORE_FILE_CANDIDATES = [
    'zigzag_signal_analyzer.py',
]


def load_indicator_core():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for fname in CORE_FILE_CANDIDATES:
        path = os.path.join(base_dir, fname)
        if not os.path.exists(path):
            continue
        spec = importlib.util.spec_from_file_location('zigzag_indicator_core', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules['zigzag_indicator_core'] = module
        spec.loader.exec_module(module)
        return module
    raise SystemExit(
        '错误：未找到指标内核脚本。请把下列任一文件与本脚本放在同一目录：\n  '
        + '\n  '.join(CORE_FILE_CANDIDATES)
    )


core = load_indicator_core()
load_csv_smart = core.load_csv_smart
build_adjusted_nav = core.build_adjusted_nav
compute_eight_indicators = core.compute_eight_indicators
compute_heat_score_series = core.compute_heat_score_series
calc_score_series = core.calc_score_series
get_status_action = core.get_status_action
MIN_ROWS = core.MIN_ROWS


# ==========================================
# 状态查找表
# ==========================================
def build_status_table():
    """
    前端要按评分显示状态徽章，但状态阈值属于 18 号内核的口径，不能在 JS 里
    重写一遍。做法是以 0.5 分为步长把 get_status_action 的结果全部枚举出来，
    下发成查找表，前端只查表、不做任何阈值判断。
    """
    uniq, keys, idx = [], [], []
    for i in range(201):
        st, mn, ac, cl = get_status_action(i / 2.0)
        key = (st, cl)
        if key not in keys:
            keys.append(key)
            uniq.append({'s': st, 'c': cl, 'a': str(ac or '')})
        idx.append(keys.index(key))
    return uniq, idx


STATUS_DEFS, SCORE2STATUS = build_status_table()


def _r1(v):
    """四舍五入到 1 位小数，NaN -> None，用于压缩 JSON 体积。"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(v), 1)


# ==========================================
# 读取基金列表
# 开源版只带一份「板块」示例基金池（公开 ETF），不含任何人的真实持仓/自选。
# 想跑自己关注的基金：复制 funds_universe_example.csv，改成自己的 基金代码/基金名称。
# ==========================================
target_df = load_csv_smart("funds_universe_example.csv")
target_df.columns = target_df.columns.str.strip()
target_df['基金代码'] = target_df['基金代码'].astype(str).str.split('.').str[0].str.strip().str.zfill(6)
if '类型' not in target_df.columns:
    target_df['类型'] = '未分类'

fund_dict = {}
for _, row in target_df.iterrows():
    code = str(row['基金代码']).strip()
    name = str(row['基金名称']).strip() if '基金名称' in target_df.columns else ''
    typ = str(row['类型']).strip() or '未分类'
    fund_dict[code] = (name, typ)

# ==========================================
# 读取历史净值
# ==========================================
hist_df = load_csv_smart("fund_nav_history.csv")
hist_df.columns = hist_df.columns.str.strip()
hist_df['基金代码'] = hist_df['基金代码'].astype(str).str.split('.').str[0].str.strip().str.zfill(6)
hist_df['单位净值'] = pd.to_numeric(hist_df['单位净值'].astype(str).str.replace(',', ''), errors='coerce')
hist_df['日期'] = pd.to_datetime(hist_df['日期'], errors='coerce')
hist_df = hist_df.dropna(subset=['单位净值', '日期'])
hist_df.sort_values(['基金代码', '日期'], inplace=True)

# ==========================================
# 逐只基金计算完整评分序列
# ==========================================
print(">>> 正在计算各基金过热评分序列（净值已复权，口径与 18 号脚本一致）...")
print(f">>> 分位窗口 {LOOKBACK_DAYS} 日 | 可选日期 {DATE_CHOICES} 个 | 走势 {TREND_DAYS} 日")

score_map = {}
meta_map = {}

for code, (name, typ) in fund_dict.items():
    df_fund = hist_df[hist_df['基金代码'] == code].copy()
    if df_fund.empty:
        print(f" ⚠️ 未找到基金 {code} - {name} 的历史数据，已跳过")
        continue

    df_fund = df_fund.drop_duplicates(subset=['日期'], keep='last').sort_values('日期')
    if len(df_fund) < MIN_ROWS:
        print(f" ⚠️ 基金 {code} 数据不足 {MIN_ROWS} 条，已跳过")
        continue

    if not name or name.lower() == 'nan':
        name = str(df_fund['基金名称'].iloc[0]) if '基金名称' in df_fund.columns else code

    adj_close, split_events = build_adjusted_nav(df_fund, code, name)
    ind = compute_eight_indicators(adj_close)
    net_count_s, intensity_s, _, _ = compute_heat_score_series(ind)
    score_s = calc_score_series(net_count_s, intensity_s).reindex(ind.index).dropna()

    if score_s.empty:
        print(f" ⚠️ 基金 {code} 评分全为空，已跳过")
        continue

    display = f"{name}({code})" if code else name
    score_map[display] = score_s
    meta_map[display] = {'code': code, 'name': name, 'type': typ}

    if split_events:
        print(f" 🔧 {code} {name} 历史净值已复权（{len(split_events)} 处事件）")

if not score_map:
    raise SystemExit('错误：没有任何基金成功算出评分，请检查数据文件。')

print(f">>> 完成：{len(score_map)} 只基金")

# ==========================================
# 交易日轴
# ==========================================
all_dates = sorted(set().union(*[set(s.index) for s in score_map.values()]))
DATES = all_dates[-SHIP_DAYS:]
DATES_IDX = pd.DatetimeIndex(DATES)
date_strs = [pd.Timestamp(d).strftime('%Y-%m-%d') for d in DATES]
# 只开放最靠后的 DATE_CHOICES 天供选择，保证每天都有足够的走势回溯长度
selectable_from = max(0, len(DATES) - DATE_CHOICES)


# ==========================================
# 构造下发数组
# ==========================================
def window_stats(arr, pos):
    """arr 中第 pos 个观测所对应的近 LOOKBACK_DAYS 窗口统计。"""
    lo = max(0, pos - LOOKBACK_DAYS + 1)
    w = arr[lo:pos + 1]
    w = w[~np.isnan(w)]
    if len(w) < MIN_PCT_SAMPLES:
        return None
    cur = arr[pos]
    return {
        'pct': float((w <= cur).sum()) / len(w) * 100.0,
        'lo': float(np.min(w)),
        'md': float(np.median(w)),
        'hi': float(np.max(w)),
        'p25': float(np.percentile(w, 25)),
        'p75': float(np.percentile(w, 75)),
        'n': int(len(w)),
        'peak': int(len(w) - 1 - int(np.argmax(w))),
    }


def streak_at(arr, pos):
    """从第 pos 个观测往回数：连续偏热返回正数，连续偏冷返回负数，都不满足返回 0。"""
    hot = 0
    for i in range(pos, max(-1, pos - 120), -1):
        if np.isnan(arr[i]) or arr[i] < HOT_THRESHOLD:
            break
        hot += 1
    if hot >= 2:
        return hot
    cold = 0
    for i in range(pos, max(-1, pos - 120), -1):
        if np.isnan(arr[i]) or arr[i] > COLD_THRESHOLD:
            break
        cold += 1
    return -cold if cold >= 2 else 0


funds_payload = []
for dn, s in score_map.items():
    own_idx = s.index
    own_arr = s.values.astype(float)

    A = {k: [] for k in ('s', 'p', 'lo', 'md', 'hi', 'q1', 'q3', 'pk', 'k', 'w')}
    stale_mask = []
    stat_cache, streak_cache = {}, {}

    for d in DATES:
        pos = int(own_idx.searchsorted(d, side='right')) - 1
        blank = pos < 0
        if not blank:
            # 披露滞后超过 FFILL_LIMIT 个交易日就不再沿用前值，视为缺席排名
            gap = int(DATES_IDX.searchsorted(d, side='right')
                      - DATES_IDX.searchsorted(own_idx[pos], side='right'))
            blank = gap > FFILL_LIMIT

        if blank:
            for key in ('s', 'p', 'lo', 'md', 'hi', 'q1', 'q3', 'pk'):
                A[key].append(None)
            A['k'].append(0)
            A['w'].append(0)
            stale_mask.append('1' if pos >= 0 else '0')
            continue

        if pos not in stat_cache:
            stat_cache[pos] = window_stats(own_arr, pos)
            streak_cache[pos] = streak_at(own_arr, pos)
        st = stat_cache[pos]

        A['s'].append(_r1(own_arr[pos]))
        A['k'].append(int(streak_cache[pos]))
        stale_mask.append('1' if own_idx[pos] < d else '0')

        if st is None:
            for key in ('p', 'lo', 'md', 'hi', 'q1', 'q3', 'pk'):
                A[key].append(None)
            A['w'].append(0)
        else:
            A['p'].append(_r1(st['pct']))
            A['lo'].append(_r1(st['lo']))
            A['md'].append(_r1(st['md']))
            A['hi'].append(_r1(st['hi']))
            A['q1'].append(_r1(st['p25']))
            A['q3'].append(_r1(st['p75']))
            A['pk'].append(st['peak'])
            A['w'].append(st['n'])

    rec = {'n': meta_map[dn]['name'], 'c': meta_map[dn]['code'], 't': meta_map[dn]['type'],
           'st': ''.join(stale_mask)}
    rec.update(A)
    funds_payload.append(rec)

types_present = []
for f in funds_payload:
    if f['t'] not in types_present:
        types_present.append(f['t'])
types_present.sort(key=lambda t: -sum(1 for f in funds_payload if f['t'] == t))

payload = {
    'dates': date_strs,
    'selFrom': selectable_from,
    'funds': funds_payload,
    'statusDefs': STATUS_DEFS,
    'score2status': SCORE2STATUS,
    'types': types_present,
    'cfg': {'lookback': LOOKBACK_DAYS, 'trend': TREND_DAYS,
            'delta': DELTA_DAYS, 'minPct': MIN_PCT_SAMPLES},
}
payload_json = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
print(f">>> 前端数据包 {len(payload_json.encode('utf-8')) / 1024:.0f} KB"
      f"（如需减小，调低 DATE_CHOICES 或 TREND_DAYS）")

# ==========================================
# CSS
# ==========================================
# 位置条的渐变底拆成 6 段纯色 div，与导出 SVG 里的画法保持一致。
CSS = """
body{background:#161616;color:#fff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;margin:0;padding:20px;}
h2{text-align:center;color:#E5C07B;margin-bottom:6px;}
.subtitle{text-align:center;color:#777;font-size:12px;margin-bottom:12px;line-height:1.7;}
.btn-group{margin:10px 0;text-align:center;}
.btn-group.tool-row{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;text-align:left;}
.tool-row-left{display:flex;align-items:center;flex-wrap:wrap;gap:6px;}
button{background:#2a2a2a;color:#ddd;border:1px solid #444;border-radius:6px;padding:5px 14px;margin:0 4px;cursor:pointer;font-size:13px;transition:background .15s,border-color .15s;}
button:hover{background:#3a3a3a;border-color:#666;}
button.active{background:#E5C07B;color:#161616;border-color:#E5C07B;font-weight:600;}
button:focus-visible{outline:2px solid #E5C07B;outline-offset:2px;}
button:disabled{opacity:.35;cursor:not-allowed;}
.screenshot-btn{background:#2f4f4f;border-color:#3d6363;color:#cfe;}
select,input[type=search]{background:#222;border:1px solid #444;border-radius:6px;color:#ddd;padding:5px 10px;font-size:13px;}
select{min-width:160px;}
#search{width:170px;}
.date-nav{display:inline-flex;align-items:center;gap:6px;}
.date-nav button{padding:5px 10px;margin:0;}
.date-tag{color:#E5C07B;font-size:12px;margin-left:8px;}
.date-tag.past{color:#F97316;}
.summary-bar{display:flex;flex-wrap:wrap;justify-content:center;align-items:center;gap:10px;margin:10px 0 15px 0;min-height:30px;}
.summary-item{border:1px solid;border-radius:6px;padding:4px 12px;display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;user-select:none;opacity:.5;transition:opacity .15s;}
.summary-item.on{opacity:1;}
.summary-count{color:#fff;font-weight:700;}
.summary-total{color:#888;font-size:12px;margin-left:6px;}
.table-container{overflow-x:auto;border:1px solid #333;border-radius:8px;}
table{border-collapse:collapse;width:100%;font-size:13px;}
th,td{border-bottom:1px solid #2b2b2b;padding:7px 10px;text-align:center;white-space:nowrap;}
thead th{background:#1f1f1f;color:#bbb;font-weight:600;position:sticky;top:0;z-index:3;cursor:pointer;user-select:none;}
thead th:hover{color:#E5C07B;}
thead th.sorted::after{content:' \\25BE';color:#E5C07B;}
thead th.sorted.asc::after{content:' \\25B4';color:#E5C07B;}
thead th.nosort{cursor:default;}
thead th.nosort:hover{color:#bbb;}
tbody tr:hover{background:#1d1d1d;}
.col-rank{width:58px;}
.rank-num{font-size:16px;font-weight:700;color:#E5C07B;}
.rank-top{color:#EF4444;}
.name-td{text-align:left;min-width:210px;position:sticky;left:0;background:#161616;z-index:2;}
tbody tr:hover .name-td{background:#1d1d1d;}
.fund-name{font-weight:600;color:#eee;}
.fund-sub{font-size:11px;color:#777;margin-top:2px;}
.stale{color:#F97316;}
.score-badge{display:inline-flex;align-items:center;gap:6px;border:1px solid;border-radius:6px;padding:3px 9px;font-size:12px;font-weight:600;}
.posbar{position:relative;width:170px;height:20px;margin:0 auto;}
.pb-seg{position:absolute;top:6px;height:8px;opacity:.30;}
.pb-band{position:absolute;top:4px;height:12px;border-radius:3px;background:rgba(229,192,123,.20);border:1px solid rgba(229,192,123,.45);}
.pb-med{position:absolute;top:2px;height:16px;width:1px;background:rgba(255,255,255,.45);}
.pb-mark{position:absolute;top:0;height:20px;width:3px;border-radius:2px;}
.spark{display:block;margin:0 auto;}
.pct-num{font-size:15px;font-weight:700;}
.pct-unit{font-size:10px;font-weight:400;opacity:.7;}
.delta{font-size:11px;margin-left:5px;}
.dim{color:#555;}
.range-txt{color:#888;font-size:11px;}
.range-txt b{color:#ccc;font-weight:600;}
.streak{font-size:12px;}
.legend{color:#666;font-size:11px;text-align:center;margin-top:12px;line-height:1.9;}
.legend .k{display:inline-block;margin:0 10px;}
.empty{text-align:center;color:#666;padding:30px;}
@media (prefers-reduced-motion:reduce){*{transition:none!important;}}
"""

# ==========================================
# 前端脚本（普通字符串，避免 f-string 花括号转义）
# ==========================================
JS = r"""
(function() {
'use strict';
var D = window.__FUND_DATA__;
var CFG = D.cfg, DATES = D.dates, FUNDS = D.funds;
FUNDS.forEach(function(f, i) { f.i = i; });

// 状态查表：阈值口径来自 18 号内核，前端只查不判
function statusOf(sc) {
    if (sc === null || sc === undefined || isNaN(sc)) return null;
    return D.statusDefs[D.score2status[Math.round(Math.max(0, Math.min(100, sc)) * 2)]];
}

var curType = 'all';
var curDate = DATES.length - 1;
var curQuick = 'all';
var activeStatus = {}, activeStatusCount = 0;
var sortKey = null, sortDir = 1;
var view = [];
var lastRows = [];

var elBody = document.getElementById('tbody');
var elSummary = document.getElementById('summary-bar');
var elDateSel = document.getElementById('date-sel');
var elSearch = document.getElementById('search');
var elDateTag = document.getElementById('date-tag');

// ---------- 排名 ----------
// 比较范围 = 当前类型分组。切类型就是换了一批同侪，名次必须重排。
function universe() {
    if (curType === 'all') return FUNDS;
    return FUNDS.filter(function(f) { return f.t === curType; });
}

function ranksAt(list, di) {
    var vals = [];
    for (var i = 0; i < list.length; i++) {
        var v = list[i].s[di];
        if (v !== null && v !== undefined) vals.push({ i: list[i].i, v: v });
    }
    vals.sort(function(a, b) { return b.v - a.v; });
    var map = {};
    for (var k = 0; k < vals.length; k++) {
        // 并列取最小名次，与 pandas rank(method='min') 一致
        if (k > 0 && vals[k].v === vals[k - 1].v) map[vals[k].i] = map[vals[k - 1].i];
        else map[vals[k].i] = k + 1;
    }
    return { map: map, total: vals.length };
}

// ---------- 渲染组件 ----------
var TRACK = [
    ['#3B82F6', 0, 20], ['#60A5FA', 20, 40], ['#22C55E', 40, 65],
    ['#EAB308', 65, 82], ['#F97316', 82, 92], ['#EF4444', 92, 100]
];

function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function clip(v) { return Math.max(0, Math.min(100, v)); }

function hexa(hex, a) {
    var h = String(hex).replace('#', '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    return 'rgba(' + parseInt(h.substr(0, 2), 16) + ',' + parseInt(h.substr(2, 2), 16) +
           ',' + parseInt(h.substr(4, 2), 16) + ',' + a + ')';
}

function posbar(f, di, color) {
    var sc = f.s[di];
    if (sc === null) return '<span class="dim">--</span>';
    var cur = clip(sc), h = '', tip;
    for (var i = 0; i < TRACK.length; i++) {
        h += '<div class="pb-seg" style="left:' + TRACK[i][1] + '%;width:' +
             (TRACK[i][2] - TRACK[i][1]) + '%;background:' + TRACK[i][0] + ';"></div>';
    }
    if (f.lo[di] !== null) {
        var lo = clip(f.lo[di]), hi = clip(f.hi[di]);
        h += '<div class="pb-band" style="left:' + lo.toFixed(1) + '%;width:' +
             Math.max(hi - lo, 0.6).toFixed(1) + '%;"></div>';
        h += '<div class="pb-med" style="left:' + clip(f.md[di]).toFixed(1) + '%;"></div>';
        tip = '近' + f.w[di] + '日区间 ' + f.lo[di].toFixed(0) + ' ~ ' + f.hi[di].toFixed(0) +
              '｜中位 ' + f.md[di].toFixed(0) + '｜P25 ' + f.q1[di].toFixed(0) +
              '｜P75 ' + f.q3[di].toFixed(0) + '｜当日 ' + cur.toFixed(0);
    } else {
        tip = '样本不足 ' + CFG.minPct + ' 条，仅显示当日评分 ' + cur.toFixed(0);
    }
    h += '<div class="pb-mark" style="left:' + cur.toFixed(1) + '%;background:' + color + ';"></div>';
    return '<div class="posbar" title="' + esc(tip) + '">' + h + '</div>';
}

function sparkline(hist, total) {
    var pts = [];
    for (var i = 0; i < hist.length; i++) {
        if (hist[i] !== null) pts.push([i, hist[i]]);
    }
    if (pts.length < 2) return '<span class="dim">--</span>';
    var w = 108, h = 26, pad = 3;
    var n = Math.max(hist.length - 1, 1), span = Math.max(total - 1, 1);
    var coords = pts.map(function(p) {
        return [pad + (w - 2 * pad) * (p[0] / n), pad + (h - 2 * pad) * ((p[1] - 1) / span)];
    });
    var first = pts[0][1], last = pts[pts.length - 1][1];
    var stroke = last < first - 0.5 ? '#FF6B6B' : (last > first + 0.5 ? '#4ADE80' : '#9CA3AF');
    var path = coords.map(function(c) { return c[0].toFixed(1) + ',' + c[1].toFixed(1); }).join(' ');
    var lc = coords[coords.length - 1];
    var tip = CFG.trend + '日排名走势：' + first + ' -> ' + last + '（共' + total + '只，1=风险最高）';
    return '<svg class="spark" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' +
           '<title>' + esc(tip) + '</title>' +
           '<polyline points="' + path + '" fill="none" stroke="' + stroke +
           '" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>' +
           '<circle cx="' + lc[0].toFixed(1) + '" cy="' + lc[1].toFixed(1) +
           '" r="2.2" fill="' + stroke + '"/></svg>';
}

function deltaHtml(v, unit) {
    if (v === null || v === undefined || isNaN(v)) return '<span class="dim">--</span>';
    if (Math.abs(v) < 0.5) return '<span class="delta" style="color:#888;">&plusmn;0</span>';
    var c = v > 0 ? '#FF6B6B' : '#4ADE80';
    return '<span class="delta" style="color:' + c + ';">' + (v > 0 ? '+' : '') +
           v.toFixed(0) + unit + '</span>';
}

function pctColor(p) {
    if (p >= 90) return '#EF4444';
    if (p >= 80) return '#F97316';
    if (p >= 60) return '#EAB308';
    if (p >= 40) return '#22C55E';
    if (p >= 20) return '#60A5FA';
    return '#3B82F6';
}

// ---------- 主重算：类型或日期一变就整体重来 ----------
function rebuild() {
    var di = curDate, list = universe();

    var from = Math.max(0, di - CFG.trend + 1);
    var histMaps = [];
    for (var d = from; d <= di; d++) histMaps.push(ranksAt(list, d));
    var cur = histMaps[histMaps.length - 1];

    var pastIdx = di - CFG.delta;
    var pastMap = pastIdx >= 0 ? ranksAt(list, pastIdx) : null;

    view = [];
    for (var i = 0; i < list.length; i++) {
        var f = list[i], sc = f.s[di];
        if (sc === null || sc === undefined) continue;

        var rk = cur.map[f.i] || null;
        var rkPast = pastMap ? (pastMap.map[f.i] || null) : null;
        var pct = f.p[di];
        var pctPast = pastIdx >= 0 ? f.p[pastIdx] : null;

        view.push({
            f: f, sc: sc, stt: statusOf(sc),
            rk: rk, total: cur.total,
            rkDelta: (rk !== null && rkPast !== null) ? (rkPast - rk) : null,
            pct: pct,
            pctDelta: (pct !== null && pctPast !== null && pctPast !== undefined) ? (pct - pctPast) : null,
            hist: histMaps.map(function(m) { return m.map[f.i] || null; }),
            stale: f.st.charAt(di) === '1', k: f.k[di], pk: f.pk[di]
        });
    }

    view.sort(function(a, b) { return (a.rk || 9999) - (b.rk || 9999); });
    if (sortKey) applySort();
    render();
}

function sortVal(r, key) {
    switch (key) {
        case 'rank': return r.rk === null ? 9999 : r.rk;
        case 'score': return r.sc;
        case 'pct': return r.pct === null ? -1 : r.pct;
        case 'rankdelta': return r.rkDelta === null ? -999 : r.rkDelta;
        case 'med': return r.f.md[curDate] === null ? -1 : r.f.md[curDate];
        case 'streak': return r.k;
        case 'peak': return r.pk === null ? 9999 : r.pk;
    }
    return 0;
}

function applySort() {
    view.sort(function(a, b) { return sortDir * (sortVal(a, sortKey) - sortVal(b, sortKey)); });
}

function passFilter(r) {
    if (activeStatusCount && !activeStatus[r.stt.s]) return false;
    var q = (elSearch.value || '').trim().toLowerCase();
    if (q && (r.f.n + r.f.c).toLowerCase().indexOf(q) < 0) return false;
    if (curQuick === 'top20' && (r.rk === null || r.rk > 20)) return false;
    if (curQuick === 'hi' && !(r.pct !== null && r.pct >= 80)) return false;
    if (curQuick === 'lo' && !(r.pct !== null && r.pct <= 20)) return false;
    if (curQuick === 'up' && !(r.rkDelta !== null && r.rkDelta >= 5)) return false;
    return true;
}

function render() {
    var rows = view.filter(passFilter), html = '';
    lastRows = rows;
    for (var i = 0; i < rows.length; i++) {
        var r = rows[i], f = r.f, di = curDate;
        var rankCls = (r.rk !== null && r.rk <= 3) ? 'rank-num rank-top' : 'rank-num';

        var rangeHtml = f.lo[di] !== null
            ? '<span class="range-txt">' + f.lo[di].toFixed(0) + ' ~ <b>' +
              f.md[di].toFixed(0) + '</b> ~ ' + f.hi[di].toFixed(0) + '</span>'
            : '<span class="dim">样本不足</span>';

        var streakHtml;
        if (r.k >= 2) streakHtml = '<span class="streak" style="color:#F97316;">偏热 ' + r.k + ' 日</span>';
        else if (r.k <= -2) streakHtml = '<span class="streak" style="color:#60A5FA;">偏冷 ' + (-r.k) + ' 日</span>';
        else streakHtml = '<span class="dim">--</span>';

        var peakHtml = (r.pk === null || r.pk === undefined) ? '<span class="dim">--</span>'
            : (r.pk > 0 ? '<span class="range-txt"><b>' + r.pk + '</b> 日前</span>'
                        : '<span class="range-txt" style="color:#EF4444;"><b>就是当日</b></span>');

        var pctHtml = r.pct === null ? '<span class="dim">--</span>'
            : '<span class="pct-num" style="color:' + pctColor(r.pct) + ';">' +
              r.pct.toFixed(0) + '<span class="pct-unit">%</span></span>' + deltaHtml(r.pctDelta, '');

        html += '<tr>' +
            '<td class="col-rank"><span class="' + rankCls + '">' +
                (r.rk === null ? '--' : r.rk) + '</span></td>' +
            '<td class="name-td"><div class="fund-name">' + esc(f.n) + '</div>' +
                '<div class="fund-sub">' + esc(f.c) + ' · ' + esc(f.t) +
                (r.stale ? ' · <span class="stale">数据滞后</span>' : '') + '</div></td>' +
            '<td><div class="score-badge" style="color:' + r.stt.c + ';border-color:' + r.stt.c +
                ';background:' + hexa(r.stt.c, 0.14) + ';" title="' + esc(r.stt.a) + '">' +
                r.stt.s + ' · ' + r.sc.toFixed(0) + '分</div></td>' +
            '<td>' + posbar(f, di, r.stt.c) + '</td>' +
            '<td>' + pctHtml + '</td>' +
            '<td>' + deltaHtml(r.rkDelta, ' 位') + '</td>' +
            '<td>' + sparkline(r.hist, r.total) + '</td>' +
            '<td>' + rangeHtml + '</td>' +
            '<td>' + streakHtml + '</td>' +
            '<td>' + peakHtml + '</td></tr>';
    }
    elBody.innerHTML = html || '<tr><td class="empty" colspan="10">当前条件下没有匹配的基金</td></tr>';
    renderSummary(rows);
}

// 汇总条同样跟随类型与日期重算
function renderSummary(rows) {
    var counts = {}, hi = 0, lo = 0;
    for (var i = 0; i < rows.length; i++) {
        var s = rows[i].stt.s;
        counts[s] = (counts[s] || 0) + 1;
        if (rows[i].pct !== null && rows[i].pct >= 80) hi++;
        if (rows[i].pct !== null && rows[i].pct <= 20) lo++;
    }
    var h = '';
    for (var k = 0; k < D.statusDefs.length; k++) {
        var def = D.statusDefs[k], c = counts[def.s] || 0;
        if (!c && !activeStatus[def.s]) continue;
        var on = (activeStatusCount === 0 || activeStatus[def.s]) ? ' on' : '';
        h += '<div class="summary-item' + on + '" data-st="' + esc(def.s) +
             '" style="border-color:' + def.c + ';"><span style="color:' + def.c + ';">' +
             def.s + '</span><span class="summary-count">' + c + '</span></div>';
    }
    h += '<div class="summary-total">共 <b>' + rows.length +
         '</b> 只 · 自身高位(≥80分位) <b style="color:#F97316;">' + hi +
         '</b> 只 · 自身低位(≤20分位) <b style="color:#60A5FA;">' + lo + '</b> 只</div>';
    elSummary.innerHTML = h;
    Array.prototype.forEach.call(elSummary.querySelectorAll('.summary-item'), function(it) {
        it.addEventListener('click', function() {
            var st = it.getAttribute('data-st');
            if (activeStatus[st]) { delete activeStatus[st]; activeStatusCount--; }
            else { activeStatus[st] = true; activeStatusCount++; }
            render();
        });
    });
}

// ---------- 控件 ----------
function buildTypeButtons() {
    var box = document.getElementById('type-btns');
    var defs = [{ k: 'all', l: '全部' }];
    D.types.forEach(function(t) { defs.push({ k: t, l: t }); });
    defs.forEach(function(d) {
        var b = document.createElement('button');
        b.textContent = d.l;
        if (d.k === 'all') b.className = 'active';
        b.addEventListener('click', function() {
            curType = d.k;
            Array.prototype.forEach.call(box.children, function(x) { x.classList.remove('active'); });
            b.classList.add('active');
            rebuild();
        });
        box.appendChild(b);
    });
}

function syncDateNav() {
    document.getElementById('date-prev').disabled = (curDate <= D.selFrom);
    document.getElementById('date-next').disabled = (curDate >= DATES.length - 1);
    var back = DATES.length - 1 - curDate;
    if (back === 0) {
        elDateTag.textContent = '当前为最新交易日';
        elDateTag.className = 'date-tag';
    } else {
        elDateTag.textContent = '回看 ' + back + ' 个交易日前 · 全表已按该日重算';
        elDateTag.className = 'date-tag past';
    }
}

function buildDateSelect() {
    for (var i = DATES.length - 1; i >= D.selFrom; i--) {
        var o = document.createElement('option');
        o.value = i;
        o.textContent = DATES[i] + (i === DATES.length - 1 ? '（最新）' : '');
        elDateSel.appendChild(o);
    }
    elDateSel.value = String(DATES.length - 1);
    elDateSel.addEventListener('change', function() {
        curDate = parseInt(this.value, 10); syncDateNav(); rebuild();
    });
    document.getElementById('date-prev').addEventListener('click', function() {
        if (curDate > D.selFrom) { curDate--; elDateSel.value = String(curDate); syncDateNav(); rebuild(); }
    });
    document.getElementById('date-next').addEventListener('click', function() {
        if (curDate < DATES.length - 1) { curDate++; elDateSel.value = String(curDate); syncDateNav(); rebuild(); }
    });
    document.getElementById('date-latest').addEventListener('click', function() {
        curDate = DATES.length - 1; elDateSel.value = String(curDate); syncDateNav(); rebuild();
    });
    syncDateNav();
}

Array.prototype.forEach.call(document.querySelectorAll('.quick-btn'), function(b) {
    b.addEventListener('click', function() {
        curQuick = b.getAttribute('data-quick');
        Array.prototype.forEach.call(document.querySelectorAll('.quick-btn'), function(x) {
            x.classList.remove('active');
        });
        b.classList.add('active');
        render();
    });
});
document.querySelector('.quick-btn[data-quick="all"]').classList.add('active');
elSearch.addEventListener('input', render);

Array.prototype.forEach.call(document.querySelectorAll('#matrix thead th[data-key]'), function(th) {
    th.addEventListener('click', function() {
        var key = th.getAttribute('data-key');
        if (sortKey === key) sortDir = -sortDir;
        else { sortKey = key; sortDir = th.getAttribute('data-dir') === 'asc' ? 1 : -1; }
        Array.prototype.forEach.call(document.querySelectorAll('#matrix thead th'), function(x) {
            x.classList.remove('sorted', 'asc');
        });
        th.classList.add('sorted');
        if (sortDir === 1) th.classList.add('asc');
        applySort();
        render();
    });
});

// ---------- 导出图片 ----------
// 不再用 html2canvas。它对 position:sticky、横向滚动容器和 CSS 简写的处理
// 在这张表上会直接产出全透明画布。既然表格内容完全由我们生成，结构已知，
// 干脆自己拼一份原生 SVG 再转 PNG：无外部依赖、不走 CDN、矢量级清晰。
var FF_ATTR = "system-ui,-apple-system,'Segoe UI','Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif";
var FF_CANVAS = 'system-ui,-apple-system,"Segoe UI","Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif';
var _measure = document.createElement('canvas').getContext('2d');

function tw(t, spec) {
    _measure.font = spec + ' ' + FF_CANVAS;
    return _measure.measureText(String(t)).width;
}
function fitText(t, spec, max) {
    t = String(t);
    if (tw(t, spec) <= max) return t;
    while (t.length > 1 && tw(t + '…', spec) > max) t = t.slice(0, -1);
    return t + '…';
}
function sesc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function T(x, y, txt, o) {
    o = o || {};
    return '<text x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" fill="' + (o.fill || '#dddddd') +
           '" font-size="' + (o.size || 12) + '" font-weight="' + (o.weight || 400) +
           '" font-family="' + FF_ATTR + '" text-anchor="' + (o.anchor || 'middle') +
           '">' + sesc(txt) + '</text>';
}
function R(x, y, w, h, o) {
    o = o || {};
    return '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + Math.max(w, 0).toFixed(1) +
           '" height="' + h.toFixed(1) + '" fill="' + (o.fill || 'none') + '"' +
           (o.stroke ? ' stroke="' + o.stroke + '" stroke-width="' + (o.sw || 1) + '"' : '') +
           (o.rx ? ' rx="' + o.rx + '"' : '') +
           (o.op !== undefined ? ' opacity="' + o.op + '"' : '') + '/>';
}
// 导出时把状态里的 emoji 去掉，改用同色圆点。emoji 在 SVG 光栅化时
// 依赖系统彩色字体，跨机器不稳定，容易变成豆腐块。
function plainStatus(s) {
    return String(s).replace(/^[^\u4e00-\u9fa5A-Za-z0-9]+/, '');
}

function buildSVG(rows) {
    var PAD = 24, ROWH = 42, HEADH = 36, TOP = 104;
    var COLS = [
        { k: 'rank',   l: '风险排名', w: 78 },
        { k: 'name',   l: '基金', w: 250, align: 'left' },
        { k: 'score',  l: '当日评分', w: 150 },
        { k: 'pos',    l: '评分位置', w: 205 },
        { k: 'pct',    l: '历史分位', w: 105 },
        { k: 'rd',     l: CFG.delta + '日排名变化', w: 115 },
        { k: 'spark',  l: CFG.trend + '日排名走势', w: 130 },
        { k: 'range',  l: '近' + CFG.lookback + '日区间', w: 145 },
        { k: 'streak', l: '连续状态', w: 105 },
        { k: 'peak',   l: '距区间高点', w: 115 }
    ];
    var x = PAD;
    for (var c = 0; c < COLS.length; c++) { COLS[c].x = x; x += COLS[c].w; }
    var W = x + PAD;
    var H = TOP + HEADH + rows.length * ROWH + 34;

    var typeLabel = (curType === 'all') ? '全部' : curType;
    var hiN = 0, loN = 0, cnt = {};
    for (var i = 0; i < rows.length; i++) {
        cnt[rows[i].stt.s] = (cnt[rows[i].stt.s] || 0) + 1;
        if (rows[i].pct !== null && rows[i].pct >= 80) hiN++;
        if (rows[i].pct !== null && rows[i].pct <= 20) loN++;
    }

    var s = '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H +
            '" viewBox="0 0 ' + W + ' ' + H + '">';
    s += R(0, 0, W, H, { fill: '#161616' });
    s += T(W / 2, 38, '基金风险排名与历史分位', { fill: '#E5C07B', size: 21, weight: 700 });
    s += T(W / 2, 60, '评估日期 ' + DATES[curDate] + ' ｜ 类型 ' + typeLabel +
           ' ｜ 共 ' + rows.length + ' 只 ｜ 自身高位(≥80分位) ' + hiN +
           ' 只 ｜ 自身低位(≤20分位) ' + loN + ' 只', { fill: '#999999', size: 12 });

    // 状态计数条
    var chips = [], cw = 0;
    for (var k = 0; k < D.statusDefs.length; k++) {
        var def = D.statusDefs[k];
        if (!cnt[def.s]) continue;
        var lab = plainStatus(def.s) + ' ' + cnt[def.s];
        var w = tw(lab, '600 12px') + 26;
        chips.push({ lab: lab, c: def.c, w: w });
        cw += w + 8;
    }
    var cx = (W - cw + 8) / 2;
    for (var j = 0; j < chips.length; j++) {
        s += R(cx, 72, chips[j].w, 22, { fill: 'none', stroke: chips[j].c, rx: 5 });
        s += '<circle cx="' + (cx + 11) + '" cy="83" r="3.5" fill="' + chips[j].c + '"/>';
        s += T(cx + 20, 87, chips[j].lab, { fill: chips[j].c, size: 12, weight: 600, anchor: 'start' });
        cx += chips[j].w + 8;
    }

    // 表头
    s += R(PAD, TOP, W - 2 * PAD, HEADH, { fill: '#1f1f1f' });
    for (var h = 0; h < COLS.length; h++) {
        var col = COLS[h];
        var hx = col.align === 'left' ? col.x + 12 : col.x + col.w / 2;
        s += T(hx, TOP + 23, col.l, { fill: '#bbbbbb', size: 12, weight: 600,
                                      anchor: col.align === 'left' ? 'start' : 'middle' });
    }
    s += '<line x1="' + PAD + '" y1="' + (TOP + HEADH) + '" x2="' + (W - PAD) +
         '" y2="' + (TOP + HEADH) + '" stroke="#3a3a3a" stroke-width="1"/>';

    // 数据行
    for (var r = 0; r < rows.length; r++) {
        var row = rows[r], f = row.f, di = curDate;
        var y = TOP + HEADH + r * ROWH;
        var mid = y + ROWH / 2;
        if (r % 2 === 1) s += R(PAD, y, W - 2 * PAD, ROWH, { fill: '#1c1c1c' });
        s += '<line x1="' + PAD + '" y1="' + (y + ROWH) + '" x2="' + (W - PAD) +
             '" y2="' + (y + ROWH) + '" stroke="#2b2b2b" stroke-width="1"/>';

        // 排名
        var C = COLS[0];
        s += T(C.x + C.w / 2, mid + 6, row.rk === null ? '--' : row.rk,
               { fill: (row.rk !== null && row.rk <= 3) ? '#EF4444' : '#E5C07B', size: 17, weight: 700 });

        // 基金名 + 副行
        C = COLS[1];
        s += T(C.x + 12, mid - 2, fitText(f.n, '600 13px', C.w - 24),
               { fill: '#eeeeee', size: 13, weight: 600, anchor: 'start' });
        var sub = f.c + ' · ' + f.t + (row.stale ? ' · 数据滞后' : '');
        s += T(C.x + 12, mid + 14, fitText(sub, '400 11px', C.w - 24),
               { fill: row.stale ? '#F97316' : '#777777', size: 11, anchor: 'start' });

        // 评分徽章
        C = COLS[2];
        var btxt = plainStatus(row.stt.s) + ' · ' + row.sc.toFixed(0) + '分';
        var bw = tw(btxt, '600 12px') + 30;
        var bx = C.x + (C.w - bw) / 2;
        s += R(bx, mid - 11, bw, 22, { fill: hexa(row.stt.c, 0.14), stroke: row.stt.c, rx: 5 });
        s += '<circle cx="' + (bx + 12) + '" cy="' + mid + '" r="3.5" fill="' + row.stt.c + '"/>';
        s += T(bx + 21, mid + 4, btxt, { fill: row.stt.c, size: 12, weight: 600, anchor: 'start' });

        // 位置条
        C = COLS[3];
        var bx0 = C.x + 14, bwid = C.w - 28;
        for (var t2 = 0; t2 < TRACK.length; t2++) {
            s += R(bx0 + bwid * TRACK[t2][1] / 100, mid - 4,
                   bwid * (TRACK[t2][2] - TRACK[t2][1]) / 100, 8,
                   { fill: TRACK[t2][0], op: 0.3 });
        }
        if (f.lo[di] !== null) {
            var plo = clip(f.lo[di]), phi = clip(f.hi[di]);
            s += R(bx0 + bwid * plo / 100, mid - 6, bwid * Math.max(phi - plo, 0.6) / 100, 12,
                   { fill: 'rgba(229,192,123,0.20)', stroke: 'rgba(229,192,123,0.45)', rx: 3 });
            s += R(bx0 + bwid * clip(f.md[di]) / 100, mid - 8, 1, 16,
                   { fill: 'rgba(255,255,255,0.45)' });
        }
        s += R(bx0 + bwid * clip(row.sc) / 100 - 1.5, mid - 10, 3, 20,
               { fill: row.stt.c, rx: 1.5 });

        // 历史分位
        C = COLS[4];
        if (row.pct === null) {
            s += T(C.x + C.w / 2, mid + 5, '--', { fill: '#555555', size: 13 });
        } else {
            var pTxt = row.pct.toFixed(0) + '%';
            var dTxt = (row.pctDelta === null || Math.abs(row.pctDelta) < 0.5) ? ''
                : (row.pctDelta > 0 ? '+' : '') + row.pctDelta.toFixed(0);
            var pW = tw(pTxt, '700 15px'), dW = dTxt ? tw(dTxt, '400 11px') + 5 : 0;
            var px = C.x + (C.w - pW - dW) / 2;
            s += T(px, mid + 5, pTxt, { fill: pctColor(row.pct), size: 15, weight: 700, anchor: 'start' });
            if (dTxt) s += T(px + pW + 5, mid + 5, dTxt,
                             { fill: row.pctDelta > 0 ? '#FF6B6B' : '#4ADE80', size: 11, anchor: 'start' });
        }

        // 排名变化
        C = COLS[5];
        if (row.rkDelta === null) s += T(C.x + C.w / 2, mid + 5, '--', { fill: '#555555', size: 13 });
        else if (Math.abs(row.rkDelta) < 0.5) s += T(C.x + C.w / 2, mid + 5, '±0', { fill: '#888888', size: 12 });
        else s += T(C.x + C.w / 2, mid + 5, (row.rkDelta > 0 ? '+' : '') + row.rkDelta + ' 位',
                    { fill: row.rkDelta > 0 ? '#FF6B6B' : '#4ADE80', size: 12, weight: 600 });

        // 排名走势
        C = COLS[6];
        var pts = [];
        for (var q = 0; q < row.hist.length; q++) if (row.hist[q] !== null) pts.push([q, row.hist[q]]);
        if (pts.length >= 2) {
            var sw = 100, sh = 26, sx = C.x + (C.w - sw) / 2, sy = mid - sh / 2;
            var nn = Math.max(row.hist.length - 1, 1), spn = Math.max(row.total - 1, 1);
            var co = pts.map(function(p) {
                return [sx + sw * (p[0] / nn), sy + 3 + (sh - 6) * ((p[1] - 1) / spn)];
            });
            var fv = pts[0][1], lv = pts[pts.length - 1][1];
            var stk = lv < fv - 0.5 ? '#FF6B6B' : (lv > fv + 0.5 ? '#4ADE80' : '#9CA3AF');
            s += '<polyline points="' + co.map(function(p) {
                    return p[0].toFixed(1) + ',' + p[1].toFixed(1);
                 }).join(' ') + '" fill="none" stroke="' + stk +
                 '" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>';
            s += '<circle cx="' + co[co.length - 1][0].toFixed(1) + '" cy="' +
                 co[co.length - 1][1].toFixed(1) + '" r="2.4" fill="' + stk + '"/>';
        } else {
            s += T(C.x + C.w / 2, mid + 5, '--', { fill: '#555555', size: 13 });
        }

        // 区间
        C = COLS[7];
        if (f.lo[di] !== null) {
            s += T(C.x + C.w / 2, mid + 4,
                   f.lo[di].toFixed(0) + ' ~ ' + f.md[di].toFixed(0) + ' ~ ' + f.hi[di].toFixed(0),
                   { fill: '#aaaaaa', size: 11 });
        } else {
            s += T(C.x + C.w / 2, mid + 4, '样本不足', { fill: '#555555', size: 11 });
        }

        // 连续状态
        C = COLS[8];
        if (row.k >= 2) s += T(C.x + C.w / 2, mid + 4, '偏热 ' + row.k + ' 日', { fill: '#F97316', size: 12 });
        else if (row.k <= -2) s += T(C.x + C.w / 2, mid + 4, '偏冷 ' + (-row.k) + ' 日', { fill: '#60A5FA', size: 12 });
        else s += T(C.x + C.w / 2, mid + 4, '--', { fill: '#555555', size: 12 });

        // 距区间高点
        C = COLS[9];
        if (row.pk === null || row.pk === undefined) s += T(C.x + C.w / 2, mid + 4, '--', { fill: '#555555', size: 12 });
        else if (row.pk > 0) s += T(C.x + C.w / 2, mid + 4, row.pk + ' 日前', { fill: '#aaaaaa', size: 11 });
        else s += T(C.x + C.w / 2, mid + 4, '就是当日', { fill: '#EF4444', size: 11, weight: 600 });
    }

    s += T(PAD, H - 12, '位置条：金色带 = 近' + CFG.lookback +
           '日评分区间，白线 = 中位数，竖针 = 当日；走势图纵轴倒置，线上行 = 名次前移 = 风险相对同类上升',
           { fill: '#666666', size: 11, anchor: 'start' });
    s += T(W - PAD, H - 12, '导出于 ' + new Date().toLocaleString('zh-CN'),
           { fill: '#666666', size: 11, anchor: 'end' });
    s += '</svg>';
    return { svg: s, w: W, h: H };
}

function download(name, url) {
    var a = document.createElement('a');
    a.download = name;
    a.href = url;
    a.click();
}

document.getElementById('btn-screenshot').addEventListener('click', function() {
    var btn = this;
    if (!lastRows.length) { alert('当前没有可导出的行。'); return; }
    btn.disabled = true;
    btn.textContent = '⏳ 正在生成...';

    var out = buildSVG(lastRows);
    var url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(out.svg);
    var base = '基金风险排名_' + DATES[curDate] +
               (curType === 'all' ? '' : '_' + curType);

    function done() { btn.disabled = false; btn.textContent = '📷 导出图片'; }
    function fallbackSVG(why) {
        console.warn('PNG 转换失败，改存 SVG：', why);
        download(base + '.svg', url);
        done();
    }

    var img = new Image();
    img.onload = function() {
        try {
            var scale = 2;
            var cv = document.createElement('canvas');
            cv.width = out.w * scale;
            cv.height = out.h * scale;
            var ctx = cv.getContext('2d');
            ctx.fillStyle = '#161616';
            ctx.fillRect(0, 0, cv.width, cv.height);
            ctx.drawImage(img, 0, 0, cv.width, cv.height);
            download(base + '.png', cv.toDataURL('image/png'));
            done();
        } catch (e) {
            fallbackSVG(e.message);
        }
    };
    img.onerror = function() { fallbackSVG('图像加载失败'); };
    img.src = url;
});

buildTypeButtons();
buildDateSelect();
rebuild();
})();
"""

# ==========================================
# 组装 HTML
# ==========================================
gen_time = datetime.now().strftime('%Y-%m-%d %H:%M')

html = (
    '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
    '<title>基金风险排名与历史分位</title>\n'
    f'<style>{CSS}</style>\n</head>\n<body>\n'

    '<h2>基金风险排名与历史分位</h2>\n'
    '<div class="subtitle">\n'
    f'    数据截至 {date_strs[-1]} ｜ 生成于 {gen_time} ｜ 共 {len(funds_payload)} 只基金<br>\n'
    '    <b>风险排名</b>＝所选日期、所选类型范围内按过热评分从高到低排序，1 号最危险；\n'
    f'    <b>历史分位</b>＝当日评分落在该基金自己近 {LOOKBACK_DAYS} 日评分分布中的位置。<br>\n'
    '    排名高说明「比同类热」，分位高说明「比自己以往热」，两者同时高才是真正的拥挤位。\n'
    '</div>\n'

    '<div id="summary-bar" class="summary-bar"></div>\n'

    '<div class="btn-group">\n'
    '    <span style="margin-right:10px;color:#aaa;">类型：</span>\n'
    '    <span id="type-btns"></span>\n'
    '    <span style="color:#666;font-size:11px;margin-left:10px;">（切换后排名在该类型内部重排）</span>\n'
    '</div>\n'

    '<div class="btn-group">\n'
    '    <span style="margin-right:10px;color:#aaa;">评估日期：</span>\n'
    '    <span class="date-nav">\n'
    '        <button id="date-prev" title="前一个交易日">◀</button>\n'
    '        <select id="date-sel"></select>\n'
    '        <button id="date-next" title="后一个交易日">▶</button>\n'
    '        <button id="date-latest">回到最新</button>\n'
    '    </span>\n'
    '    <span id="date-tag" class="date-tag"></span>\n'
    '</div>\n'

    '<div class="btn-group tool-row">\n'
    '    <div class="tool-row-left">\n'
    '        <span style="color:#aaa;margin-right:6px;">快捷视图：</span>\n'
    '        <button class="quick-btn" data-quick="all">全部</button>\n'
    '        <button class="quick-btn" data-quick="top20">风险前 20</button>\n'
    '        <button class="quick-btn" data-quick="hi">自身高位 ≥80 分位</button>\n'
    '        <button class="quick-btn" data-quick="lo">自身低位 ≤20 分位</button>\n'
    '        <button class="quick-btn" data-quick="up">排名快速上升</button>\n'
    '        <input id="search" type="search" placeholder="搜索基金名 / 代码" autocomplete="off">\n'
    '    </div>\n'
    '    <button id="btn-screenshot" class="screenshot-btn">📷 导出图片</button>\n'
    '</div>\n'

    '<div class="table-container">\n<table id="matrix">\n<thead>\n<tr>\n'
    '    <th class="col-rank" data-key="rank" data-dir="asc">风险排名</th>\n'
    '    <th class="nosort">基金</th>\n'
    '    <th data-key="score" data-dir="desc">当日评分</th>\n'
    '    <th class="nosort">评分位置（0–100 刻度 + 自身区间）</th>\n'
    '    <th data-key="pct" data-dir="desc">历史分位</th>\n'
    f'    <th data-key="rankdelta" data-dir="desc">{DELTA_DAYS}日排名变化</th>\n'
    f'    <th class="nosort">{TREND_DAYS}日排名走势</th>\n'
    f'    <th data-key="med" data-dir="desc">近{LOOKBACK_DAYS}日区间</th>\n'
    '    <th data-key="streak" data-dir="desc">连续状态</th>\n'
    '    <th data-key="peak" data-dir="asc">距区间高点</th>\n'
    '</tr>\n</thead>\n<tbody id="tbody"></tbody>\n</table>\n</div>\n'

    '<div class="legend">\n'
    f'    <span class="k">位置条：<b style="color:#E5C07B;">浅金色带</b> = 近{LOOKBACK_DAYS}日评分区间</span>\n'
    '    <span class="k">白色细线 = 区间中位数</span>\n'
    '    <span class="k">彩色竖针 = 当日位置</span>\n'
    '    <span class="k">走势图纵轴倒置，线往上走 = 名次前移 = 风险相对同类上升</span><br>\n'
    f'    <span class="k">「{DELTA_DAYS}日排名变化」为正表示名次前移了几位（更危险），为负表示后退（更安全）</span>\n'
    '</div>\n'

    '<script>window.__FUND_DATA__ = ' + payload_json + ';</script>\n'
    '<script>' + JS + '</script>\n'
    '</body>\n</html>\n'
)

with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
    f.write(html)
print(f">>> 已生成 {OUTPUT_HTML}（{len(html.encode('utf-8')) / 1024:.0f} KB）")

# ==========================================
# CSV 明细（最新交易日 · 全部基金口径）
# ==========================================
if EXPORT_CSV:
    di = len(DATES) - 1
    snap = [f for f in funds_payload if f['s'][di] is not None]
    snap.sort(key=lambda f: -f['s'][di])
    total = len(snap)
    recs = []
    for rank, f in enumerate(snap, start=1):
        st, mn, ac, cl = get_status_action(f['s'][di])
        recs.append({
            '风险排名': rank,
            '参与排名总数': total,
            '基金代码': f['c'],
            '基金名称': f['n'],
            '类型': f['t'],
            '评估日期': date_strs[di],
            '数据是否滞后': '是' if f['st'][di] == '1' else '否',
            '当日评分': f['s'][di],
            '状态': st,
            f'{LOOKBACK_DAYS}日历史分位%': f['p'][di],
            '区间最低': f['lo'][di],
            '区间中位': f['md'][di],
            '区间最高': f['hi'][di],
            '样本数': f['w'][di],
            '距区间高点天数': f['pk'][di],
            '连续状态(正=偏热日,负=偏冷日)': f['k'][di],
        })
    pd.DataFrame(recs).to_csv(OUTPUT_CSV, index=False, encoding='utf-8-sig')
    print(f">>> 已生成 {OUTPUT_CSV}")