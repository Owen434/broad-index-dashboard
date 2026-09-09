# -*- coding: utf-8 -*-
"""
基金历史净值 + 特征排行 抓取（优化版 v3）
=========================================================
v3 针对「基本信息 1000 只串行 600 秒」的专项优化：

结论先行：原版那把 xq_lock 是多余的。
ak.fund_individual_basic_info_xq 底层只是一个普通 requests.get
（https://danjuanfunds.com/djapi/fund/{code}），没有 V8/JS 引擎，
本身线程安全。加锁把 1000 次请求全串行了。

三层策略，逐层兜底：
  第 1 层 ak.fund_name_em()          1 次请求  -> 全市场【基金类型】
  第 2 层 ak.fund_scale_open_sina()  5 次请求  -> 全市场【成立日期 + 最近总份额】
  第 3 层 蛋卷单只接口（并发，无锁）  只补前两层没覆盖到的漏网基金

1000 只基金：600 秒 -> 通常 10 秒内（6 次批量请求），
少量漏网的走并发兜底，也在十几秒量级。且结果永久缓存。

用法：
    python fetch_fund_nav.py            # 增量模式
    python fetch_fund_nav.py --full     # 强制全量重抓
    python fetch_fund_nav.py --no-xq    # 只用批量接口，不做单只兜底（最快）

注：净值抓取走的是 ak.fund_open_fund_info_em，这个接口对场外/联接基金覆盖最稳；
funds_universe_example.csv 里如果混了场内 ETF 代码，个别可能抓不到净值，
脚本会打印失败列表并跳过，不影响其余基金正常生成。
"""

import os
import sys
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd

# ==================== 配置 ====================
NAV_FILE     = "fund_nav_history.csv"
FEATURE_FILE = "fund_feature_ranking.csv"
BASIC_CACHE  = "fund_basic_info_cache.csv"
# 开源版：基金列表改从 funds_universe_example.csv 读（基金代码/基金名称/类型），
# 不再依赖你本地的基金池筛选推荐表——那份表带有个人筛选逻辑，不适合开源。
FUND_LIST_FILE = "funds_universe_example.csv"

MAX_WORKERS      = 12    # 东财净值接口并发
XQ_WORKERS       = 10    # 蛋卷兜底接口并发（无锁；报限流就调到 5）
BASIC_TTL_DAYS   = 30    # 基本信息缓存有效期
RETRY            = 2
FORCE_FULL       = "--full" in sys.argv
SKIP_XQ          = "--no-xq" in sys.argv
# =============================================

CURRENT_DATE = time.strftime("%Y/%m/%d")

RANK_COLS = ['近1周', '近1月', '近3月', '近6月', '近1年',
             '近2年', '近3年', '今年来', '成立来', '自定义', '手续费']
RATING_COLS = ['上海证券', '招商证券', '济安金信', '晨星评级']
SINA_CATS = ["股票型基金", "混合型基金", "债券型基金", "货币型基金", "QDII基金"]

TARGET_COLS = [
    "基金代码", "基金名称", "日期", "上海证券", "招商证券", "济安金信", "晨星评级",
    "类型", "最新规模", "成立时间", "近1周", "近1月", "近3月",
    "近6月", "近1年", "近2年", "近3年", "今年来", "成立来", "自定义", "手续费"
]


def safe_call(func, *args, retries=RETRY, delay=0.5, **kwargs):
    for i in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception:
            if i == retries:
                return None
            time.sleep(delay * (i + 1))
    return None


def fmt_date(x):
    try:
        return pd.to_datetime(x).strftime('%Y/%m/%d')
    except Exception:
        return str(x) if x is not None else ""


# ============ 基本信息：三层策略 ============

def load_basic_cache():
    if os.path.exists(BASIC_CACHE):
        try:
            df = pd.read_csv(BASIC_CACHE, dtype={"基金代码": str}, encoding='utf-8-sig')
            df["基金代码"] = df["基金代码"].str.zfill(6)
            return {r["基金代码"]: {k: ("" if pd.isna(v) else v) for k, v in r.items()}
                    for _, r in df.iterrows()}
        except Exception:
            pass
    return {}


def save_basic_cache(cache):
    try:
        pd.DataFrame(list(cache.values())).to_csv(
            BASIC_CACHE, index=False, encoding='utf-8-sig')
    except Exception as e:
        print(f"  基本信息缓存写入失败: {e}")


