# -*- coding: utf-8 -*-
"""
宽基指数收益日历（交互式看板）
=========================================================
把「每日涨跌幅」按公历月份铺成挂历格子，红涨绿跌，一眼看出哪段时间连续放量、
哪段时间连续阴跌；下方配套星期/月份统计表，看胜率和平均涨跌幅。

开源改造要点（相对旧版 matplotlib 出图脚本）：
  1. 复用 price_movement_patterns.py 里已经维护的 BROAD_INDICES / get_stock_data，
     不再重复写一遍指数抓取逻辑，也避免同一批指数在 CI 里被多个脚本各抓一次。
  2. 输出改成纯前端交互的单文件 HTML（原生 JS 画日历格子，不依赖任何图表库），
     不再逐个基金/逐年导出 PNG——几十张高清 PNG（单张常有 1~3MB）按天提交进 git，
     仓库体积会线性爆炸；现在无论多少个指数、多少年数据，永远只有一个几十 KB 的
     文本文件，diff 也是文本级别，体积和历史包大小都可控。
  3. 移动端可看：viewport meta + 响应式网格（桌面4列，平板2列，手机1列）。
"""
import os
import sys
import json
import importlib.util
import warnings
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings('ignore')

CORE_FILE = 'price_movement_patterns.py'
YEARS_BACK = 2  # 展示今年 + 去年
OUTPUT_FILE = 'index_return_calendar.html'


