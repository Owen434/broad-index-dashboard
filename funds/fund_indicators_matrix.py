# -*- coding: utf-8 -*-
"""
基金近六十日八大指标交互式矩阵
==========================================
指标口径与 zigzag_signal_analyzer.py 完全一致：
  1) 净值先做「后复权」（拆分 / 份额折算 / 大比例分红），再计算指标；
  2) 八大指标、过热评分、状态标签全部直接调用 18 号脚本里的函数，
     算法只维护一份，两个看板的数值永远不会漂移。
"""
import os
import sys
import importlib.util
import warnings

import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')

# ==========================================
# 复用 18 号脚本的指标内核
# ==========================================
# 把两个脚本放在同一目录下即可。18 号脚本的主程序写在 __name__ == "__main__" 保护里，
# 这里 import 它只会拿到函数定义，不会触发它去生成 HTML。
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


def _fmt(value, spec, suffix=''):
    """NaN 统一显示成 --，避免表格里出现 nan。"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return '--'
    return format(value, spec) + suffix


# ==========================================
# 读取基金列表
# 开源版只带一份「板块」示例基金池（公开 ETF），不含任何人的真实持仓/自选。
# 想跑自己关注的基金：复制 funds_universe_example.csv，改成自己的 基金代码/基金名称，
# 类型列可以继续填「板块」，也可以自定义成其他标签（网页上的分类按钮会跟着数据走）。
# ==========================================
target_df = load_csv_smart("funds_universe_example.csv")
target_df.columns = target_df.columns.str.strip()
target_df['基金代码'] = target_df['基金代码'].astype(str).str.split('.').str[0].str.strip().str.zfill(6)
if '类型' not in target_df.columns:
    target_df['类型'] = '未分类'

fund_dict = {}  # code -> (name, type)
for _, row in target_df.iterrows():
    code = str(row['基金代码']).strip()
    name = str(row['基金名称']).strip() if '基金名称' in target_df.columns else ''
    typ = str(row['类型']).strip()
    fund_dict[code] = (name, typ)

# ==========================================
# 读取历史净值
# ==========================================
hist_df = load_csv_smart("1.基金历史净值.csv")
hist_df.columns = hist_df.columns.str.strip()
hist_df['基金代码'] = hist_df['基金代码'].astype(str).str.split('.').str[0].str.strip().str.zfill(6)
hist_df['单位净值'] = pd.to_numeric(hist_df['单位净值'].astype(str).str.replace(',', ''), errors='coerce')
hist_df['日期'] = pd.to_datetime(hist_df['日期'], errors='coerce')
hist_df = hist_df.dropna(subset=['单位净值', '日期'])
hist_df.sort_values(['基金代码', '日期'], inplace=True)

# ==========================================
# 按基金计算指标（口径 = 18 号脚本）
# ==========================================
data_dict = {}      # display_name -> DataFrame (index=日期, columns=指标)
heat_scores = {}
dates_set = set()

print(">>> 正在计算各基金八大指标（净值已复权，口径与 18 号脚本一致）...")
for code, (name, typ) in fund_dict.items():
    df_fund = hist_df[hist_df['基金代码'] == code].copy()
    if df_fund.empty:
        print(f" ⚠️ 未找到基金 {code} - {name} 的历史数据，已跳过")
        continue

    # 与 18 号脚本相同的清洗：同一天只保留最后一条，按日期升序
    df_fund = df_fund.drop_duplicates(subset=['日期'], keep='last').sort_values('日期')
    if len(df_fund) < MIN_ROWS:
        print(f" ⚠️ 基金 {code} 数据不足 {MIN_ROWS} 条，已跳过")
        continue

    if not name or name.lower() == 'nan':
        name = str(df_fund['基金名称'].iloc[0]) if '基金名称' in df_fund.columns else code

    # ★ 关键：先复权，消除拆分 / 份额折算 / 大比例分红造成的净值跳空，再算指标
    adj_close, split_events = build_adjusted_nav(df_fund, code, name)
    ind = compute_eight_indicators(adj_close)

    # 评分同样走 18 号脚本的函数，保证顶部风险标签与波段信号面板一致
    net_count_s, intensity_s, all_red_s, all_green_s = compute_heat_score_series(ind)
    score_s = calc_score_series(net_count_s, intensity_s)

    # 列名映射回本脚本表格使用的名称
    df_ind = pd.DataFrame({
        'macd_hist': ind['macd'],
        'rsi': ind['rsi'],
        'bias': ind['bias'],
        'kdj': ind['kdj'],
        'cci': ind['cci'],
        'sar_stat': np.where(ind['sar_bullish'].fillna(False).astype(bool), '看涨', '看跌'),
        'boll_pctb': ind['boll_pctb'],
        'boll_bw_pct': ind['bw_pct'],
    }, index=ind.index)

    df_last60 = df_ind.tail(60).copy()
    display_name = f"{name}({code})" if code else name
    data_dict[display_name] = df_last60
    for d in df_last60.index:
        dates_set.add(d)

    latest_net = net_count_s.iloc[-1]
    latest_int = intensity_s.iloc[-1]
    latest_score = score_s.iloc[-1]
    score_val = None if pd.isna(latest_score) else float(latest_score)
    status, meaning, action, color = get_status_action(score_val)
    heat_scores[display_name] = {
        'net_count': -99 if pd.isna(latest_net) else float(latest_net),
        'intensity': -99 if pd.isna(latest_int) else float(latest_int),
        'score': 0.0 if score_val is None else score_val,
        'status': status,
        'meaning': meaning,
        'action': action,
        'color': color,
    }
    if split_events:
        print(f" 🔧 {code} {name} 历史净值已复权（{len(split_events)} 处事件），指标基于复权后净值计算")

# 排序：先按 net_count 降序（全红在前，全绿在后），
# 同 net_count 时按 intensity 降序（同样是2个红，谁的红更"红"排前面）
sorted_fund_names = sorted(
    data_dict.keys(),
    key=lambda n: (heat_scores[n]['net_count'], heat_scores[n]['intensity']),
    reverse=True
)

# -------------------- 统计各状态数量，用于顶部汇总条 --------------------
status_order = ['🔴 极端风险', '🟠 高风险', '🟡 偏热', '🟢 正常', '🟦 偏冷', '🔵 冰点', '⚪ 无数据']
status_color_map = {
    '🔴 极端风险': '#EF4444',
    '🟠 高风险': '#F97316',
    '🟡 偏热': '#EAB308',
    '🟢 正常': '#22C55E',
    '🟦 偏冷': '#60A5FA',
    '🔵 冰点': '#3B82F6',
    '⚪ 无数据': '#888888',
}
# 状态 -> 短英文标识（用于HTML data-属性 & JS联动统计）
status_slug_map = {
    '🔴 极端风险': 'extreme',
    '🟠 高风险': 'high',
    '🟡 偏热': 'hot',
    '🟢 正常': 'normal',
    '🟦 偏冷': 'cool',
    '🔵 冰点': 'cold',
    '⚪ 无数据': 'nodata',
}
status_counts = {s: 0 for s in status_order}
for name in data_dict.keys():
    st = heat_scores.get(name, {}).get('status', '⚪ 无数据')
    if st in status_counts:
        status_counts[st] += 1
    else:
        status_counts['⚪ 无数据'] += 1

total_funds = len(data_dict)

# 注意：这里为「全部」状态也生成一份 data-status 项（即使当前计数为0），
# 是为了让JS在切换类型筛选时，能对任意可能出现的状态动态显示/隐藏计数气泡，
# 而不仅限于「全量基金」下出现过的状态。
summary_bar_html = '<div class="summary-bar">'
for st in status_order:
    color = status_color_map[st]
    slug = status_slug_map[st]
    cnt = status_counts[st]
    display_style = 'display:none;' if cnt == 0 else ''
    summary_bar_html += f"""
        <div class="summary-item" data-status="{slug}" style="border-color:{color};{display_style}">
            <span class="summary-label" style="color:{color};">{st}</span>
            <span class="summary-count">{cnt}</span>
        </div>
    """
summary_bar_html += f'<div class="summary-total">共 <span id="summary-total-num">{total_funds}</span> 只基金</div>'
summary_bar_html += '</div>'

# -------------------- 构建表格日期表头 --------------------
sorted_dates = sorted(list(dates_set), reverse=True)[:60]
date_strs = [d.strftime('%Y-%m-%d') for d in sorted_dates]

index_names = sorted_fund_names

# 构造基金名称 -> 类型映射（用于类型筛选）
name_to_type = {}
for code, (name, typ) in fund_dict.items():
    display_name = f"{name}({code})" 
    name_to_type[display_name] = typ

# -------------------- 生成HTML（交互部分与原代码完全一致，仅修改区域->类型） --------------------
html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>基金近六十日六大指标交互式矩阵</title>
    <style>
        body {{
            background-color: #161616;
            color: #ffffff;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0;
            padding: 20px;
        }}
        h2 {{
            text-align: center;
            color: #E5C07B;
            margin-bottom: 15px;
        }}
        .btn-group {{
            margin: 10px 0;
            text-align: center;
        }}
        .btn-group.indicator-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 10px;
            text-align: left;
        }}
        .indicator-row-left {{
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            text-align: left;
        }}
        .summary-bar {{
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            align-items: center;
            gap: 10px;
            margin: 10px 0 15px 0;
        }}
        .summary-item {{
            display: flex;
            align-items: center;
            gap: 6px;
            background-color: #1e1e1e;
            border: 1px solid;
            border-radius: 20px;
            padding: 5px 14px;
            font-size: 12px;
        }}
        .summary-label {{
            font-weight: bold;
        }}
        .summary-count {{
            color: #fff;
            font-weight: bold;
            background-color: rgba(255,255,255,0.1);
            border-radius: 10px;
            padding: 1px 8px;
        }}
        .summary-total {{
            color: #888;
            font-size: 12px;
            margin-left: 8px;
        }}
        .btn-group button {{
            background-color: #333;
            color: #ccc;
            border: 1px solid #555;
            padding: 6px 14px;
            margin: 0 4px 8px 4px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            transition: background-color 0.2s, color 0.2s;
        }}
        .btn-group button.active {{
            background-color: #61AFEF;
            color: #fff;
            border-color: #61AFEF;
        }}
        .btn-group button:hover {{
            background-color: #444;
        }}
        .btn-group button.active:hover {{
            background-color: #528BC6;
        }}
        .table-container {{
            overflow-x: auto;
            max-width: 100%;
            border: 1px solid #333;
            border-radius: 8px;
            background-color: #1e1e1e;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5);
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            font-size: 11px;
            white-space: nowrap;
        }}
        th, td {{
            border: 1px solid #2a2a2a;
            padding: 4px 6px;
            text-align: center;
            vertical-align: middle;
        }}
        th {{
            background-color: #252525;
            color: #61AFEF;
            position: sticky;
            top: 0;
            z-index: 10;
            font-size: 12px;
            padding: 8px 10px;
        }}
        .index-name-th {{
            position: sticky;
            left: 0;
            background-color: #252525;
            z-index: 20;
            font-weight: bold;
            color: #C678DD;
            text-align: left;
            padding-left: 12px;
        }}
        .index-name-td {{
            position: sticky;
            left: 0;
            background-color: #1b1b1b;
            z-index: 5;
            font-weight: bold;
            color: #C678DD;
            text-align: left;
            padding-left: 12px;
            vertical-align: top;
            padding-top: 10px;
        }}
        .indicators-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 2px;
            font-size: 10px;
            text-align: left;
            min-width: 130px;
        }}
        .indicator-item {{
            background: rgba(255, 255, 255, 0.02);
            padding: 2px 4px;
            border-radius: 2px;
            display: flex;
            justify-content: space-between;
        }}
        .ind-label {{
            color: #888;
            margin-right: 3px;
        }}
        .ind-val {{
            font-weight: bold;
        }}
        .hidden {{
            display: none;
        }}
        .col-hidden {{
            display: none;
        }}
        .fund-name {{
            margin-bottom: 4px;
        }}
        .risk-badge {{
            display: inline-block;
            font-size: 10px;
            font-weight: normal;
            border: 1px solid;
            border-radius: 10px;
            padding: 1px 6px;
            white-space: nowrap;
        }}
        .screenshot-btn {{
            background-color: #2a2a2a;
            color: #E5C07B;
            border: 1px solid #E5C07B;
            padding: 6px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }}
        .screenshot-btn:hover {{
            background-color: #E5C07B;
            color: #1b1b1b;
        }}
        .screenshot-btn:disabled {{
            opacity: 0.6;
            cursor: default;
        }}
    </style>
</head>
<body>
    <h2>基金近六十日核心指标矩阵（MACD, RSI, BIAS, KDJ, CCI, SAR, BOLL, BW%）</h2>

    {summary_bar_html}

    <!-- 类型切换按钮 -->
    <div class="btn-group">
        <span style="margin-right:10px; color:#aaa;">类型：</span>
        <button id="btn-all" class="active">全部</button>
        <button id="btn-sector">板块</button>
    </div>

    <!-- 时间范围切换按钮（控制显示最近多少个交易日的列） -->
    <div class="btn-group">
        <span style="margin-right:10px; color:#aaa;">时间范围：</span>
        <button class="range-btn" data-range="5">近5日</button>
        <button class="range-btn" data-range="10">近10日</button>
        <button class="range-btn" data-range="20">近20日</button>
        <button class="range-btn" data-range="30">近30日</button>
        <button class="range-btn active" data-range="60">近60日</button>
    </div>

    <!-- 指标切换按钮 + 截图导出按钮（同一行，截图按钮靠右） -->
    <div class="btn-group indicator-row">
        <div class="indicator-row-left">
            <span style="margin-right:10px; color:#aaa;">指标：</span>
            <button id="btn-macd" class="active">MACD</button>
            <button id="btn-rsi"  class="active">RSI</button>
            <button id="btn-bias" class="active">BIAS</button>
            <button id="btn-kdj"  class="active">KDJ</button>
            <button id="btn-cci"  class="active">CCI</button>
            <button id="btn-sar"  class="active">SAR</button>
            <button id="btn-boll" class="active">BOLL</button>
            <button id="btn-bw"  class="active">BW%</button>
        </div>
        <button id="btn-screenshot" class="screenshot-btn">📷 保存截图</button>
    </div>

    <div class="table-container" id="capture-area">
        <table>
            <thead>
                <tr>
                    <th class="index-name-th">基金 \\ 日期</th>
"""
# 补充日期列表头（原代码遗漏了这一步，导致表格没有日期）
for ds in date_strs:
    html_content += f'<th>{ds}</th>'
