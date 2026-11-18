# -*- coding: utf-8 -*-
"""
近三年 A 股全市场汇总分析
==========================

功能：
1. 读取当前 A 股上市公司清单，并按股票缓存前复权日线数据；
2. 统计全市场日度宽度、日收益横截面分位数、周度/月度收益，生成交互式 HTML 汇总看板；
3. 生成全市场收益分析 HTML，不输出评分看板；
4. 不生成任何单只股票的 K 线 HTML。


常用参数：
    --workers 8              抓取并发数
    --force-refresh          忽略价格缓存，重新抓取
    --max-stocks 100         试运行前 100 只；0 表示全部
    --no-score               只生成收益汇总，不计算评分看板
    --output-dir a_share_dashboard

说明：
- 行情接口优先使用当前版本的 ak.stock_zh_a_daily_sina(..., adjust="qfq")；
  对旧版 AKShare 再回退到 ak.stock_zh_a_daily。
- 缓存文件按股票代码拆分为 csv.gz；缓存必须包含本次目标交易日，刷新失败时沿用旧缓存但阻止不完整最新日期发布。
- 年度/月度“累计涨幅”优先取上一期最后一个有效收盘价到本期末的收益；
  上市首期若无上期收盘价，则取上市后首个有效收盘价到本期末，
  汇总中的“等权平均”是股票收益的算术平均；“市场指数收益”是每日等权收益复合。
- 评分看板需要同目录存在 18_zigzag_signal_analyzer.py（或 18.zigzag_signal_analyzer.py）。
"""

from __future__ import annotations

import argparse
import gc
import html
import importlib.util
import itertools
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = "a_share_dashboard"
DEFAULT_CACHE_DIR = ".cache/a_share"
UNIVERSE_CACHE_NAME = "a_share_universe.csv"
UNIVERSE_CACHE_DAYS = 7
# 全历史日期统一按股票池规模校验样本覆盖率；页面仅列示最近 10 个被排除日期。
MIN_DAILY_COVERAGE_RATIO = 0.80
RECENT_DAILY_COMPLETENESS_DAYS = 10
MARKET_CLOSE_HOUR = 16
DEFAULT_WORKERS = 8
DEFAULT_RETRY = 2
DEFAULT_SCORE_HISTORY_DAYS = 250

CORE_CANDIDATES = (
    "18_zigzag_signal_analyzer.py",
    "18.zigzag_signal_analyzer.py",
)
BOARD_DASHBOARD_CANDIDATES = (
    "3.stock_board_score_dashboard(1).py",
    "3.stock_board_score_dashboard.py",
    "stock_board_score_dashboard.py",
)


@dataclass
class Config:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    cache_dir: Path
    output_dir: Path
    workers: int = DEFAULT_WORKERS
    retry: int = DEFAULT_RETRY
    force_refresh: bool = False
    max_stocks: int = 0
    no_score: bool = False
    score_history_days: int = DEFAULT_SCORE_HISTORY_DAYS


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _import_akshare():
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "未安装 akshare。请先执行：pip install akshare pandas numpy plotly"
        ) from exc
    return ak


def _resolve_completed_end_date(requested_end: pd.Timestamp) -> pd.Timestamp:
    """
    将结束日期限制为最近一个已完成的 A 股交易日。

    优先使用上证指数日线日期作为市场交易日参考：
    - 16:00 前运行时，排除当天，防止把盘中日线缓存为最终日线；
    - 16:00 后运行时，使用接口实际返回的最近交易日；
    - 指数接口不可用时，盘中运行退回前一个工作日，盘后保留请求日期。
    """
    requested = pd.Timestamp(requested_end).normalize()
    now_cn = pd.Timestamp.now(tz="Asia/Shanghai")
    today_cn = now_cn.tz_localize(None).normalize()
    target = min(requested, today_cn)

    if target < today_cn:
        return target

    try:
        ak = _import_akshare()
        fetcher = getattr(ak, "stock_zh_index_daily_tx", None)
        if fetcher is not None:
            raw = fetcher(
                symbol="sh000001",
                start_date=(today_cn - pd.Timedelta(days=20)).strftime("%Y%m%d"),
                end_date=target.strftime("%Y%m%d"),
            )
        else:
            fetcher = getattr(ak, "stock_zh_index_daily_em", None)
            raw = (
                fetcher(
                    symbol="sh000001",
                    startdate=(today_cn - pd.Timedelta(days=20)).strftime("%Y%m%d"),
                    enddate=target.strftime("%Y%m%d"),
                )
                if fetcher is not None else None
            )

        if raw is not None and not raw.empty:
            date_col = next(
                (c for c in raw.columns if str(c).strip().lower() in {"date", "日期", "交易日期"}),
                None,
            )
            if date_col is not None:
                dates = pd.to_datetime(raw[date_col], errors="coerce").dropna()
                dates = dates[dates <= target]
                if now_cn.hour < MARKET_CLOSE_HOUR:
                    dates = dates[dates.dt.normalize() < today_cn]
                if not dates.empty:
                    return pd.Timestamp(dates.max()).normalize()
    except Exception as exc:
        print(f"⚠️ 获取上证指数交易日期失败，将使用保守日期回退：{exc}")

    if now_cn.hour < MARKET_CLOSE_HOUR:
        fallback = today_cn - pd.offsets.BDay(1)
        print(
            f"⚠️ 当前为北京时间 {now_cn:%H:%M}，尚未收盘；"
            f"无法确认交易日历，结束日期暂回退至 {fallback:%Y-%m-%d}"
        )
        return pd.Timestamp(fallback).normalize()

    return target


def _safe_code(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(value).strip())


def exchange_code(raw: Any) -> str:
    """把 6 位股票代码转换为 Sina 日线接口使用的 sh/sz/bj 前缀。"""
    code = str(raw).strip()
    code = re.sub(r"\.0$", "", code)
    if code.isdigit():
        code = code.zfill(6)
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("600", "601", "603", "605", "688", "689", "50", "51")):
        return "sh" + code
    if code.startswith(("000", "001", "002", "003", "300", "301", "302", "399")):
        return "sz" + code
    # 北交所代码存在 43/83/87/88/92 等号段；按现行接口统一走 bj。
    if code.startswith(("4", "8", "92")):
        return "bj" + code
    return code


def _plain_code(value: Any) -> str:
    code = str(value).strip()
    if code[:2].lower() in {"sh", "sz", "bj"}:
        return code[2:]
    return code.zfill(6) if code.isdigit() else code


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig")