def load_core():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, CORE_FILE)
    if not os.path.exists(path):
        raise SystemExit(f'错误：未找到 {CORE_FILE}，请把它和本脚本放在同一目录下（stocks/）')
    spec = importlib.util.spec_from_file_location('price_movement_core', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['price_movement_core'] = module
    spec.loader.exec_module(module)
    return module


core = load_core()
BROAD_INDICES = core.BROAD_INDICES
get_stock_data = core.get_stock_data


def fetch_index_returns(code):
    """抓单个指数并算出「日涨跌幅(%)」序列，索引为日期"""
    name = BROAD_INDICES[code][0]
    df = get_stock_data(code, name)
    if df is None or df.empty or 'Close' not in df.columns:
        return code, None
    close = df['Close'].dropna()
    if len(close) < 2:
        return code, None
    ret = close.pct_change() * 100
    return code, ret.dropna()


def build_entity(code, ret_series):
    name, _api_type, _symbol, _region, category = BROAD_INDICES[code]
    latest_year = ret_series.index.max().year
    years = {}
    for y in range(latest_year - YEARS_BACK + 1, latest_year + 1):
        sub = ret_series[ret_series.index.year == y]
        if sub.empty:
            continue
        years[str(y)] = {d.strftime('%Y-%m-%d'): round(float(v), 4) for d, v in sub.items()}
    if not years:
        return None
    return {'id': code, 'name': name, 'category': category, 'years': years}


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
  .hidden { display:none !important; }
  #ranking-title { text-align:center; color:#4338ca; font-size:15px; margin:14px 0 10px 0; font-weight:bold; }
  .rank-table-wrap { overflow-x:auto; }
  .rank-table { border-collapse:collapse; width:100%; font-size:11px; white-space:nowrap; }
  .rank-table th, .rank-table td { border:1px solid #e5e7eb; padding:5px 7px; text-align:center; }
  .rank-table th { background:#f9fafb; color:#1f2937; font-weight:bold; }
  .rank-month-sub { font-size:9px; color:#6b7280; font-weight:normal; margin-top:2px; line-height:1.4; }
  .rank-name { text-align:left; color:#4338ca; font-weight:bold; }
  .rank-rank { font-weight:bold; color:#6b7280; }
  .rank-rank.rank-top { color:#d97706; }
  .rank-na { color:#9ca3af; }
  .rank-empty { text-align:center; color:#6b7280; padding:30px 0; }
</style>
</head>
<body>
<h2>__HEADER_TITLE__</h2>
<div class="subtitle">__SUBTITLE__</div>

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
    <span class="group-label" style="margin-left:14px;">视图：</span>
    <button class="view-btn active" data-view="calendar">日历视图</button>
    <button class="view-btn" data-view="ranking">月度排名看板</button>
  </div>
  <button id="btn-screenshot" class="screenshot-btn">📷 保存截图</button>
</div>

<div id="capture-area">
  <div id="calendar-view">
    <div id="entity-title"></div>
    <div class="calendar-grid" id="calendar-grid"></div>
    <div class="stats-row">
      <div class="stats-col">
        <h4>星期统计（当前年份）</h4>
        <table id="weekday-table"></table>
      </div>
      <div class="stats-col">
        <h4>月份统计（当前年份）</h4>
        <table id="month-table"></table>
      </div>
    </div>
  </div>
  <div id="ranking-view" class="hidden">
    <div id="ranking-title"></div>
    <div id="ranking-board"></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
const DATA = __DATA_JSON__;
let currentEntity = null;
let currentYear = null;
let currentCategory = '__ALL__';
let currentView = 'calendar';

function pctColor(v) {
  if (v === null || v === undefined || isNaN(v)) return 'transparent';
  const strength = Math.min(Math.abs(v) / 2.5, 1.0);
  const alpha = (0.15 + strength * 0.65).toFixed(2);
  if (v > 0) return `rgba(239, 68, 68, ${alpha})`;
  if (v < 0) return `rgba(34, 197, 94, ${alpha})`;
  return '#fafafa';
}

const MONTH_NAMES = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const WEEK_HEADERS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

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

function fmtPct(v) { return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; }

function buildMonthCard(year, month, daily) {
  const weeks = monthMatrix(year, month);
  const rets = [];
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
        rets.push(v);
        row += `<td class="day has-val" style="background:${pctColor(v)};"><div class="day-num">${day}</div><div class="day-val">${fmtPct(v)}</div></td>`;
      }
    });
    row += '</tr>';
    rows += row;
  });

  let statHtml = '<div class="month-stat">当月无交易数据</div>';
  if (rets.length) {
    const up = rets.filter(r => r > 0);
    const down = rets.filter(r => r < 0);
    const cum = (rets.reduce((acc, r) => acc * (1 + r / 100), 1) - 1) * 100;
    const avgUp = up.length ? up.reduce((a, b) => a + b, 0) / up.length : 0;
    const avgDown = down.length ? down.reduce((a, b) => a + b, 0) / down.length : 0;
    const color = cum > 0 ? '#dc2626' : (cum < 0 ? '#16a34a' : '#6b7280');
    statHtml = `<div class="month-stat" style="color:${color}">累计:${fmtPct(cum)} | 上涨${up.length}天 | 下跌${down.length}天</div>
                <div class="month-stat sub">平均涨幅${fmtPct(avgUp)} | 平均跌幅${fmtPct(avgDown)}</div>`;
  }

  return `<div class="month-card">
    <div class="month-header">${MONTH_NAMES[month - 1]} ${year}</div>
    <table class="month-table">
      <thead><tr>${WEEK_HEADERS.map((h, i) => `<th class="${i === 0 || i === 6 ? 'weekend' : ''}">${h}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${statHtml}
  </div>`;
}

function buildStatsRows(entries) {
  return entries.map(({ label, vals }) => {
    const total = vals.length;
    const up = vals.filter(v => v > 0).length;
    const down = vals.filter(v => v < 0).length;
    const upProb = total ? (up / total * 100).toFixed(1) + '%' : '0.0%';
    const avg = total ? vals.reduce((a, b) => a + b, 0) / total : 0;
    const avgColor = avg > 0 ? '#dc2626' : (avg < 0 ? '#16a34a' : '#374151');
    return `<tr><td>${label}</td><td>${total}</td><td>${up}</td><td>${down}</td><td>${upProb}</td>
             <td style="color:${avgColor};font-weight:bold;">${total ? fmtPct(avg) : '0.00%'}</td></tr>`;
  }).join('');
}

function renderAll() {
  const entity = DATA.entities.find(e => e.id === currentEntity);
  if (!entity) return;
  const years = Object.keys(entity.years).sort().reverse();
  if (!currentYear || !entity.years[currentYear]) currentYear = years[0];

  const yearGroup = document.getElementById('year-group');
  yearGroup.innerHTML = years.map(y => `<button class="year-btn ${y === currentYear ? 'active' : ''}" data-year="${y}">${y}</button>`).join('');
  yearGroup.querySelectorAll('.year-btn').forEach(btn => {
    btn.addEventListener('click', function () { currentYear = this.dataset.year; renderAll(); if (currentView === 'ranking') renderRanking(); });
  });

  document.getElementById('entity-title').textContent = `${entity.name}（${entity.category}） - ${currentYear}年 收益日历`;

  const daily = entity.years[currentYear];
  let calHtml = '';
  for (let m = 1; m <= 12; m++) calHtml += buildMonthCard(parseInt(currentYear, 10), m, daily);
  document.getElementById('calendar-grid').innerHTML = calHtml;

  const wdNames = { 1: '星期一', 2: '星期二', 3: '星期三', 4: '星期四', 5: '星期五' };
  const weekdayEntries = [];
  for (let wd = 1; wd <= 5; wd++) {
    const vals = Object.entries(daily).filter(([d]) => {
      const jsWd = new Date(d + 'T00:00:00').getDay();
      const iso = jsWd === 0 ? 7 : jsWd;
      return iso === wd;
    }).map(([, v]) => v);
    weekdayEntries.push({ label: wdNames[wd], vals });
  }
  document.getElementById('weekday-table').innerHTML =
    '<thead><tr><th>星期</th><th>交易天数</th><th>上涨天数</th><th>下跌天数</th><th>上涨概率</th><th>平均涨跌幅</th></tr></thead><tbody>' +
    buildStatsRows(weekdayEntries) + '</tbody>';

  const monthEntries = [];
  for (let m = 1; m <= 12; m++) {
    const prefix = `${currentYear}-${String(m).padStart(2, '0')}-`;
    const vals = Object.entries(daily).filter(([d]) => d.startsWith(prefix)).map(([, v]) => v);
    monthEntries.push({ label: `${m}月`, vals });
  }
  document.getElementById('month-table').innerHTML =
    '<thead><tr><th>月份</th><th>交易天数</th><th>上涨天数</th><th>下跌天数</th><th>上涨概率</th><th>平均涨跌幅</th></tr></thead><tbody>' +
    buildStatsRows(monthEntries) + '</tbody>';
}

function setCategory(cat) {
  currentCategory = cat;
  document.querySelectorAll('.category-btn').forEach(b => b.classList.toggle('active', b.dataset.category === cat));
  document.querySelectorAll('.entity-btn').forEach(b => b.classList.toggle('hidden', !(cat === '__ALL__' || b.dataset.category === cat)));
  const visible = Array.from(document.querySelectorAll('.entity-btn:not(.hidden)'));
  if (visible.length && !visible.some(b => b.dataset.id === currentEntity)) setEntity(visible[0].dataset.id);
  if (currentView === 'ranking') renderRanking();
}

// ==================== 月度收益排名看板 ====================
function monthColor(v) {
  if (v === null || v === undefined || isNaN(v)) return 'transparent';
  const strength = Math.min(Math.abs(v) / 8.0, 1.0);  
  const alpha = (0.15 + strength * 0.65).toFixed(2);
  if (v > 0) return `rgba(239, 68, 68, ${alpha})`;
  if (v < 0) return `rgba(34, 197, 94, ${alpha})`;
  return '#fafafa';
}

function buildRankingBoard(category, year) {
  const pool = DATA.entities.filter(e => (category === '__ALL__' || e.category === category) && e.years[year]);
  if (!pool.length) return '<div class="rank-empty">当前分类/年份下没有数据</div>';

  const monthsSet = new Set();
  pool.forEach(e => Object.keys(e.years[year]).forEach(d => monthsSet.add(parseInt(d.slice(5, 7), 10))));
  const months = Array.from(monthsSet).sort((a, b) => a - b);
  if (!months.length) return '<div class="rank-empty">当前分类/年份下没有数据</div>';
  const latestMonth = months[months.length - 1];

  const rows = pool.map(e => {
    const daily = e.years[year];
    const perMonth = {};
    months.forEach(m => {
      const prefix = `${year}-${String(m).padStart(2, '0')}-`;
      const vals = Object.entries(daily).filter(([d]) => d.startsWith(prefix)).map(([, v]) => v);
      perMonth[m] = vals.length ? (vals.reduce((acc, r) => acc * (1 + r / 100), 1) - 1) * 100 : null;
    });
    return { id: e.id, name: e.name, category: e.category, perMonth };
  });

  rows.sort((a, b) => {
    const av = a.perMonth[latestMonth], bv = b.perMonth[latestMonth];
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    return bv - av;
  });

  const monthStats = {};
  months.forEach(m => {
    const vals = rows.map(r => r.perMonth[m]).filter(v => v !== null && v !== undefined);
    const up = vals.filter(v => v > 0).length;
    const down = vals.filter(v => v < 0).length;
    const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
    const range = vals.length ? Math.max(...vals) - Math.min(...vals) : 0;
    monthStats[m] = { up, down, avg, range };
  });

  const thead = '<tr><th>排名</th><th>代码</th><th>名称</th><th>分类</th>' +
    months.map(m => {
      const st = monthStats[m];
      return `<th>${m}月<div class="rank-month-sub">↑${st.up} ↓${st.down}<br>均${fmtPct(st.avg)}<br>差${st.range.toFixed(2)}%</div></th>`;
    }).join('') + '</tr>';

  const tbody = rows.map((r, idx) => {
    const cells = months.map(m => {
      const v = r.perMonth[m];
      if (v === null || v === undefined) return '<td class="rank-na">-</td>';
      return `<td style="background:${monthColor(v)};">${fmtPct(v)}</td>`;
    }).join('');
    return `<tr><td class="rank-rank ${idx < 3 ? 'rank-top' : ''}">${idx + 1}</td><td>${r.id}</td>` +
           `<td class="rank-name">${r.name}</td><td>${r.category}</td>${cells}</tr>`;
  }).join('');

  return `<div class="rank-table-wrap"><table class="rank-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table></div>`;
}

function renderRanking() {
  const years = Array.from(new Set(DATA.entities.flatMap(e => Object.keys(e.years)))).sort().reverse();
  if (!currentYear || !years.includes(currentYear)) currentYear = years[0];
  const label = currentCategory === '__ALL__' ? '全部分类' : currentCategory;
  document.getElementById('ranking-title').textContent = `${label} - ${currentYear}年 月度收益排名看板`;
  document.getElementById('ranking-board').innerHTML = buildRankingBoard(currentCategory, currentYear);
}

function setView(view) {
  currentView = view;
  document.querySelectorAll('.view-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  document.getElementById('calendar-view').classList.toggle('hidden', view !== 'calendar');
  document.getElementById('ranking-view').classList.toggle('hidden', view !== 'ranking');
  document.getElementById('entity-bar').classList.toggle('hidden', view !== 'calendar');
  if (view === 'ranking') renderRanking();
}

function setEntity(id) {
  currentEntity = id;
  currentYear = null;
  document.querySelectorAll('.entity-btn').forEach(b => b.classList.toggle('active', b.dataset.id === id));
  renderAll();
}

document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.category-btn').forEach(btn => btn.addEventListener('click', function () { setCategory(this.dataset.category); }));
  document.querySelectorAll('.entity-btn').forEach(btn => btn.addEventListener('click', function () { setEntity(this.dataset.id); }));
  document.querySelectorAll('.view-btn').forEach(btn => btn.addEventListener('click', function () { setView(this.dataset.view); }));
  if (DATA.entities.length) setEntity(DATA.entities[0].id);

  const btnScreenshot = document.getElementById('btn-screenshot');
  btnScreenshot.addEventListener('click', function () {
    const target = document.getElementById('capture-area');
    if (typeof html2canvas === 'undefined') { alert('截图组件加载失败，请检查网络连接后重试'); return; }
    btnScreenshot.disabled = true;
    btnScreenshot.textContent = '⏳ 正在生成截图...';
    html2canvas(target, { backgroundColor: '#f6f7f9', scale: window.devicePixelRatio > 1 ? 2 : 1, useCORS: true }).then(canvas => {
      const link = document.createElement('a');
      const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
      link.download = 'return_calendar_' + ts + '.png';
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
    """把 entities（每个含 id/name/category/years）渲染成一个自带交互的单文件 HTML"""
    categories = []
    for e in entities:
        if e['category'] not in categories:
            categories.append(e['category'])

    category_buttons = '<button class="category-btn active" data-category="__ALL__">全部</button>'
    for c in categories:
        category_buttons += f'<button class="category-btn" data-category="{_esc_attr(c)}">{c}</button>'

    entity_buttons = ''
    for e in entities:
        label = f"{e['name']}({e['id']})"
        entity_buttons += (f'<button class="entity-btn" data-id="{_esc_attr(e["id"])}" '
                           f'data-category="{_esc_attr(e["category"])}">{label}</button>')

    html = _HTML_TEMPLATE
    html = html.replace('__PAGE_TITLE__', page_title)
    html = html.replace('__HEADER_TITLE__', header_title)
    html = html.replace('__SUBTITLE__', subtitle)
    html = html.replace('__CATEGORY_BUTTONS__', category_buttons)
    html = html.replace('__ENTITY_BUTTONS__', entity_buttons)
    html = html.replace('__DATA_JSON__', json.dumps({'entities': entities}, ensure_ascii=False))

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ 已生成交互式收益日历: {os.path.abspath(output_path)}")


def main():
    print('>>> 正在获取宽基指数历史行情，计算日涨跌幅...')
    entities = []
    data_end = None
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_index_returns, code): code for code in BROAD_INDICES}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                _, ret = fut.result()
            except Exception as e:
                print(f'  ⚠️ {code} 获取失败: {e}')
                continue
            if ret is None or ret.empty:
                print(f'  ⚠️ {code} 无有效数据，跳过')
                continue
            ent = build_entity(code, ret)
            if ent is None:
                continue
            entities.append(ent)
            last_date = ret.index.max()
            if data_end is None or last_date > data_end:
                data_end = last_date

    if not entities:
        print('未获取到任何指数数据，退出。')
        return

    order = {code: i for i, code in enumerate(BROAD_INDICES)}
    entities.sort(key=lambda e: order.get(e['id'], 999))

    subtitle = (f"数据截止：{data_end.strftime('%Y-%m-%d') if data_end is not None else '未知'}"
               f"    |    生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")

    render_calendar_dashboard(
        entities,
        page_title='宽基指数收益日历',
        header_title='宽基指数收益日历（红涨绿跌）',
        subtitle=subtitle,
        output_path=OUTPUT_FILE,
    )


if __name__ == '__main__':
    main()
