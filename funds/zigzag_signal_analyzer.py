#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基金波浪理论(ZigZag) + 波段信号量化看板 (全功能单文件整合版)
================================================
"""

import os
import json
import warnings
import pandas as pd
import numpy as np
import plotly.graph_objects as go

warnings.filterwarnings("ignore")

# ==========================================
# 全局配置
# ==========================================
WINDOW_CONFIG = {
    '1M': {'label': '近1月', 'days': 30, 'trading_days': 21},
    '3M': {'label': '近3月', 'days': 90, 'trading_days': 63},
    '6M': {'label': '近6月', 'days': 180, 'trading_days': 126},
    '1Y': {'label': '近1年', 'days': 365, 'trading_days': 252},
}
WINDOW_ORDER = ['1M', '3M', '6M', '1Y']

ZIGZAG_THRESHOLD = 0.05
OVERHEAT_LINE = 80   # 过热预警虚线
OVERSOLD_LINE = 20   # 过冷预警虚线
EXTREME_HIGH_LINE = 90  # 极端过热预警虚线
EXTREME_LOW_LINE = 10   # 极端过冷预警虚线
MIN_ROWS = 30        # 数据不足30个交易日的基金跳过

# ---- 净值拆分 / 份额折算 / 大比例分红 的复权处理 ----
# 'auto'  : 复权净值 > 日增长率 > 自动跳变检测（推荐）
# 'column': 只用 复权净值/累计净值 列，没有就用单位净值原样
# 'off'   : 完全不复权（保留旧行为）
NAV_ADJUST_MODE = 'auto'
SPLIT_JUMP_THRESHOLD = 0.15   # 单日涨跌超过 ±15% 视为疑似拆分/折算（非杠杆基金几乎不可能）
GROWTH_MATCH_TOL = 0.02       # 净值涨跌幅与日增长率偏离超过 2% 才判定为断点（核心校验阈值）
DIVIDEND_ADJUST = True        # 没有日增长率时，是否用累计净值推普通分红（可靠性较低）
DIV_MIN_AMOUNT = 0.0005       # 每份分红额超过该值才复权，用于滤掉累计净值4位小数的舍入噪声
MAX_DIV_PER_YEAR = 4          # 年均分红次数超过该值 → 判定累计净值不可靠，放弃分红复权
MAX_TOTAL_FACTOR = 20.0       # 累计复权倍数超过该值 → 判定检测失控，放弃复权并告警

# Plotly 图表通用配置：去掉右上角半截的 plotly logo 图标
PLOTLY_CONFIG = {'displaylogo': False, 'responsive': True,
                 'modeBarButtonsToRemove': ['lasso2d', 'select2d']}


# ==========================================
# 净值复权：处理基金拆分 / 份额折算 / 大比例分红
# ==========================================
def _to_num(series):
    return pd.to_numeric(series.astype(str).str.replace(',', '').str.replace('%', ''),
                         errors='coerce')


def _apply_factors(nav, factors, events, code, fund_name):
    """套用复权因子并做失控保护。"""
    if not events:
        return nav, []
    span = float(np.max(factors)) / max(float(np.min(factors)), 1e-12)
    if span > MAX_TOTAL_FACTOR or not np.isfinite(span):
        print(f" ⚠️ {code} {fund_name} 复权因子累计达 {span:.1f} 倍，疑似误判，已放弃复权（请检查数据源）")
        return nav, []
    adj = pd.Series(nav.values.astype(float) * factors, index=nav.index)
    if not np.isfinite(adj.values).all() or (adj <= 0).any():
        print(f" ⚠️ {code} {fund_name} 复权结果异常，已放弃复权")
        return nav, []
    return adj, events


def _adjust_by_growth(nav, growth, code, fund_name, jump_threshold):
    """核心思路：净值的实际涨跌幅本来就该等于数据源给的日增长率。

    两者吻合 → 这一天没有任何除权动作，原样保留；
    两者对不上 → 说明当天发生了拆分/份额折算/分红，用日增长率还原真实涨跌，
    并把该日之前的历史净值整体缩放（后复权，最新净值保持不变）。
    """
    vals = nav.values.astype(float)
    g = growth.values.astype(float)
    n = len(vals)
    factors = np.ones(n)
    f = 1.0
    events = []
    for i in range(n - 1, 0, -1):
        prev, cur = vals[i - 1], vals[i]
        if prev > 0 and cur > 0 and np.isfinite(g[i]):
            ro = cur / prev            # 净值实际比值
            rt = 1.0 + g[i]            # 日增长率给出的真实比值
            if rt > 0 and abs(ro / rt - 1.0) > GROWTH_MATCH_TOL:
                f *= ro / rt
                events.append({
                    'date': nav.index[i].strftime('%Y-%m-%d'),
                    'kind': '拆分/份额折算' if abs(ro - 1.0) > jump_threshold else '分红除权',
                    'from': round(prev, 4),
                    'to': round(cur, 4),
                    'ratio': round(ro, 4),
                    'real': round(g[i] * 100, 2),
                })
        factors[i - 1] = f
    events.reverse()
    return _apply_factors(nav, factors, events, code, fund_name)


def _adjust_by_jump(nav, cum, code, fund_name, jump_threshold):
    """没有日增长率时的兜底：只认异常跳空，外加累计净值推出的分红。"""
    vals = nav.values.astype(float)
    n = len(vals)
    factors = np.ones(n)
    f = 1.0
    events = []
    years = max((nav.index[-1] - nav.index[0]).days / 365.0, 0.5)
    for i in range(n - 1, 0, -1):
        prev, cur = vals[i - 1], vals[i]
        if prev <= 0 or cur <= 0:
            factors[i - 1] = f
            continue
        ro = cur / prev
        # 当日新增每份分红额 = 累计分红(累计净值-单位净值)的增量。
        # 不能直接比“累计净值涨跌幅 vs 单位净值涨跌幅”：分红后两条线会永久差一个常数，
        # 那样会把分红之后的每一天都误判成分红。
        div = None
        if cum is not None and pd.notna(cum.iloc[i]) and pd.notna(cum.iloc[i - 1]):
            d = (float(cum.iloc[i]) - cur) - (float(cum.iloc[i - 1]) - prev)
            if DIV_MIN_AMOUNT < d < prev * 0.6:
                div = d

        kind, rt = None, None
        if abs(ro - 1.0) > jump_threshold:
            kind, rt = '拆分/份额折算', 1.0     # 默认认为当日真实涨跌 0%
            if div is not None and abs((cur + div) / prev - 1.0) <= jump_threshold:
                kind, rt = '大比例分红', (cur + div) / prev
        elif DIVIDEND_ADJUST and div is not None:
            kind, rt = '分红除权', (cur + div) / prev

        if kind is not None and rt and rt > 0:
            f *= ro / rt
            events.append({
                'date': nav.index[i].strftime('%Y-%m-%d'), 'kind': kind,
                'from': round(prev, 4), 'to': round(cur, 4), 'ratio': round(ro, 4),
            })
        factors[i - 1] = f
    events.reverse()

    # 累计净值质量兜底：分红次数明显超常 → 认为该列不可靠，只保留跳空复权
    minor = [e for e in events if e['kind'] == '分红除权']
    if len(minor) > MAX_DIV_PER_YEAR * years:
        print(f" ⚠️ {code} {fund_name} 由累计净值推出 {len(minor)} 次分红（年均过高），"
              f"判定该列不可靠，仅保留跳空复权")
        return _adjust_by_jump_only(nav, code, fund_name, jump_threshold)
    return _apply_factors(nav, factors, events, code, fund_name)


def _adjust_by_jump_only(nav, code, fund_name, jump_threshold):
    vals = nav.values.astype(float)
    n = len(vals)
    factors = np.ones(n)
    f = 1.0
    events = []
    for i in range(n - 1, 0, -1):
        prev, cur = vals[i - 1], vals[i]
        if prev > 0 and cur > 0:
            ro = cur / prev
            if abs(ro - 1.0) > jump_threshold:
                f *= ro
                events.append({'date': nav.index[i].strftime('%Y-%m-%d'),
                               'kind': '拆分/份额折算', 'from': round(prev, 4),
                               'to': round(cur, 4), 'ratio': round(ro, 4)})
        factors[i - 1] = f
    events.reverse()
    return _apply_factors(nav, factors, events, code, fund_name)


def build_adjusted_nav(fund_data, code='', fund_name='', mode=NAV_ADJUST_MODE,
                       jump_threshold=SPLIT_JUMP_THRESHOLD):
    """把单位净值序列还原成连续可比的复权净值。

    采用「后复权到最新」：只把历史值按比例缩放，最新一天的净值保持真实值不变，
    因此面板上显示的“最新净值”不受影响，而 ZigZag / 八大指标不再被跳空污染。

    优先级：复权净值列 > 日增长率校验 > 跳空+累计净值兜底。
    返回 (adj_series, events)。
    """
    nav = _to_num(fund_data['单位净值'])
    nav.index = fund_data['日期']
    nav = nav.dropna()

    if mode == 'off' or len(nav) < 2:
        return nav, []

    # 优先级 1：数据源直接给了复权净值
    for col in ('复权净值', '后复权净值', '复权单位净值'):
        if col in fund_data.columns:
            adj = _to_num(fund_data[col])
            adj.index = fund_data['日期']
            adj = adj.reindex(nav.index)
            if adj.notna().all() and (adj > 0).all():
                return adj * (nav.iloc[-1] / adj.iloc[-1]), []

    # 优先级 2：用日增长率做逐日校验（最可靠，能自动区分“真实暴跌”和“除权跳空”）
    if mode == 'auto':
        for col in ('日增长率', '日增长率(%)', '日涨幅', '涨跌幅'):
            if col in fund_data.columns:
                g = _to_num(fund_data[col])
                g.index = fund_data['日期']
                g = g.reindex(nav.index) / 100.0
                if g.iloc[1:].notna().mean() > 0.9:
                    adj, events = _adjust_by_growth(nav, g, code, fund_name, jump_threshold)
                    _log_events(code, fund_name, events, '日增长率校验')
                    return adj, events

    if mode != 'auto':
        return nav, []

    # 优先级 3：只有单位净值（+累计净值）时的兜底方案
    cum = None
    if '累计净值' in fund_data.columns:
        cum = _to_num(fund_data['累计净值'])
        cum.index = fund_data['日期']
        cum = cum.reindex(nav.index)
    adj, events = _adjust_by_jump(nav, cum, code, fund_name, jump_threshold)
    _log_events(code, fund_name, events, '跳空检测')
    return adj, events


def _log_events(code, fund_name, events, source):
    if not events:
        return
    major = [e for e in events if e['kind'] != '分红除权']
    minor = len(events) - len(major)
    for e in major:
        real = f"，当日真实涨跌 {e['real']}%" if 'real' in e else ''
        print(f" 🔧 {code} {fund_name} [{source}] {e['kind']}：{e['date']} "
              f"{e['from']} → {e['to']}（比例 {e['ratio']}{real}），历史净值已复权")
    if minor:
        print(f" 🔧 {code} {fund_name} [{source}] 另有 {minor} 次分红除权，已一并复权")


# ==========================================
# 算法函数：ZigZag 波浪理论
# ==========================================
def calculate_zigzag(df: pd.DataFrame, price_col: str = 'close', change_pct: float = 0.05):
    prices = df[price_col].values
    n = len(prices)
    pivots = np.zeros(n, dtype=int)
    trend = 0
    last_pivot_idx = 0
    last_pivot_price = prices[0]

    for i in range(1, n):
        price = prices[i]
        if trend == 0:
            if price >= last_pivot_price * (1 + change_pct):
                pivots[0] = -1
                trend = 1
                last_pivot_idx = i
                last_pivot_price = price
                pivots[i] = 1
            elif price <= last_pivot_price * (1 - change_pct):
                pivots[0] = 1
                trend = -1
                last_pivot_idx = i
                last_pivot_price = price
                pivots[i] = -1
        elif trend == 1:
            if price > last_pivot_price:
                pivots[last_pivot_idx] = 0
                last_pivot_idx = i
                last_pivot_price = price
                pivots[i] = 1
            elif price <= last_pivot_price * (1 - change_pct):
                trend = -1
                last_pivot_idx = i
                last_pivot_price = price
                pivots[i] = -1
        elif trend == -1:
            if price < last_pivot_price:
                pivots[last_pivot_idx] = 0
                last_pivot_idx = i
                last_pivot_price = price
                pivots[i] = -1
            elif price >= last_pivot_price * (1 + change_pct):
                trend = 1
                last_pivot_idx = i
                last_pivot_price = price
                pivots[i] = 1

    df_out = df.copy()
    df_out['pivot'] = pivots
    return df_out


# ==========================================
# 算法函数：波段技术指标与评分
# ==========================================
def rolling_percentile_fast(series: pd.Series, window: int):
    """滚动百分位计算"""
    def pct_rank(x):
        if len(x) < 1:
            return np.nan
        return float((x <= x[-1]).sum()) / len(x)
    return series.rolling(window, min_periods=1).apply(pct_rank, raw=True)


def compute_eight_indicators(close: pd.Series) -> pd.DataFrame:
    """计算 MACD / RSI / BIAS / KDJ / CCI / SAR / BOLL%B / BW%"""
    exp1 = close.ewm(span=12, adjust=False).mean()
    exp2 = close.ewm(span=26, adjust=False).mean()
    dif = exp1 - exp2
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_hist = 2 * (dif - dea)

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-6)
    rsi = 100 - (100 / (1 + rs))

    ma6 = close.rolling(window=6).mean()
    bias = (close - ma6) / ma6 * 100

    low9 = close.rolling(window=9).min()
    high9 = close.rolling(window=9).max()
    rsv = (close - low9) / (high9 - low9 + 1e-6) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d

    tp = close
    tp_ma = tp.rolling(window=14).mean()
    md = (tp - tp_ma).abs().rolling(window=14).mean()
    cci = (tp - tp_ma) / (0.015 * md + 1e-6)

    ma5 = close.rolling(window=5).mean()
    sar_bullish = close > ma5

    mid = close.rolling(window=20).mean()
    std = close.rolling(window=20).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    pctb = (close - lower) / (upper - lower + 1e-6)

    bw = (upper - lower) / (mid + 1e-6)
    bw_pct20 = rolling_percentile_fast(bw, 20)
    bw_pct60 = rolling_percentile_fast(bw, 60)
    bw_pct = pd.concat([bw_pct20, bw_pct60], axis=1).max(axis=1)

    return pd.DataFrame({
        'macd': macd_hist, 'rsi': rsi, 'bias': bias, 'kdj': j, 'cci': cci,
        'sar_bullish': sar_bullish, 'boll_pctb': pctb, 'bw_pct': bw_pct,
    })


def compute_heat_score_series(ind: pd.DataFrame):
    idx = ind.index
    intensity = pd.Series(0.0, index=idx)
    red = pd.Series(0.0, index=idx)
    green = pd.Series(0.0, index=idx)

    rsi, bias, kdj, cci = ind['rsi'], ind['bias'], ind['kdj'], ind['cci']
    pctb, bw_pct, sar_bull = ind['boll_pctb'], ind['bw_pct'], ind['sar_bullish']

    red_rsi, green_rsi = rsi >= 70, rsi <= 30
    red += red_rsi.astype(float)
    green += green_rsi.astype(float)
    intensity = intensity.add(((rsi - 70) / 30).where(red_rsi, 0), fill_value=0)
    intensity = intensity.add((-(30 - rsi) / 30).where(green_rsi, 0), fill_value=0)

    red_bias, green_bias = bias >= 4, bias <= -4
    red += red_bias.astype(float)
    green += green_bias.astype(float)
    intensity = intensity.add((((bias - 4) / 4).clip(upper=1.5)).where(red_bias, 0), fill_value=0)
    intensity = intensity.add(((-(-4 - bias) / 4).clip(lower=-1.5)).where(green_bias, 0), fill_value=0)

    red_kdj, green_kdj = kdj >= 90, kdj <= 10
    red += red_kdj.astype(float)
    green += green_kdj.astype(float)
    intensity = intensity.add(((kdj - 90) / 30).where(red_kdj, 0), fill_value=0)
    intensity = intensity.add((-(10 - kdj) / 30).where(green_kdj, 0), fill_value=0)

    red_cci, green_cci = cci >= 100, cci <= -100
    red += red_cci.astype(float)
    green += green_cci.astype(float)
    intensity = intensity.add((((cci - 100) / 100).clip(upper=1.5)).where(red_cci, 0), fill_value=0)
    intensity = intensity.add(((-(-100 - cci) / 100).clip(lower=-1.5)).where(green_cci, 0), fill_value=0)

    red_pctb, green_pctb = pctb >= 0.9, pctb <= 0.1
    red += red_pctb.astype(float)
    green += green_pctb.astype(float)
    intensity = intensity.add((((pctb - 0.9) / 0.1).clip(upper=1.5)).where(red_pctb, 0), fill_value=0)
    intensity = intensity.add(((-(0.1 - pctb) / 0.1).clip(lower=-1.5)).where(green_pctb, 0), fill_value=0)

    sar_valid = sar_bull.notna()
    red_sar = sar_bull == True
    green_sar = (sar_bull == False) & sar_valid
    red += red_sar.astype(float)
    green += green_sar.astype(float)
    intensity = intensity.add(pd.Series(np.where(red_sar, 0.3, 0.0), index=idx), fill_value=0)
    intensity = intensity.add(pd.Series(np.where(green_sar, -0.3, 0.0), index=idx), fill_value=0)

    amp = ((bw_pct - 0.8) / 0.2).clip(lower=0)
    intensity = intensity.add(amp.where((bw_pct >= 0.8) & (red > green), 0), fill_value=0)
    intensity = intensity.add((-amp).where((bw_pct >= 0.8) & (green > red), 0), fill_value=0)

    net_count = red - green
    core_ok = rsi.notna() & bias.notna() & kdj.notna() & cci.notna() & pctb.notna() & sar_bull.notna()
    net_count = net_count.where(core_ok)
    intensity = intensity.where(core_ok)

    all_red = (red_rsi & red_bias & red_kdj & red_cci & red_pctb & red_sar) & core_ok
    all_green = (green_rsi & green_bias & green_kdj & green_cci & green_pctb & green_sar) & core_ok

    return net_count, intensity, all_red, all_green


def calc_score_series(net_count: pd.Series, intensity: pd.Series, max_net: float = 6, max_intensity: float = 9.0):
    norm_net = net_count / max_net
    norm_intensity = (intensity / max_intensity).clip(-1, 1)
    return (50 + norm_net * 35 + norm_intensity * 15).clip(0, 100)


def get_status_action(score):
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return ('⚪ 无数据', '数据不足', '--', '#888888')
    if score < 20:
        return ('🔵 冰点', '极度超卖 / 极度看跌', '重点关注加仓机会', '#3B82F6')
    elif score < 40:
        return ('🟦 偏冷', '偏弱 / 超卖区域', '可考虑分批加仓', '#60A5FA')
    elif score < 60:
        return ('🟢 正常', '健康区间', '正常持有', '#22C55E')
    elif score < 80:
        return ('🟡 偏热', '开始升温', '观察，暂不加仓', '#EAB308')
    elif score < 90:
        return ('🟠 高风险', '多数指标过热', '考虑减仓', '#F97316')
    else:
        return ('🔴 极端风险', '几乎全部爆红', '优先大幅减仓', '#EF4444')


def compute_fund_signal(close: pd.Series):
    ind = compute_eight_indicators(close)
    latest_ind = ind.iloc[-1]

    fixed_indicators = {
        'macd': None if pd.isna(latest_ind['macd']) else round(float(latest_ind['macd']), 3),
        'rsi': None if pd.isna(latest_ind['rsi']) else round(float(latest_ind['rsi']), 1),
        'bias': None if pd.isna(latest_ind['bias']) else round(float(latest_ind['bias']), 2),
        'kdj': None if pd.isna(latest_ind['kdj']) else round(float(latest_ind['kdj']), 1),
        'cci': None if pd.isna(latest_ind['cci']) else round(float(latest_ind['cci']), 1),
        'sar': None if pd.isna(latest_ind['sar_bullish']) else ('看涨' if bool(latest_ind['sar_bullish']) else '看跌'),
        'boll_pctb': None if pd.isna(latest_ind['boll_pctb']) else round(float(latest_ind['boll_pctb']), 3),
        'bw_pct': None if pd.isna(latest_ind['bw_pct']) else round(float(latest_ind['bw_pct']) * 100, 1),
    }

    net, inten, all_red, all_green = compute_heat_score_series(ind)
    score_series = calc_score_series(net, inten)
    latest_score = score_series.iloc[-1]
    latest_score_val = None if pd.isna(latest_score) else float(latest_score)

    # 计算不同窗口下的评分历史分位数时间序列
    window_pct_series = {}
    for key in WINDOW_ORDER:
        t_days = WINDOW_CONFIG[key]['trading_days']
        window_pct_series[key] = rolling_percentile_fast(score_series, t_days) * 100.0

    # 全历史分位序列
    def expanding_pct(s):
        return s.expanding(min_periods=2).apply(lambda x: (x <= x[-1]).sum() / len(x) * 100.0, raw=True)
    window_pct_series['ALL'] = expanding_pct(score_series)

    # 修复分位数不同步问题：让顶部面板分位数与图表曲线最新值严格一致
    percentile_snapshot = {}
    for key in WINDOW_ORDER:
        last_pct = window_pct_series[key].iloc[-1]
        percentile_snapshot[key] = round(float(last_pct), 1) if pd.notna(last_pct) else None

    status, meaning, action, color = get_status_action(latest_score_val)
    latest_all_red = bool(all_red.iloc[-1]) if len(all_red) else False
    latest_all_green = bool(all_green.iloc[-1]) if len(all_green) else False

    ind_history = {
        'dates': [d.strftime('%Y-%m-%d') for d in ind.index],
        'macd': [None if pd.isna(v) else round(float(v), 3) for v in ind['macd']],
        'rsi': [None if pd.isna(v) else round(float(v), 1) for v in ind['rsi']],
        'bias': [None if pd.isna(v) else round(float(v), 2) for v in ind['bias']],
        'kdj': [None if pd.isna(v) else round(float(v), 1) for v in ind['kdj']],
        'cci': [None if pd.isna(v) else round(float(v), 1) for v in ind['cci']],
        'sar': [None if pd.isna(v) else ('看涨' if bool(v) else '看跌') for v in ind['sar_bullish']],
        'boll_pctb': [None if pd.isna(v) else round(float(v), 3) for v in ind['boll_pctb']],
        'bw_pct': [None if pd.isna(v) else round(float(v) * 100, 1) for v in ind['bw_pct']],
    }

    pct_histories_json = {}
    for k, s in window_pct_series.items():
        pct_histories_json[k] = [None if pd.isna(v) else round(float(v), 1) for v in s.values]

    signal = {
        'latest': {
            'score': None if latest_score_val is None else round(latest_score_val, 1),
            'status': status, 'meaning': meaning, 'action': action, 'color': color,
            'all_red': latest_all_red, 'all_green': latest_all_green,
        },
        'score_history': {
            'dates': [d.strftime('%Y-%m-%d') for d in score_series.index],
            'score': [None if pd.isna(v) else round(float(v), 2) for v in score_series.values],
            'pct_windows': pct_histories_json
        },
    }

    return fixed_indicators, percentile_snapshot, signal, ind_history


# ==========================================
# 构建 Plotly 图表
# ==========================================
def build_nav_figure(df_plot, pivots_df, peaks, troughs, code_str, fund_name, last_date, last_nav,
                      next_date, last_peak_val, last_trough_val, initial_pct=1.5,
                      split_events=None):
    initial_sim_nav = last_nav * (1 + initial_pct / 100)
    show_sim = abs(initial_pct) > 1e-9   # 模拟涨幅为 0 时隐藏模拟线段与标记
    init_color = 'gray'
    init_text = f"模拟: {initial_sim_nav:.4f}"
    if last_peak_val != 'null' and initial_sim_nav > last_peak_val:
        init_color = 'red'
        init_text = f"突破波峰: {initial_sim_nav:.4f}"
    elif last_trough_val != 'null' and initial_sim_nav < last_trough_val:
        init_color = 'green'
        init_text = f"跌破波谷: {initial_sim_nav:.4f}"

    fig = go.Figure()
    # ★ 统一转成 python 原生类型，避免 plotly 二进制序列化
    def _xy(d):
        return (d.index.strftime('%Y-%m-%d').tolist(),
                pd.to_numeric(d['close'], errors='coerce').astype(float).tolist())

    x_nav,  y_nav  = _xy(df_plot)
    x_piv,  y_piv  = _xy(pivots_df)
    x_pk,   y_pk   = _xy(peaks)
    x_tr,   y_tr   = _xy(troughs)
    last_date_s = last_date.strftime('%Y-%m-%d')
    next_date_s = next_date.strftime('%Y-%m-%d')
    
    
    # 强制加上严格的 hovertemplate，避免 Plotly 识别错误显示行索引和计数
    fig.add_trace(go.Scatter(
        x=x_nav, y=y_nav, mode='lines', name='单位净值',
        line=dict(color='gray', width=1.5, dash='solid'), opacity=0.6,
        hovertemplate='日期: %{x|%Y-%m-%d}<br>单位净值: %{y:.4f}<extra></extra>'
    ))
    fig.add_trace(go.Scatter(
        x=[last_date_s, next_date_s], y=[last_nav, initial_sim_nav], mode='lines',
        name='模拟趋势', line=dict(color=init_color, width=1.5, dash='solid'), showlegend=False,
        hoverinfo='skip', visible=show_sim
    ))
    fig.add_trace(go.Scatter(
        x=[next_date_s], y=[initial_sim_nav], mode='markers', name='模拟下一交易日',
        marker=dict(color=init_color, size=14, symbol='circle', line=dict(color='black', width=1)),
        cliponaxis=False, showlegend=True, visible=show_sim,
        hovertemplate='日期: %{x|%Y-%m-%d}<br>模拟净值: %{y:.4f}<extra></extra>',
    ))
    fig.add_trace(go.Scatter(
        x=[next_date_s], y=[initial_sim_nav], mode='text', showlegend=False,
        text=[f"&nbsp;<br>{init_text}"], textposition='bottom left', cliponaxis=False,
        textfont=dict(color=init_color, size=10), hoverinfo='skip', visible=show_sim,
    ))
    fig.add_trace(go.Scatter(
        x=x_piv, y=y_piv, mode='lines+markers',
        name=f'ZigZag 浪形 (阈值: {ZIGZAG_THRESHOLD*100}%)',
        line=dict(color='blue', width=3), marker=dict(size=6, color='blue'),
        hovertemplate='日期: %{x|%Y-%m-%d}<br>ZigZag点: %{y:.4f}<extra></extra>',
    ))
    # 图例占位：Plotly 3.x 不会为“完全没有数据点”的轨迹生成图例，
    # 因此放 1 个空值(None)占位点——图上不显示任何东西，但图例条目会正常出现。
    # 真正的三角用文字绘制，便于像素级避让净值线
    fig.add_trace(go.Scatter(
        x=[x_nav[0]] if x_nav else [last_date_s], y=[None], mode='markers', name='波峰 (浪顶)',
        marker=dict(color='red', size=12, symbol='triangle-down'), hoverinfo='skip',
    ))
    fig.add_trace(go.Scatter(
        x=x_pk, y=y_pk, mode='text', showlegend=False, cliponaxis=False,
        text=[f"{i + 1}<br>▼" for i in range(len(peaks))], textposition='top center',
        textfont=dict(color='red', size=11),
        hovertemplate='波峰日期: %{x|%Y-%m-%d}<br>波峰净值: %{y:.4f}<extra></extra>',
    ))
    fig.add_trace(go.Scatter(
        x=[x_nav[0]] if x_nav else [last_date_s], y=[None], mode='markers', name='波谷 (浪底)',
        marker=dict(color='green', size=12, symbol='triangle-up'), hoverinfo='skip',
    ))
    fig.add_trace(go.Scatter(
        x=x_tr, y=y_tr, mode='text', showlegend=False, cliponaxis=False,
        text=[f"▲<br>{i + 1}" for i in range(len(troughs))], textposition='bottom center',
        textfont=dict(color='green', size=11),
        hovertemplate='波谷日期: %{x|%Y-%m-%d}<br>波谷净值: %{y:.4f}<extra></extra>',
    ))

    # 累计涨幅：只做图例展示（不画线），数值随所选时间窗口在前端实时刷新。
    # 基准日口径与天天基金等平台一致：取「窗口起点当天或之前最近的一个交易日」，
    # 而不是窗口内的第一个交易日——否则遇上周末/长假，基准会被整体后移，涨幅算不准。
    cutoff = max(df_plot.index.min(), last_date - pd.DateOffset(years=1))
    _before = df_plot.index[df_plot.index <= cutoff]
    default_start = _before[-1] if len(_before) else df_plot.index[0]
    win_series = df_plot['close'].loc[default_start:]
    cum_pct = None
    if len(win_series) >= 2 and float(win_series.iloc[0]) > 0:
        cum_pct = (float(win_series.iloc[-1]) / float(win_series.iloc[0]) - 1.0) * 100
    cum_color = '#888888' if cum_pct is None else ('#FF3333' if cum_pct >= 0 else '#00CC00')
    cum_name = ('近1年累计涨幅 --' if cum_pct is None else f'近1年累计涨幅 {cum_pct:+.2f}%')
    legend_anchor_x = x_nav[0] if x_nav else last_date_s
    fig.add_trace(go.Scatter(
        x=[legend_anchor_x], y=[None], mode='lines', name=cum_name, meta='cumret',
        line=dict(color=cum_color, width=3), hoverinfo='skip', showlegend=True,
    ))

    # 在净值图上标出拆分/折算发生的位置
    # 注意：不能用 fig.add_vline(annotation_text=...)，Plotly 会对字符串日期做求均值运算而报 TypeError，
    # 这里改成 add_shape + add_annotation 手动画。
    for e in (split_events or []):
        if e.get('kind') == '分红除权':
            continue    # 普通分红次数多、幅度小，不在图上逐个标注
        fig.add_shape(type='line', xref='x', yref='paper',
                      x0=e['date'], x1=e['date'], y0=0, y1=1,
                      line=dict(color='#E5C07B', width=1, dash='dot'), layer='below')
        fig.add_annotation(xref='x', yref='paper', x=e['date'], y=0.99,
                           text=f"{e['kind']} {e['ratio']}", showarrow=False,
                           xanchor='left', yanchor='top',
                           font=dict(color='#E5C07B', size=10),
                           bgcolor='rgba(0,0,0,0.35)')

    # 拓展右侧 margin，防止 Plotly 工具栏遮挡标签
    fig.update_layout(
        title={'text': f"{code_str} {fund_name} 波浪理论分析 (ZigZag)" +
                       ('　<span style="font-size:13px;color:#E5C07B">历史净值已复权</span>' if split_events else ''),
               'x': 0.5,
               'xanchor': 'center', 'font': {'size': 20}},
        xaxis_title='日期', yaxis_title='单位净值',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1,
                    bgcolor='rgba(0,0,0,0)'),
        hovermode='closest', template='plotly_dark',
        modebar=dict(bgcolor='rgba(0,0,0,0)', color='#888', activecolor='#61AFEF'),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#cccccc', family='-apple-system,sans-serif', size=12),
        hoverlabel=dict(bgcolor='#252525', bordercolor='#444', font=dict(color='#eee')),
        width=1150, height=560, margin=dict(l=90, r=150, t=80, b=60),
    )
    # 默认按 1 年的时间范围展示（纵轴同步收敛到该窗口内的净值范围）
    fig.update_xaxes(
        range=[default_start.strftime('%Y-%m-%d'),
               (next_date + pd.Timedelta(days=12)).strftime('%Y-%m-%d')],
        type='date', tickformat='%Y-%m-%d', hoverformat='%Y-%m-%d',
        showgrid=True, gridwidth=1, gridcolor='#2a2a2a',
        showspikes=True, spikemode='across', spikethickness=1.5, 
        spikedash='dash', spikecolor='#61AFEF' # 将垂直参考线改为天蓝色
    )
    # 纵轴范围只统计可见窗口内的净值（含模拟点），避免被窗口外的历史极值拉扁
    y_vals = [float(v) for v in win_series.values if pd.notna(v)]
    if show_sim:
        y_vals.append(float(initial_sim_nav))
    if y_vals:
        lo, hi = min(y_vals), max(y_vals)
        pad = (hi - lo) * 0.12 if hi > lo else max(abs(hi) * 0.02, 0.01)
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='#2a2a2a',
                         autorange=False, range=[lo - pad, hi + pad])
    else:
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='#2a2a2a', autorange=True)
    return fig


def _hover_tag(value, display, red_ge=None, green_le=None):
    if value is None:
        return '--'
    if red_ge is not None and value >= red_ge:
        return f"{display} 🔴"
    if green_le is not None and value <= green_le:
        return f"{display} 🟢"
    return f"{display} ⚪"


def build_score_figure(fund_name, code_str, signal, ind_history):
    dates = pd.to_datetime(signal['score_history']['dates'])
    x_dates = dates.strftime('%Y-%m-%d').tolist()
    score = signal['score_history']['score']
    # 默认使用 1Y 窗口的历史分位线
    score_pct = signal['score_history']['pct_windows']['1Y']

    customdata = []
    n = len(ind_history['dates'])
    for i in range(n):
        macd = ind_history['macd'][i]
        rsi = ind_history['rsi'][i]
        bias = ind_history['bias'][i]
        kdj = ind_history['kdj'][i]
        cci = ind_history['cci'][i]
        sar = ind_history['sar'][i]
        pctb = ind_history['boll_pctb'][i]
        bw = ind_history['bw_pct'][i]
        customdata.append([
            _hover_tag(macd, '--' if macd is None else f"{macd:+.3f}", red_ge=0) if macd is not None else '--',
            _hover_tag(rsi, '--' if rsi is None else f"{rsi:.1f}", 70, 30),
            _hover_tag(bias, '--' if bias is None else f"{bias:+.2f}%", 4, -4),
            _hover_tag(kdj, '--' if kdj is None else f"{kdj:.1f}", 90, 10),
            _hover_tag(cci, '--' if cci is None else f"{cci:.1f}", 100, -100),
            ('--' if sar is None else (f"{sar} 🔴" if sar == '看涨' else f"{sar} 🟢")),
            _hover_tag(pctb, '--' if pctb is None else f"{pctb:.3f}", 0.9, 0.1),
            _hover_tag(bw, '--' if bw is None else f"{bw:.0f}%", 80, 20),
        ])

    fig = go.Figure()
    fig.add_hrect(y0=OVERHEAT_LINE, y1=100, fillcolor='rgba(239,68,68,0.06)', line_width=0, layer='below')
    fig.add_hrect(y0=0, y1=OVERSOLD_LINE, fillcolor='rgba(59,130,246,0.08)', line_width=0, layer='below')
    # 极端区叠加一层，颜色更深，视觉上区分“预警”与“极端预警”
    fig.add_hrect(y0=EXTREME_HIGH_LINE, y1=100, fillcolor='rgba(239,68,68,0.10)', line_width=0, layer='below')
    fig.add_hrect(y0=0, y1=EXTREME_LOW_LINE, fillcolor='rgba(59,130,246,0.12)', line_width=0, layer='below')

    fig.add_trace(go.Scatter(
        x=x_dates, y=score, mode='lines', name='综合评分',
        line=dict(color='#61AFEF', width=2), yaxis='y1',
        customdata=customdata,
        hovertemplate=(
            '综合评分 %{y:.1f}分<br>'
            'MACD %{customdata[0]} RSI %{customdata[1]}<br>'
            'BIAS %{customdata[2]} KDJ %{customdata[3]}<br>'
            'CCI %{customdata[4]} SAR %{customdata[5]}<br>'
            'BOLL(%%B) %{customdata[6]} BW%% %{customdata[7]}'
            '<extra></extra>'
        ),
    ))
    fig.add_trace(go.Scatter(
        x=x_dates, y=score_pct, mode='lines', name='评分历史分位(%)',
        line=dict(color='#E5C07B', width=1.3, dash='dot'), yaxis='y2', opacity=0.85,
        hovertemplate='评分历史分位 %{y:.1f}%<extra></extra>',
    ))
    fig.add_trace(go.Scatter(
        x=[x_dates[0], x_dates[-1]], y=[OVERHEAT_LINE, OVERHEAT_LINE], mode='lines',
        name='过热预警', line=dict(color='#EF4444', width=1.2, dash='dash'),
        yaxis='y1', hoverinfo='skip', showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=[x_dates[0], x_dates[-1]], y=[OVERSOLD_LINE, OVERSOLD_LINE], mode='lines',
        name='过冷预警', line=dict(color='#3B82F6', width=1.2, dash='dash'),
        yaxis='y1', hoverinfo='skip', showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=[x_dates[0], x_dates[-1]], y=[EXTREME_HIGH_LINE, EXTREME_HIGH_LINE], mode='lines',
        name='极端过热', line=dict(color='#FF2D55', width=1.4, dash='dot'),
        yaxis='y1', hoverinfo='skip', showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=[x_dates[0], x_dates[-1]], y=[EXTREME_LOW_LINE, EXTREME_LOW_LINE], mode='lines',
        name='极端过冷', line=dict(color='#22D3EE', width=1.4, dash='dot'),
        yaxis='y1', hoverinfo='skip', showlegend=False,
    ))

    # 拓展右侧 margin=r 为 140，保证工具栏和右侧 Y2 轴标签不重叠
    fig.update_layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#cccccc', family='-apple-system,sans-serif', size=12),
        modebar=dict(bgcolor='rgba(0,0,0,0)', color='#888', activecolor='#61AFEF'),
        hoverlabel=dict(
            bgcolor='rgba(37,37,37,0.85)', 
            bordercolor='#61AFEF', 
            font=dict(color='#e6e6e6', size=12)
        ),
        hovermode='x unified', title=None, width=1150, height=440,
        margin=dict(l=70, r=140, t=60, b=50),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0,
                    font=dict(size=11), bgcolor='rgba(0,0,0,0)'),
        annotations=[
            dict(xref='paper', x=1.0, y=OVERHEAT_LINE, yref='y1', text=f'过热预警 {OVERHEAT_LINE}分',
                 showarrow=False, font=dict(color='#EF4444', size=10), xanchor='right', yanchor='bottom'),
            dict(xref='paper', x=1.0, y=OVERSOLD_LINE, yref='y1', text=f'过冷预警 {OVERSOLD_LINE}分',
                 showarrow=False, font=dict(color='#3B82F6', size=10), xanchor='right', yanchor='top'),
            dict(xref='paper', x=1.0, y=EXTREME_HIGH_LINE, yref='y1', text=f'极端过热 {EXTREME_HIGH_LINE}分',
                 showarrow=False, font=dict(color='#FF2D55', size=10), xanchor='right', yanchor='bottom'),
            dict(xref='paper', x=1.0, y=EXTREME_LOW_LINE, yref='y1', text=f'极端过冷 {EXTREME_LOW_LINE}分',
                 showarrow=False, font=dict(color='#22D3EE', size=10), xanchor='right', yanchor='top'),
        ],
        xaxis=dict(
            type='date', tickformat='%Y-%m-%d', hoverformat='%Y-%m-%d',
            showgrid=True, gridwidth=1, gridcolor='#2a2a2a',
            showspikes=True, spikemode='across', spikethickness=1.5, 
            spikedash='dash', spikecolor='#61AFEF'  # 将垂直参考线改为天蓝色
        ),
        yaxis=dict(title='综合评分', range=[0, 100], showgrid=True, gridwidth=1, gridcolor='#2a2a2a'),
        yaxis2=dict(title='评分分位(%)', range=[0, 100], overlaying='y', side='right', showgrid=False, autorange=False),
    )
    if len(dates) > 0:
        default_start = dates.max() - pd.DateOffset(years=1)
        fig.update_xaxes(range=[max(dates.min(), default_start).strftime('%Y-%m-%d'),
                                (dates.max() + pd.Timedelta(days=3)).strftime('%Y-%m-%d')])
    return fig


# ==========================================
# 样式与配色渲染辅助
# ==========================================
def _color_threshold(value, red_ge=None, green_le=None):
    if value is None:
        return '#888888'
    if red_ge is not None and value >= red_ge:
        return '#FF3333'
    if green_le is not None and value <= green_le:
        return '#00CC00'
    return '#FFFFFF'


def style_fixed_indicators(fixed):
    macd = fixed['macd']
    macd_color = '#888888' if macd is None else ('#FF3333' if macd >= 0 else '#00CC00')
    sar = fixed['sar']
    sar_color = '#888888' if sar is None else ('#c0392b' if sar == '看涨' else '#27ae60')
    return {
        'macd': {'display': '--' if macd is None else f"{macd:+.3f}", 'color': macd_color},
        'rsi': {'display': '--' if fixed['rsi'] is None else f"{fixed['rsi']:.1f}",
                'color': _color_threshold(fixed['rsi'], 70, 30)},
        'bias': {'display': '--' if fixed['bias'] is None else f"{fixed['bias']:+.2f}%",
                 'color': _color_threshold(fixed['bias'], 4, -4)},
        'kdj': {'display': '--' if fixed['kdj'] is None else f"{fixed['kdj']:.1f}",
                'color': _color_threshold(fixed['kdj'], 90, 10)},
        'cci': {'display': '--' if fixed['cci'] is None else f"{fixed['cci']:.1f}",
                'color': _color_threshold(fixed['cci'], 100, -100)},
        'sar': {'display': sar or '--', 'color': sar_color},
        'boll_pctb': {'display': '--' if fixed['boll_pctb'] is None else f"{fixed['boll_pctb']:.3f}",
                      'color': _color_threshold(fixed['boll_pctb'], 0.9, 0.1)},
        'bw_pct': {'display': '--' if fixed['bw_pct'] is None else f"{fixed['bw_pct']:.0f}%",
                   'color': _color_threshold(fixed['bw_pct'], 80, 20)},
    }


def style_percentile(value):
    color = _color_threshold(value, 90, 10)
    display = '--' if value is None else f"{value:.1f}%"
    return {'display': display, 'color': color, 'value': value}


# ==========================================
# 页面前端模板 (HTML / CSS / JS)
# ==========================================
PAGE_CSS = r"""
:root{
  --bg:#161616; --panel:#1e1e1e; --panel2:#252525; --border:#2a2a2a; --border2:#333;
  --text:#ffffff; --text-dim:#888; --blue:#61AFEF; --gold:#E5C07B; --purple:#C678DD;
  --red:#FF3333; --green:#00CC00;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
.app{max-width:1220px;margin:0 auto;padding:26px 20px 60px;}

.topbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;
  padding-bottom:16px;border-bottom:1px solid var(--border2);margin-bottom:18px;}
