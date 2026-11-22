"""Build the sector dashboard from the market workflow's existing outputs."""
import argparse
import json
import os
import re
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from a_share_market_dashboard import exchange_code

HERE = Path(__file__).resolve().parent


def calendar(path):
    source = path.read_text(encoding="utf-8")
    marker = re.search(r"\bconst\s+DATA\s*=\s*", source)
    if not marker:
        raise ValueError(f"{path}: 无法识别宽基日历数据格式")
    payload, _ = json.JSONDecoder().raw_decode(source[marker.end():])
    names, series = {}, {}
    for item in payload["entities"]:
        if item["category"] == "A股宽基":
            code = item["id"]
            names[code] = item["name"]
            values = {date: value for year in item["years"].values() for date, value in year.items()}
            series[code] = pd.Series(values, dtype=float)
    if not names:
        raise ValueError("宽基收益日历没有 A 股宽基")
    return names, pd.DataFrame(series).sort_index()


def read_close(path):
    frame = pd.read_csv(path, usecols=["Date", "Close"])
    close = pd.to_numeric(frame["Close"], errors="coerce")
    close.index = pd.to_datetime(frame["Date"], errors="raise").dt.strftime("%Y-%m-%d")
    return close[~close.index.duplicated(keep="last")].where(close.gt(0) & np.isfinite(close)).sort_index()


def summarize(frame):
    """Return one row per statistic, plus the count of usable stocks per date."""
    metrics = pd.concat([frame.mean().to_frame().T, frame.quantile([.25, .50, .75])])
    return json.loads(metrics.to_json(orient="values", double_precision=6)), frame.count().tolist()


def build(args):
    members = pd.read_csv(args.boards, dtype=str, encoding="utf-8-sig")
    cols = ["板块类别", "板块名称", "股票代码"]
    members = members[cols].apply(lambda col: col.str.strip()).drop_duplicates()
    if members.isna().any().any() or not members["股票代码"].str.fullmatch(r"[0-9]{6}").all():
        raise ValueError("板块映射含缺失值或非六位股票代码")
    names, benchmarks = calendar(args.calendar)
    summary = pd.read_csv(args.summary, usecols=["date", "is_complete"]).sort_values("date")
    good = summary.loc[summary.is_complete.astype(str).str.lower().eq("true"), "date"]
    if good.empty:
        raise ValueError("全市场尚无覆盖率达标的交易日")
    sessions = pd.Index(sorted(set(summary.date[summary.date <= good.max()])))
    # Use all market dates for benchmark alignment so a lagging index never hides stock data.
    start_year = int(sessions[-1][:4]) - 1
    dates = sessions[sessions >= f"{start_year}-01-01"]
    daily_dates = dates[-40:]
    ends = pd.Series(dates, index=dates).groupby(dates.str[:7]).agg(["first", "last"])
    months = [[m, row['first'], row['last']] for m,row in ends.iterrows()]
    previous = pd.Series(sessions, index=sessions).shift()
    codes = sorted(set(members["股票代码"]))
    stock_prices = {}
    for code in codes:
        path = args.cache / "prices" / (exchange_code(code) + ".csv.gz")
        if path.is_file():
            stock_prices[code] = read_close(path)
    if not stock_prices:
        raise ValueError(f"{args.cache}/prices 中没有匹配的股票行情")
    prices = pd.DataFrame(stock_prices).reindex(index=sessions, columns=codes)
    daily = prices.pct_change(fill_method=None).loc[daily_dates].T * 100
    monthly = {}
    for month, first, last in months:
        prior = previous[first]
        monthly[month] = (prices.loc[last] / prices.loc[prior] - 1) * 100 if pd.notna(prior) else pd.Series(np.nan,index=codes)
    monthly = pd.DataFrame(monthly)
    if not daily.iloc[:, -1].notna().any():
        raise ValueError("最新完整交易日无有效板块成分股收益")
    boards, monthly_stats = [], {}
    for (kind, name), rows in members.groupby(cols[:2], sort=False):
        stocks = rows["股票代码"]
        day_stats, day_counts = summarize(daily.loc[stocks])
        month_stats, month_counts = summarize(monthly.loc[stocks])
        boards.append([kind, name, len(stocks), {"1":day_stats}, day_counts, month_counts])
        monthly_stats[kind + name] = month_stats
    aligned = benchmarks.reindex(sessions)
    bench, monthbench = [], {}
    for code,name in names.items():
        bench.append([name,code,json.loads(aligned.loc[daily_dates,code].to_json(orient="values"))])
        monthbench[code] = []
        for _,first,last in months:
            values = aligned.loc[first:last,code]
            monthbench[code].append(round(100*((1+values/100).prod()-1),6) if values.notna().all() else None)
    template = HERE.joinpath("board_rotation.template.html").read_text(encoding="utf-8")
    for marker, payload in [("__DAILY_DATA__", [daily_dates.tolist(),bench,boards]),
                            ("__MONTH_DATA__", [months,monthly_stats,monthbench])]:
        template = template.replace(marker, json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(",",":")).replace("<","\\u003c"))
    template = template.replace("__ASOF__",dates[-1])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile("w",dir=args.output.parent,encoding="utf-8",delete=False) as tmp:
        tmp.write(template)
    os.replace(tmp.name,args.output)
    print(f"{args.output}: {len(boards)} 个板块，{len(stock_prices)}/{len(codes)} 只匹配缓存，截至 {dates[-1]}，宽基日历截至 {benchmarks.index[-1]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key,default in {"boards":HERE/"ztjj_board_stocks.csv","cache":HERE/".cache/a_share",
                        "calendar":HERE.parent/"docs/index_return_calendar.html",
                        "summary":HERE/"a_share_dashboard/a_share_daily_summary.csv",
                        "output":HERE/"a_share_dashboard/board_rotation.html"}.items():
        parser.add_argument("--"+key,type=Path,default=default)
    build(parser.parse_args())