def _is_complete(rec):
    """三个目标字段是否齐备"""
    return bool(rec) and all(str(rec.get(k, "")).strip() not in ("", "nan")
                             for k in ("类型", "最新规模", "成立时间"))


def refresh_basic_info(codes, cache):
    cutoff = (datetime.now() - timedelta(days=BASIC_TTL_DAYS)).strftime('%Y-%m-%d')

    todo = [c for c in codes
            if FORCE_FULL or not _is_complete(cache.get(c))
            or str(cache.get(c, {}).get("缓存日期", "")) < cutoff]

    if not todo:
        print(f"基本信息全部命中缓存（{len(codes)} 只），0 次网络请求")
        return cache

    print(f"基本信息需更新 {len(todo)}/{len(codes)} 只")
    today = datetime.now().strftime('%Y-%m-%d')
    todo_set = set(todo)
    staged = {c: dict(cache.get(c, {"基金代码": c})) for c in todo}
    for c in staged:
        staged[c]["基金代码"] = c

    # ---------- 第 1 层：全市场基金类型（1 次请求） ----------
    print("  [1/3] 批量拉取全市场基金类型...")
    df_name = safe_call(ak.fund_name_em)
    if df_name is not None and not df_name.empty:
        df_name = df_name.copy()
        df_name["基金代码"] = df_name["基金代码"].astype(str).str.zfill(6)
        tmap = df_name.drop_duplicates("基金代码").set_index("基金代码")["基金类型"].to_dict()
        hit = 0
        for c in todo_set:
            if tmap.get(c):
                staged[c]["类型"] = tmap[c]
                hit += 1
        print(f"        类型命中 {hit}/{len(todo_set)}")
    else:
        print("        fund_name_em 获取失败，跳过")

    # ---------- 第 2 层：成立日期 + 规模（5 次请求） ----------
    print("  [2/3] 批量拉取成立日期与规模...")
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(safe_call, ak.fund_scale_open_sina, symbol=s): s
                for s in SINA_CATS}
        parts = [f.result() for f in as_completed(futs)]
    parts = [p for p in parts if p is not None and not p.empty]

    if parts:
        scale = pd.concat(parts, ignore_index=True)
        scale["基金代码"] = scale["基金代码"].astype(str).str.zfill(6)
        scale = scale.drop_duplicates("基金代码").set_index("基金代码")
        hit_d = hit_s = 0
        for c in todo_set:
            if c not in scale.index:
                continue
            row = scale.loc[c]
            d = row.get("成立日期")
            if pd.notna(d):
                staged[c]["成立时间"] = fmt_date(d)
                hit_d += 1
            sz = row.get("最近总份额")
            if pd.notna(sz):
                # 新浪单位为万份，换算成「亿元份额」口径便于阅读
                staged[c]["最新规模"] = round(float(sz) / 10000.0, 4)
                hit_s += 1
        print(f"        成立日期命中 {hit_d}/{len(todo_set)}，规模命中 {hit_s}/{len(todo_set)}")
    else:
        print("        fund_scale_open_sina 获取失败，跳过")

    # ---------- 第 3 层：并发兜底（无锁） ----------
    missing = [c for c in todo if not _is_complete(staged.get(c))]
    if missing and not SKIP_XQ:
        print(f"  [3/3] {len(missing)} 只仍缺字段，走蛋卷单只接口并发兜底"
              f"（{XQ_WORKERS} 线程，无锁）...")

        def _one(code):
            info = safe_call(ak.fund_individual_basic_info_xq, symbol=code, timeout=8)
            if info is None or info.empty:
                return None
            return dict(zip(info['item'], info['value']))

        done, ok = 0, 0
        with ThreadPoolExecutor(max_workers=XQ_WORKERS) as ex:
            futs = {ex.submit(_one, c): c for c in missing}
            for fut in as_completed(futs):
                c = futs[fut]
                done += 1
                d = fut.result()
                if d:
                    ok += 1
                    if not str(staged[c].get("类型", "")).strip():
                        staged[c]["类型"] = d.get("基金类型", "")
                    if not str(staged[c].get("最新规模", "")).strip():
                        staged[c]["最新规模"] = d.get("最新规模", "")
                    if not str(staged[c].get("成立时间", "")).strip():
                        staged[c]["成立时间"] = fmt_date(d.get("成立时间", ""))
                if done % 25 == 0 or done == len(missing):
                    print(f"        兜底进度: {done}/{len(missing)}", end="\r")
        print(f"\n        兜底成功 {ok}/{len(missing)}")
    elif missing:
        print(f"  [3/3] 跳过单只兜底（--no-xq），{len(missing)} 只字段可能不全")

    for c in todo:
        staged[c]["缓存日期"] = today
        cache[c] = staged[c]
    save_basic_cache(cache)

    still = sum(1 for c in codes if not _is_complete(cache.get(c)))
    print(f"基本信息完成，仍不完整 {still} 只")
    return cache