html_content += "</tr></thead><tbody>"

# 生成数据行
for name in index_names:
    df_ind = data_dict[name]
    typ = name_to_type.get(name, '')
    # 类型映射为class：目前公开版只保留「板块」一类，其余自定义类型归入 sector 之外的通用样式
    type_class = "sector" if typ == "板块" else ""

    hs = heat_scores.get(name, {})
    row_status = hs.get('status', '⚪ 无数据')
    row_color = hs.get('color', '#888888')
    row_slug = status_slug_map.get(row_status, 'nodata')
    row_score = hs.get('score', 0)

    html_content += (
        f'<tr data-type="{type_class}" data-status="{row_slug}">'
        f'<td class="index-name-td">'
        f'<div class="fund-name">{name}</div>'
        f'<div class="risk-badge" style="color:{row_color};border-color:{row_color};">'
        f'{row_status} · {row_score:.0f}分</div>'
        f'</td>'
    )
    
    for d in sorted_dates:
        if d in df_ind.index:
            row = df_ind.loc[d]
            macd = float(row.get('macd_hist', np.nan))
            rsi = float(row.get('rsi', np.nan))
            bias = float(row.get('bias', np.nan))
            kdj = float(row.get('kdj', np.nan))
            cci = float(row.get('cci', np.nan))
            sar = str(row.get('sar_stat', '中性'))
            boll = float(row.get('boll_pctb', np.nan))
            bw_pct = float(row.get('boll_bw_pct', np.nan))

            # 配色（与原逻辑一致）
            def _c(v, red_ge=None, green_le=None):
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    return "#888888"
                if red_ge is not None and v >= red_ge:
                    return "#FF3333"
                if green_le is not None and v <= green_le:
                    return "#00CC00"
                return "#FFFFFF"

            c_macd = "#888888" if np.isnan(macd) else ("#FF3333" if macd >= 0 else "#00CC00")
            c_rsi   = _c(rsi, 70, 30)
            c_bias  = _c(bias, 4, -4)
            c_kdj   = _c(kdj, 90, 10)
            c_cci   = _c(cci, 100, -100)
            c_sar   = "#c0392b" if sar == "看涨" else "#27ae60"
            c_boll = _c(boll, 0.9, 0.1)
            c_bw = _c(bw_pct, 0.8, 0.2)

            # 判断全红/全绿（仅针对RSI, BIAS, KDJ, CCI, SAR）
            all_red = (c_rsi == '#FF3333' and c_bias == '#FF3333' and c_kdj == '#FF3333' and c_cci == '#FF3333' and c_sar == '#c0392b' and c_boll == '#FF3333')
            all_green = (c_rsi == '#00CC00' and c_bias == '#00CC00' and c_kdj == '#00CC00' and c_cci == '#00CC00' and c_sar == '#27ae60' and c_boll == '#00CC00')
            
            bg_style = ""
            if all_red:
                bg_style = ' style="background-color: rgba(255,0,0,0.2);"'
            elif all_green:
                bg_style = ' style="background-color: rgba(0,255,0,0.2);"'

            cell_html = f"""
            <div class="indicators-grid">
                <div class="indicator-item ind-macd"><span class="ind-label">MACD</span><span class="ind-val" style="color:{c_macd};">{_fmt(macd, '+.3f')}</span></div>
                <div class="indicator-item ind-rsi"><span class="ind-label">RSI</span><span class="ind-val" style="color:{c_rsi};">{_fmt(rsi, '.1f')}</span></div>
                <div class="indicator-item ind-bias"><span class="ind-label">BIAS</span><span class="ind-val" style="color:{c_bias};">{_fmt(bias, '+.2f', '%')}</span></div>
                <div class="indicator-item ind-kdj"><span class="ind-label">KDJ</span><span class="ind-val" style="color:{c_kdj};">{_fmt(kdj, '.1f')}</span></div>
                <div class="indicator-item ind-cci"><span class="ind-label">CCI</span><span class="ind-val" style="color:{c_cci};">{_fmt(cci, '.1f')}</span></div>
                <div class="indicator-item ind-sar"><span class="ind-label">SAR</span><span class="ind-val" style="color:{c_sar};">{sar}</span></div>
                <div class="indicator-item ind-boll"><span class="ind-label">BOLL</span><span class="ind-val" style="color:{c_boll};">{_fmt(boll, '.3f')}</span></div>
                <div class="indicator-item ind-bw"><span class="ind-label">BW%</span><span class="ind-val" style="color:{c_bw};">{_fmt(np.nan if np.isnan(bw_pct) else bw_pct*100, '.0f', '%')}</span></div>
            </div>
            """
            html_content += f"<td{bg_style}>{cell_html}</td>"
        else:
            html_content += "<td>-</td>"
    html_content += "</tr>"

