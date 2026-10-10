# -*- coding: utf-8 -*-
"""
基金近六十日「过热评分」交互式矩阵  +  下一交易日涨跌情景模拟
=================================================================
  1) 单元格内容从「八大指标数值」改为「🟡 偏热 · 69分」形式的评分徽章；
     八大指标退居 hover 悬浮提示（title），信息不丢失，版面大幅压缩。
  2) 新增最左侧的「T+1 情景模拟」列组：
        假设下一交易日涨跌幅为 -3% / -2% / ... / +3%，
        用该涨跌幅推算出下一日净值 → 追加到复权净值序列尾部
        → 重新计算八大指标 → 重新计算过热评分 → 输出评分徽章。
     每个模拟单元格同时给出相对当日评分的变化（如 +6分），
     用来回答「明天再涨 2% 会不会冲进高风险区」这类问题。

"""
import os
import sys
import importlib.util
import warnings

import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')

# ==========================================
# 可调参数
# ==========================================
# 下一交易日模拟的涨跌幅（%），从高到低排列，渲染时也按此顺序从左到右
SIM_RETURNS = [6.0,5.0,4.0,3.0, 2.0, 1.0, 0.0, -1.0, -2.0, -3.0,-4.0,-5.0,-6.0]
SHOW_HIST_DAYS = 60          # 历史列最多显示多少个交易日
CELL_SHOW_INDICATOR_TIP = True   # 单元格是否附带八大指标 hover 提示


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


def _fmt(value, spec, suffix=''):
    """NaN 统一显示成 --，避免表格里出现 nan。"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return '--'
    return format(value, spec) + suffix


def _hex_to_rgba(hex_color, alpha):
    """#RRGGBB -> rgba(r,g,b,alpha)，用于评分徽章的淡色底。"""
    h = str(hex_color).lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6:
        return f'rgba(136,136,136,{alpha})'
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f'rgba({r},{g},{b},{alpha})'


def _indicator_tip(row):
    """把八大指标压成一行 hover 文本。"""
    if not CELL_SHOW_INDICATOR_TIP or row is None:
        return ''
    bw = row.get('boll_bw_pct', np.nan)
    bw = np.nan if bw is None else float(bw)
    parts = [
        f"MACD {_fmt(float(row.get('macd_hist', np.nan)), '+.3f')}",
        f"RSI {_fmt(float(row.get('rsi', np.nan)), '.1f')}",
        f"BIAS {_fmt(float(row.get('bias', np.nan)), '+.2f', '%')}",
        f"KDJ {_fmt(float(row.get('kdj', np.nan)), '.1f')}",
        f"CCI {_fmt(float(row.get('cci', np.nan)), '.1f')}",
        f"SAR {row.get('sar_stat', '--')}",
        f"BOLL {_fmt(float(row.get('boll_pctb', np.nan)), '.3f')}",
        f"BW% {_fmt(np.nan if (bw is None or np.isnan(bw)) else bw * 100, '.0f', '%')}",
    ]
    txt = ' | '.join(parts)
    return txt.replace('"', "'")


def _ind_row_from_series(s):
    """把 compute_eight_indicators 的最后一行转成本脚本内部列名的 dict。"""
    sar_val = s.get('sar_bullish', np.nan)
    try:
        sar_txt = '看涨' if bool(sar_val) else '看跌'
    except Exception:
        sar_txt = '--'
    return {
        'macd_hist': s.get('macd', np.nan),
        'rsi': s.get('rsi', np.nan),
        'bias': s.get('bias', np.nan),
        'kdj': s.get('kdj', np.nan),
        'cci': s.get('cci', np.nan),
        'sar_stat': sar_txt,
        'boll_pctb': s.get('boll_pctb', np.nan),
        'boll_bw_pct': s.get('bw_pct', np.nan),
    }