# ============ 净值抓取 ============

def fetch_nav(code, name):
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(safe_call, ak.fund_open_fund_info_em, code, indicator="单位净值走势")
        f2 = ex.submit(safe_call, ak.fund_open_fund_info_em, code, indicator="累计净值走势")
        df_unit, df_acc = f1.result(), f2.result()

    if df_unit is None or df_unit.empty:
        return None

    df_unit = df_unit.rename(columns={"净值日期": "日期", "日增长率": "增长率"})
    if df_acc is not None and not df_acc.empty:
        df_acc = df_acc.rename(columns={"净值日期": "日期"})
        df = pd.merge(df_unit, df_acc[['日期', '累计净值']], on="日期", how="left")
    else:
        df = df_unit.copy()
        df["累计净值"] = pd.NA

    df["日期"] = pd.to_datetime(df["日期"])
    df = df.sort_values("日期")
    df["增长值"] = pd.to_numeric(df["单位净值"], errors="coerce").diff()
    df["基金代码"] = code
    df["基金名称"] = name
    return df[["基金代码", "基金名称", "日期", "单位净值", "累计净值", "增长值", "增长率"]]


def load_old_nav():
    if not os.path.exists(NAV_FILE):
        return pd.DataFrame()
    try:
        df = pd.read_csv(NAV_FILE, dtype={"基金代码": str}, encoding='utf-8-sig')
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        return df.dropna(subset=["日期"])
    except Exception as e:
        print(f"警告：读取旧净值失败 {e}")
        return pd.DataFrame()


# ============ 主流程 ============

def load_csv_smart(path, **kw):
    """CSV 编码兼容：utf-8-sig → gbk → gb18030"""
    for enc in ('utf-8-sig', 'gbk', 'gb18030'):
        try:
            return pd.read_csv(path, encoding=enc, **kw)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, encoding='utf-8', errors='ignore', **kw)


