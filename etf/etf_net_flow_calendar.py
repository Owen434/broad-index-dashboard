# -*- coding: utf-8 -*-
"""
ETF 净申赎金额日历（交互式看板）
=========================================================
把 etf_broadbase_dashboard.py 已经算好的「每日净申赎金额」按公历月份铺成挂历格子，
红色 = 净申购（资金流入），绿色 = 净赎回（资金流出），样式与 index_return_calendar.py 一致。
只展示近两年（今年 + 去年），统计口径全部是金额（亿元），不含星期统计、涨幅分布等板块。

数据来源（不重复抓取）：
  1. 默认直接读取 etf_broadbase_dashboard.py 落盘的 etf_flow.parquet（秒开，不联网）；
  2. 找不到 parquet，或命令行加 --refresh 时，自动导入 etf_broadbase_dashboard.py
     并跑一遍它的 main() 重新采集，然后再出图。

用法：
  python etf_net_flow_calendar.py            # 读 etf_flow.parquet 直接出图
  python etf_net_flow_calendar.py --refresh  # 先重跑 ETF 采集再出图

两个脚本放在同一目录即可；输出单文件 etf_net_flow_calendar.html。
"""
import os
import sys
import json
import glob
import argparse
import importlib.util
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_CANDIDATES = ['etf_broadbase_dashboard.py', '4_etf_broadbase_dashboard.py']  # 找不到再用通配
DATA_FILE = 'etf_flow.parquet'          # 与 ETF 脚本的 OUT_DATA 保持一致
OUTPUT_FILE = 'etf_net_flow_calendar.html'
YEARS_BACK = 3                         

# 属于「宽基」的分组（与 ETF_UNIVERSE 里的分组名一致），其余归为「行业主题」
BROAD_GROUPS = {
    '沪深300', '上证50', '上证180', '中证500', '中证800', '中证1000',
    '创业板', '科创50', '深证100', 'MSCI中国A50', '中证A500',
}


# ==================== 取数 ====================

def _find_core_path():
    for name in CORE_CANDIDATES:
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p):
            return p
    hits = sorted(glob.glob(os.path.join(BASE_DIR, '*etf_broadbase_dashboard.py')))
    return hits[0] if hits else None


