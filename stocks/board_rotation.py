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
    template = DASHBOARD_HTML
    for marker, payload in [("__DAILY_DATA__", [daily_dates.tolist(),bench,boards]),
                            ("__MONTH_DATA__", [months,monthly_stats,monthbench])]:
        template = template.replace(marker, json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(",",":")).replace("<","\\u003c"))
    template = template.replace("__ASOF__",dates[-1])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile("w",dir=args.output.parent,encoding="utf-8",delete=False) as tmp:
        tmp.write(template)
    os.replace(tmp.name,args.output)
    print(f"{args.output}: {len(boards)} 个板块，{len(stock_prices)}/{len(codes)} 只匹配缓存，截至 {dates[-1]}，宽基日历截至 {benchmarks.index[-1]}")


DASHBOARD_HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>板块轮动与宽基比较</title><style>
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

.matrix{height:auto;max-height:none;overflow-x:auto;overflow-y:hidden;isolation:isolate}.matrix table{border-collapse:separate;border-spacing:0;font-variant-numeric:tabular-nums}.matrix th{position:sticky;top:0;background:var(--panel);z-index:2}.matrix td:first-child,.matrix th:first-child{position:sticky;left:0;background:var(--panel);z-index:1;min-width:108px}.matrix th:first-child{z-index:3}.matrix td:not(:first-child){text-align:center;min-width:83px}.matrix th{text-align:center}.matrix th button{padding:4px 7px}.matrix td button{background:transparent;border:0;padding:1px 3px;min-width:64px;font-variant-numeric:tabular-nums}.matrix .chosen{box-shadow:inset 0 0 0 2px var(--blue)}.rowname small{color:var(--muted);margin-left:6px}.toolbar .field.wide{flex-basis:100%}.hint{margin:4px 0}.kpi strong{font-variant-numeric:tabular-nums}button:focus-visible{outline:2px solid var(--blue)} .legend{display:inline-flex;gap:10px}.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:3px}.toolbar input{max-width:160px}
.matrix{overflow:visible}.matrix table{width:100%;table-layout:fixed;white-space:normal}.matrix td,.matrix th{min-width:0!important;padding:5px 2px;overflow-wrap:anywhere}.matrix td:first-child,.matrix th:first-child{width:110px;position:static}.matrix td button,.matrix th button{min-width:0;max-width:100%;padding:3px 1px;white-space:normal}.matrix th .hint{font-size:10px}.legend{flex-wrap:wrap}#pager{margin:6px 0}.matrix td button{font-variant-numeric:tabular-nums}button:disabled{opacity:.4;cursor:default}</style></head><body><main><div class="top"><div><h1>板块轮动与宽基比较</h1><div class="muted">A 股市场 · 板块区间收益 · 相对宽基强弱</div></div><span class="tag">行情截至 __ASOF__</span></div>
<section class="toolbar" id="tools" aria-label="分析设置"></section><div class="kpis" id="cards"></div>
<section class="panel" aria-live="polite"><h2>单格明细</h2><div id="detail"></div></section><section class="panel"><div class="panelhead"><div><h2 id="title"></h2><div class="hint" id="hint"></div></div><div class="legend muted"><span><i style="background:#ef4444"></i><b id="positive">跑赢</b></span><span><i style="background:#16a34a"></i><b id="negative">跑输</b></span><span id="unit"></span></div></div><div class="quick" id="pager"></div><div class="tablewrap matrix" id="matrix"></div></section>
<details class="panel"><summary>数据与计算口径</summary><p>行业、概念及成分股来自上传的板块清单。成分股使用全市场流程的前复权收盘价；单日要求相邻交易日均有价格，月度要求上月末及本月末（本月至最新完整交易日）均有价格。缺失样本不计入统计，单格明细显示有效家数；宽基日涨跌幅取自项目已发布的收益日历，单位为 %。比较前分别将个股和指数日收益逐日复利。板块默认取个股区间收益的等权均值，也可切换横截面分位数。超额 = 所选板块统计值 − 同期宽基收益，单位为百分点。自身表现按板块收益正负着色；相对表现按超额正负着色。</p><p>单日热力图每列为一个交易日。显示最近 40 个交易日，宽基日历未覆盖的比较显示为缺数。月度排名覆盖本年和上年，先计算个股月收益，再取板块统计值；历史沿用当前成分映射。最近月份截至最后一个已有交易日。月表列头汇总全部同类板块的正负数、均值及极差。持平按两位小数判定。</p></details></main>
<script>
const [dates,bench,boards]=__DAILY_DATA__,themes={
 light:{bg:'#f6f7f9',panel:'#ffffff',line:'#e5e7eb',text:'#1f2937',muted:'#6b7280',plot:'#ffffff',grid:'#e5e7eb',head:'#f3f4f6',cell:'#f3f4f6'},
 mist:{bg:'#eef2f5',panel:'#f8fafc',line:'#d7dee7',text:'#243447',muted:'#64748b',plot:'#f8fafc',grid:'#d7dee7',head:'#e8eef4',cell:'#dce5ed'},
 blue:{bg:'#eaf3fb',panel:'#f7fbff',line:'#c8dced',text:'#1e3a5f',muted:'#5b7693',plot:'#f7fbff',grid:'#c8dced',head:'#e5f0fa',cell:'#d7e7f4'},
 dark:{bg:'#0d121a',panel:'#151e2b',line:'#273447',text:'#e8eef7',muted:'#91a0b4',plot:'#151e2b',grid:'#273447',head:'#202c3c',cell:'#273447'}
};
const $=id=>document.getElementById(id),metrics=['等权平均','P25','中位数','P75'],state={cat:'行业',base:Math.max(0,bench.findIndex(x=>x[1]==='sh000300')),w:1,m:0,filter:'全部',theme:'light',search:'',end:dates.length-1},fields=[
 ['cat','板块类别',['行业','概念']],['m','板块收益口径',metrics.map((v,i)=>[i,v])],['theme','背景风格',[['light','浅色'],['mist','雾灰'],['blue','浅蓝'],['dark','深色']]],['base','比较宽基',bench.map((b,i)=>[i,b[0]])],['filter','排序日表现',['全部','跑赢','跑输','持平']]];
