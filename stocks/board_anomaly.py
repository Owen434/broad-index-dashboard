from pathlib import Path
import json
import os
import tempfile
import numpy as np
import pandas as pd
from a_share_market_dashboard import exchange_code
from board_rotation import read_close

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "a_share_dashboard"


def build():
    summary = pd.read_csv(OUT / "a_share_daily_summary.csv")
    completed = summary.loc[summary.is_complete.astype(str).str.lower().eq("true"), "date"]
    if completed.empty:
        raise ValueError("全市场汇总没有完整交易日")
    dates = pd.Index(sorted(set(summary.loc[summary.date <= completed.max(), "date"])))
    if len(dates) < 2:
        raise ValueError("行情不足两个交易日")
    trend_dates = dates[-20:]
    members = pd.read_csv(ROOT / "1.ztjj_board_stocks.csv", dtype=str, encoding="utf-8-sig")
    seed = pd.read_csv(ROOT / "a_share_universe_seed.csv", dtype=str, encoding="utf-8-sig")
    names = dict(zip(members["股票代码"], members["股票名称"]))
    names.update(zip(seed.code, seed.name))
    codes = sorted(names)
    ids = {code:i for i,code in enumerate(codes)}
    market = [ids[c] for c in seed.code.unique()]
    stocks = []
    flags = np.full((len(codes),len(trend_dates)), -1, dtype=np.int8)
    for i, code in enumerate(codes):
        row = [code, names[code], None, None, None, -1, -1]
        path = ROOT / ".cache" / "a_share" / "prices" / (exchange_code(code)+".csv.gz")
        if path.is_file():
            price = read_close(path).reindex(dates)
            ret = price.pct_change(fill_method=None).round(12)
            today = ret.iloc[-1]
            history = ret.iloc[:-1].dropna().tail(120)
            if pd.notna(today):
                row[2] = float(today * 100)
                if len(history) == 120:
                    row[3] = int((history.abs() >= abs(today)).sum())
            average = price.rolling(20,min_periods=20).mean().reindex(trend_dates)
            tail = price.reindex(trend_dates)
            flags[i] = np.where(average.isna(), -1, tail > average)
            if pd.notna(average.iloc[-1]):
                row[4] = float((tail.iloc[-1]/average.iloc[-1]-1)*100)
            row[5:] = [int(flags[i,-1]),int(flags[i,-2])]
        stocks.append(row)
    if not any(stocks[i][2] is not None for i in market):
        raise ValueError("最新完整交易日未匹配到有效个股日收益")
    boards = []
    for (kind,name), group in members.groupby(["板块类别","板块名称"],sort=False):
        indices = [ids[c] for c in group["股票代码"].unique()]
        sample = flags[indices]
        boards.append([kind,name,indices,(sample==1).sum(axis=0).tolist(),
                       (sample>=0).sum(axis=0).tolist()])
    payload = json.dumps([stocks,boards,market,trend_dates.tolist()],ensure_ascii=False,
                         allow_nan=False,separators=(",",":")).replace("<","\\u003c")
    with tempfile.NamedTemporaryFile("w",dir=OUT,encoding="utf-8",delete=False) as temp:
        temp.write(HTML.replace("__DATA__",payload))
    os.replace(temp.name,OUT / "board_anomaly.html")
    print(f"{dates[-1]}: {len(boards)} 个板块，市场异动有效家数 {sum(stocks[i][3] is not None for i in market)}/{len(market)}")

HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>板块异动与均线宽度</title><style>
*{box-sizing:border-box}body{margin:auto;max-width:1540px;background:#f6f7f9;color:#1f2937;padding:20px;font:14px/1.5 system-ui,sans-serif}h1{font-size:24px}h2{font-size:18px}section,details{background:white;border:1px solid #dce2e8;border-radius:12px;padding:14px;margin:12px 0;min-width:0}.grid{display:grid;grid-template-columns:1.2fr 1fr;gap:12px}.group{display:inline-block;margin:4px 12px 4px 0}.group small{display:block;color:#607084}button,input{background:white;border:1px solid #d6dee7;border-radius:6px;padding:5px 8px;margin:2px;color:inherit}button{cursor:pointer}button.on{background:#347ae0;color:white}button:disabled{opacity:.45}table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:5px 2px;border-bottom:1px solid #e8edf1;text-align:right}th:first-child,td:first-child{text-align:left}th{color:#506074}.sel,#market{background:#edf5ff}#market{padding:9px}.bars{display:flex;height:95px;align-items:end;gap:3px;border-bottom:1px solid #8996a2;background:repeating-linear-gradient(to top,#eef1f5 0 1px,transparent 1px 25%)}.bar{flex:1;background:#78a8e9}.axis{display:flex;justify-content:space-between;color:#64748b}.up{color:#ae3737}.down{color:#16805f}@media(max-width:1050px){.grid{display:block}}@media(max-width:600px){body{padding:6px}section{padding:7px}td,th{font-size:10px}button{padding:4px}}
</style><h1>板块异动与均线宽度</h1><div id="asof"></div><section id="controls"></section><div class="grid"><section><h2>板块异动排行</h2><input id="query" aria-label="搜索板块" placeholder="搜索板块名称"><p id="market"></p><div id="rank"></div></section><section id="detail"></section></div><details><summary>计算口径</summary><p>以全市场日汇总中最近完整交易日为准。个股日收益取连续两个市场交易日的前复权收盘价。历史尾部频率为 (k+1)/121，k 为此前 120 个有效日收益中绝对值达到或超过当日的天数。全市场使用备用股票池去重，并按相同方向、阈值和有效性筛选。该频率衡量波动罕见程度。</p><p>MA20 使用连续 20 个交易日的有效收盘价；上穿和下穿要求昨日、今日均有有效均线。无行情、样本不足分别从相应指标分母剔除。较全市场和站上占比日变化均以百分点计。</p></details>
<script>
const [stocks,boards,marketIds,dates]=__DATA__;
const $=id=>document.getElementById(id),esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const pct=x=>Number.isFinite(x)?(100*x).toFixed(1)+'%':'—',num=x=>Number.isFinite(x)?(x>0?'+':'')+x.toFixed(2):'—';
const ratio=(n,d)=>d?n/d:null,delta=(a,b)=>a==null||b==null?null:100*(a-b);
const state={category:'行业',cut:5,dir:'双向',sort:'异动占比',list:'异动股',page:0,stockPage:0,selected:null};
const fields=[['category','类别',['行业','概念']],['cut','尾部频率上限（%）',[1,5,10]],['dir','方向',['双向','上涨','下跌']],['sort','排序',['异动占比','异动家数','站上 MA20 占比','站上占比日变化']]];
function buttons(k,items){return items.map(v=>'<button data-key="'+k+'" data-val="'+esc(v)+'" class="'+(state[k]===v?'on':'')+'">'+esc(v)+'</button>').join('')}
function nav(k,n){const count=Math.max(1,Math.ceil(n/12));return '<button data-page="'+k+'" data-move="-1" '+(state[k]===0?'disabled':'')+'>上一页</button> '+(state[k]+1)+' / '+count+' <button data-page="'+k+'" data-move="1" '+(state[k]+1>=count?'disabled':'')+'>下一页</button>'}
function counts(ids){const up=[],down=[];let valid=0;for(const i of ids){const s=stocks[i];if(s[3]==null)continue;valid++;if(100*(1+s[3])>121*state.cut)continue;if(s[2]>0)up.push(i);if(s[2]<0)down.push(i)}
 const hits=state.dir==='上涨'?up:state.dir==='下跌'?down:up.concat(down);return {valid,up:up.length,down:down.length,hits,rate:ratio(hits.length,valid)}}
function draw(){
 $('asof').textContent='行情截至 '+dates.at(-1);
 $('controls').innerHTML=fields.map(([k,n,v])=>'<div class="group"><small>'+n+'</small>'+buttons(k,v)+'</div>').join('');
 const market=counts(marketIds);
 $('market').textContent='全市场'+state.dir+'异动占比 '+pct(market.rate)+'　'+market.hits.length+' / '+market.valid+' 只有效　上行 '+market.up+'　下行 '+market.down;
 const rows=boards.filter(b=>b[0]===state.category&&b[1].includes($('query').value.trim())).map(b=>{
 const t=b[3].map((v,i)=>ratio(v,b[4][i]));return {b,...counts(b[2]),t,width:t.at(-1),change:delta(t.at(-1),t.at(-2))}});
 const metric=q=>({'异动占比':q.rate,'异动家数':q.hits.length,'站上 MA20 占比':q.width,'站上占比日变化':q.change})[state.sort]??-Infinity;
 rows.sort((a,b)=>metric(b)-metric(a)||b.hits.length-a.hits.length||a.b[1].localeCompare(b.b[1],'zh'));
 const part=rows.slice(12*state.page,12*(state.page+1)),cur=part.find(q=>q.b[1]===state.selected)||part[0];
 $('rank').innerHTML='<table><tr><th>板块</th><th>上行</th><th>下行</th><th>异动 / 有效</th><th>异动占比</th><th>较全市场<br>（百分点）</th><th>站上 MA20</th><th>日变化<br>（百分点）</th></tr>'+part.map(q=>'<tr class="'+(cur===q?'sel':'')+'"><td><button data-board="'+esc(q.b[1])+'">'+esc(q.b[1])+'</button></td><td class="up">'+q.up+'</td><td class="down">'+q.down+'</td><td>'+q.hits.length+'/'+q.valid+'</td><td>'+pct(q.rate)+'</td><td>'+num(delta(q.rate,market.rate))+'</td><td>'+pct(q.width)+'</td><td>'+num(q.change)+'</td></tr>').join('')+'</table>'+nav('page',rows.length);
 if(!cur){$('detail').textContent='没有匹配板块';return}
 const b=cur.b,up=b[2].filter(i=>stocks[i][5]===1&&stocks[i][6]===0),down=b[2].filter(i=>stocks[i][5]===0&&stocks[i][6]===1);
 const list=({'异动股':cur.hits,'今日上穿':up,'今日下穿':down})[state.list].slice().sort((i,j)=>(stocks[i][3]??Infinity)-(stocks[j][3]??Infinity)||Math.abs(stocks[j][2])-Math.abs(stocks[i][2]));
 state.stockPage=Math.min(state.stockPage,Math.max(0,Math.ceil(list.length/12)-1));
 $('detail').innerHTML='<h2>'+esc(b[1])+'</h2><p>成分 '+b[2].length+' 只　异动 '+cur.hits.length+' 只　上穿 '+up.length+' 只　下穿 '+down.length+' 只</p><h3>近 20 日站上 MA20 的成分股占比</h3><div class="bars">'+cur.t.map((v,i)=>'<span class="bar" style="height:'+100*(v??0)+'%" title="'+dates[i]+' · '+b[3][i]+'/'+b[4][i]+' · '+pct(v)+'"></span>').join('')+'</div><div class="axis"><small>'+dates[0]+'</small><small>'+dates.at(-1)+' · '+pct(cur.width)+'</small></div><h3>个股明细</h3>'+buttons('list',['异动股','今日上穿','今日下穿'])+'<p>'+list.length+' 只</p><table><tr><th>股票 / 代码</th><th>日涨跌幅</th><th>历史尾部频率</th><th>距 MA20</th></tr>'+list.slice(state.stockPage*12,(state.stockPage+1)*12).map(i=>{const s=stocks[i];return '<tr><td>'+esc(s[1])+' '+esc(s[0])+'</td><td>'+num(s[2])+'%</td><td>'+(s[3]==null?'—':((1+s[3])*100/121).toFixed(2)+'%')+'</td><td>'+(s[4]==null?'—':num(s[4])+'%')+'</td></tr>'}).join('')+'</table>'+nav('stockPage',list.length)
}
document.onclick=e=>{const el=e.target.closest('button');if(!el||el.disabled)return;const d=el.dataset;if(d.key){state[d.key]=d.key==='cut'?Number(d.val):d.val;if(d.key!=='list')state.page=0;state.stockPage=0}else if(d.page){state[d.page]+=Number(d.move);if(d.page==='page')state.stockPage=0}else{state.selected=d.board;state.stockPage=0}draw()};$('query').oninput=()=>{state.page=state.stockPage=0;draw()};draw();
</script></html>'''

if __name__ == '__main__':
    build()