def run_core_main():
    """导入 ETF 脚本并重跑采集，返回它 main() 产出的完整 DataFrame"""
    path = _find_core_path()
    if path is None:
        raise SystemExit('错误：未找到 etf_broadbase_dashboard.py，请把它和本脚本放在同一目录下')
    spec = importlib.util.spec_from_file_location('etf_core', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['etf_core'] = module
    spec.loader.exec_module(module)
    print('>>> 正在运行 ETF 采集脚本（首跑较慢，之后走缓存）...')
    return module.main()


def find_data_file():
    for d in (os.getcwd(), BASE_DIR):
        p = os.path.join(d, DATA_FILE)
        if os.path.exists(p):
            return p
    return None


def load_etf_data(refresh=False):
    if not refresh:
        p = find_data_file()
        if p:
            print(f'>>> 读取已有 ETF 数据: {p}')
            return pd.read_parquet(p)
        print(f'未找到 {DATA_FILE}，转为运行 ETF 采集脚本')
    return run_core_main()


# ==================== 组装数据 ====================

def build_entities(full):
    """
    full 列：日期/基金代码/基金简称/分组/净申赎金额(元)。
    基金代码以 'G:' 开头的是「分组合并」行（同一标的指数的多只 ETF 按日相加）。
    返回 entities，每个含 id/name/category/kind/scale/years{年:{日期:金额(亿元)}}
    """
    need = ['日期', '基金代码', '基金简称', '分组', '净申赎金额']
    miss = [c for c in need if c not in full.columns]
    if miss:
        raise SystemExit(f'错误：{DATA_FILE} 缺少列 {miss}，请用最新版 etf_broadbase_dashboard.py 重新生成')

    df = full[need].copy()
    df['日期'] = pd.to_datetime(df['日期'])
    df['基金代码'] = df['基金代码'].astype(str)
    df = df.dropna(subset=['日期', '净申赎金额'])
    df = df[np.isfinite(df['净申赎金额'])]
    if df.empty:
        return [], None

    data_end = df['日期'].max()
    y0 = data_end.year - YEARS_BACK + 1
    df = df[df['日期'].dt.year >= y0]

    is_group = df['基金代码'].str.startswith('G:')
    members = (df[~is_group].groupby('分组')['基金代码'].nunique().to_dict())

    # 按原始出现顺序排序：合并口径在前，单只在后
    order = {c: i for i, c in enumerate(full['基金代码'].astype(str).drop_duplicates())}

    entities = []
    for code, sub in df.groupby('基金代码'):
        grp = str(sub['分组'].iloc[0])
        kind = 'group' if code.startswith('G:') else 'etf'
        category = '宽基' if grp in BROAD_GROUPS else '行业主题'
        if kind == 'group':
            name = f"{grp}（合并{members.get(grp, 0)}只）"
        else:
            name = f"{sub['基金简称'].iloc[0]}({code})"

        years = {}
        for y, ys in sub.groupby(sub['日期'].dt.year):
            years[str(y)] = {d.strftime('%Y-%m-%d'): round(float(v) / 1e8, 4)
                             for d, v in zip(ys['日期'], ys['净申赎金额'])}
        if not years:
            continue
        abs_all = np.abs(sub['净申赎金额'].to_numpy(dtype='float64')) / 1e8
        scale = float(np.nanpercentile(abs_all, 90)) if abs_all.size else 1.0
        scale = max(scale, 1e-4)  # 颜色深浅按该标的自身 90 分位定标，避免小品种永远是浅色
        entities.append({'id': code, 'name': name, 'category': category, 'kind': kind,
                         'scale': round(scale, 4), 'years': years, '_o': order.get(code, 9999)})

    entities.sort(key=lambda e: (0 if e['kind'] == 'group' else 1, e['_o']))
    for e in entities:
        e.pop('_o')
    return entities, data_end


# ==================== 交互式 HTML 渲染 ====================

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<style>
  body { background:#f6f7f9; color:#1f2937; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; margin:0; padding:16px; }
  h2 { text-align:center; color:#1f2937; margin:4px 0 4px 0; font-size:20px; }
  .subtitle { text-align:center; color:#6b7280; font-size:12px; margin-bottom:14px; }
  .group-label { color:#4b5563; margin-right:8px; font-size:12px; font-weight:bold; }
  .btn-group { margin:8px 0; text-align:center; }
  .btn-group.indicator-row { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; text-align:left; }
  .btn-group button, #year-group button {
    background:#fff; color:#374151; border:1px solid #d1d5db; padding:6px 14px; margin:0 4px 8px 4px;
    border-radius:4px; cursor:pointer; font-size:12px; transition:background-color .2s,color .2s;
  }
  .btn-group button.active, #year-group button.active { background:#3b82f6; color:#fff; border-color:#3b82f6; }
  .btn-group button:hover:not(.active) { background:#f3f4f6; }
  .btn-group button.hidden { display:none; }
  .screenshot-btn { background:#fff; color:#059669; border:1px solid #059669; padding:6px 16px; border-radius:4px; cursor:pointer; font-size:12px; }
  .screenshot-btn:hover { background:#059669; color:#fff; }
  .screenshot-btn:disabled { opacity:.6; cursor:default; }
  #entity-title { text-align:center; color:#4338ca; font-size:15px; margin:14px 0 6px 0; font-weight:bold; }
  .calendar-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
  @media (max-width:900px) { .calendar-grid { grid-template-columns:repeat(2,1fr); } }
  @media (max-width:480px) { .calendar-grid { grid-template-columns:1fr; } .btn-group button { padding:5px 10px; font-size:11px; } }
  .month-card { background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:8px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
  .month-header { text-align:center; background:#f3f4f6; color:#1f2937; font-weight:bold; padding:4px; border-radius:4px; margin-bottom:4px; font-size:13px; border:1px solid #e5e7eb; }
  .month-table { width:100%; border-collapse:collapse; table-layout:fixed; }
  .month-table th { font-size:10px; color:#6b7280; padding:2px; font-weight:bold; }
  .month-table th.weekend { color:#ef4444; }
  .month-table td.day { text-align:center; vertical-align:top; border:1px solid #f3f4f6; height:36px; font-size:10px; color:#4b5563; padding:2px; background:#fafafa; }
  .month-table td.day.has-val { color:#111827; }
  .month-table td.empty { border:none; background:transparent; }
  .day-num { font-weight:bold; font-size:11px; }
  .day-val { font-size:9px; font-weight:bold; }
  .month-stat { text-align:center; font-size:11px; margin-top:4px; font-weight:bold; color:#374151; }
  .month-stat.sub { color:#6b7280; font-weight:normal; }
  .stats-row { display:flex; flex-wrap:wrap; gap:16px; margin-top:22px; }
  .stats-col { flex:1; min-width:280px; overflow-x:auto; }
  .stats-col h4 { color:#2563eb; font-size:13px; margin:0 0 6px 0; }
  .stats-col table { width:100%; border-collapse:collapse; font-size:12px; white-space:nowrap; }
  .stats-col th, .stats-col td { border:1px solid #e5e7eb; padding:5px 8px; text-align:center; }
  .stats-col th { background:#f9fafb; color:#1f2937; font-weight:bold; }
  .stats-col tr.total td { background:#f9fafb; font-weight:bold; }
  .note { color:#6b7280; font-size:11px; margin-top:14px; line-height:1.7; }
  .hidden { display:none !important; }
</style>
</head>
<body>
<h2>__HEADER_TITLE__</h2>
<div class="subtitle">__SUBTITLE__</div>

<div class="btn-group" id="kind-bar">
  <span class="group-label">口径：</span>
  <button class="kind-btn active" data-kind="group">分组合并（同指数多只ETF合计）</button>
  <button class="kind-btn" data-kind="etf">单只ETF</button>
</div>

<div class="btn-group" id="category-bar">
  <span class="group-label">分类：</span>
  __CATEGORY_BUTTONS__
</div>

<div class="btn-group" id="entity-bar">
  <span class="group-label">标的：</span>
  __ENTITY_BUTTONS__
</div>

<div class="btn-group indicator-row">
  <div>
    <span class="group-label">年份：</span>
    <span id="year-group"></span>
  </div>
  <button id="btn-screenshot" class="screenshot-btn">📷 保存截图</button>
</div>

<div id="capture-area">
  <div id="entity-title"></div>
  <div class="calendar-grid" id="calendar-grid"></div>
  <div class="stats-row">
    <div class="stats-col">
      <h4>月份统计（当前年份，单位：亿元）</h4>
      <table id="month-table"></table>
    </div>
  </div>
  <div class="note">
    说明：格内数值为当日净申赎金额（亿元），红色=净申购（资金流入），绿色=净赎回（资金流出），颜色深浅按该标的自身的单日金额分布定标；
    净申赎金额 = 当日份额变动 × 单位净值；沪市份额次日才披露，末端未披露日、份额折算日留空；「分组合并」为同一标的指数多只ETF按日相加。
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
const DATA = __DATA_JSON__;
let currentEntity = null;
let currentYear = null;
let currentCategory = '__ALL__';
let currentKind = 'group';

function amtColor(v, scale) {
  if (v === null || v === undefined || isNaN(v)) return 'transparent';
  const strength = Math.min(Math.abs(v) / (scale || 1), 1.0);
  const alpha = (0.15 + strength * 0.65).toFixed(2);
  if (v > 0) return `rgba(239, 68, 68, ${alpha})`;
  if (v < 0) return `rgba(34, 197, 94, ${alpha})`;
  return '#fafafa';
}

const WEEK_HEADERS = ['周日','周一','周二','周三','周四','周五','周六'];

function monthMatrix(year, month) {
  const first = new Date(year, month - 1, 1);
  const startWeekday = first.getDay();
  const daysInMonth = new Date(year, month, 0).getDate();
  const weeks = [];
  let week = new Array(startWeekday).fill(0);
  for (let d = 1; d <= daysInMonth; d++) {
    week.push(d);
    if (week.length === 7) { weeks.push(week); week = []; }
  }
  if (week.length) {
    while (week.length < 7) week.push(0);
    weeks.push(week);
  }
  return weeks;
}

// 金额格式：带符号，亿元；格内按量级自适应小数位，保证小格子放得下
function fmtAmt(v) { return (v >= 0 ? '+' : '') + v.toFixed(2); }
function fmtCell(v) {
  const a = Math.abs(v);
  const dec = a >= 100 ? 0 : (a >= 10 ? 1 : 2);
  return (v >= 0 ? '+' : '') + v.toFixed(dec);
}
function sum(arr) { return arr.reduce((a, b) => a + b, 0); }
function colorOf(v) { return v > 0 ? '#dc2626' : (v < 0 ? '#16a34a' : '#6b7280'); }

function buildMonthCard(year, month, daily, scale) {
  const weeks = monthMatrix(year, month);
  const vals = [];
  let rows = '';
  weeks.forEach(week => {
    let row = '<tr>';
    week.forEach(day => {
      if (day === 0) { row += '<td class="empty"></td>'; return; }
      const key = `${year}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
      const v = daily[key];
      if (v === undefined) {
        row += `<td class="day"><div class="day-num">${day}</div></td>`;
      } else {
        vals.push(v);
        row += `<td class="day has-val" style="background:${amtColor(v, scale)};"><div class="day-num">${day}</div><div class="day-val">${fmtCell(v)}</div></td>`;
      }
    });
    row += '</tr>';
    rows += row;
  });

  let statHtml = '<div class="month-stat">当月无数据</div>';
  if (vals.length) {
    const inflow = vals.filter(v => v > 0);
    const outflow = vals.filter(v => v < 0);
    const total = sum(vals);
    const avgIn = inflow.length ? sum(inflow) / inflow.length : 0;
    const avgOut = outflow.length ? sum(outflow) / outflow.length : 0;
    statHtml = `<div class="month-stat" style="color:${colorOf(total)}">合计:${fmtAmt(total)}亿 | 流入${inflow.length}天 | 流出${outflow.length}天</div>
                <div class="month-stat sub">日均流入${fmtAmt(avgIn)} | 日均流出${fmtAmt(avgOut)}</div>`;
  }

  return `<div class="month-card">
    <div class="month-header">${year}年${month}月</div>
    <table class="month-table">
      <thead><tr>${WEEK_HEADERS.map((h, i) => `<th class="${i === 0 || i === 6 ? 'weekend' : ''}">${h}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${statHtml}
  </div>`;
}

function statRow(label, vals, cls) {
  const total = vals.length;
  const inflow = vals.filter(v => v > 0).length;
  const outflow = vals.filter(v => v < 0).length;
  const s = sum(vals);
  const avg = total ? s / total : 0;
  return `<tr class="${cls || ''}"><td>${label}</td><td>${total}</td><td>${inflow}</td><td>${outflow}</td>
    <td style="color:${colorOf(s)};font-weight:bold;">${total ? fmtAmt(s) : '0.00'}</td>
    <td style="color:${colorOf(avg)};font-weight:bold;">${total ? fmtAmt(avg) : '0.00'}</td></tr>`;
}

function renderAll() {
  const entity = DATA.entities.find(e => e.id === currentEntity);
  if (!entity) return;
  const years = Object.keys(entity.years).sort().reverse();
  if (!currentYear || !entity.years[currentYear]) currentYear = years[0];

  const yearGroup = document.getElementById('year-group');
  yearGroup.innerHTML = years.map(y => `<button class="year-btn ${y === currentYear ? 'active' : ''}" data-year="${y}">${y}</button>`).join('');
  yearGroup.querySelectorAll('.year-btn').forEach(btn => {
    btn.addEventListener('click', function () { currentYear = this.dataset.year; renderAll(); });
  });

  const daily = entity.years[currentYear];
  const yearTotal = sum(Object.values(daily));
  document.getElementById('entity-title').innerHTML =
    `${entity.name}（${entity.category}） - ${currentYear}年 净申赎日历｜年内合计 <span style="color:${colorOf(yearTotal)}">${fmtAmt(yearTotal)} 亿元</span>`;

  let calHtml = '';
  for (let m = 1; m <= 12; m++) calHtml += buildMonthCard(parseInt(currentYear, 10), m, daily, entity.scale);
  document.getElementById('calendar-grid').innerHTML = calHtml;

  let body = '';
  for (let m = 1; m <= 12; m++) {
    const prefix = `${currentYear}-${String(m).padStart(2, '0')}-`;
    const vals = Object.entries(daily).filter(([d]) => d.startsWith(prefix)).map(([, v]) => v);
    body += statRow(`${m}月`, vals);
  }
  body += statRow('全年', Object.values(daily), 'total');
  document.getElementById('month-table').innerHTML =
    '<thead><tr><th>月份</th><th>交易天数</th><th>流入天数</th><th>流出天数</th><th>净申赎合计(亿元)</th><th>日均净申赎(亿元)</th></tr></thead><tbody>' +
    body + '</tbody>';
}

function applyFilter() {
  document.querySelectorAll('.kind-btn').forEach(b => b.classList.toggle('active', b.dataset.kind === currentKind));
  document.querySelectorAll('.category-btn').forEach(b => b.classList.toggle('active', b.dataset.category === currentCategory));
  document.querySelectorAll('.entity-btn').forEach(b => {
    const ok = b.dataset.kind === currentKind && (currentCategory === '__ALL__' || b.dataset.category === currentCategory);
    b.classList.toggle('hidden', !ok);
  });
  const visible = Array.from(document.querySelectorAll('.entity-btn:not(.hidden)'));
  if (!visible.length) {
    currentEntity = null;
    document.getElementById('entity-title').textContent = '当前口径/分类下没有数据';
    document.getElementById('calendar-grid').innerHTML = '';
    document.getElementById('month-table').innerHTML = '';
    document.getElementById('year-group').innerHTML = '';
    return;
  }
  if (!visible.some(b => b.dataset.id === currentEntity)) setEntity(visible[0].dataset.id);
}

function setEntity(id) {
  currentEntity = id;
  currentYear = null;
  document.querySelectorAll('.entity-btn').forEach(b => b.classList.toggle('active', b.dataset.id === id));
  renderAll();
}

document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.kind-btn').forEach(btn => btn.addEventListener('click', function () { currentKind = this.dataset.kind; applyFilter(); }));
  document.querySelectorAll('.category-btn').forEach(btn => btn.addEventListener('click', function () { currentCategory = this.dataset.category; applyFilter(); }));
  document.querySelectorAll('.entity-btn').forEach(btn => btn.addEventListener('click', function () { setEntity(this.dataset.id); }));
  applyFilter();

  const btnScreenshot = document.getElementById('btn-screenshot');
  btnScreenshot.addEventListener('click', function () {
    const target = document.getElementById('capture-area');
    if (typeof html2canvas === 'undefined') { alert('截图组件加载失败，请检查网络连接后重试'); return; }
    btnScreenshot.disabled = true;
    btnScreenshot.textContent = '⏳ 正在生成截图...';
    html2canvas(target, { backgroundColor: '#f6f7f9', scale: window.devicePixelRatio > 1 ? 2 : 1, useCORS: true }).then(canvas => {
      const link = document.createElement('a');
      const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
      link.download = 'etf_net_flow_calendar_' + ts + '.png';
      link.href = canvas.toDataURL('image/png');
      link.click();
      btnScreenshot.disabled = false;
      btnScreenshot.textContent = '📷 保存截图';
    }).catch(err => {
      console.error(err); alert('截图失败，请稍后重试');
      btnScreenshot.disabled = false; btnScreenshot.textContent = '📷 保存截图';
    });
  });
});
</script>
</body>
</html>
"""


def _esc_attr(s):
    return str(s).replace('"', '&quot;')


def render_calendar_dashboard(entities, page_title, header_title, subtitle, output_path):
    """把 entities 渲染成一个自带交互的单文件 HTML"""
    categories = []
    for e in entities:
        if e['category'] not in categories:
            categories.append(e['category'])

    category_buttons = '<button class="category-btn active" data-category="__ALL__">全部</button>'
    for c in categories:
        category_buttons += f'<button class="category-btn" data-category="{_esc_attr(c)}">{c}</button>'

    entity_buttons = ''
    for e in entities:
        entity_buttons += (f'<button class="entity-btn" data-id="{_esc_attr(e["id"])}" '
                           f'data-kind="{e["kind"]}" data-category="{_esc_attr(e["category"])}">'
                           f'{e["name"]}</button>')

    payload = json.dumps({'entities': entities}, ensure_ascii=False, separators=(',', ':'))
    payload = payload.replace('</', '<\\/')   # 防止字符串里出现 </script> 截断脚本

    html = _HTML_TEMPLATE
    html = html.replace('__PAGE_TITLE__', page_title)
    html = html.replace('__HEADER_TITLE__', header_title)
    html = html.replace('__SUBTITLE__', subtitle)
    html = html.replace('__CATEGORY_BUTTONS__', category_buttons)
    html = html.replace('__ENTITY_BUTTONS__', entity_buttons)
    html = html.replace('__DATA_JSON__', payload)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    size_kb = os.path.getsize(output_path) / 1024
    print(f"✅ 已生成 ETF 净申赎日历: {os.path.abspath(output_path)}  [{size_kb:.0f}KB]")


def main():
    ap = argparse.ArgumentParser(description='ETF 净申赎金额日历')
    ap.add_argument('--refresh', action='store_true', help='先重跑 etf_broadbase_dashboard.py 采集再出图')
    args = ap.parse_args()

    full = load_etf_data(refresh=args.refresh)
    if full is None or len(full) == 0:
        print('未获取到 ETF 数据，退出。')
        return

    entities, data_end = build_entities(full)
    if not entities:
        print('没有可用的净申赎金额数据（份额接口可能全部失败），退出。')
        return

    n_group = sum(e['kind'] == 'group' for e in entities)
    print(f'>>> 标的 {len(entities)} 个（分组合并 {n_group} / 单只 {len(entities) - n_group}），'
          f'数据截止 {data_end:%Y-%m-%d}')

    subtitle = (f"数据截止：{data_end.strftime('%Y-%m-%d')}"
                f"    |    近{YEARS_BACK}年    |    单位：亿元"
                f"    |    生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")

    render_calendar_dashboard(
        entities,
        page_title='ETF 净申赎日历',
        header_title='ETF 净申赎金额日历（红流入 绿流出）',
        subtitle=subtitle,
        output_path=OUTPUT_FILE,
    )


if __name__ == '__main__':
    main()