def simulate_next_day(adj_close, pct):
    """
    在复权净值序列尾部追加一个「下一交易日」的模拟净值：
        新净值 = 最新净值 × (1 + pct/100)
    然后完整重算八大指标与过热评分，返回 (指标dict, 评分float或None)。

    注意：评分链路 compute_heat_score_series -> calc_score_series 与实盘完全一致，
    所以模拟值和真实次日收盘后跑出来的结果是同一套口径。
    """
    s = pd.Series(adj_close).dropna()
    if len(s) < MIN_ROWS:
        return None, None
    last_val = float(s.iloc[-1])
    new_val = last_val * (1.0 + pct / 100.0)

    last_date = s.index[-1]
    try:
        new_date = last_date + pd.tseries.offsets.BDay(1)
    except Exception:
        new_date = last_date + 1

    sim = pd.concat([s, pd.Series([new_val], index=[new_date])])
    sim = sim[~sim.index.duplicated(keep='last')].sort_index()

    ind = compute_eight_indicators(sim)
    net_s, int_s, _, _ = compute_heat_score_series(ind)
    score_s = calc_score_series(net_s, int_s)

    last_score = score_s.iloc[-1]
    score_val = None if pd.isna(last_score) else float(last_score)
    return _ind_row_from_series(ind.iloc[-1]), score_val


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

fund_dict = {}  # code -> (name, type)
for _, row in target_df.iterrows():
    code = str(row['基金代码']).strip()
    name = str(row['基金名称']).strip() if '基金名称' in target_df.columns else ''
    typ = str(row['类型']).strip()
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
# 按基金计算指标 + 逐日评分 + T+1 情景模拟
# ==========================================
data_dict = {}      # display_name -> DataFrame(index=日期, 指标 + score)
heat_scores = {}
sim_dict = {}       # display_name -> {pct: {'score':x,'status':..,'color':..,'tip':..}}
dates_set = set()

print(f">>> 正在计算各基金八大指标与过热评分...")
print(f">>> 同时模拟下一交易日涨跌 {SIM_RETURNS} 共 {len(SIM_RETURNS)} 种情景")

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

    # ★ 先复权，消除拆分 / 份额折算 / 大比例分红造成的净值跳空，再算指标
    adj_close, split_events = build_adjusted_nav(df_fund, code, name)
    ind = compute_eight_indicators(adj_close)

    net_count_s, intensity_s, all_red_s, all_green_s = compute_heat_score_series(ind)
    score_s = calc_score_series(net_count_s, intensity_s)

    df_ind = pd.DataFrame({
        'macd_hist': ind['macd'],
        'rsi': ind['rsi'],
        'bias': ind['bias'],
        'kdj': ind['kdj'],
        'cci': ind['cci'],
        'sar_stat': np.where(ind['sar_bullish'].fillna(False).astype(bool), '看涨', '看跌'),
        'boll_pctb': ind['boll_pctb'],
        'boll_bw_pct': ind['bw_pct'],
        'score': score_s.reindex(ind.index),
    }, index=ind.index)

    df_last60 = df_ind.tail(SHOW_HIST_DAYS).copy()
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

    # ---------- T+1 情景模拟 ----------
    base_score = score_val
    sims = {}
    for pct in SIM_RETURNS:
        ind_row, sim_score = simulate_next_day(adj_close, pct)
        st, mn, ac, cl = get_status_action(sim_score)
        delta = None
        if sim_score is not None and base_score is not None:
            delta = sim_score - base_score
        sims[pct] = {
            'score': sim_score,
            'status': st,
            'meaning': mn,
            'action': ac,
            'color': cl,
            'delta': delta,
            'tip': _indicator_tip(ind_row) if ind_row else '',
        }
    sim_dict[display_name] = sims

    if split_events:
        print(f" 🔧 {code} {name} 历史净值已复权（{len(split_events)} 处事件）")

print(f">>> 完成：{len(data_dict)} 只基金，{len(data_dict) * len(SIM_RETURNS)} 次情景重算")

# 排序：先按 net_count 降序，同 net_count 时按 intensity 降序
sorted_fund_names = sorted(
    data_dict.keys(),
    key=lambda n: (heat_scores[n]['net_count'], heat_scores[n]['intensity']),
    reverse=True
)

