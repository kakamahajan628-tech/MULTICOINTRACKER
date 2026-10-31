"use strict";
const $=id=>document.getElementById(id);
const num=(v,d=2)=>v==null?"—":Number(v).toLocaleString(undefined,{maximumFractionDigits:d});
const signed=v=>(v>0?"+":"")+num(v,3);
const price=v=>num(v,Math.max(2,Math.min(12,6-Math.floor(Math.log10(Math.abs(Number(v))||1)))));
let latest=null, selected="", history=[];
const historyCache=new Map();
function mergeHistory(data){
 for(const key of historyCache.keys())if(!data.markets[key])historyCache.delete(key);
 for(const [key,market] of Object.entries(data.markets)){
   const epoch=market.quality.book_epoch,old=historyCache.get(key);
   const rows=old?.epoch===epoch?old.rows:[];
   const combined=new Map(rows.map(r=>[r.t,r]));
   for(const row of market.history||[])combined.set(row.t,row);
   const kept=[...combined.values()].sort((a,b)=>a.t-b.t).filter(r=>r.t>=data.received_ms-120000).slice(-240);
   historyCache.set(key,{epoch,rows:kept});market.history=kept;
 }
 return data;
}
function node(tag,text,cls){const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;}
function empty(parent,text){parent.replaceChildren(node("div",text,"empty"));}
function row(parent,a,b){const r=node("div",null,"data-row");r.append(node("span",a),node("span",b));parent.append(r);}
function metric(label,value,unit){const n=node("div",null,"metric");n.append(node("label",label),node("strong",value),node("span",unit));return n;}
function setup(canvas){const r=canvas.getBoundingClientRect(),d=devicePixelRatio||1;canvas.width=r.width*d;canvas.height=r.height*d;const c=canvas.getContext("2d");c.scale(d,d);c.clearRect(0,0,r.width,r.height);return [c,r.width,r.height];}
function drawHeat(){
 const [c,w,h]=setup($("heatmap")); const seconds=Number($("window").value);
 history=(latest?.markets[selected]?.history||[]).filter(x=>x.t>=latest.received_ms-seconds*1000);
 if(!history.length){c.fillStyle="#8e9cb3";c.fillText("Waiting for synchronized order book…",20,40);return;}
 const prices=history.flatMap(x=>[...x.bid,...x.ask].map(l=>Number(l[0])));
 const low=Math.min(...prices),high=Math.max(...prices),range=high-low||1;
 const left=12,right=w-78,top=10,bottom=h-26;
 const y=p=>bottom-(p-low)/range*(bottom-top);
 const max=Math.max(1,...history.flatMap(x=>[...x.bid,...x.ask].map(l=>Number(l[0])*Number(l[1]))));
 const cw=(right-left)/history.length;
 history.forEach((col,i)=>{
   for(const side of ["bid","ask"])for(const [p,q] of col[side]){
     const intensity=Math.min(1,Math.sqrt(Number(p)*Number(q)/max));
     c.fillStyle=side==="bid"?"rgba(66,216,176,"+(0.06+0.88*intensity)+")":"rgba(241,118,145,"+(0.06+0.88*intensity)+")";
     c.fillRect(left+i*cw,y(Number(p))-1,Math.max(1,cw+0.5),2);
   }
 });
 c.strokeStyle="#eef5ff";c.lineWidth=1.2;c.beginPath();history.forEach((col,i)=>{const x=left+(i+.5)*cw;i?c.lineTo(x,y(col.mid)):c.moveTo(x,y(col.mid));});c.stroke();
 c.font="10px monospace";c.fillStyle="#8e9cb3";
 for(let i=0;i<5;i++){const p=low+range*i/4,yy=y(p);c.fillText(price(p),right+9,yy+3);c.strokeStyle="#ffffff09";c.beginPath();c.moveTo(left,yy);c.lineTo(right,yy);c.stroke();}
 c.fillText(new Date(history[0].t).toLocaleTimeString(),left,h-6);
 c.fillText(new Date(history.at(-1).t).toLocaleTimeString(),right-66,h-6);
}
function drawCvd(){
 const [c,w,h]=setup($("cvd"));if(!history.length)return;
 const vals=history.map(x=>x.cvd),lo=Math.min(...vals),hi=Math.max(...vals),range=hi-lo||1;
 c.strokeStyle="#42d8b0";c.lineWidth=1.5;c.beginPath();
 vals.forEach((v,i)=>{const x=10+i/(vals.length-1||1)*(w-85),y=h-18-(v-lo)/range*(h-35);i?c.lineTo(x,y):c.moveTo(x,y);});c.stroke();
 c.fillStyle="#8e9cb3";c.font="10px monospace";c.fillText(num(hi,3),w-70,15);c.fillText(num(lo,3),w-70,h-15);
}
function render(data){
 latest=data;const keys=Object.keys(data.markets);if(!selected||!keys.includes(selected))selected=keys[0]||"";
 $("market").replaceChildren(...keys.map(k=>{const o=node("option",k);o.value=k;return o;}));$("market").value=selected;
 $("mode").textContent=data.mode==="demo"?"SYNTHETIC DEMO":data.mode.toUpperCase()+(data.replay_finished?" · FINISHED":"");
 const market=data.markets[selected];if(!market){$("status").textContent="Waiting for exchange metadata";return;}
 const q=market.quality,m=market.derived;
 $("status").textContent=q.fresh?"BOOK SYNCHRONIZED":"BOOK UNAVAILABLE / STALE";
 $("status").style.color=q.fresh?"#42d8b0":"#edc16d";
 $("quality").textContent="Book age "+num(q.book_age_ms,0)+" ms · epoch "+q.book_epoch+" · tape "+(q.tape_connected?"connected":"offline")+(q.last_error?" · "+q.last_error:"");
 $("count").textContent=num(data.event_count,0)+" events";
 $("metrics").replaceChildren(
 metric("MID PRICE",price(m?.mid),"USDT"),
 metric("SPREAD",num(m?.spread_bps,3),"basis points"),
 metric("BOOK IMBALANCE",m?signed(m.imbalance_top20):"—","top 20 visible levels"),
 metric("CVD",m?signed(m.cvd_base):"—","base · current stream segment"),
 metric("DELTA · 5S",m?signed(m.delta_5s_base):"—","base · aggressor buys − sells"),
 metric("OFI · 10S",m?signed(m.ofi_10s_base):"—","base · best quote changes"));
 $("cvd-epoch").textContent="SEGMENT "+(m?.cvd_epoch??"—");
 const ladder=$("ladder"),book=market.observed.book;ladder.replaceChildren();
 if(book){
 const levels=[...book.bid.slice(0,7),...book.ask.slice(0,7)];
 const max=Math.max(...levels.map(([p,q])=>Number(p)*Number(q)),1);
 function add(l,side){const [p,q]=l,n=node("div",null,"level "+side+"-row");n.style.setProperty("--width",(Number(p)*Number(q)/max*100)+"%");n.append(node("span",price(p),"price"),node("span",num(q,3)),node("span",num(Number(p)*Number(q),0)));ladder.append(n);}
 book.ask.slice(0,7).reverse().forEach(l=>add(l,"ask"));ladder.append(node("div",price(m.mid),"mid-label"));book.bid.slice(0,7).forEach(l=>add(l,"bid"));
 }else empty(ladder,"Waiting for a fresh synchronized book");
 const feed=$("signals");feed.replaceChildren();
 const signals=data.inferred_signals.filter(s=>s.venue+":"+s.symbol===selected).slice(-12).reverse();
 if(!signals.length)empty(feed,"No candidates meet the current thresholds.");
 for(const s of signals){const n=node("div",null,"signal"),title=node("div",null,"signal-title");title.append(node("span",s.kind.replaceAll("_"," ")),node("span",num(s.score,2)));n.append(title,node("small",new Date(s.received_ms).toLocaleTimeString()+" · INFERRED"),node("p",JSON.stringify(s.evidence)));feed.append(n);}
 const bands=$("bands"),model=market.modeled_liquidations;bands.replaceChildren();
 for(const b of model.bands)row(bands,b.side+" "+b.leverage+"×",price(b.price_low)+"–"+price(b.price_high)+" · est. "+num(b.scenario_quote_notional/1e6,1)+"M");
 if(!model.bands.length)empty(bands,model.status||"No unswept scenario envelopes");
 $("assumptions").textContent=(model.assumptions||"Awaiting public OI and mark observations.")+" "+(model.uncertainty||"")+(model.stale?" CONTEXT IS STALE.":"");
 const venues=$("venues");venues.replaceChildren();
 for(const group of data.cross_venue){for(const [v,mid] of Object.entries(group.venue_mids)){let total=0;for(const side of ["bid","ask"])for(const contributions of Object.values(group.depth[side]))total+=Number(contributions[v]||0);row(venues,v+" · "+price(mid),num(total/1e6,2)+"M USDT visible");}}
 if(!data.cross_venue.length)empty(venues,"Fresh venue books required");
 const observed=$("observed");observed.replaceChildren();
 for(const event of market.observed.recent.slice(-6).reverse())row(observed,new Date(event.received_ms).toLocaleTimeString()+" · "+event.kind,event.kind==="mark_and_oi"?"Mark "+price(event.mark)+" · OI "+num(Number(event.oi_quote)/1e9,2)+"B USDT":event.position_side+" · "+num(event.qty,3)+" @ "+price(event.price));
 drawHeat();drawCvd();
}
async function poll(){try{const response=await fetch("/api/liquidity/state?market="+encodeURIComponent(selected)+"&since="+(historyCache.get(selected)?.rows.at(-1)?.t||0),{credentials:"same-origin"});if(!response.ok)throw Error(response.status===401?"Access token required":"Service unavailable");render(mergeHistory(await response.json()));}catch(e){$("status").textContent=e.message;$("status").style.color="#edc16d";}finally{setTimeout(poll,1000);}}
$("market").onchange=()=>{selected=$("market").value;if(latest)render(latest);};
$("window").onchange=()=>{drawHeat();drawCvd();};

$("heatmap").onmousemove=e=>{if(!history.length)return;const r=e.target.getBoundingClientRect();const i=Math.max(0,Math.min(history.length-1,Math.floor((e.clientX-r.left-12)/(r.width-90)*history.length)));const col=history[i];$("hover").textContent=new Date(col.t).toLocaleTimeString()+" · mid "+price(col.mid);};
window.addEventListener("resize",()=>{drawHeat();drawCvd();});poll();