def main():
    t0 = time.time()

    if not os.path.exists(FUND_LIST_FILE):
        print(f"错误：找不到文件 {FUND_LIST_FILE}")
        return

    print("正在读取基金列表...")
    codes_df = load_csv_smart(FUND_LIST_FILE, dtype=str)
    codes_df.columns = codes_df.columns.str.strip()
    name_map = dict(zip(
        codes_df["基金代码"].astype(str).str.strip().str.zfill(6),
        codes_df["基金名称"].astype(str).str.strip(),
    ))
    codes = list(name_map.keys())
    print(f"基金池共 {len(codes)} 只")

    print("正在并发获取全局评级与排行数据...")
    with ThreadPoolExecutor(max_workers=2) as ex:
        fr = ex.submit(safe_call, ak.fund_rating_all)
        fk = ex.submit(safe_call, ak.fund_open_fund_rank_em)
        rating_all, rank_all = fr.result(), fk.result()

    rating_map, rank_map, remote_latest = {}, {}, {}

    if rating_all is not None and not rating_all.empty:
        rating_all = rating_all.copy()
        rating_all["代码"] = rating_all["代码"].astype(str).str.zfill(6)
        cols = [c for c in RATING_COLS if c in rating_all.columns]
        rating_map = rating_all.drop_duplicates("代码").set_index("代码")[cols] \
                               .to_dict(orient="index")
    else:
        print("警告：评级数据获取失败")

    if rank_all is not None and not rank_all.empty:
        rank_all = rank_all.copy()
        rank_all["基金代码"] = rank_all["基金代码"].astype(str).str.zfill(6)
        rank_all = rank_all.drop_duplicates("基金代码")
        cols = [c for c in RANK_COLS if c in rank_all.columns]
        rank_map = rank_all.set_index("基金代码")[cols].to_dict(orient="index")
        if "日期" in rank_all.columns:
            remote_latest = pd.Series(
                pd.to_datetime(rank_all["日期"], errors="coerce").values,
                index=rank_all["基金代码"]).dropna().to_dict()
    else:
        print("警告：排行数据获取失败")

    # --- 增量判断 ---
    old_nav = load_old_nav()
    local_latest = old_nav.groupby("基金代码")["日期"].max().to_dict() \
        if not old_nav.empty else {}

    if FORCE_FULL or old_nav.empty:
        need, reason = list(codes), ("全量模式" if FORCE_FULL else "本地无历史数据")
    else:
        need = [c for c in codes
                if local_latest.get(c) is None or remote_latest.get(c) is None
                or local_latest[c] < pd.Timestamp(remote_latest[c])]
        reason = "增量模式"

    print(f"净值抓取（{reason}）：需更新 {len(need)}/{len(codes)} 只，"
          f"跳过 {len(codes) - len(need)} 只（省去 {(len(codes)-len(need))*2} 次请求）")

    new_nav_list, failed = [], []
    if need:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(fetch_nav, c, name_map.get(c, "未知")): c for c in need}
            done = 0
            for fut in as_completed(futs):
                code = futs[fut]
                done += 1
                try:
                    df, err = fut.result(), "无数据返回"
                except Exception as e:
                    df, err = None, str(e)
                if df is not None and not df.empty:
                    new_nav_list.append(df)
                else:
                    failed.append((code, err))
                if done % 10 == 0 or done == len(need):
                    print(f"净值进度: [{done}/{len(need)}]      ", end="\r")
        print()

    if failed:
        print(f"!!! {len(failed)} 只净值抓取失败: "
              f"{', '.join(c for c, _ in failed[:10])}{' ...' if len(failed) > 10 else ''}")

    # --- 基本信息 ---
    basic_cache = refresh_basic_info(codes, load_basic_cache())

    # --- 保存净值 ---
    if new_nav_list:
        new_nav = pd.concat(new_nav_list, ignore_index=True)
        if not old_nav.empty:
            touched = set(new_nav["基金代码"])
            final_nav = pd.concat(
                [old_nav[~old_nav["基金代码"].isin(touched)], new_nav], ignore_index=True)
        else:
            final_nav = new_nav
        final_nav = final_nav.drop_duplicates(subset=["基金代码", "日期"], keep='last')
        final_nav = final_nav.sort_values(by=["基金代码", "日期"], ascending=[True, False])
        out = final_nav.copy()
        out["日期"] = out["日期"].dt.strftime('%Y/%m/%d')
        out.to_csv(NAV_FILE, index=False, encoding='utf-8-sig')
        print(f"净值已保存：{len(out)} 行，{out['基金代码'].nunique()} 只基金")
    else:
        print("净值无更新，文件保持不变")

    # --- 特征表（纯本地拼装） ---
    feature_list = []
    for code in codes:
        b = basic_cache.get(code, {})
        feat = {
            "基金代码": code,
            "基金名称": name_map.get(code, "未知"),
            "日期": CURRENT_DATE,
            "类型": b.get("类型", ""),
            "最新规模": b.get("最新规模", ""),
            "成立时间": b.get("成立时间", ""),
        }
        feat.update(rating_map.get(code, {}))
        feat.update(rank_map.get(code, {}))
        feature_list.append(feat)

    if feature_list:
        new_feature = pd.DataFrame(feature_list)
        if os.path.exists(FEATURE_FILE):
            try:
                old_feature = pd.read_csv(FEATURE_FILE, dtype={"基金代码": str},
                                          encoding='utf-8-sig')
                final_feature = pd.concat([old_feature, new_feature], ignore_index=True)
            except Exception:
                final_feature = new_feature
        else:
            final_feature = new_feature
        final_feature = final_feature.drop_duplicates(subset=["基金代码"], keep='last')
        cols = [c for c in TARGET_COLS if c in final_feature.columns]
        final_feature[cols].sort_values("基金代码").to_csv(
            FEATURE_FILE, index=False, encoding='utf-8-sig')
        print(f"特征表已保存：{len(final_feature)} 只基金")

    


if __name__ == "__main__":
    main()
    main()