# -------------------- 状态定义 --------------------
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

# -------------------- 表头 --------------------
sorted_dates = sorted(list(dates_set), reverse=True)[:SHOW_HIST_DAYS]
date_strs = [d.strftime('%Y-%m-%d') for d in sorted_dates]

index_names = sorted_fund_names

name_to_type = {}
for code, (name, typ) in fund_dict.items():
    display_name = f"{name}({code})"
    name_to_type[display_name] = typ


def _esc_attr(s):
    return str(s).replace('"', '&quot;')


# 类型筛选按钮：按 CSV 里「类型」列实际出现的值动态生成，不写死"板块"
_type_counts = {}
for _name in index_names:
    _t = name_to_type.get(_name, '未分类')
    _type_counts[_t] = _type_counts.get(_t, 0) + 1

type_buttons_html = '<button class="type-btn active" data-type="__ALL__">全部</button>'
for _t in sorted(_type_counts):
    type_buttons_html += (f'<button class="type-btn" data-type="{_esc_attr(_t)}">'
                          f'{_t}（{_type_counts[_t]}）</button>')


def render_badge(status, color, score, delta=None, tip='', extra_class=''):
    """统一的评分徽章渲染：🟡 偏热 · 69分 （+6）"""
    bg = _hex_to_rgba(color, 0.14)
    if score is None or (isinstance(score, float) and np.isnan(score)):
        score_txt = '--'
    else:
        score_txt = f'{score:.0f}分'
    delta_html = ''
    if delta is not None and not (isinstance(delta, float) and np.isnan(delta)):
        if abs(delta) < 0.5:
            d_color, d_sign = '#888888', '±0'
        elif delta > 0:
            d_color, d_sign = '#FF6B6B', f'+{delta:.0f}'
        else:
            d_color, d_sign = '#4ADE80', f'{delta:.0f}'
        delta_html = f'<span class="score-delta" style="color:{d_color};">{d_sign}</span>'
    tip_attr = f' title="{tip}"' if tip else ''
    return (
        f'<div class="score-badge {extra_class}" '
        f'style="color:{color};border-color:{color};background:{bg};"{tip_attr}>'
        f'<span class="score-status">{status}</span>'
        f'<span class="score-num">{score_txt}</span>{delta_html}</div>'
    )


# -------------------- 生成 HTML --------------------
sim_col_count = len(SIM_RETURNS)