const fixed=x=>{let n=Math.round((Math.abs(x)+1e-10)*100)/100;return n?(x<0?-n:n):0},fmt=x=>!Number.isFinite(x)?'—':(fixed(x)>0?'+':'')+fixed(x).toFixed(2);
const [months,monthstats,monthbench]=__MONTH_DATA__;
Object.assign(state,{view:'单日热力图',mode:'相对宽基',year:months.at(-1)[0].slice(0,4)});
const monthly=()=>state.view==='月度排名',relative=()=>state.mode==='相对宽基';
const esc=x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const years=[...new Set(months.map(x=>x[0].slice(0,4)))].reverse();
function controls(){
 const extra=[['view','看板',['单日热力图','月度排名']],['mode','表现口径',['相对宽基','板块自身']]];
 const active=extra.concat(fields.filter(([k])=>!(k==='w'&&monthly()||k==='base'&&!relative())).map(f=>f[0]==='filter'?['filter','表现筛选',relative()?['全部','跑赢','跑输','持平']:['全部','上涨','下跌','持平']]:f));
 if(monthly())active.splice(2,0,['year','年份',years]);
 $('tools').innerHTML=active.map(([key,label,values])=>'<div class="field '+(key==='base'?'wide':'')+'"><span>'+label+'</span><div class="quick">'+values.map(v=>{const [k,n]=Array.isArray(v)?v:[v,v];return '<button data-key="'+key+'" data-value="'+k+'" aria-pressed="'+(k===state[key])+'" class="'+(k===state[key]?'active':'')+'">'+n+'</button>'}).join('')+'</div></div>').join('')+'<label class="field">板块搜索<input id="search" placeholder="输入名称"></label>';
 $('search').value=state.search;$('search').oninput=e=>{state.search=e.target.value;render()};
}
let selected=null,page=0;
function render(){
 const {m,w,base}=state,idx=bench[base],mon=monthly(),rel=relative(),pos=rel?'跑赢':'上涨',neg=rel?'跑输':'下跌',unit=rel?' 个百分点':'%';
 const available=mon?months.map(([label,start,finish],i)=>({i,label:Number(label.slice(5))+'月',start,finish})).filter(p=>p.start.startsWith(state.year)):dates.map((label,i)=>({i,label,start:dates[i-w+1],finish:label})).slice(w-1);
 if(!mon)available.reverse();
 const capacity=Math.max(1,Math.floor(($('matrix').clientWidth-112)/(mon?70:75)));
 const pages=Math.ceil(available.length/capacity);
 page=Math.min(page,pages-1);
 const periods=available.slice(page*capacity,(page+1)*capacity);
 $('pager').innerHTML=pages<=1?'':'<button data-turn="-1" '+(page===0?'disabled':'')+'>上一组</button><span class="hint">'+(page+1)+' / '+pages+'</span><button data-turn="1" '+(page===pages-1?'disabled':'')+'>下一组</button>';
 $('pager').onclick=e=>{const b=e.target.closest('button');if(b&&!b.disabled){page+=Number(b.dataset.turn);state.end=-1;selected=null;render()}};
 const ordered=periods.find(p=>p.i===state.end)||(mon?periods.at(-1):periods[0]);state.end=ordered.i;
 const benchAt=i=>mon?monthbench[idx[1]][i]:idx[2][i];
 const refs=Object.fromEntries(periods.map(p=>[p.i,benchAt(p.i)]));
 const value=(g,i)=>mon?monthstats[g[0]+g[1]][m][i]:g[3][w][m][i-w+1];
 const score=(g,i)=>(value(g,i)==null||(rel&&refs[i]==null)?null:fixed(value(g,i)-(rel?refs[i]:0))),all=boards.filter(g=>g[0]===state.cat).sort((a,b)=>(score(b,ordered.i)??-Infinity)-(score(a,ordered.i)??-Infinity)||a[1].localeCompare(b[1],'zh'));
 const sign=x=>x==null?'缺数':x>0?pos:x<0?neg:'持平';
 const rows=all.filter(g=>g[1].includes(state.search)&&(state.filter==='全部'||sign(score(g,ordered.i))===state.filter));
 const stats=i=>{const vs=all.map(g=>score(g,i)).filter(Number.isFinite);return {count:vs.length,up:vs.filter(v=>v>0).length,down:vs.filter(v=>v<0).length,mean:vs.reduce((a,b)=>a+b,0)/vs.length,range:vs.length?Math.max(...vs)-Math.min(...vs):null}};
 const s=stats(ordered.i);
 $('cards').innerHTML=[[mon?'排序月份':'排序日期',ordered.label],...(rel?[['比较宽基',idx[0]],['同期基准收益',fmt(refs[ordered.i])+'%']]:[['板块口径',metrics[m]],['同类板块均值',fmt(s.mean)+'%']]),[pos+'板块',s.up],[neg+'板块',s.down],['持平板块',s.count-s.up-s.down]].map(([k,v])=>'<div class="kpi"><label>'+k+'</label><strong>'+v+'</strong></div>').join('');
 $('title').textContent=state.cat+'板块'+(rel?'相对'+idx[0]:'自身')+(mon?'的月度排名':'的单日表现');
 $('positive').textContent=pos;$('negative').textContent=neg;$('unit').textContent=(rel?'pp = 百分点':'单位：%');
 $('hint').textContent=(mon?'月份从 1 月向右排列。':'交易日从新到旧排列。')+'点击列头排序。当前显示 '+rows.length+' / '+all.length+' 个板块，排序列缺数 '+(all.length-s.count)+' 个。'+(mon?'列头统计覆盖本类别全部板块；极差为最高值减最低值。':'');
 function detail(g,p){selected=[g,p.i];$('detail').textContent=g[1]+'（'+(mon?g[5]:g[4])[p.i]+'/'+g[2]+' 只有效） · '+p.start+' 至 '+p.finish+'　｜　板块'+metrics[m]+' '+fmt(value(g,p.i))+'%'+(rel?'　｜　'+idx[0]+' '+fmt(refs[p.i])+'%　｜　超额 '+fmt(score(g,p.i))+' 个百分点':'')}
 if(rows.length){const g=rows.includes(selected?.[0])?selected[0]:rows[0];detail(g,periods.find(p=>p.i===selected?.[1])||ordered)}else{$('detail').textContent='当前筛选无匹配板块。';selected=null}
 function heading(p){const st=mon?stats(p.i):null;return '<th><button data-end="'+p.i+'" class="'+(ordered.i===p.i?'active':'')+'">'+p.label+'</button>'+(mon?'<div class="hint">↑'+st.up+'　↓'+st.down+'<br>均 '+fmt(st.mean)+(rel?' pp':'%')+'<br>差 '+(st.range==null?'—':st.range.toFixed(2))+' pp</div>':'')+'</th>'}
 $('matrix').innerHTML='<table><thead><tr><th>同类排名 / 板块</th>'+periods.map(heading).join('')+'</tr></thead><tbody>'+rows.map((g,j)=>'<tr><td class="rowname">'+(all.indexOf(g)+1)+'　'+esc(g[1])+'<small>'+g[2]+'只</small></td>'+periods.map(p=>{const x=score(g,p.i);if(x==null)return '<td><button data-row="'+j+'" data-col="'+p.i+'">—</button></td>';return '<td style="background:rgba('+(x>0?'239,68,68':'22,163,74')+','+(x?Math.min(.58,.08+Math.abs(x)*(mon?.055:.09)):0)+')" class="'+(selected?.[0]===g&&selected[1]===p.i?'chosen':'')+'"><button title="'+esc(g[1])+' · '+p.label+' · '+fmt(x)+unit+'" data-row="'+j+'" data-col="'+p.i+'">'+fmt(x)+(rel?' pp':'%')+'</button></td>'}).join('')+'</tr>').join('')+'</tbody></table>'+(rows.length?'':'<p class="hint">没有匹配板块。</p>');
 $('matrix').onclick=e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.end!==undefined){state.end=+b.dataset.end;selected=null;render()}else{detail(rows[+b.dataset.row],periods.find(p=>p.i===+b.dataset.col));$('matrix').querySelector('.chosen')?.classList.remove('chosen');b.parentElement.classList.add('chosen')}};
}
$('tools').onclick=e=>{const b=e.target.closest('button');if(!b)return;const {key,value}=b.dataset;state[key]=['w','m','base'].includes(key)?Number(value):value;if(key==='mode')state.filter='全部';if(key==='view'||key==='year'){state.end=-1;page=0;}selected=null;const t=themes[state.theme];for(const k of ['bg','panel','line','text','muted'])document.documentElement.style.setProperty('--'+k,t[k]);controls();render()};controls();render();
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(render,100)});</script></body></html>'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key,default in {"boards":HERE/"ztjj_board_stocks.csv","cache":HERE/".cache/a_share",
                        "calendar":HERE.parent/"docs/index_return_calendar.html",
                        "summary":HERE/"a_share_dashboard/a_share_daily_summary.csv",
                        "output":HERE/"a_share_dashboard/board_rotation.html"}.items():
        parser.add_argument("--"+key,type=Path,default=default)
    build(parser.parse_args())