def _atomic_to_csv(df: pd.DataFrame, path: Path, **kwargs: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        kwargs.setdefault("index", False)
        df.to_csv(tmp, **kwargs)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _load_module(path: Path, alias: str):
    spec = importlib.util.spec_from_file_location(alias, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _find_file(candidates: Iterable[str], explicit: Optional[str] = None) -> Optional[Path]:
    dirs = [Path.cwd(), Path(__file__).resolve().parent]
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.is_file() else None
    seen = set()
    for directory in dirs:
        directory = directory.resolve()
        if directory in seen:
            continue
        seen.add(directory)
        search_dirs = [directory, directory / "upload"]
        for search_dir in search_dirs:
            for name in candidates:
                p = search_dir / name
                if p.is_file():
                    return p
    return None


# ---------------------------------------------------------------------------
# 股票池与缓存行情
# ---------------------------------------------------------------------------
def _normalize_universe(raw: pd.DataFrame) -> pd.DataFrame:
    """兼容 stock_info_a_code_name 与 stock_zh_a_spot_em 的列名。"""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["code", "name", "symbol"])
    df = raw.copy()
    cols = {str(c).strip(): c for c in df.columns}

    def pick(names: Iterable[str]) -> Optional[Any]:
        for name in names:
            if name in cols:
                return cols[name]
        return None

    code_col = pick(("code", "代码", "证券代码", "股票代码"))
    name_col = pick(("name", "名称", "证券简称", "股票名称"))
    if code_col is None:
        return pd.DataFrame(columns=["code", "name", "symbol"])
    out = pd.DataFrame()
    out["code"] = df[code_col].map(_plain_code)
    out["name"] = df[name_col].astype(str).str.strip() if name_col is not None else out["code"]
    out = out[out["code"].str.match(r"^\d{6}$", na=False)].copy()
    out = out[out["code"].str.match(r"^(?:00[0-3]|30[0-2]|60[0-5]|68[89]|4\d{2}|8\d{2}|92\d)\d{3}$")].copy()
    out["symbol"] = out["code"].map(exchange_code)
    out = out.drop_duplicates("code").sort_values("code").reset_index(drop=True)
    return out


def _load_universe_file(path: Path) -> Optional[pd.DataFrame]:
    """读取股票池文件；接口失败时允许使用旧缓存或静态种子文件。"""
    if not path.exists():
        return None
    try:
        df = _read_csv(path)
        if {"code", "name", "symbol"}.issubset(df.columns) and not df.empty:
            return df
    except Exception as exc:
        print(f"⚠️ 股票池文件读取失败：{path}，{exc}")
    return None


def load_universe(cfg: Config) -> pd.DataFrame:
    path = cfg.cache_dir / UNIVERSE_CACHE_NAME
    stale_cache: Optional[pd.DataFrame] = None

    # 缓存不过期时直接使用；即使过期，也保留它作为接口失败时的降级数据。
    if path.exists() and not cfg.force_refresh:
        cached = _load_universe_file(path)
        if cached is not None:
            stale_cache = cached
            age_days = (time.time() - path.stat().st_mtime) / 86400.0
            if age_days <= UNIVERSE_CACHE_DAYS:
                return cached

    ak = _import_akshare()
    raw = None
    errors = []

    # 每个接口只尝试一次，避免在限流期间快速重复请求。
    for method in ("stock_info_a_code_name", "stock_zh_a_spot_em"):
        try:
            fn = getattr(ak, method)
            raw = fn()
            if raw is not None and not raw.empty:
                break
        except Exception as exc:  # pragma: no cover - 由实时接口决定
            errors.append(f"{method}: {exc}")

    universe = _normalize_universe(raw)
    if not universe.empty:
        _atomic_to_csv(universe, path, encoding="utf-8-sig")
        print(f">>> A 股股票池：{len(universe)} 只，已缓存到 {path}")
        return universe

    # 接口限流或暂时不可用时，优先使用旧缓存。
    if stale_cache is not None and not stale_cache.empty:
        print("⚠️ 股票池接口暂时不可用，使用已有股票池缓存。")
        return stale_cache

    # 首次运行没有缓存时，使用随代码提交的静态种子文件。
    seed_candidates = (
        Path(__file__).resolve().parent / "a_share_universe_seed.csv",
        Path.cwd() / "a_share_universe_seed.csv",
    )
    for seed_path in seed_candidates:
        seed = _load_universe_file(seed_path)
        if seed is not None and not seed.empty:
            print(f"⚠️ 股票池接口暂时不可用，使用静态种子文件：{seed_path}")
            _atomic_to_csv(seed, path, encoding="utf-8-sig")
            return seed

    raise RuntimeError(
        "无法获取 A 股股票池，且没有可用的缓存或静态种子文件。"
        + ("；".join(errors) if errors else "")
    )


def _normalize_price_frame(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["Date", "Close"])
    df = raw.copy()
    if isinstance(df.index, pd.DatetimeIndex) and "date" not in {str(c).lower() for c in df.columns}:
        df = df.reset_index()
    mapping: Dict[Any, str] = {}
    for col in df.columns:
        low = str(col).strip().lower()
        if low in {"date", "日期", "交易日期", "index"}:
            mapping[col] = "Date"
        elif low in {"close", "收盘", "收盘价"}:
            mapping[col] = "Close"
    df = df.rename(columns=mapping)
    if "Date" not in df.columns or "Close" not in df.columns:
        return pd.DataFrame(columns=["Date", "Close"])
    out = df[["Date", "Close"]].copy()
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
    out = out.dropna(subset=["Date", "Close"])
    out = out[out["Close"] > 0].drop_duplicates("Date", keep="last")
    return out.sort_values("Date").reset_index(drop=True)


def _merge_price_frames(existing: Optional[pd.DataFrame],
                        incoming: pd.DataFrame) -> pd.DataFrame:
    """合并新旧行情，避免接口只返回部分数据时覆盖原有完整历史缓存。"""
    frames = []
    if existing is not None and not existing.empty:
        frames.append(_normalize_price_frame(existing))
    if incoming is not None and not incoming.empty:
        frames.append(_normalize_price_frame(incoming))
    if not frames:
        return pd.DataFrame(columns=["Date", "Close"])
    merged = pd.concat(frames, ignore_index=True)
    return _normalize_price_frame(merged)


def _price_cache_path(cfg: Config, code: str) -> Path:
    return cfg.cache_dir / "prices" / f"{_safe_code(code)}.csv.gz"


def _cache_is_fresh(path: Path, expected_latest: pd.Timestamp) -> bool:
    """缓存必须覆盖本次目标交易日，不能用几天前的行情冒充最新数据。"""
    try:
        sample = pd.read_csv(path, usecols=["Date"], compression="infer")
        dates = pd.to_datetime(sample["Date"], errors="coerce").dropna()
        if dates.empty:
            return False
        target = pd.Timestamp(expected_latest).normalize()
        # 目标日期是已确认的最近完整交易日。若个股当日停牌，缓存会被重新检查/抓取，
        # 最终仍由全市场覆盖率门槛决定是否允许发布。
        return bool((dates.dt.normalize() >= target).any())
    except Exception:
        return False


def _fetch_daily_price(ak: Any, symbol: str,
                       start_date: Optional[pd.Timestamp] = None,
                       end_date: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """获取前复权日线：优先新浪接口，失败时回退东方财富接口。"""
    start_text = (
        (pd.Timestamp(start_date) - pd.offsets.BDay(5)).strftime("%Y%m%d")
        if start_date is not None else None
    )
    end_text = pd.Timestamp(end_date).strftime("%Y%m%d") if end_date is not None else None
    errors: List[str] = []

    # AKShare 1.20.46+ 使用 stock_zh_a_daily_sina；旧版本尝试旧名称。
    for fetcher_name in ("stock_zh_a_daily_sina", "stock_zh_a_daily"):
        fetcher = getattr(ak, fetcher_name, None)
        if fetcher is None:
            continue
        kwargs: Dict[str, Any] = {"symbol": symbol, "adjust": "qfq"}
        if start_text is not None:
            kwargs["start_date"] = start_text
        if end_text is not None:
            kwargs["end_date"] = end_text
        try:
            raw = fetcher(**kwargs)
            if raw is not None and not raw.empty:
                return raw
            errors.append(f"{fetcher_name}: 接口返回空数据")
        except Exception as exc:
            errors.append(f"{fetcher_name}: {exc}")

    # 新浪接口对少数股票可能因股本字段缺失而报 Length mismatch；回退东方财富日线。
    fallback = getattr(ak, "stock_zh_a_hist", None)
    if fallback is not None:
        kwargs = {
            "symbol": _plain_code(symbol),
            "period": "daily",
            "adjust": "qfq",
        }
        if start_text is not None:
            kwargs["start_date"] = start_text
        if end_text is not None:
            kwargs["end_date"] = end_text
        try:
            raw = fallback(**kwargs)
            if raw is not None and not raw.empty:
                return raw
            errors.append("stock_zh_a_hist: 接口返回空数据")
        except Exception as exc:
            errors.append(f"stock_zh_a_hist: {exc}")

    detail = "；".join(errors) if errors else "当前 AKShare 未提供可用日线接口"
    raise RuntimeError(f"{symbol} 行情获取失败：{detail}")


def load_or_fetch_price(code: str, name: str, cfg: Config) -> Tuple[str, str, pd.DataFrame, Optional[str]]:
    path = _price_cache_path(cfg, code)
    # analyze() 已将 cfg.end_date 解析为最近一个已完成交易日；缓存必须覆盖这一天。
    expected_latest = pd.Timestamp(cfg.end_date).normalize()

    # 无论缓存是否过期，都先读入旧缓存。刷新失败时必须保留它，不能让整只股票的历史数据消失。
    cached_frame: Optional[pd.DataFrame] = None
    if path.exists():
        try:
            cached_raw = pd.read_csv(path, compression="infer")
            cached_frame = _normalize_price_frame(cached_raw)
            if cached_frame.empty:
                cached_frame = None
        except Exception as exc:
            print(f"⚠️ {code} 缓存读取失败，将尝试重新抓取：{exc}")
            cached_frame = None

    if (
        cached_frame is not None
        and not cfg.force_refresh
        and _cache_is_fresh(path, expected_latest)
    ):
        return code, name, cached_frame, None

    ak = _import_akshare()
    last_error = None
    retry_limit = min(max(int(cfg.retry), 0), 1)
    for attempt in range(retry_limit + 1):
        try:
            raw = _fetch_daily_price(ak, code, cfg.start_date, cfg.end_date)
            fetched_frame = _normalize_price_frame(raw)
            if fetched_frame.empty:
                raise ValueError("接口返回空数据")

            # 新接口响应可能不完整；按日期合并，保留旧缓存中未被本次响应覆盖的历史记录。
            merged_frame = _merge_price_frames(cached_frame, fetched_frame)
            _atomic_to_csv(
                merged_frame, path, index=False, compression="gzip", encoding="utf-8"
            )
            return code, name, merged_frame, None
        except Exception as exc:  # pragma: no cover - 由实时接口决定
            last_error = str(exc)
            if attempt < retry_limit:
                time.sleep(10 * (2 ** attempt))

    # 刷新失败时继续使用旧缓存，保证单只股票的历史数据不会从全市场统计中消失。
    if cached_frame is not None and not cached_frame.empty:
        return code, name, cached_frame, f"刷新失败，沿用旧缓存：{last_error or '未知错误'}"

    return (
        code,
        name,
        pd.DataFrame(columns=["Date", "Close"]),
        last_error or "未知错误",
    )


# ---------------------------------------------------------------------------
# 收益统计
# ---------------------------------------------------------------------------
def _period_returns(close: pd.Series, freq: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    close = close.dropna().sort_index()
    s = close[(close.index >= start) & (close.index <= end)]
    if s.empty:
        return pd.DataFrame(columns=["period", "return"])
    # pandas 2.2 将 Y/M 别名改为 YE/ME；先用新别名，旧版失败再回退。
    new_freq = {"Y": "YE", "M": "ME"}.get(freq, freq)
    try:
        grouped = s.resample(new_freq)
    except ValueError:  # 兼容 pandas 1.x
        grouped = s.resample(freq)
    rows = []
    for period, group in grouped:
        if group.empty:
            continue
        first_date = group.index[0]
        prev = close.iloc[:close.index.searchsorted(first_date)]
        if not prev.empty:
            base = float(prev.iloc[-1])
        elif len(group) >= 2:
            # 新上市首期无上期收盘价：上市首个可用收盘价作为基准。
            base = float(group.iloc[0])
        else:
            continue
        rows.append({"period": pd.Timestamp(period), "return": float(group.iloc[-1]) / base - 1.0})
    return pd.DataFrame(rows, columns=["period", "return"])



def _new_daily_accumulator() -> Dict[pd.Timestamp, Dict[str, Any]]:
    """按交易日累积全市场股票收益率；保留横截面样本以计算精确分位数和直方图。"""
    return {}


# 直方图采用 1 个百分点的主体分箱，尾部合并，避免极端新股收益压扁主体分布。
_HIST_EDGES = np.array([-0.95] + list(np.arange(-0.30, 0.301, 0.01)) + [5.0], dtype=float)
_HIST_LABELS = (
    ["≤-30%"]
    + [f"{int(round(_HIST_EDGES[i] * 100))}~{int(round(_HIST_EDGES[i + 1] * 100))}%"
       for i in range(1, len(_HIST_EDGES) - 2)]
    + ["≥30%"]
)


def _add_daily_returns(acc: Dict[pd.Timestamp, Dict[str, Any]], close: pd.Series,
                       start: pd.Timestamp, end: pd.Timestamp) -> None:
    ret = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    ret = ret[(ret.index >= start) & (ret.index <= end)].clip(-0.95, 5.0)
    for dt, value in ret.items():
        v = float(value)
        key = pd.Timestamp(dt).normalize()
        row = acc.setdefault(key, {
            "count": 0, "sum_return": 0.0, "up": 0, "down": 0, "flat": 0,
            "returns": [],
        })
        row["count"] += 1
        row["sum_return"] += v
        row["up"] += int(v > 0)
        row["down"] += int(v < 0)
        row["flat"] += int(v == 0)
        row["returns"].append(v)


def _aggregate_period_records(records: List[Dict[str, Any]], period_name: str) -> pd.DataFrame:
    columns = [period_name, "avg_return", "median_return", "up_ratio", "stock_count",
               "q5_return", "q25_return", "q75_return", "q95_return", "skewness", "kurtosis",
               "max_return", "max_code", "max_name", "min_return", "min_code", "min_name"]
    if not records:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(records)
    g = df.groupby(period_name)["return"]
    out = g.agg(
        avg_return="mean", median_return="median", stock_count="count",
        q5_return=lambda s: s.quantile(0.05),
        q25_return=lambda s: s.quantile(0.25),
        q75_return=lambda s: s.quantile(0.75),
        q95_return=lambda s: s.quantile(0.95),
        skewness=lambda s: s.skew(),
        kurtosis=lambda s: s.kurt() + 3.0,
    ).reset_index()
    up = (df.assign(_up=df["return"] > 0).groupby(period_name)["_up"].mean()
          .rename("up_ratio").reset_index())
    out = out.merge(up, on=period_name, how="left")
    detail_rows = []
    for period, sub in df.groupby(period_name):
        max_row = sub.loc[sub["return"].idxmax()]
        min_row = sub.loc[sub["return"].idxmin()]
        detail_rows.append({
            period_name: period,
            "max_return": float(max_row["return"]),
            "max_code": str(max_row["code"]),
            "max_name": str(max_row["name"]),
            "min_return": float(min_row["return"]),
            "min_code": str(min_row["code"]),
            "min_name": str(min_row["name"]),
        })
    out = out.merge(pd.DataFrame(detail_rows), on=period_name, how="left")
    for col in ("avg_return", "median_return", "up_ratio", "q5_return", "q25_return",
                "q75_return", "q95_return", "skewness", "kurtosis"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values(period_name).reset_index(drop=True)


def _aggregate_monthly_records(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """月度横截面收益统计，并保留股票年内累计收益的等权均值。"""
    out = _aggregate_period_records(records, "period")
    if not records:
        out["ytd_return"] = np.nan
        return out
    df = pd.DataFrame(records).copy()
    df["period"] = pd.to_datetime(df["period"])
    df["year"] = df["period"].dt.year
    df = df.sort_values(["code", "year", "period"])
    df["ytd_return"] = df.groupby(["code", "year"])["return"].transform(
        lambda s: (1.0 + s).cumprod() - 1.0
    )
    ytd = df.groupby("period")["ytd_return"].agg(ytd_return="mean").reset_index()
    return out.merge(ytd, on="period", how="left")


def _daily_frame(
    acc: Dict[pd.Timestamp, Dict[str, Any]],
    expected_stock_count: int = 0,
) -> pd.DataFrame:
    rows = []
    ordered_items = sorted(acc.items())
    min_sample_count = (
        max(1, int(math.ceil(expected_stock_count * MIN_DAILY_COVERAGE_RATIO)))
        if expected_stock_count > 0 else 1
    )

    for dt, item in ordered_items:
        values = np.asarray(item["returns"], dtype=float)
        if values.size:
            q5, q25, median, q75, q95 = np.quantile(
                values, [0.05, 0.25, 0.50, 0.75, 0.95]
            )
            vmin, vmax = float(np.min(values)), float(np.max(values))
            if np.isclose(vmin, vmax):
                span = max(0.005, abs(vmin) * 0.08)
                edges = np.linspace(vmin - span, vmax + span, 9)
            else:
                edges = np.linspace(vmin, vmax, 21)
            hist = np.histogram(values, bins=edges)[0].astype(int).tolist()
        else:
            q5 = q25 = median = q75 = q95 = np.nan
            edges = np.array([], dtype=float)
            hist = [0] * (len(_HIST_EDGES) - 1)

        count = int(item["count"])
        up, down, flat = int(item["up"]), int(item["down"]), int(item["flat"])
        coverage = (
            min(1.0, count / expected_stock_count)
            if expected_stock_count > 0 else np.nan
        )
        # 对全历史日期统一校验，防止缓存抓取失败后只剩少数股票仍被当成完整市场数据。
        is_complete = count >= min_sample_count

        rows.append({
            "date": dt.strftime("%Y-%m-%d"),
            "avg_return": float(item["sum_return"] / count) if count else np.nan,
            "median_return": float(median),
            "q5_return": float(q5),
            "q25_return": float(q25),
            "q75_return": float(q75),
            "q95_return": float(q95),
            "up": up, "down": down, "flat": flat, "total": count,
            "coverage_ratio": float(coverage) if np.isfinite(coverage) else np.nan,
            "is_complete": bool(is_complete),
            "up_ratio": up / count if count else np.nan,
            "down_ratio": down / count if count else np.nan,
            "flat_ratio": flat / count if count else np.nan,
            "hist_edges": edges.tolist() if values.size else [],
            "hist_counts": hist,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["avg_return"] = pd.to_numeric(out["avg_return"], errors="coerce")
    # 样本不足的近期日期不参与累计收益复合，避免少数股票扭曲全市场序列。
    valid_daily_returns = out["avg_return"].where(out["is_complete"], np.nan)
    out["cumulative"] = (1.0 + valid_daily_returns.fillna(0.0)).cumprod() - 1.0
    return out


def _build_rankings(period_rows: List[Dict[str, Any]], top_n: int = 20) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    if not period_rows:
        return {}
    df = pd.DataFrame(period_rows)
    result: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for period, sub in df.groupby("period"):
        sub = sub.sort_values("return", ascending=False)
        def pack(x: pd.DataFrame) -> List[Dict[str, Any]]:
            return [
                {"code": str(r["code"]), "name": str(r["name"]),
                 "return": round(float(r["return"]), 6)}
                for _, r in x.iterrows()
            ]
        # 年度排名以四位年份作为键，便于直接与年度图表联动。
        key = str(pd.Timestamp(period).year)
        result[key] = {"top": pack(sub.head(top_n)), "bottom": pack(sub.tail(top_n).sort_values("return"))}
    return result


def _welch_mean_test(a: pd.Series, b: pd.Series) -> Tuple[float, float]:
    """返回 Welch t 统计量和双侧 p 值；无 scipy 时用正态近似兜底。"""
    x = pd.to_numeric(a, errors="coerce").dropna().to_numpy(dtype=float)
    y = pd.to_numeric(b, errors="coerce").dropna().to_numpy(dtype=float)
    if len(x) < 2 or len(y) < 2:
        return np.nan, np.nan
    vx, vy = float(np.var(x, ddof=1)), float(np.var(y, ddof=1))
    se2 = vx / len(x) + vy / len(y)
    if not np.isfinite(se2) or se2 <= 0:
        return 0.0, 1.0 if np.isclose(np.mean(x), np.mean(y)) else 0.0
    t_value = (float(np.mean(x)) - float(np.mean(y))) / math.sqrt(se2)
    denom = (vx / len(x)) ** 2 / (len(x) - 1) + (vy / len(y)) ** 2 / (len(y) - 1)
    df = (se2 ** 2 / denom) if denom > 0 else np.inf
    try:
        from scipy.stats import t as student_t  # type: ignore
        p_value = float(2.0 * student_t.sf(abs(t_value), df))
    except Exception:
        # A 股全市场样本量通常很大，正态近似足以作为无 scipy 环境的降级方案。
        p_value = float(math.erfc(abs(t_value) / math.sqrt(2.0)))
    return float(t_value), max(0.0, min(1.0, p_value))


def _distribution_summary(series: pd.Series) -> Dict[str, Any]:
    s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if s.empty:
        return {"count": 0, "mean": None, "std": None, "median": None,
                "q1": None, "q3": None, "skewness": None, "kurtosis": None}
    return {
        "count": int(s.size),
        "mean": float(s.mean()),
        "std": float(s.std(ddof=1)) if s.size > 1 else 0.0,
        "median": float(s.median()),
        "q1": float(s.quantile(0.25)),
        "q3": float(s.quantile(0.75)),
        "skewness": float(s.skew()) if s.size > 2 and np.isfinite(s.skew()) else None,
        "kurtosis": float(s.kurt() + 3.0) if s.size > 3 and np.isfinite(s.kurt()) else None,
    }


def _build_distribution_analysis(records: List[Dict[str, Any]], group_kind: str) -> Dict[str, Any]:
    """按年份或月份汇总收益分布，并进行两两 Welch 均值检验。"""
    if not records:
        return {"groups": [], "comparisons": [], "method": "Welch 独立样本均值检验"}
    df = pd.DataFrame(records).copy()
    df["period"] = pd.to_datetime(df["period"], errors="coerce")
    df["return"] = pd.to_numeric(df["return"], errors="coerce")
    df = df.dropna(subset=["period", "return"])
    if group_kind == "year":
        df["group"] = df["period"].dt.year.astype(int)
        label_fn = lambda x: str(int(x))
    else:
        df["group"] = df["period"].dt.month.astype(int)
        label_fn = lambda x: f"{int(x)}月"

    groups: List[Dict[str, Any]] = []
    values: Dict[str, pd.Series] = {}
    for group, sub in sorted(df.groupby("group"), key=lambda item: item[0]):
        label = label_fn(group)
        values[label] = sub["return"]
        summary = _distribution_summary(sub["return"])
        groups.append({"group": int(group), "label": label, **summary})

    comparisons: List[Dict[str, Any]] = []
    for label_a, label_b in itertools.combinations(values.keys(), 2):
        t_value, p_value = _welch_mean_test(values[label_a], values[label_b])
        mean_diff = float(values[label_a].mean() - values[label_b].mean())
        comparisons.append({
            "a": label_a, "b": label_b, "t_stat": t_value,
            "p_value": p_value, "mean_diff": mean_diff,
            "similar": bool(np.isfinite(p_value) and p_value >= 0.05),
        })
    # 把“未发现显著差异”的组合排在前面，便于直接找到统计上相近的组。
    comparisons.sort(key=lambda x: (not x["similar"], -(x["p_value"] if x["p_value"] is not None else -1)))
    return {"groups": groups, "comparisons": comparisons, "method": "Welch 独立样本均值检验"}


# ---------------------------------------------------------------------------
# 评分历史与 stock_board_score_dashboard
# ---------------------------------------------------------------------------
def _load_score_core(explicit: Optional[str] = None):
    path = _find_file(CORE_CANDIDATES, explicit)
    if path is None:
        return None, None
    try:
        return _load_module(path, "a_share_score_core"), path
    except Exception as exc:
        print(f"⚠️ 评分内核加载失败：{exc}")
        return None, path


def _score_frame(close: pd.Series, core: Any, history_days: int) -> Optional[pd.DataFrame]:
    if core is None:
        return None
    try:
        min_rows = int(getattr(core, "MIN_ROWS", 60))
        if len(close) < min_rows:
            return None
        ind = core.compute_eight_indicators(close)
        net_s, int_s, _, _ = core.compute_heat_score_series(ind)
        score_s = core.calc_score_series(net_s, int_s)
        score_s = pd.to_numeric(score_s, errors="coerce").dropna()
        if history_days and len(score_s) > history_days:
            score_s = score_s.tail(history_days)
        if score_s.empty:
            return None
        return pd.DataFrame({"score": score_s})
    except Exception:
        return None


def _load_board_dashboard_module():
    path = _find_file(BOARD_DASHBOARD_CANDIDATES)
    if path is None:
        return None, None
    try:
        return _load_module(path, "a_share_board_score_dashboard"), path
    except Exception as exc:
        print(f"⚠️ stock_board_score_dashboard 模块加载失败：{exc}")
        return None, path


def write_score_dashboard(score_rows: List[Dict[str, Any]], output_path: Path, core: Any,
                          history_days: int) -> bool:
    module, module_path = _load_board_dashboard_module()
    if module is None or not hasattr(module, "build_board_score_dashboard"):
        print("⚠️ 未找到 3.stock_board_score_dashboard.py，跳过评分看板。")
        return False
    if not score_rows:
        print("⚠️ 没有有效评分历史，跳过评分看板。")
        return False
    try:
        module.build_board_score_dashboard(
            score_rows, str(output_path), core_module=core, history_days=history_days
        )
        print(f">>> 已生成评分汇总看板：{output_path}（复用 {module_path.name}）")
        return True
    except Exception as exc:
        print(f"⚠️ 评分汇总看板生成失败：{exc}")
        return False


# ---------------------------------------------------------------------------
# 收益 HTML 看板
# ---------------------------------------------------------------------------
def _json_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")



def render_market_dashboard(payload: Dict[str, Any], title: str = "A 股全市场宽度与收益分析") -> str:
    data = _json_script(payload)
    template = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--line:#e5e7eb;--text:#1f2937;--muted:#6b7280;--red:#ef4444;--green:#16a34a;--blue:#3b82f6;--gold:#d97706}
*{box-sizing:border-box}body{margin:0;padding:22px;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif}
main{max-width:1540px;margin:auto}h1{font-size:24px;margin:0 0 4px}.muted,.hint{color:var(--muted);font-size:12px}
.top{display:flex;justify-content:space-between;align-items:end;margin-bottom:16px}.tag{border:1px solid var(--line);padding:5px 10px;border-radius:20px;color:var(--muted);font-size:11px}
.toolbar,.panel,.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px}.toolbar{padding:13px;display:flex;gap:11px;align-items:end;flex-wrap:wrap;margin-bottom:13px}
.field{display:grid;gap:5px;color:var(--muted);font-size:11px}input,button,select{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px 10px;font:inherit;font-size:12px}input{color-scheme:light}button{cursor:pointer}button.active{background:#3b82f6;border-color:#3b82f6;color:white}
.quick,.switch{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.spacer{flex:1}
.kpis{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:9px;margin-bottom:13px}.kpi{padding:12px;min-width:0}.kpi label{display:block;color:var(--muted);font-size:11px}.kpi strong{display:block;font-size:20px;margin-top:5px;overflow-wrap:anywhere}
.up{color:var(--red)}.down{color:var(--green)}
.grid{display:grid;grid-template-columns:1.55fr 1fr;gap:13px}.panel{padding:14px;margin-bottom:13px;min-width:0}.panelhead{display:flex;justify-content:space-between;align-items:start;gap:10px;flex-wrap:wrap;margin-bottom:7px}.panel h2{font-size:15px;margin:0 0 3px}.chart{height:370px;width:100%}.chart.tall{height:430px}.chart.compact{height:320px}
.tablewrap{overflow:auto;margin-top:5px}table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}td,th{padding:8px 9px;border-bottom:1px solid var(--line);text-align:right}td:first-child,th:first-child{text-align:left}th{color:var(--muted);font-weight:500}
.hidden{display:none!important}.foot{text-align:center;padding:8px 0 20px}
.calendar-years{display:flex;gap:5px;flex-wrap:wrap}.calendar-grid{display:grid;grid-template-columns:repeat(4,minmax(220px,1fr));gap:10px}.month-card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px}.month-header{text-align:center;color:var(--text);font-weight:600;padding:4px;background:var(--calendar-head);border-radius:5px;margin-bottom:4px}.month-table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:10px}.month-table th{color:var(--muted);padding:3px;text-align:center}.month-table th.weekend{color:var(--red)}.month-table td{height:34px;text-align:center;vertical-align:top;border:1px solid var(--calendar-cell-line);padding:2px;color:var(--text)}.month-table td.empty{border-color:transparent;background:transparent}.day-num{font-weight:600}.day-val{font-size:9px;font-weight:600}.month-stat{text-align:center;font-size:10px;margin-top:5px;color:var(--muted)}.month-stat strong{font-size:11px}.calendar-year-btn{padding:5px 9px}.calendar-year-btn.active{background:#3b82f6;border-color:#3b82f6}
@media(max-width:1050px){.calendar-grid{grid-template-columns:repeat(3,minmax(220px,1fr))}}@media(max-width:760px){.calendar-grid{grid-template-columns:repeat(2,minmax(180px,1fr))}}@media(max-width:500px){.calendar-grid{grid-template-columns:1fr}}
@media(max-width:1050px){.kpis{grid-template-columns:repeat(3,1fr)}.grid{grid-template-columns:1fr}}
@media(max-width:620px){body{padding:11px}.top{display:block}.tag{display:inline-block;margin-top:8px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.chart,.chart.tall{height:340px}}
</style>
</head>
<body><main>
<div class="top"><div><h1>__TITLE__</h1><div class="muted">A 股全市场 · 日度宽度 · 股票横截面分布 · 分位数日历</div></div><span class="tag">交互式行情分析</span></div>
<section class="toolbar">
 <label class="field">开始日期<input id="startDate" type="date"></label>
 <label class="field">结束日期<input id="endDate" type="date"></label>
 <div class="field"><span>快捷范围</span><div class="quick"><button data-quick="ytd">今年以来</button><button data-quick="last">去年</button><button data-quick="prev">前年</button></div></div>
 <div class="field"><span>背景风格</span><div class="quick"><button class="theme-btn active" data-theme="light">浅色</button><button class="theme-btn" data-theme="mist">雾灰</button><button class="theme-btn" data-theme="blue">浅蓝</button><button class="theme-btn" data-theme="dark">深色</button></div></div>
 <div class="spacer"></div>
 <div class="field"><span>展示模式</span><div class="switch"><button id="rangeMode" class="active">区间范围</button><button id="latestMode">最新日期</button></div></div>
 <button id="applyDates">应用日期</button>
</section>
<div id="kpis" class="kpis"></div>

<section id="dailyPanel" class="panel">
 <div class="panelhead"><div><h2>全市场日度宽度</h2><div class="hint">上涨/下跌家数与上涨/下跌比例可切换查看</div></div><div class="switch"><button id="breadthCount" class="active">上涨/下跌家数</button><button id="breadthRatio">上涨/下跌比例</button></div></div>
 <div id="dailyChart" class="chart tall"></div>
</section>

<section id="distributionPanel" class="panel">
 <div class="panelhead">
  <div><h2 id="distributionTitle">每日股票涨跌幅分布</h2><div class="hint">按所选交易日统计全市场股票收益率横截面；直方图尾部合并显示</div></div>
  <label class="field">分布日期<select id="distributionDate"></select></label>
 </div>
 <div id="distributionStats" class="kpis"></div>
 <div id="distributionChart" class="chart"></div>
</section>

<section id="calendarPanel" class="panel">
 <div class="panelhead">
  <div><h2>每日涨跌幅分位数日历</h2><div class="hint">挂历颜色与日涨跌幅方向一致，可切换均值、中位数、P25、P75</div></div>
  <div class="switch"><span class="muted">年份：</span><span id="calendarYears"></span><span class="muted">指标：</span><button class="calendarMetric active" data-metric="avg_return">均值</button><button class="calendarMetric" data-metric="median_return">中位数</button><button class="calendarMetric" data-metric="q5_return">P5</button><button class="calendarMetric" data-metric="q25_return">P25</button><button class="calendarMetric" data-metric="q75_return">P75</button><button class="calendarMetric" data-metric="q95_return">P95</button></div>
 </div>
 <div id="calendarGrid" class="calendar-grid"></div>
 <div class="tablewrap"><table id="calendarTable"></table></div>
</section>

<section id="annualPanel" class="panel">
 <div class="panelhead"><div><h2>年度表现</h2><div class="hint">保留原年度汇总功能，展示各年度股票收益横截面统计</div></div></div>
 <div class="tablewrap"><table id="annualTable"></table></div>
</section>
<section id="rankPanel" class="panel">
 <div class="panelhead"><div><h2 id="rankTitle">年度涨跌幅排名</h2><div class="hint">年度涨幅前 20 与跌幅后 20</div></div><label class="field">查看年份<select id="rankYear"></select></label></div>
 <div class="tablewrap"><table id="rankTable"></table></div>
</section>
<div id="foot" class="muted foot"></div>
</main>

<script>
const DATA=__DATA__;
const $=id=>document.getElementById(id);
const fmtPct=v=>v==null||!Number.isFinite(Number(v))?'--':(Number(v)*100).toFixed(2)+'%';
const fmtNum=v=>v==null||!Number.isFinite(Number(v))?'--':Number(v).toLocaleString('zh-CN');
const C={red:'#f06c75',green:'#38bd91',blue:'#70a9f5',gold:'#e9bd70',muted:'#91a0b4'};
const CFG={responsive:true,displaylogo:false,modeBarButtonsToRemove:['lasso2d','select2d']};
let BASE={paper_bgcolor:'#fff',plot_bgcolor:'#fff',font:{color:'#374151',size:11},margin:{l:58,r:58,t:28,b:58},legend:{orientation:'h',y:1.12},xaxis:{gridcolor:'#e5e7eb'},yaxis:{gridcolor:'#e5e7eb'}};
const THEMES={
 light:{bg:'#f6f7f9',panel:'#ffffff',line:'#e5e7eb',text:'#1f2937',muted:'#6b7280',plot:'#ffffff',grid:'#e5e7eb',head:'#f3f4f6',cell:'#f3f4f6'},
 mist:{bg:'#eef2f5',panel:'#f8fafc',line:'#d7dee7',text:'#243447',muted:'#64748b',plot:'#f8fafc',grid:'#d7dee7',head:'#e8eef4',cell:'#dce5ed'},
 blue:{bg:'#eaf3fb',panel:'#f7fbff',line:'#c8dced',text:'#1e3a5f',muted:'#5b7693',plot:'#f7fbff',grid:'#c8dced',head:'#e5f0fa',cell:'#d7e7f4'},
 dark:{bg:'#0d121a',panel:'#151e2b',line:'#273447',text:'#e8eef7',muted:'#91a0b4',plot:'#151e2b',grid:'#273447',head:'#202c3c',cell:'#273447'}
};
function applyTheme(name){
 const t=THEMES[name]||THEMES.light, root=document.documentElement;
 currentTheme=name;
 Object.entries({'--bg':t.bg,'--panel':t.panel,'--line':t.line,'--text':t.text,'--muted':t.muted,'--calendar-head':t.head,'--calendar-cell-line':t.cell}).forEach(([k,v])=>root.style.setProperty(k,v));
 BASE={paper_bgcolor:t.plot,plot_bgcolor:t.plot,font:{color:t.text,size:11},margin:{l:58,r:58,t:28,b:58},legend:{orientation:'h',y:1.12},xaxis:{gridcolor:t.grid},yaxis:{gridcolor:t.grid}};
 document.querySelectorAll('.theme-btn').forEach(b=>b.classList.toggle('active',b.dataset.theme===name));
 ['dailyChart','distributionChart'].forEach(id=>{const el=$(id);if(el&&el.data)Plotly.relayout(el,{paper_bgcolor:t.plot,plot_bgcolor:t.plot,'font.color':t.text,'xaxis.gridcolor':t.grid,'yaxis.gridcolor':t.grid});});
 if(mode==='range'){renderDailyChart(inRange());renderDistribution(selectedDay());renderCalendar();}
}
const usableDaily=(DATA.daily||[]).filter(d=>d.is_complete!==false);
const allDates=usableDaily.map(d=>d.date);
const minDate=allDates[0],maxDate=allDates[allDates.length-1];
const years=[...new Set(allDates.map(d=>Number(d.slice(0,4))))].sort((a,b)=>a-b);
const currentYear=years[years.length-1];
let mode='range',breadthMode='count',calendarMetric='avg_return',selectedCalendarYear=currentYear,selectedDistributionDate=maxDate,currentTheme='light';

function inRange(){return usableDaily.filter(d=>d.date>=$('startDate').value&&d.date<=$('endDate').value);}
function getLatest(rows){return rows.length?rows[rows.length-1]:null;}
function setDateInputs(a,b){$('startDate').value=a;$('endDate').value=b;}
function yearBounds(y){
 const a=`${y}-01-01`,b=`${y}-12-31`;
 return [a<minDate?minDate:a,b>maxDate?maxDate:b];
}
function setQuick(which){
 let y=currentYear;
 if(which==='last')y=currentYear-1;
 if(which==='prev')y=currentYear-2;
 const [a,b]=yearBounds(y);setDateInputs(a,b);selectedDistributionDate=b;
 document.querySelectorAll('[data-quick]').forEach(x=>x.classList.toggle('active',x.dataset.quick===which));
 render();
}
function buildCalendarYearSelect(){
 $('calendarYears').innerHTML=years.slice().reverse().map(y=>`<button class="calendar-year-btn ${y===selectedCalendarYear?'active':''}" data-year="${y}">${y}</button>`).join('');
 $('calendarYears').querySelectorAll('button').forEach(b=>b.onclick=()=>{selectedCalendarYear=Number(b.dataset.year);buildCalendarYearSelect();renderCalendar();});
}
function buildDateSelect(rows){
 const sel=$('distributionDate');
 const dates=rows.map(d=>d.date);
 if(!dates.includes(selectedDistributionDate))selectedDistributionDate=dates[dates.length-1]||'';
 sel.innerHTML=dates.map(d=>`<option value="${d}">${d}</option>`).join('');
 sel.value=selectedDistributionDate;
 sel.disabled=mode==='latest';
}
function selectedDay(){
 const rows=inRange();
 if(mode==='latest')return getLatest(rows);
 return rows.find(d=>d.date===selectedDistributionDate)||getLatest(rows);
}
function renderKpis(rows,day){
 const mean=rows.length?rows.reduce((s,d)=>s+Number(d.avg_return||0),0)/rows.length:null;
 let wealth=1;rows.forEach(d=>{wealth*=1+Number(d.avg_return||0);});const cumulative=rows.length?wealth-1:null;
 const items=mode==='latest'?[
  ['交易日期',day?.date||'--',''],
  ['上涨家数',fmtNum(day?.up),'up'],
  ['下跌家数',fmtNum(day?.down),'down'],
  ['等权平均涨幅',fmtPct(day?.avg_return),Number(day?.avg_return)>=0?'up':'down'],
  ['横截面中位数',fmtPct(day?.median_return),Number(day?.median_return)>=0?'up':'down'],
  ['P5 / P95',fmtPct(day?.q5_return)+' / '+fmtPct(day?.q95_return),'']
 ]:[
  ['区间交易日',fmtNum(rows.length)+' 天',''],
  ['区间日均收益',fmtPct(mean),Number(mean)>=0?'up':'down'],
  ['平均上涨比例',fmtPct(rows.length?rows.reduce((s,d)=>s+Number(d.up_ratio||0),0)/rows.length:null),'up'],
  ['最新日上涨家数',fmtNum(day?.up),'up'],
  ['最新日下跌家数',fmtNum(day?.down),'down']
 ];
 $('kpis').innerHTML=items.map(x=>`<div class="kpi"><label>${x[0]}</label><strong class="${x[2]}">${x[1]}</strong></div>`).join('');
}
function renderDailyChart(rows){
 const x=rows.map(d=>d.date);
 let traces,l=JSON.parse(JSON.stringify(BASE)); l.hovermode='x unified'; l.xaxis={type:'date',tickformat:'%Y-%m-%d',gridcolor:THEMES[currentTheme].grid};
 if(breadthMode==='count'){
  traces=[{name:'上涨家数',x,y:rows.map(d=>d.up),type:'bar',marker:{color:'rgba(240,108,117,.75)'}},{name:'下跌家数',x,y:rows.map(d=>-d.down),type:'bar',marker:{color:'rgba(56,189,145,.75)'},customdata:rows.map(d=>d.down),hovertemplate:'%{x}<br>下跌：%{customdata} 家<extra></extra>'}];
  l.barmode='relative';l.yaxis={title:'上涨 / 下跌家数',gridcolor:THEMES[currentTheme].grid};
 }else{
  traces=[{name:'上涨比例',x,y:rows.map(d=>Number(d.up_ratio)*100),type:'scatter',mode:'lines',line:{color:C.red,width:1.8}},{name:'下跌比例',x,y:rows.map(d=>Number(d.down_ratio)*100),type:'scatter',mode:'lines',line:{color:C.green,width:1.8}}];
  l.yaxis={title:'比例（%）',range:[0,100],gridcolor:THEMES[currentTheme].grid,zeroline:false,dtick:10};
  l.shapes=[{type:'line',xref:'paper',x0:0,x1:1,y0:20,y1:20,line:{color:'#d97706',dash:'dash',width:1}},{type:'line',xref:'paper',x0:0,x1:1,y0:80,y1:80,line:{color:'#d97706',dash:'dash',width:1}}];
  l.annotations=[{xref:'paper',x:1,y:20,text:'20%',showarrow:false,font:{size:10,color:'#d97706'}},{xref:'paper',x:1,y:80,text:'80%',showarrow:false,font:{size:10,color:'#d97706'}}];
 }
 Plotly.react('dailyChart',traces,l,CFG);
}
function renderDistribution(day){
 if(!day){Plotly.purge('distributionChart');$('distributionStats').innerHTML='';return;}
 $('distributionTitle').textContent=(mode==='latest'?'最新交易日':'所选交易日')+'：'+day.date+' 股票涨跌幅分布';
 const edges=day.hist_edges||[]; const counts=day.hist_counts||[];
 const labels=edges.slice(0,-1).map((v,i)=>`${Math.round(v*100)}%~${Math.round(edges[i+1]*100)}%`);
 const barColors=labels.map((_,i)=>i<Math.floor(labels.length/2)?C.green:C.red);
 Plotly.react('distributionChart',[{x:labels,y:counts,type:'bar',marker:{color:barColors},hovertemplate:'涨跌幅区间：%{x}<br>股票数量：%{y}<extra></extra>'}],
  {...BASE,margin:{l:55,r:20,t:20,b:80},xaxis:{title:'个股日涨跌幅区间',tickangle:-50,gridcolor:THEMES[currentTheme].grid},yaxis:{title:'股票数量',gridcolor:THEMES[currentTheme].grid},showlegend:false},CFG);
 const stats=[['股票样本数',fmtNum(day.total)],['样本覆盖率',fmtPct(day.coverage_ratio)],['均值',fmtPct(day.avg_return)],['中位数 P50',fmtPct(day.median_return)],['下分位 P5',fmtPct(day.q5_return)],['下分位 P25',fmtPct(day.q25_return)],['上分位 P75',fmtPct(day.q75_return)],['上分位 P95',fmtPct(day.q95_return)],['上涨 / 下跌',fmtNum(day.up)+' / '+fmtNum(day.down)]];
 $('distributionStats').innerHTML=stats.map((x,i)=>`<div class="kpi"><label>${x[0]}</label><strong>${x[1]}</strong></div>`).join('');
}
function monthMatrix(year,month){const first=new Date(year,month-1,1),start=first.getDay(),days=new Date(year,month,0).getDate();let weeks=[],w=new Array(start).fill(0);for(let d=1;d<=days;d++){w.push(d);if(w.length===7){weeks.push(w);w=[];}}if(w.length){while(w.length<7)w.push(0);weeks.push(w);}return weeks;}
function metricColor(v){if(v==null||!Number.isFinite(Number(v)))return 'transparent';const a=Math.min(.82,.16+Math.min(Math.abs(Number(v))*7,1)*.66).toFixed(2);return Number(v)>=0?`rgba(240,108,117,${a})`:`rgba(56,189,145,${a})`;}
function fmtCell(v){return v==null||!Number.isFinite(Number(v))?'':`${Number(v)>=0?'+':''}${(Number(v)*100).toFixed(2)}%`;}
function renderCalendar(){
 const year=Number(selectedCalendarYear), rows=usableDaily.filter(d=>Number(d.date.slice(0,4))===year), byDate=Object.fromEntries(rows.map(d=>[d.date,d]));
 let html=''; const headers=['日','一','二','三','四','五','六'];
 for(let month=1;month<=12;month++){let body='';monthMatrix(year,month).forEach(week=>{body+='<tr>'+week.map(day=>{if(!day)return '<td class="empty"></td>';const key=`${year}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}`,d=byDate[key],v=d?d[calendarMetric]:null;return `<td style="background:${metricColor(v)}"><div class="day-num">${day}</div><div class="day-val">${fmtCell(v)}</div></td>`;}).join('')+'</tr>';});
  const monthRows=rows.filter(d=>Number(d.date.slice(5,7))===month), vals=monthRows.map(d=>Number(d[calendarMetric])).filter(Number.isFinite); const cumulative=vals.length?((vals.reduce((a,b)=>a*(1+b),1)-1)):null; const up=vals.filter(v=>v>0).length,down=vals.filter(v=>v<0).length;
  html+=`<div class="month-card"><div class="month-header">${month}月</div><table class="month-table"><thead><tr>${headers.map((h,i)=>`<th class="${i===0||i===6?'weekend':''}">${h}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table><div class="month-stat">${vals.length?`交易日 ${vals.length} 天 · 上涨 ${up} 天 · 下跌 ${down} 天 · 累计涨幅 <strong>${fmtCell(cumulative)}</strong>`:'暂无数据'}</div></div>`;
 }
 $('calendarGrid').innerHTML=html;
 let table='<tr><th>月份</th><th>股票样本</th><th>均值</th><th>中位数</th><th>P5</th><th>P25</th><th>P75</th><th>P95</th></tr>';
 const monthlyRows=(DATA.monthly||[]).filter(d=>Number(d.year)===year);
 for(let m=1;m<=12;m++){const d=monthlyRows.find(x=>Number(x.month_num)===m);table+=`<tr><td>${m}月</td><td>${d?fmtNum(d.stock_count):'--'}</td><td>${d?fmtPct(d.avg_return):'--'}</td><td>${d?fmtPct(d.median_return):'--'}</td><td>${d?fmtPct(d.q5_return):'--'}</td><td>${d?fmtPct(d.q25_return):'--'}</td><td>${d?fmtPct(d.q75_return):'--'}</td><td>${d?fmtPct(d.q95_return):'--'}</td></tr>`;} $('calendarTable').innerHTML=table;
}
function renderAnnual(){
 const rows=DATA.annual||[];
 $('annualTable').innerHTML='<tr><th>年份</th><th>股票样本</th><th>均值</th><th>中位数</th><th>P5</th><th>P25</th><th>P75</th><th>P95</th><th>上涨占比</th></tr>'+ 
 rows.map(d=>`<tr><td>${d.year}</td><td>${fmtNum(d.stock_count)}</td><td>${fmtPct(d.avg_return)}</td><td>${fmtPct(d.median_return)}</td><td>${fmtPct(d.q5_return)}</td><td>${fmtPct(d.q25_return)}</td><td>${fmtPct(d.q75_return)}</td><td>${fmtPct(d.q95_return)}</td><td>${fmtPct(d.up_ratio)}</td></tr>`).join('');
}
function renderRank(){
 const y=$('rankYear').value,r=(DATA.rankings||{})[y]||{top:[],bottom:[]};
 $('rankTitle').textContent=y+' 年度涨幅前后 20';
 const n=Math.max(r.top.length,r.bottom.length);let rows='';
 for(let i=0;i<n;i++){const a=r.top[i]||{},b=r.bottom[i]||{};rows+=`<tr><td>${i+1}</td><td>${a.name||'--'}</td><td>${a.code||'--'}</td><td class="up">${fmtPct(a.return)}</td><td>${b.name||'--'}</td><td>${b.code||'--'}</td><td class="down">${fmtPct(b.return)}</td></tr>`;}
 $('rankTable').innerHTML='<tr><th>排名</th><th>上涨股票</th><th>代码</th><th>涨幅</th><th>下跌股票</th><th>代码</th><th>跌幅</th></tr>'+rows;
}
function render(){
 const rows=inRange();
 buildDateSelect(rows);
 const latestDay=getLatest(rows);
 const distributionDay=mode==='latest'?latestDay:(rows.find(d=>d.date===selectedDistributionDate)||latestDay);
 renderKpis(rows,latestDay);
 $('dailyPanel').classList.toggle('hidden',mode==='latest');
 $('calendarPanel').classList.toggle('hidden',mode==='latest');
 $('annualPanel').classList.toggle('hidden',mode==='latest');
 $('rankPanel').classList.toggle('hidden',mode==='latest');
 renderDistribution(distributionDay);
 if(mode==='range'){
  renderDailyChart(rows);renderCalendar();
  renderAnnual();
 }
}
$('applyDates').onclick=()=>{
 let a=$('startDate').value,b=$('endDate').value;
 if(!a||!b||a>b){alert('请检查开始日期和结束日期');return;}
 document.querySelectorAll('[data-quick]').forEach(x=>x.classList.remove('active'));
 selectedDistributionDate=b;render();
};
document.querySelectorAll('[data-quick]').forEach(b=>b.onclick=()=>setQuick(b.dataset.quick));
document.querySelectorAll('.theme-btn').forEach(b=>b.onclick=()=>applyTheme(b.dataset.theme));
$('rangeMode').onclick=()=>{mode='range';$('rangeMode').classList.add('active');$('latestMode').classList.remove('active');render();};
$('latestMode').onclick=()=>{mode='latest';$('latestMode').classList.add('active');$('rangeMode').classList.remove('active');render();};
$('breadthCount').onclick=()=>{breadthMode='count';$('breadthCount').classList.add('active');$('breadthRatio').classList.remove('active');renderDailyChart(inRange());};
$('breadthRatio').onclick=()=>{breadthMode='ratio';$('breadthRatio').classList.add('active');$('breadthCount').classList.remove('active');renderDailyChart(inRange());};
$('distributionDate').onchange=()=>{selectedDistributionDate=$('distributionDate').value;renderDistribution(selectedDay());};
document.querySelectorAll('.calendarMetric').forEach(b=>b.onclick=()=>{calendarMetric=b.dataset.metric;document.querySelectorAll('.calendarMetric').forEach(x=>x.classList.toggle('active',x===b));renderCalendar();});
 $('rankYear').innerHTML=(DATA.annual||[]).map(d=>`<option value="${d.year}">${d.year}</option>`).join('');
 if(DATA.annual&&DATA.annual.length) $('rankYear').value=String(DATA.annual[DATA.annual.length-1].year);
$('rankYear').onchange=renderRank;
buildCalendarYearSelect();
const [defaultStart,defaultEnd]=yearBounds(currentYear);
setDateInputs(defaultStart,defaultEnd);
document.querySelector('[data-quick="ytd"]').classList.add('active');
selectedDistributionDate=maxDate;
const incompleteDays=DATA.meta.incomplete_days||[];
const excludedDetail=incompleteDays.length
 ? incompleteDays.map(d=>`${d.date}（${fmtNum(d.total)}/${fmtNum(d.expected)}，覆盖率 ${fmtPct(d.coverage_ratio)}）`).join('、')
 : '近期没有样本不足日期';
const qualityNote=(DATA.meta.excluded_daily_count||0)>0
 ? '；已排除样本不足日期 '+fmtNum(DATA.meta.excluded_daily_count)+' 天；最近被排除日期：'+excludedDetail
 : '；全部交易日样本覆盖率检查通过';
$('foot').textContent='统计说明：日历中的均值、中位数、P25、P75均按当日有效股票收益率计算；停牌、未上市或缺少有效价格的股票不计入当日样本。全历史完整性门槛：'+fmtPct(DATA.meta.min_daily_coverage_ratio||0.80)+qualityNote+'。缓存目录：'+(DATA.meta.cache_dir||'--')+'；生成时间：'+(DATA.meta.generated_at||'--');
render();
</script></body></html>"""
    return template.replace("__TITLE__", html.escape(title)).replace("__DATA__", data)



def build_market_payload(daily: pd.DataFrame, annual: pd.DataFrame, weekly: pd.DataFrame,
                         monthly: pd.DataFrame, rankings: Dict[str, Any],
                         stock_count: int, cfg: Config, cache_dir: Path) -> Dict[str, Any]:
    def records(df: pd.DataFrame) -> List[Dict[str, Any]]:
        if df is None or df.empty:
            return []
        out = df.copy()
        for col in out.columns:
            if pd.api.types.is_float_dtype(out[col]):
                out[col] = out[col].round(8)
        return out.where(pd.notna(out), None).to_dict("records")

    a = annual.copy()
    if not a.empty:
        a["year"] = a["period"].dt.year.astype(int)
        a = a.drop(columns=["period"])
    w = weekly.copy()
    if not w.empty:
        w["period"] = pd.to_datetime(w["period"]).dt.strftime("%Y-%m-%d")
    m = monthly.copy()
    if not m.empty:
        m["year"] = m["period"].dt.year.astype(int)
        m["month_num"] = m["period"].dt.month.astype(int)
        m["month"] = m["period"].dt.strftime("%Y-%m")
        m["period"] = m["month"]
    incomplete_days = []
    if daily is not None and not daily.empty and "is_complete" in daily.columns:
        incomplete_all = daily.loc[
            ~daily["is_complete"].astype(bool),
            ["date", "total", "coverage_ratio"],
        ]
        incomplete_days = [
            {
                "date": str(row.date),
                "total": int(row.total),
                "expected": int(stock_count),
                "coverage_ratio": (
                    float(row.coverage_ratio)
                    if pd.notna(row.coverage_ratio) else None
                ),
            }
            for row in incomplete_all.tail(RECENT_DAILY_COMPLETENESS_DAYS).itertuples(index=False)
        ]
        excluded_daily_count = int(len(incomplete_all))
    else:
        excluded_daily_count = 0

    return {
        "meta": {
            "stock_count": int(stock_count),
            "min_daily_coverage_ratio": MIN_DAILY_COVERAGE_RATIO,
            "excluded_daily_count": excluded_daily_count,
            "incomplete_days": incomplete_days,
            "latest_target_date": cfg.end_date.strftime("%Y-%m-%d"),
            "start": cfg.start_date.strftime("%Y-%m-%d"),
            "end": cfg.end_date.strftime("%Y-%m-%d"),
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "cache_dir": str(cache_dir),
        },
        "histogram_labels": _HIST_LABELS,
        "daily": records(daily),
        "annual": records(a),
        "weekly": records(w),
        "monthly": records(m),
        "rankings": rankings,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def analyze(cfg: Config, core: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Path]]:
    # 手动触发工作流时也只分析已收盘交易日，避免把盘中日线写入长期缓存。
    requested_end = cfg.end_date
    cfg.end_date = _resolve_completed_end_date(cfg.end_date)
    if cfg.end_date != requested_end:
        print(
            f">>> 结束日期由 {requested_end:%Y-%m-%d} 调整为最近已完成交易日 "
            f"{cfg.end_date:%Y-%m-%d}"
        )

    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    universe = load_universe(cfg)
    if cfg.max_stocks and cfg.max_stocks > 0:
        universe = universe.head(cfg.max_stocks).copy()
    print(f">>> 准备分析 {len(universe)} 只 A 股，价格缓存：{cfg.cache_dir / 'prices'}")

    daily_acc = _new_daily_accumulator()
    annual_records: List[Dict[str, Any]] = []
    weekly_records: List[Dict[str, Any]] = []
    monthly_records: List[Dict[str, Any]] = []
    score_rows: List[Dict[str, Any]] = []
    done = 0
    failed = 0
    stale_cache_fallback = 0
    target_date_price_count = 0
    target_date_missing_count = 0
    tasks = [(str(r.symbol), str(r.name)) for r in universe.itertuples(index=False)]

    def process(code: str, name: str, frame: pd.DataFrame, err: Optional[str]) -> None:
        nonlocal done, failed, stale_cache_fallback
        nonlocal target_date_price_count, target_date_missing_count
        done += 1
        if frame is None or frame.empty:
            failed += 1
            if failed <= 10:
                print(f"⚠️ {name}({code}) 跳过：{err or '无数据'}")
            return
        if err:
            stale_cache_fallback += 1
            if stale_cache_fallback <= 10:
                print(f"⚠️ {name}({code})：{err}")
        frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
        frame_dates = frame["Date"].dropna().dt.normalize()
        if bool((frame_dates == cfg.end_date.normalize()).any()):
            target_date_price_count += 1
        else:
            target_date_missing_count += 1
        close = pd.Series(pd.to_numeric(frame["Close"], errors="coerce").values,
                          index=frame["Date"]).dropna()
        close = close[~close.index.duplicated(keep="last")].sort_index()
        if close.empty:
            failed += 1
            return
        _add_daily_returns(daily_acc, close, cfg.start_date, cfg.end_date)
        ar = _period_returns(close, "Y", cfg.start_date, cfg.end_date)
        wr = _period_returns(close, "W-FRI", cfg.start_date, cfg.end_date)
        mr = _period_returns(close, "M", cfg.start_date, cfg.end_date)
        for row in ar.itertuples(index=False):
            annual_records.append({"period": row.period, "code": _plain_code(code), "name": name,
                                   "return": float(row[1])})
        for row in wr.itertuples(index=False):
            weekly_records.append({"period": row.period, "code": _plain_code(code), "name": name,
                                  "return": float(row[1])})
        for row in mr.itertuples(index=False):
            monthly_records.append({"period": row.period, "code": _plain_code(code), "name": name,
                                    "return": float(row[1])})
        if core is not None:
            sf = _score_frame(close, core, cfg.score_history_days)
            if sf is not None:
                score_rows.append({
                    "code": _plain_code(code), "key": _safe_code(code), "kind": "板块",
                    "cats": ["全市场"], "boards": ["全部A股"],
                    "bpairs": [("全部A股", "全市场")], "ind": sf,
                })

    # 网络抓取使用分批 Future，避免一次性创建 5000+ 个任务并长期保留结果。
    # GitHub Actions 内存较小，且 AKShare 的底层网络/解析库不适合高并发长时间运行。
    workers = max(1, min(int(cfg.workers), 8))
    batch_size = max(workers * 4, 16)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch_start in range(0, len(tasks), batch_size):
            batch = tasks[batch_start:batch_start + batch_size]
            futures = [pool.submit(load_or_fetch_price, code, name, cfg)
                       for code, name in batch]

            for fut in as_completed(futures):
                try:
                    code, name, frame, err = fut.result()
                except Exception as exc:
                    # 单个线程异常不应中断全市场汇总。
                    print(f"⚠️ 某个行情任务异常，已跳过：{exc}")
                    done += 1
                    failed += 1
                    continue

                process(code, name, frame, err)
                # 尽快释放当前行情 DataFrame，降低长期运行时的内存峰值。
                del frame

                if done % 100 == 0 or done == len(tasks):
                    print(f"    —— 进度 {done}/{len(tasks)}，失败 {failed}，缓存/取数完成")

            # 当前批次的 Future 不再需要，主动释放引用并回收内存。
            del futures
            gc.collect()

    daily = _daily_frame(daily_acc, expected_stock_count=len(universe))
    if daily.empty:
        raise RuntimeError("没有得到任何有效日线数据；请检查 akshare、网络连接或价格缓存。")

    # 明确检查本次目标交易日，防止网页默默回退到几天前的旧日期。
    target_date_text = cfg.end_date.strftime("%Y-%m-%d")
    target_rows = daily.loc[daily["date"] == target_date_text]
    min_target_samples = max(1, int(math.ceil(len(universe) * MIN_DAILY_COVERAGE_RATIO)))
    target_count = int(target_rows.iloc[-1]["total"]) if not target_rows.empty else 0
    target_coverage = target_count / len(universe) if len(universe) else 0.0

    print("\n========== 最新交易日数据检查 ==========")
    print(f"目标交易日：{target_date_text}")
    print(f"股票池数量：{len(universe)}")
    print(f"目标日有效收益样本：{target_count}")
    print(f"目标日行情缓存含目标日期：{target_date_price_count}")
    print(f"目标日行情缓存不含目标日期：{target_date_missing_count}")
    print(f"最低有效样本要求：{min_target_samples}")
    print(f"目标日收益样本覆盖率：{target_coverage:.2%}")
    print(f"刷新失败后沿用旧缓存：{stale_cache_fallback} 只")
    print("========================================\n")

    if target_rows.empty or target_count < min_target_samples:
        raise RuntimeError(
            f"最新完整交易日 {target_date_text} 的全市场样本不足："
            f"{target_count}/{len(universe)}（{target_coverage:.2%}），"
            f"要求至少 {min_target_samples} 只（{MIN_DAILY_COVERAGE_RATIO:.0%}）。"
            "为避免发布过期页面，本次停止生成；请检查行情接口和缓存刷新日志。"
        )

    complete_days = int(daily["is_complete"].astype(bool).sum())
    if complete_days == 0:
        raise RuntimeError(
            "没有任何交易日通过全市场样本覆盖率检查，拒绝生成/发布不完整看板；"
            "请检查行情接口、股票池和缓存。"
        )
    annual = _aggregate_period_records(annual_records, "period")
    weekly = _aggregate_period_records(weekly_records, "period")
    monthly = _aggregate_period_records(monthly_records, "period")
    rankings = _build_rankings(annual_records, 20)
    payload = build_market_payload(daily, annual, weekly, monthly, rankings,
                                   len(universe), cfg, cfg.cache_dir)

    files = {
        "universe": cfg.output_dir / "a_share_universe.csv",
        "daily": cfg.output_dir / "a_share_daily_summary.csv",
        "annual": cfg.output_dir / "a_share_annual_returns.csv",
        "weekly": cfg.output_dir / "a_share_weekly_returns.csv",
        "monthly": cfg.output_dir / "a_share_monthly_returns.csv",
        "dashboard": cfg.output_dir / "a_share_market_dashboard.html",
    }
    _atomic_to_csv(universe, files["universe"], encoding="utf-8-sig")
    _atomic_to_csv(daily, files["daily"], encoding="utf-8-sig")
    _atomic_to_csv(annual, files["annual"], encoding="utf-8-sig")
    _atomic_to_csv(weekly, files["weekly"], encoding="utf-8-sig")
    _atomic_to_csv(monthly, files["monthly"], encoding="utf-8-sig")
    with open(files["dashboard"], "w", encoding="utf-8") as f:
        f.write(render_market_dashboard(payload))
    if stale_cache_fallback:
        print(
            f"⚠️ 本次有 {stale_cache_fallback} 只股票抓取失败，"
            "已使用旧缓存继续统计；请留意近期日期的样本覆盖率。"
        )
    recent_window = daily.tail(RECENT_DAILY_COMPLETENESS_DAYS)
    recent_incomplete_count = int((~recent_window["is_complete"].astype(bool)).sum())
    if recent_incomplete_count:
        print(
            f"⚠️ 最近 {len(recent_window)} 个有数据的交易日中，"
            f"{recent_incomplete_count} 个日期未达到 {MIN_DAILY_COVERAGE_RATIO:.0%} 覆盖率，"
            "网页默认排除这些日期。"
        )
    print(f">>> 已生成收益汇总看板：{files['dashboard']}")
    return payload, score_rows, files


def parse_args() -> Config:
    today = pd.Timestamp.now(tz="Asia/Shanghai").tz_localize(None).normalize()
    default_start = today - pd.DateOffset(years=3)
    parser = argparse.ArgumentParser(description="近三年 A 股全市场收益与评分汇总看板")
    parser.add_argument("--start-date", default=default_start.strftime("%Y-%m-%d"))
    parser.add_argument("--end-date", default=today.strftime("%Y-%m-%d"))
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--retry", type=int, default=DEFAULT_RETRY)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--score-history-days", type=int, default=DEFAULT_SCORE_HISTORY_DAYS)
    parser.add_argument("--core-file", default=None, help="评分内核路径；默认自动寻找 18_zigzag_signal_analyzer.py")
    parser.add_argument("--no-score", action="store_true", help="跳过评分历史与 stock_board_score_dashboard")
    args = parser.parse_args()
    start = pd.Timestamp(args.start_date).normalize()
    end = pd.Timestamp(args.end_date).normalize()
    if end < start:
        parser.error("--end-date 不能早于 --start-date")
    # core-file 由 main 读取；挂到 Config 上仅为保持参数解析集中。
    cfg = Config(start, end, Path(args.cache_dir), Path(args.output_dir),
                 max(1, args.workers), max(0, args.retry), args.force_refresh,
                 max(0, args.max_stocks), args.no_score, max(0, args.score_history_days))
    cfg.core_file = args.core_file  # type: ignore[attr-defined]
    return cfg


def main() -> None:
    cfg = parse_args()
    # 本脚本只输出全市场收益分析页面，不再生成评分看板。
    payload, score_rows, files = analyze(cfg, None)
    print("\n🎉 A 股全市场汇总完成")
    print(f"   收益看板：{files['dashboard'].resolve()}")
    print(f"   汇总 CSV：{files['annual'].resolve()} / {files['weekly'].resolve()} / {files['monthly'].resolve()}")


if __name__ == "__main__":
    main()