html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>基金过热评分矩阵 + T+1 情景模拟</title>
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
            margin-bottom: 6px;
        }}
        .subtitle {{
            text-align: center;
            color: #777;
            font-size: 12px;
            margin-bottom: 12px;
        }}
        .btn-group {{
            margin: 10px 0;
            text-align: center;
        }}
        .btn-group.tool-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 10px;
            text-align: left;
        }}
        .tool-row-left {{
            display: flex;
            align-items: center;
            flex-wrap: wrap;
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
        .summary-label {{ font-weight: bold; }}
        .summary-count {{
            color: #fff;
            font-weight: bold;
            background-color: rgba(255,255,255,0.1);
            border-radius: 10px;
            padding: 1px 8px;
        }}
        .summary-total {{ color: #888; font-size: 12px; margin-left: 8px; }}
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
        .btn-group button:hover {{ background-color: #444; }}
        .btn-group button.active:hover {{ background-color: #528BC6; }}
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
        /* T+1 模拟列的表头与单元格：深蓝底 + 左右分隔线 */
        th.sim-col {{ background-color: #1d2735; color: #7FB3FF; }}
        td.sim-col {{ background-color: rgba(97,175,239,0.05); }}
        th.sim-first, td.sim-first {{ border-left: 2px solid #3a5a80; }}
        th.sim-last,  td.sim-last  {{ border-right: 2px solid #3a5a80; }}
        .sim-head-main {{ display:block; font-weight:bold; }}
        .sim-head-sub  {{ display:block; font-size:10px; color:#6b7f99; font-weight:normal; }}
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
            vertical-align: middle;
        }}
        /* 评分徽章 */
        .score-badge {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            border: 1px solid;
            border-radius: 12px;
            padding: 3px 9px;
            font-size: 11px;
            line-height: 1.2;
            white-space: nowrap;
            cursor: default;
        }}
        .score-status {{ font-weight: normal; }}
        .score-num {{ font-weight: bold; }}
        .score-delta {{ font-size: 10px; font-weight: bold; }}
        .score-badge.sim-badge {{ box-shadow: 0 0 0 1px rgba(255,255,255,0.04) inset; }}
        .hidden {{ display: none; }}
        .col-hidden {{ display: none; }}
        .fund-name {{ margin-bottom: 4px; }}
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
        .screenshot-btn:hover {{ background-color: #E5C07B; color: #1b1b1b; }}
        .screenshot-btn:disabled {{ opacity: 0.6; cursor: default; }}
    </style>
</head>
<body>
    <h2>基金过热评分矩阵（近{SHOW_HIST_DAYS}日） + 下一交易日涨跌情景模拟</h2>
    <div class="subtitle">
        单元格 = 当日八大指标算出的过热评分与状态；鼠标悬停可看八大指标明细。
        左侧 T+1 列 = 假设下一交易日涨跌该幅度 → 推算净值 → 重算指标 → 重算评分，括号内为相对今日评分的变化。
    </div>

    {summary_bar_html}

    <!-- 类型切换按钮：按 CSV 里实际出现的「类型」值动态生成 -->
    <div class="btn-group">
        <span style="margin-right:10px; color:#aaa;">类型：</span>
        {type_buttons_html}
    </div>

    <!-- 时间范围切换按钮（只作用于历史列） -->
    <div class="btn-group">
        <span style="margin-right:10px; color:#aaa;">历史范围：</span>
        <button class="range-btn" data-range="5">近5日</button>
        <button class="range-btn" data-range="10">近10日</button>
        <button class="range-btn" data-range="20">近20日</button>
        <button class="range-btn" data-range="30">近30日</button>
        <button class="range-btn active" data-range="{SHOW_HIST_DAYS}">近{SHOW_HIST_DAYS}日</button>
    </div>

    <div class="btn-group tool-row">
        <div class="tool-row-left">
            <span style="margin-right:10px; color:#aaa;">显示：</span>
            <button id="btn-sim" class="active">T+1 模拟列</button>
            <button id="btn-delta" class="active">评分变化</button>
        </div>
        <button id="btn-screenshot" class="screenshot-btn">📷 保存截图</button>
    </div>

    <div class="table-container" id="capture-area">
        <table>
            <thead>
                <tr>
                    <th class="index-name-th">基金 \\ 日期</th>
"""

# ---- T+1 模拟列表头 ----
for i, pct in enumerate(SIM_RETURNS):
    cls = 'sim-col'
    if i == 0:
        cls += ' sim-first'
    if i == sim_col_count - 1:
        cls += ' sim-last'
    sign = '+' if pct > 0 else ''
    html_content += (
        f'<th class="{cls}">'
        f'<span class="sim-head-main">T+1 {sign}{pct:.0f}%</span>'
        f'<span class="sim-head-sub">模拟</span>'
        f'</th>'
    )

# ---- 历史日期列表头 ----
for ds in date_strs:
    html_content += f'<th class="hist-col">{ds}</th>'
html_content += "</tr></thead><tbody>"

# -------------------- 数据行 --------------------
for name in index_names:
    df_ind = data_dict[name]
    typ = name_to_type.get(name, '')
    type_class = typ  # data-type 直接用 CSV 里的真实类型值

    hs = heat_scores.get(name, {})
    row_status = hs.get('status', '⚪ 无数据')
    row_color = hs.get('color', '#888888')
    row_slug = status_slug_map.get(row_status, 'nodata')
    row_score = hs.get('score', 0)
    row_action = str(hs.get('action', '')).replace('"', "'")

    html_content += (
        f'<tr data-type="{_esc_attr(type_class)}" data-status="{row_slug}">'
        f'<td class="index-name-td">'
        f'<div class="fund-name">{name}</div>'
        f'<div class="risk-badge" style="color:{row_color};border-color:{row_color};" title="{row_action}">'
        f'{row_status} · {row_score:.0f}分</div>'
        f'</td>'
    )

    # ---- T+1 模拟单元格 ----
    sims = sim_dict.get(name, {})
    for i, pct in enumerate(SIM_RETURNS):
        cls = 'sim-col'
        if i == 0:
            cls += ' sim-first'
        if i == sim_col_count - 1:
            cls += ' sim-last'
        s = sims.get(pct)
        if not s:
            html_content += f'<td class="{cls}">-</td>'
            continue
        badge = render_badge(
            s['status'], s['color'], s['score'],
            delta=s['delta'], tip=s['tip'], extra_class='sim-badge'
        )
        html_content += f'<td class="{cls}">{badge}</td>'

    # ---- 历史评分单元格 ----
    for d in sorted_dates:
        if d in df_ind.index:
            row = df_ind.loc[d]
            sc = row.get('score', np.nan)
            sc_val = None if pd.isna(sc) else float(sc)
            st, mn, ac, cl = get_status_action(sc_val)
            badge = render_badge(st, cl, sc_val, delta=None, tip=_indicator_tip(row))
            html_content += f'<td class="hist-col">{badge}</td>'
        else:
            html_content += '<td class="hist-col">-</td>'
    html_content += "</tr>"

html_content += """
        </tbody>
    </table>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
document.addEventListener('DOMContentLoaded', function() {
    // ---- 类型切换（按钮是动态生成的，数量不固定）----
    const typeBtns = document.querySelectorAll('.type-btn');
    const rows = document.querySelectorAll('tr[data-type]');

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
            row.classList.toggle('hidden', type !== '__ALL__' && row.dataset.type !== type);
        });
        typeBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.type === type));
        updateSummary();
    }

    typeBtns.forEach(btn => btn.addEventListener('click', function() { setType(this.dataset.type); }));
    updateSummary();

    // ---- 时间范围切换：只对历史列生效，T+1 模拟列不受影响 ----
    const rangeBtns = document.querySelectorAll('.range-btn');
    const matrixTable = document.querySelector('.table-container table');

    function applyRangeTo(cells, n) {
        let histIdx = 0;
        cells.forEach(function(cell) {
            if (!cell.classList.contains('hist-col')) return;   // 名称列 & 模拟列跳过
            histIdx++;
            cell.classList.toggle('col-hidden', histIdx > n);
        });
    }

    function setRange(n) {
        applyRangeTo(matrixTable.querySelectorAll('thead th'), n);
        matrixTable.querySelectorAll('tbody tr').forEach(function(tr) {
            applyRangeTo(tr.querySelectorAll('td'), n);
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
    const defaultRange = parseInt(document.querySelector('.range-btn.active').dataset.range, 10);
    setRange(defaultRange);

    // ---- T+1 模拟列显隐 ----
    const btnSim = document.getElementById('btn-sim');
    btnSim.addEventListener('click', function() {
        const on = this.classList.toggle('active');
        document.querySelectorAll('.sim-col').forEach(function(c) {
            c.classList.toggle('col-hidden', !on);
        });
    });

    // ---- 评分变化数字显隐 ----
    const btnDelta = document.getElementById('btn-delta');
    btnDelta.addEventListener('click', function() {
        const on = this.classList.toggle('active');
        document.querySelectorAll('.score-delta').forEach(function(c) {
            c.classList.toggle('hidden', !on);
        });
    });

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
                link.download = '基金评分矩阵_' + ts + '.png';
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

output_html = "fund_score_matrix.html"
with open(output_html, "w", encoding="utf-8") as f:
    f.write(html_content)
print(f"✅ 成功生成评分矩阵HTML页面: {output_html}")