.topbar-left{display:flex;align-items:center;gap:12px;}
.topbar-left h1{font-size:20px;margin:0;font-weight:700;color:var(--gold);}

.topbar-right{display:flex;align-items:center;gap:16px;}
.select-group{display:flex;align-items:center;gap:8px;}
.select-group label{font-size:13px;color:var(--text-dim);}
.select-group select{background:var(--panel2);color:var(--text);border:1px solid var(--border2);
  border-radius:6px;padding:7px 12px;font-size:13.5px;cursor:pointer;}
.select-group select:focus{outline:2px solid var(--blue);outline-offset:1px;}

.heat-strip{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:22px;}
.heat-pill{display:flex;align-items:center;gap:7px;background:var(--panel);
  border:1px solid var(--border2);border-radius:999px;padding:6px 12px;
  font-size:12.5px;color:var(--text-dim);cursor:pointer;transition:.15s;}
.heat-pill:hover{border-color:var(--blue);color:var(--text);}
.heat-pill.active{border-color:var(--blue);color:var(--text);background:rgba(97,175,239,.08);}
.heat-pill .pill-score{font-weight:700;color:var(--pillcolor,#fff);}

.card{background:var(--panel);border:1px solid var(--border2);border-radius:12px;
  padding:20px 22px;margin-bottom:20px;box-shadow:0 8px 24px rgba(0,0,0,.35);}
.card-head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:8px;}
.card-head h2{font-size:15.5px;margin:0;font-weight:700;color:var(--gold);}
.card-head .hint{font-size:11.5px;color:var(--text-dim);}

.time-btn-group{display:flex;gap:4px;background:#2a2a2a;padding:3px;border-radius:6px;}
.nav-time-btn, .time-btn{background:transparent;border:none;color:#aaa;padding:4px 10px;font-size:12px;
  border-radius:4px;cursor:pointer;transition:.15s;}
.nav-time-btn.active, .nav-time-btn:hover, .time-btn.active, .time-btn:hover{background:var(--blue);color:#fff;font-weight:600;}

.sim-control{display:flex;align-items:center;gap:8px;font-size:12.5px;color:#aaa;}
.sim-control input{width:76px;background:#252525;border:1px solid #333;color:#eee;
  border-radius:5px;padding:5px 8px;font-size:13px;text-align:center;}

.fund-meta{font-size:12.5px;color:#888;margin:2px 0 12px;}
.chart-slot{width:100%;overflow-x:auto;}
.nav-chart-wrap, .score-chart-wrap{display:none;}

.signal-panel{padding-top:8px;border-radius:8px;transition:background-color .2s;}
.signal-panel.tint-red{background:rgba(255,0,0,.10);}
.signal-panel.tint-green{background:rgba(0,255,0,.10);}
.score-row{display:flex;gap:24px;align-items:center;flex-wrap:wrap;
  padding:10px 12px 16px;margin-bottom:16px;border-bottom:1px dashed var(--border2);}
.score-number{font-size:42px;font-weight:800;line-height:1;}
.score-unit{font-size:14px;color:var(--text-dim);margin-left:4px;font-weight:400;}
.status-block{display:flex;flex-direction:column;gap:4px;}
.status-badge{font-size:14px;font-weight:700;}
.status-meaning{font-size:12px;color:var(--text-dim);}
.status-action{font-size:12.5px;font-weight:600;}
.all-flag{font-size:11.5px;font-weight:700;padding:3px 10px;border-radius:999px;margin-left:auto;}
.all-flag.red{color:#FF3333;background:rgba(255,51,51,.12);border:1px solid rgba(255,51,51,.4);}
.all-flag.green{color:#00CC00;background:rgba(0,204,0,.12);border:1px solid rgba(0,204,0,.4);}

.indicator-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px 14px;margin-bottom:16px;padding:0 12px;}
.ind-cell{background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px;padding:9px 12px;}
.ind-cell .ind-label{display:block;font-size:11px;color:var(--text-dim);margin-bottom:3px;}
.ind-cell .ind-value{font-size:15px;font-weight:700;}

.pct-strip-label{font-size:11.5px;color:var(--text-dim);margin-bottom:8px;padding:0 12px;}
.pct-strip{display:flex;gap:10px;flex-wrap:wrap;padding:0 12px;}
.pct-pill{flex:1;min-width:110px;background:rgba(255,255,255,.03);border:1px solid var(--border);
  border-radius:8px;padding:8px 12px;text-align:center;}
.pct-pill .pct-label{display:block;font-size:11px;color:var(--text-dim);margin-bottom:3px;}
.pct-pill .pct-value{font-size:15px;font-weight:700;}
/* 将 Plotly 工具栏移动到图表内部左侧 */
.js-plotly-plot .plotly .modebar-container {
    right: auto !important;   /* 取消默认靠右对齐 */
    left: -20px !important;    /* 靠左定位，80px 是为了避开 Y 轴数值标签，可微调 */
    top: 12px !important;     /* 保持在图表内部 */
}
"""

PAGE_JS = r"""
let currentCode = "";
let currentWindow = "1Y";
let currentNavWindow = "1Y";

function initApp(){
  const typeSelect = document.getElementById('type-select');
  typeSelect.innerHTML = "";
  ALL_TYPES.forEach(function(t){
    let opt = document.createElement('option');
    opt.value = t; opt.textContent = t;
    typeSelect.appendChild(opt);
  });
  if (ALL_TYPES.length > 0){
    onTypeChange();
  }
}

function onTypeChange(){
  const selectedType = document.getElementById('type-select').value;
  const funds = TYPE_FUNDS[selectedType] || [];
  
  const fundSelect = document.getElementById('fund-select');
  fundSelect.innerHTML = "";
  funds.forEach(function(code){
    let opt = document.createElement('option');
    opt.value = code;
    opt.textContent = code + ' · ' + FUND_META[code].name;
    fundSelect.appendChild(opt);
  });

  const heatStrip = document.getElementById('heat-strip');
  heatStrip.innerHTML = "";
  funds.forEach(function(code){
    const latest = FUND_SIGNALS[code].latest;
    const scoreTxt = (latest.score === null) ? '--' : latest.score.toFixed(0);
    const btn = document.createElement('button');
    btn.className = 'heat-pill';
    btn.style.setProperty('--pillcolor', latest.color);
    btn.dataset.code = code;
    btn.onclick = function(){ selectFund(code); };
    btn.innerHTML = FUND_META[code].name + ' <span class="pill-score">' + scoreTxt + '</span>';
    heatStrip.appendChild(btn);
  });

  if (funds.length > 0){
    selectFund(funds[0]);
  }
}

function selectFund(code){
  document.getElementById('fund-select').value = code;
  onFundChange();
}

function onFundChange(){
  currentCode = document.getElementById('fund-select').value;
  
  document.querySelectorAll('.nav-chart-wrap').forEach(el => el.style.display = 'none');
  const navEl = document.getElementById('nav-wrap-' + currentCode);
  if (navEl) navEl.style.display = 'block';

  document.querySelectorAll('.score-chart-wrap').forEach(el => el.style.display = 'none');
  const scoreEl = document.getElementById('score-wrap-' + currentCode);
  if (scoreEl) scoreEl.style.display = 'block';

  document.querySelectorAll('.heat-pill').forEach(el => {
    el.classList.toggle('active', el.dataset.code === currentCode);
  });

  const meta = FUND_META[currentCode];
  if (meta){
    document.getElementById('fund-meta-line').innerText =
      meta.name + '（' + currentCode + '） 最新净值 ' + meta.last_nav.toFixed(4) + '  截至 ' + meta.last_date;
  }

  updateFixedIndicators();
  updatePercentileStrip();
  updatePanel();
  document.getElementById('target-pct-input').value = '1.5';
  
  executeQuantSimulation();
  switchNavWindow(currentNavWindow); // 保持净值图表的选择状态
  switchScoreWindow(currentWindow);  // 保持评分图表的选择状态
}

function updateFixedIndicators(){
  const f = FUND_FIXED[currentCode];
  if (!f) return;
  ['macd','rsi','bias','kdj','cci','sar','boll_pctb','bw_pct'].forEach(key => {
    const el = document.getElementById('ind-' + key);
    if (el){ el.innerText = f[key].display; el.style.color = f[key].color; }
  });
}

function updatePercentileStrip(){
  const p = FUND_PCT[currentCode];
  if (!p) return;
  ['1M','3M','6M','1Y'].forEach(w => {
    const valEl = document.getElementById('pct-value-' + w);
    if (valEl){ valEl.innerText = p[w].display; valEl.style.color = p[w].color; }
  });
}

function updatePanel(){
  const latest = FUND_SIGNALS[currentCode].latest;
  const scoreEl = document.getElementById('score-number');
  scoreEl.innerText = (latest.score === null) ? '--' : latest.score.toFixed(1);
  scoreEl.style.color = latest.color;

  document.getElementById('status-badge').innerText = latest.status;
  document.getElementById('status-badge').style.color = latest.color;
  document.getElementById('status-meaning').innerText = latest.meaning;
  document.getElementById('status-action').innerText = latest.action;
  document.getElementById('status-action').style.color = latest.color;

  const panel = document.getElementById('signal-panel');
  const flag = document.getElementById('all-flag');
  panel.classList.remove('tint-red', 'tint-green');
  flag.style.display = 'none';
  if (latest.all_red){
    panel.classList.add('tint-red');
    flag.textContent = '⚠ 六项全红'; flag.className = 'all-flag red'; flag.style.display = 'inline-block';
  } else if (latest.all_green){
    panel.classList.add('tint-green');
    flag.textContent = '⚠ 六项全绿'; flag.className = 'all-flag green'; flag.style.display = 'inline-block';
  }
}

function executeQuantSimulation(){
  const inputVal = parseFloat(document.getElementById('target-pct-input').value);
  if (Number.isNaN(inputVal)) return;
  const meta = FUND_META[currentCode];
  if (!meta) return;
  const simNav = meta.last_nav * (1 + inputVal / 100);

  let color = 'gray';
  let tipText = '模拟: ' + simNav.toFixed(4);
  if (meta.peak !== null && simNav > meta.peak){
    color = 'red'; tipText = '突破波峰: ' + simNav.toFixed(4);
  } else if (meta.trough !== null && simNav < meta.trough){
    color = 'green'; tipText = '跌破波谷: ' + simNav.toFixed(4);
  }

  const gdId = 'nav-chart-' + currentCode;
  const gd = document.getElementById(gdId);
  if (!gd || !window.Plotly) return;

  // 模拟涨幅为 0 时，不显示模拟线段、标记与文字
  const showSim = Math.abs(inputVal) > 1e-9;
  Plotly.restyle(gdId, {visible: showSim}, [1, 2, 3]);
  if (!showSim){ applyNavView(); return; }

  Plotly.restyle(gdId, {y: [[meta.last_nav, simNav]], 'line.color': color}, [1]);
  Plotly.restyle(gdId, {y: [[simNav]], 'marker.color': [[color]]}, [2]);
  Plotly.restyle(gdId, {y: [[simNav]], text: [['&nbsp;<br>' + tipText]], 'textfont.color': [[color]]}, [3]);
  applyNavView();   // 模拟点变化后同步纵轴范围
}

// 净值图表 - 时间窗口标签
const NAV_WIN_LABEL = {'1M':'近1月','3M':'近3月','6M':'近6月','1Y':'近1年','ALL':'全部区间'};
const NAV_WIN_DAYS  = {'1M':30,'3M':90,'6M':180,'1Y':365};

// 净值图表 - 切换时间窗口
function switchNavWindow(winKey){
  currentNavWindow = winKey;
  document.querySelectorAll('.nav-time-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.win === winKey);
  });
  applyNavView();
}

// 按当前窗口刷新：X 轴范围 + Y 轴自适应 + 累计涨幅图例
function applyNavView(){
  const winKey = currentNavWindow;
  const gdId = 'nav-chart-' + currentCode;
  const gd = document.getElementById(gdId);
  if (!gd || !window.Plotly || !gd.data) return;

  const meta = FUND_META[currentCode];
  if (!meta) return;

  const lastD = new Date(meta.last_date);
  let startD = new Date(meta.first_date);
  if (NAV_WIN_DAYS[winKey]){
    const shifted = new Date(lastD.getTime() - NAV_WIN_DAYS[winKey] * 86400000);
    if (shifted > startD) startD = shifted;
  }
  const cutoffStr = startD.toISOString().slice(0,10);
  const endStr = new Date(lastD.getTime() + 5*86400000).toISOString().slice(0,10);

  // 取净值主曲线（trace 0）的数据
  const xs = gd.data[0].x || [];
  const ys = gd.data[0].y || [];

  // 基准日：窗口起点当天或之前最近的一个交易日（与天天基金等平台的阶段涨幅口径一致）。
  // 窗口起点常落在周末或长假，若只取窗口“内”的第一个交易日，基准会被后移、涨幅偏小。
  let baseIdx = -1;
  for (let i = 0; i < xs.length; i++){
    if (String(xs[i]).slice(0,10) <= cutoffStr) baseIdx = i; else break;
  }
  if (baseIdx < 0) baseIdx = 0;
  // X 轴左端对齐到基准日，保证图例里的涨幅可以在图上一眼核对
  const startStr = String(xs[baseIdx] || cutoffStr).slice(0,10);

  let firstVal = null, lastVal = null, lo = null, hi = null;
  for (let i = baseIdx; i < xs.length; i++){
    const d = String(xs[i]).slice(0,10);
    if (d > endStr) break;
    const v = ys[i];
    if (v === null || v === undefined || isNaN(v)) continue;
    if (firstVal === null) firstVal = v;
    lastVal = v;
    if (lo === null || v < lo) lo = v;
    if (hi === null || v > hi) hi = v;
  }

  // 模拟下一交易日的点也要纳入纵轴范围
  const inputVal = parseFloat(document.getElementById('target-pct-input').value);
  if (!Number.isNaN(inputVal) && Math.abs(inputVal) > 1e-9){
    const simNav = meta.last_nav * (1 + inputVal / 100);
    if (lo === null || simNav < lo) lo = simNav;
    if (hi === null || simNav > hi) hi = simNav;
  }

  const relayoutObj = {'xaxis.range': [startStr, endStr]};
  if (lo !== null && hi !== null){
    const pad = (hi > lo) ? (hi - lo) * 0.12 : Math.max(Math.abs(hi) * 0.02, 0.01);
    relayoutObj['yaxis.autorange'] = false;
    relayoutObj['yaxis.range'] = [lo - pad, hi + pad];
  } else {
    relayoutObj['yaxis.autorange'] = true;
  }
  Plotly.relayout(gdId, relayoutObj);

  // 刷新“累计涨幅”图例：窗口内首末净值的涨跌幅
  let cumIdx = -1;
  for (let i = 0; i < gd.data.length; i++){
    if (gd.data[i].meta === 'cumret'){ cumIdx = i; break; }
  }
  if (cumIdx >= 0){
    const label = NAV_WIN_LABEL[winKey] || '';
    let name = label + '累计涨幅 --';
    let color = '#888888';
    if (firstVal !== null && lastVal !== null && firstVal > 0){
      const pct = (lastVal / firstVal - 1) * 100;
      name = label + '累计涨幅 ' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
      color = (pct >= 0) ? '#FF3333' : '#00CC00';
    }
    Plotly.restyle(gdId, {name: [name], 'line.color': [color]}, [cumIdx]);
  }
}

// 评分分位图表 - 切换时间窗口
function switchScoreWindow(winKey){
  currentWindow = winKey;
  document.querySelectorAll('.time-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.win === winKey);
  });

  const gdId = 'score-chart-' + currentCode;
  const gd = document.getElementById(gdId);
  if (!gd || !window.Plotly) return;

  const scoreData = FUND_SCORE_HISTORIES[currentCode];
  if (!scoreData) return;

  const newPctSeries = scoreData.pct_windows[winKey];
  Plotly.restyle(gdId, {y: [newPctSeries]}, [1]);

  const dates = scoreData.dates;
  if (dates && dates.length > 0){
    const lastD = new Date(dates[dates.length - 1]);
    let startD = new Date(dates[0]);
    if (winKey === '1M') startD = new Date(lastD.getTime() - 30 * 86400000);
    else if (winKey === '3M') startD = new Date(lastD.getTime() - 90 * 86400000);
    else if (winKey === '6M') startD = new Date(lastD.getTime() - 180 * 86400000);
    else if (winKey === '1Y') startD = new Date(lastD.getTime() - 365 * 86400000);

    Plotly.relayout(gdId, {
      'xaxis.range': [startD.toISOString().slice(0,10), lastD.toISOString().slice(0,10)]
    });
  }
}

window.addEventListener('DOMContentLoaded', initApp);
"""

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>基金波段信号量化看板</title>
<script src="https://cdn.plot.ly/plotly-3.0.1.min.js"></script>
<style>
__CSS__
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="topbar-left">
      <h1>基金波段信号量化看板</h1>
    </div>
    <div class="topbar-right">
      <div class="select-group">
        <label for="type-select">类型</label>
        <select id="type-select" onchange="onTypeChange()"></select>
      </div>
      <div class="select-group">
        <label for="fund-select">基金</label>
        <select id="fund-select" onchange="onFundChange()"></select>
      </div>
    </div>
  </header>

  <div class="heat-strip" id="heat-strip"></div>

  <section class="card">
    <div class="card-head">
      <h2>波段信号</h2>
      <div class="hint">八项指标为固定周期技术指标，不随历史分位数窗口变化</div>
    </div>
    <div class="signal-panel" id="signal-panel">
      <div class="score-row">
        <div><span class="score-number" id="score-number">--</span><span class="score-unit">/ 100分</span></div>
        <div class="status-block">
          <span class="status-badge" id="status-badge">--</span>
          <span class="status-meaning" id="status-meaning"></span>
          <span class="status-action" id="status-action"></span>
        </div>
        <span class="all-flag" id="all-flag" style="display:none;"></span>
      </div>
      <div class="indicator-grid">
__INDICATOR_CELLS__
      </div>
      <div class="pct-strip-label">历史分位数（最新评分在过去不同时间段的历史百分位排名）</div>
      <div class="pct-strip">
__PCT_STRIP__
      </div>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div style="display:flex; flex-direction:column; gap:4px;">
        <h2>净值走势 &amp; 波浪结构（ZigZag）</h2>
        <div class="fund-meta" id="fund-meta-line" style="margin:0;"></div>
      </div>
      <div style="display:flex; align-items:center; gap:16px; flex-wrap:wrap;">
        <div class="sim-control">
          <span>模拟下一日涨幅</span>
          <input type="number" id="target-pct-input" value="1.5" step="0.1" oninput="executeQuantSimulation()">
          <span>%</span>
        </div>
        <div class="time-btn-group">
          <button class="nav-time-btn" data-win="1M" onclick="switchNavWindow('1M')">1月</button>
          <button class="nav-time-btn" data-win="3M" onclick="switchNavWindow('3M')">3月</button>
          <button class="nav-time-btn" data-win="6M" onclick="switchNavWindow('6M')">6月</button>
          <button class="nav-time-btn active" data-win="1Y" onclick="switchNavWindow('1Y')">1年</button>
          <button class="nav-time-btn" data-win="ALL" onclick="switchNavWindow('ALL')">全部</button>
        </div>
      </div>
    </div>
    <div class="chart-slot">
__NAV_CHARTS__
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>评分历史 &amp; 分位范围</h2>
      <div class="time-btn-group">
        <button class="time-btn" data-win="1M" onclick="switchScoreWindow('1M')">1月</button>
        <button class="time-btn" data-win="3M" onclick="switchScoreWindow('3M')">3月</button>
        <button class="time-btn" data-win="6M" onclick="switchScoreWindow('6M')">6月</button>
        <button class="time-btn active" data-win="1Y" onclick="switchScoreWindow('1Y')">1年</button>
        <button class="time-btn" data-win="ALL" onclick="switchScoreWindow('ALL')">全部</button>
      </div>
    </div>
    <div class="chart-slot">
__SCORE_CHARTS__
    </div>
  </section>

  <footer>生成时间：__GEN_TIME__ ｜ ZigZag 阈值 __ZIGZAG_PCT__% ｜ 仅供个人研究参考，不构成投资建议</footer>
</div>

<script>
var ALL_TYPES = __ALL_TYPES_JSON__;
var TYPE_FUNDS = __TYPE_FUNDS_JSON__;
var FUND_META = __FUND_META_JSON__;
var FUND_FIXED = __FUND_FIXED_JSON__;
var FUND_PCT = __FUND_PCT_JSON__;
var FUND_SIGNALS = __FUND_SIGNALS_JSON__;
var FUND_SCORE_HISTORIES = __FUND_SCORE_HISTORIES_JSON__;
__JS_BLOCK__
</script>
</body>
</html>
"""

INDICATOR_LABELS = [
    ('macd', 'MACD'), ('rsi', 'RSI'), ('bias', 'BIAS'), ('kdj', 'KDJ'),
    ('cci', 'CCI'), ('sar', 'SAR'), ('boll_pctb', 'BOLL(%B)'), ('bw_pct', 'BW%'),
]


# ==========================================
# 辅助函数：智能读取 CSV
# ==========================================
def load_csv_smart(file_path):
    for enc in ['utf-8-sig', 'gb18030', 'gbk', 'utf-8']:
        try:
            return pd.read_csv(file_path, encoding=enc)
        except Exception:
            continue
    raise ValueError(f"无法读取 {file_path}")


# ==========================================
# 主程序
# ==========================================
if __name__ == "__main__":
    target_file = "funds_universe_example.csv"
    nav_file = "1.基金历史净值.csv"

    if not os.path.exists(target_file) or not os.path.exists(nav_file):
        print("错误：缺失 funds_universe_example.csv 或 1.基金历史净值.csv 文件！")
        raise SystemExit(0)

    target_df = load_csv_smart(target_file)
    nav_df = load_csv_smart(nav_file)

    target_df.columns = target_df.columns.str.strip()
    nav_df.columns = nav_df.columns.str.strip()

    # 清洗基金代码，防止浮点转串报错
    nav_df['基金代码'] = nav_df['基金代码'].astype(str).str.split('.').str[0].str.strip().str.zfill(6)
    target_df['基金代码'] = target_df['基金代码'].astype(str).str.split('.').str[0].str.strip().str.zfill(6)

    # 清洗净值数据与时间格式
    nav_df['单位净值'] = pd.to_numeric(nav_df['单位净值'].astype(str).str.replace(',', ''), errors='coerce')
    nav_df['日期'] = pd.to_datetime(nav_df['日期'], errors='coerce')
    nav_df = nav_df.dropna(subset=['单位净值', '日期'])

    if '类型' not in target_df.columns:
        target_df['类型'] = '未分类'
    else:
        target_df['类型'] = target_df['类型'].fillna('未分类').replace('', '未分类')

    all_types = list(target_df['类型'].unique())
    type_funds_map = {}
    
    nav_charts_html = []
    score_charts_html = []
    
    fund_meta_json = {}
    fund_fixed_json = {}
    fund_pct_json = {}
    fund_signals_json = {}
    fund_score_histories_json = {}

    print(f"开始处理，目标基金数：{len(target_df)}，包含类型：{all_types}")

    for _, row in target_df.iterrows():
        code = str(row['基金代码'])
        type_name = str(row['类型'])

        if type_name not in type_funds_map:
            type_funds_map[type_name] = []
        
        fund_data = nav_df[nav_df['基金代码'] == code].copy()
        if fund_data.empty:
            print(f" ⚠️ 警告：基金 {code} 在净值文件中未找到数据，已跳过")
            continue
        
        fund_data = fund_data.drop_duplicates(subset=['日期'], keep='last').sort_values('日期')
        if len(fund_data) < MIN_ROWS:
            print(f" ⚠️ 基金 {code} 数据不足 {MIN_ROWS} 条，已跳过")
            continue

        type_funds_map[type_name].append(code)

        fund_name = fund_data['基金名称'].iloc[0]
        # 先做复权，消除拆分/份额折算/大比例分红造成的净值跳空
        adj_close, split_events = build_adjusted_nav(fund_data, code, fund_name)
        df_plot = adj_close.to_frame('close')
        df_plot.index.name = '日期'
        df_zigzag = calculate_zigzag(df_plot, price_col='close', change_pct=ZIGZAG_THRESHOLD)
        pivots_df = df_zigzag[df_zigzag['pivot'] != 0].copy()
        peaks = pivots_df[pivots_df['pivot'] == 1]
        troughs = pivots_df[pivots_df['pivot'] == -1]

        last_peak_val = float(peaks['close'].iloc[-1]) if not peaks.empty else 'null'
        last_trough_val = float(troughs['close'].iloc[-1]) if not troughs.empty else 'null'
        last_date = df_plot.index[-1]
        first_date = df_plot.index[0]
        last_nav = float(df_plot['close'].iloc[-1])
        next_date = last_date + pd.tseries.offsets.BDay(1)

        fixed_indicators, percentile_snapshot, signal, ind_history = compute_fund_signal(df_plot['close'])

        nav_fig = build_nav_figure(
            df_plot, pivots_df, peaks, troughs, code, fund_name,
            last_date, last_nav, next_date, last_peak_val, last_trough_val,
            split_events=split_events,
        )
        nav_charts_html.append(f'<div class="nav-chart-wrap" id="nav-wrap-{code}">{nav_fig.to_html(full_html=False, include_plotlyjs=False, div_id=f"nav-chart-{code}", config=PLOTLY_CONFIG)}</div>')

        score_fig = build_score_figure(fund_name, code, signal, ind_history)
        score_charts_html.append(f'<div class="score-chart-wrap" id="score-wrap-{code}">{score_fig.to_html(full_html=False, include_plotlyjs=False, div_id=f"score-chart-{code}", config=PLOTLY_CONFIG)}</div>')

        # 缓存 JSON 结构数据，补充记录起始日期以供视图缩放
        fund_meta_json[code] = {
            'name': fund_name,
            'last_nav': round(last_nav, 4),
            'peak': None if last_peak_val == 'null' else round(float(last_peak_val), 4),
            'trough': None if last_trough_val == 'null' else round(float(last_trough_val), 4),
            'last_date': last_date.strftime('%Y-%m-%d'),
            'first_date': first_date.strftime('%Y-%m-%d'),
        }
        fund_fixed_json[code] = style_fixed_indicators(fixed_indicators)
        fund_pct_json[code] = {w: style_percentile(percentile_snapshot[w]) for w in WINDOW_ORDER}
        fund_signals_json[code] = {'latest': signal['latest']}
        fund_score_histories_json[code] = signal['score_history']

        

    indicator_cells = '\n'.join(
        f'        <div class="ind-cell"><span class="ind-label">{label}</span>'
        f'<span class="ind-value" id="ind-{key}">--</span></div>' for key, label in INDICATOR_LABELS
    )
    pct_strip = '\n'.join(
        f'        <div class="pct-pill"><span class="pct-label">{WINDOW_CONFIG[k]["label"]}</span>'
        f'<span class="pct-value" id="pct-value-{k}">--</span></div>' for k in WINDOW_ORDER
    )

    html = HTML_TEMPLATE
    html = html.replace('__NAV_CHARTS__', '\n'.join(nav_charts_html))
    html = html.replace('__INDICATOR_CELLS__', indicator_cells)
    html = html.replace('__PCT_STRIP__', pct_strip)
    html = html.replace('__SCORE_CHARTS__', '\n'.join(score_charts_html))
    html = html.replace('__GEN_TIME__', pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'))
    html = html.replace('__ZIGZAG_PCT__', f'{ZIGZAG_THRESHOLD*100:.0f}')
    html = html.replace('__CSS__', PAGE_CSS)
    html = html.replace('__JS_BLOCK__', PAGE_JS)
    
    html = html.replace('__ALL_TYPES_JSON__', json.dumps(all_types, ensure_ascii=False))
    html = html.replace('__TYPE_FUNDS_JSON__', json.dumps(type_funds_map, ensure_ascii=False))
    html = html.replace('__FUND_META_JSON__', json.dumps(fund_meta_json, ensure_ascii=False))
    html = html.replace('__FUND_FIXED_JSON__', json.dumps(fund_fixed_json, ensure_ascii=False))
    html = html.replace('__FUND_PCT_JSON__', json.dumps(fund_pct_json, ensure_ascii=False))
    html = html.replace('__FUND_SIGNALS_JSON__', json.dumps(fund_signals_json, ensure_ascii=False))
    html = html.replace('__FUND_SCORE_HISTORIES_JSON__', json.dumps(fund_score_histories_json, ensure_ascii=False))

    with open("zigzag_signal_analyzer.html", 'w', encoding='utf-8') as f:
        f.write(html)