html_content += """
        </tbody>
    </table>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
document.addEventListener('DOMContentLoaded', function() {
    // ---- 类型切换 ----
    const btnAll = document.getElementById('btn-all');
    const btnSector = document.getElementById('btn-sector');
    const rows = document.querySelectorAll('tr[data-type]');

    // ---- 顶部风险汇总条联动 ----
    const summaryItems = document.querySelectorAll('.summary-item');
    const summaryTotalNum = document.getElementById('summary-total-num');

    function updateSummary() {
        const statusCounts = {};
        let visibleTotal = 0;
        rows.forEach(row => {
            if (!row.classList.contains('hidden')) {
                visibleTotal++;
                const st = row.dataset.status;
                statusCounts[st] = (statusCounts[st] || 0) + 1;
            }
        });
        summaryItems.forEach(item => {
            const st = item.dataset.status;
            const cnt = statusCounts[st] || 0;
            item.querySelector('.summary-count').textContent = cnt;
            item.style.display = cnt === 0 ? 'none' : '';
        });
        if (summaryTotalNum) summaryTotalNum.textContent = visibleTotal;
    }

    function setType(type) {
        rows.forEach(row => {
            if (type === 'all') {
                row.classList.remove('hidden');
            } else {
                if (row.dataset.type === type) {
                    row.classList.remove('hidden');
                } else {
                    row.classList.add('hidden');
                }
            }
        });
        // 更新按钮激活样式
        [btnAll, btnSector].forEach(btn => btn.classList.remove('active'));
        if (type === 'all') btnAll.classList.add('active');
        else if (type === 'sector') btnSector.classList.add('active');
        // 联动更新顶部风险汇总条
        updateSummary();
    }

    btnAll.addEventListener('click', function() { setType('all'); });
    btnSector.addEventListener('click', function() { setType('sector'); });

    // 初始加载时也统计一次（默认是"全部"，与后端渲染的初始值一致，但保证逻辑统一）
    updateSummary();

    // ---- 指标切换 ----
    const indicators = ['macd', 'rsi', 'bias', 'kdj', 'cci', 'sar', 'boll', 'bw'];
    indicators.forEach(ind => {
        const btn = document.getElementById('btn-' + ind);
        btn.addEventListener('click', function() {
            document.querySelectorAll('.ind-' + ind).forEach(item => item.classList.toggle('hidden'));
            this.classList.toggle('active');
        });
    });

    // ---- 时间范围切换（日期列已按从新到旧排列，保留前 N 列） ----
    const rangeBtns = document.querySelectorAll('.range-btn');
    const matrixTable = document.querySelector('.table-container table');

    function setRange(n) {
        matrixTable.querySelectorAll('thead th').forEach(function(th, i) {
            if (i === 0) return;          // 第一列是基金名称，永远保留
            th.classList.toggle('col-hidden', i > n);
        });
        matrixTable.querySelectorAll('tbody tr').forEach(function(tr) {
            tr.querySelectorAll('td').forEach(function(td, i) {
                if (i === 0) return;
                td.classList.toggle('col-hidden', i > n);
            });
        });
        rangeBtns.forEach(function(b) {
            b.classList.toggle('active', parseInt(b.dataset.range, 10) === n);
        });
    }

    rangeBtns.forEach(function(btn) {
        btn.addEventListener('click', function() {
            setRange(parseInt(this.dataset.range, 10));
        });
    });

    // 默认显示全部60日
    setRange(60);

    // ---- 截图导出 ----
    const btnScreenshot = document.getElementById('btn-screenshot');
    if (btnScreenshot) {
        btnScreenshot.addEventListener('click', function() {
            const target = document.getElementById('capture-area');
            if (!target || typeof html2canvas === 'undefined') {
                alert('截图组件加载失败，请检查网络连接后重试');
                return;
            }
            btnScreenshot.disabled = true;
            btnScreenshot.textContent = '⏳ 正在生成截图...';
            // 表格有横向滚动条，需按完整内容宽高截取，而不是只截可视区域
            html2canvas(target, {
                backgroundColor: '#1e1e1e',
                width: target.scrollWidth,
                height: target.scrollHeight,
                windowWidth: target.scrollWidth,
                windowHeight: target.scrollHeight,
                scale: window.devicePixelRatio > 1 ? 2 : 1,
                useCORS: true
            }).then(function(canvas) {
                const link = document.createElement('a');
                const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
                link.download = '基金指标矩阵_' + ts + '.png';
                link.href = canvas.toDataURL('image/png');
                link.click();
                btnScreenshot.disabled = false;
                btnScreenshot.textContent = '📷 保存截图';
            }).catch(function(err) {
                console.error('截图失败:', err);
                alert('截图失败，请稍后重试');
                btnScreenshot.disabled = false;
                btnScreenshot.textContent = '📷 保存截图';
            });
        });
    }
});
</script>
</body>
</html>
"""

output_html = "fund_indicators_matrix.html"
with open(output_html, "w", encoding="utf-8") as f:
    f.write(html_content)
print(f"✅ 成功生成基金指标矩阵HTML页面: {output_html}")