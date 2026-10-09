'use strict';
const $ = (id) => document.getElementById(id);
const ns = 'http://www.w3.org/2000/svg';
const money = (v, decimals = 0) => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',minimumFractionDigits:decimals,maximumFractionDigits:decimals}).format(v);
const num = (v, d=1) => new Intl.NumberFormat('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}).format(v);
const pct = (v) => v == null ? 'Undefined' : `${v > 0 ? '+' : ''}${num(v,2)}%`;
const shortDate = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'});
function svgEl(tag, attrs={}, text) { const e=document.createElementNS(ns,tag); for(const [k,v] of Object.entries(attrs)) e.setAttribute(k,v); if(text!==undefined)e.textContent=text; return e; }
function add(parent,tag,attrs,text){const e=svgEl(tag,attrs,text);parent.append(e);return e;}
function svgRoot(label, w, h){const s=svgEl('svg',{viewBox:`0 0 ${w} ${h}`,role:'img','aria-label':label});add(s,'title',{},label);return s;}
const state={pf:1,effect:'Combined intervention',day:'2020-06-07',model:'AC',case:'CFR',hour:12};
let data, dayTimer=null;

// The hero is an illustrative diagram. Particle motion never encodes saved MW.
const nodes=[[136,85],[144,234],[270,105],[270,213],[370,68],[370,258],[456,145],[570,100],[570,230]];
nodes.forEach(([x,y],i)=>{const node=add($('grid-nodes'),'g',{'class':'reveal-item','data-reveal':3+i*.15});add(node,'circle',{cx:x,cy:y,r:8,fill:'#fafaf7',stroke:'#b4c8bb','stroke-width':1});add(node,'circle',{cx:x,cy:y,r:3,fill:x===456?'#b77a38':'#5f927a'});});
for(const id of ['dc-layer-dots','ac-layer-dots'])for(let r=0;r<4;r++)for(let c=0;c<7;c++)add($(id),'circle',{cx:36+c*16+r*10,cy:25+c*3.5-r*3.5,r:1.5,fill:id.startsWith('dc')?'#648d76':'#708daf',opacity:.8});
const routes=[[[136,85],[270,105],[456,145],[570,100],[623,160],[719,160],[719,101],[805,101]],[[144,234],[270,213],[456,145],[570,230],[623,160],[719,160],[719,243],[805,243]],[[370,68],[456,145],[570,100],[623,160],[719,160],[719,243],[805,243]],[[370,258],[456,145],[570,230],[623,160],[719,160],[719,101],[805,101]]];
const motionPreference=matchMedia('(prefers-reduced-motion: reduce)');
let heroPaused=motionPreference.matches,heroTime=0,previousFrame=null,heroFrame;
const revealItems=[...document.querySelectorAll('#grid-scene [data-reveal]')];
const revealComplete=7.8;
let introTime=motionPreference.matches?revealComplete:0;
const particles=[];
routes.forEach((points,routeIndex)=>{
  const lengths=points.slice(1).map((p,i)=>Math.hypot(p[0]-points[i][0],p[1]-points[i][1]));
  const total=lengths.reduce((a,b)=>a+b,0);
  for(let i=0;i<7;i++)particles.push({points,lengths,total,offset:i/7,rate:.035+routeIndex*.005,e:add($('energy-particles'),'circle',{r:2.4,fill:routeIndex%2?'#7190b9':'#76a28b',opacity:.8})});
});
function drawParticles(){particles.forEach(p=>{let length=((heroTime*p.rate+p.offset)%1)*p.total;let index=0;while(index<p.lengths.length-1&&length>p.lengths[index]){length-=p.lengths[index];index++;}const a=p.points[index],b=p.points[index+1],t=length/p.lengths[index];p.e.setAttribute('cx',a[0]+(b[0]-a[0])*t);p.e.setAttribute('cy',a[1]+(b[1]-a[1])*t);});}
function updatePauseButton(){$('pause-hero').textContent=heroPaused?'Play animation ▷':'Pause animation Ⅱ';$('pause-hero').setAttribute('aria-pressed',String(heroPaused));}
function drawReveal(){revealItems.forEach(el=>{const progress=Math.max(0,Math.min(1,(introTime-Number(el.dataset.reveal))/.55));const eased=1-(1-progress)**3;el.setAttribute('opacity',eased);el.style.transform=`translateY(${(1-eased)*7}px) scale(${.94+.06*eased})`;});}
function frame(time){if(!heroPaused&&!document.hidden){const dt=previousFrame===null?0:Math.min((time-previousFrame)/1000,.1);introTime=Math.min(revealComplete,introTime+dt);if(introTime>7.2)heroTime+=dt;drawReveal();drawParticles();}previousFrame=time;heroFrame=requestAnimationFrame(frame);}
drawParticles();drawReveal();updatePauseButton();heroFrame=requestAnimationFrame(frame);
$('pause-hero').addEventListener('click',()=>{heroPaused=!heroPaused;updatePauseButton();});
$('replay-hero').addEventListener('click',()=>{introTime=motionPreference.matches?revealComplete:0;heroTime=0;previousFrame=null;heroPaused=motionPreference.matches;drawReveal();drawParticles();updatePauseButton();});
motionPreference.addEventListener('change',e=>{heroPaused=e.matches;if(e.matches){introTime=revealComplete;drawReveal();stopDay();}updatePauseButton();});

// The Pages deployment injects the actual owner/repository. Never invent a URL.
fetch('site-config.json').then(r=>r.ok?r.json():null).then(config=>{
  const url=config?.repository_url;
  if(typeof url!=='string'||!/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/.test(url))return;
  document.querySelectorAll('[data-repository-link]').forEach(link=>{link.href=url;link.hidden=false;});
  document.querySelectorAll('[data-repository-pending]').forEach(el=>{el.hidden=true;});
}).catch(()=>{});

function selectedEffects(){return data.effects.filter(r=>r['Power factor']===state.pf&&r.Effect===state.effect).sort((a,b)=>a.Day.localeCompare(b.Day));}
function updateResults(){
  const s=data.summary.find(r=>r['Power factor']===state.pf&&r.Effect===state.effect);
  if(!s)throw new Error('Selected effect is absent from summary');
  const er=s['Sample aggregate erosion %'];
  $('dc-savings').textContent=money(s['Validated sample DC savings $']);
  $('ac-savings').textContent=money(s['Validated sample AC savings $']);
  $('aggregate-erosion').textContent=pct(er);$('aggregate-erosion').style.color=er<0?'var(--blue)':'var(--orange)';
  $('erosion-description').textContent=er<0?'AC estimates a larger benefit':'AC estimates a smaller benefit';
  $('aggregate-note').textContent=`These are selected-day totals, not annual projections. ${s['Amplification days (<-0.01%)']} days show amplification and ${s['Erosion days (>0.01%)']} show erosion; ${s['Undefined DC-benefit days']} have undefined percentages. Aggregate erosion uses the ratio of summed savings, rather than the average daily percentage.`;
  const rows=selectedEffects();drawDaily(rows);renderDailyTable(rows);updateDayReading(rows);renderReversals();updateHourly();
}
function drawDaily(rows){
  const w=880,h=280,l=55,r=14,t=25,b=42,plotH=h-t-b,plotW=w-l-r;
  const vals=rows.map(x=>x['Benefit erosion %']).filter(x=>x!=null&&Number.isFinite(x));
  const low=Math.min(0,...vals),high=Math.max(0,...vals);
  const tickStep=(high-low)>180?50:(high-low)>80?25:(high-low)>35?10:5;
  const min=Math.floor((low-1)/tickStep)*tickStep,max=Math.ceil((high+1)/tickStep)*tickStep;
  const y=v=>t+(max-v)/(max-min)*plotH;
  const x=i=>l+(i+.5)*plotW/rows.length;
  const svg=svgRoot(`Daily benefit erosion for ${state.effect}, power factor ${state.pf}. Full values in the table below.`,w,h);
  svg.setAttribute('role','group');
  for(let tick=min;tick<=max;tick+=tickStep){add(svg,'line',{x1:l,x2:w-r,y1:y(tick),y2:y(tick),stroke:tick===0?'#b5c2b7':'#e4e9e1','stroke-width':tick===0?1.3:.7});add(svg,'text',{x:l-9,y:y(tick)+3,'text-anchor':'end',fill:'#7a897e','font-size':10},`${tick}%`);}
  rows.forEach((row,i)=>{
    const v=row['Benefit erosion %'],cx=x(i),bw=Math.min(18,plotW/rows.length*.58),selected=row.Day===state.day;
    if(selected)add(svg,'rect',{x:cx-plotW/rows.length*.43,y:t,width:plotW/rows.length*.86,height:plotH,rx:3,fill:'#e8eee5',opacity:.7});
    const group=add(svg,'g',{tabindex:0,role:'button','aria-label':`${shortDate(row.Day)}: ${pct(v)}. Select this day.`});
    // Full-column hit target makes short and zero-benefit bars equally selectable.
    add(group,'rect',{x:cx-plotW/rows.length*.46,y:t,width:plotW/rows.length*.92,height:plotH,fill:'transparent'});
    if(v==null){add(group,'line',{x1:cx-4,x2:cx+4,y1:y(0)-4,y2:y(0)+4,stroke:'#9aaba0','stroke-width':1.5});add(group,'line',{x1:cx-4,x2:cx+4,y1:y(0)+4,y2:y(0)-4,stroke:'#9aaba0','stroke-width':1.5});}
    else add(group,'rect',{x:cx-bw/2,y:Math.min(y(0),y(v)),width:bw,height:Math.max(1,Math.abs(y(v)-y(0))),rx:1.5,fill:v<0?'#5478aa':'#b77a38',opacity:selected?1:.8});
    add(group,'title',{},`${row.Day}\nDC ${money(row['DC savings $'],2)} · AC ${money(row['AC savings $'],2)}\nErosion ${pct(v)}`);
    const choose=()=>{state.day=row.Day;$('day-select').value=row.Day;drawDaily(rows);updateDayReading(rows);};
    group.addEventListener('click',choose);group.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();choose();}});
    if(i%3===0||i===rows.length-1)add(svg,'text',{x:cx,y:h-20,'text-anchor':'middle',fill:'#7a897e','font-size':9},shortDate(row.Day));
  });
  if(rows.some(r=>r['Benefit erosion %']==null))add(svg,'text',{x:w-r,y:h-4,'text-anchor':'end',fill:'#7a897e','font-size':9},'× Undefined: DC benefit effectively zero');
  $('daily-chart').replaceChildren(svg);
}
function updateDayReading(rows=selectedEffects()){
  const row=rows.find(r=>r.Day===state.day);if(!row)return;
  const reading=$('day-reading');reading.replaceChildren();
  for(const [label,value] of [['DC',money(row['DC savings $'],2)],['AC',money(row['AC savings $'],2)],['Erosion',pct(row['Benefit erosion %'])]]){const span=document.createElement('span');span.append(`${label} `);const strong=document.createElement('strong');strong.textContent=value;span.append(strong);reading.append(span);}
  $('selection-reason').textContent=`Selection context: ${row['Selection Reason']}.`;
}
function renderDailyTable(rows){const tbody=$('daily-table');tbody.replaceChildren();for(const row of rows){const tr=document.createElement('tr');[row.Day,money(row['DC savings $'],2),money(row['AC savings $'],2),pct(row['Benefit erosion %'])].forEach((v,i)=>{const td=document.createElement(i===0?'th':'td');if(i===0)td.scope='row';else td.className='numeric';td.textContent=v;tr.append(td);});tbody.append(tr);}}
function renderReversals(){
  const rows=data.interactions.filter(r=>r['Power factor']===state.pf&&r['DC classification']!==r['AC classification']);
  const host=$('reversal-list');host.replaceChildren();
  for(const r of rows){const item=document.createElement('div');item.className='reversal-item';const left=document.createElement('div'),time=document.createElement('time');time.dateTime=r.Day;time.textContent=shortDate(r.Day)+', 2020';const desc=document.createElement('p');desc.textContent=`${r['DC classification']} → ${r['AC classification']}`;left.append(time,desc);const right=document.createElement('div');right.className='reversal-values';const first=document.createElement('div');first.textContent=`DC ${money(r['DC interaction $'],2)}`;const second=document.createElement('span');second.textContent=`AC ${money(r['AC interaction $'],2)}`;right.append(first,second);item.append(left,right);host.append(item);}
}
function hourlyRows(){return data.hourly.filter(r=>r['Power factor']===state.pf&&r.Case===state.case&&r['Network model']===state.model).sort((a,b)=>a.Timestamp.localeCompare(b.Timestamp));}
function updateHourly(){const rows=hourlyRows();if(rows.length!==24)throw new Error('Pilot hourly records missing');$('hourly-pf').textContent=num(state.pf,2);drawHourly(rows);updateHourReading(rows);}
function drawHourly(rows){
  const w=840,h=208,l=43,r=16,t=20,b=34,pw=w-l-r,ph=h-t-b;
  const x=i=>l+i/23*pw,y=v=>t+(160-v)/120*ph;
  const svg=svgRoot(`${state.model} data-center demand schedule for ${state.case}, June 7, power factor ${state.pf}.`,w,h);
  for(const v of [50,75,100,125,150]){add(svg,'line',{x1:l,x2:w-r,y1:y(v),y2:y(v),stroke:v===100?'#b7cbb7':'#edf0e9','stroke-dasharray':v===100?'3 4':'none'});add(svg,'text',{x:l-8,y:y(v)+3,'text-anchor':'end',fill:'#7a897e','font-size':10},String(v));}
  const path=rows.map((v,i)=>`${i?'L':'M'}${x(i)} ${y(v['DC power MW'])}`).join(' ');
  add(svg,'path',{d:`${path} L${x(23)} ${y(40)} L${x(0)} ${y(40)} Z`,fill:'#eaf2e9',opacity:.8});
  add(svg,'path',{d:path,fill:'none',stroke:'#44765b','stroke-width':2.5,'stroke-linejoin':'round'});
  rows.forEach((v,i)=>{const dot=add(svg,'circle',{cx:x(i),cy:y(v['DC power MW']),r:2.5,fill:'#44765b'});add(dot,'title',{},`${String(i).padStart(2,'0')}:00 · ${num(v['DC power MW'],2)} MW`);});
  [0,6,12,18,23].forEach(i=>add(svg,'text',{x:x(i),y:h-12,'text-anchor':'middle',fill:'#7a897e','font-size':10},`${String(i).padStart(2,'0')}:00`));
  add(svg,'line',{id:'hour-cursor',x1:x(state.hour),x2:x(state.hour),y1:t,y2:h-b,stroke:'#21362f','stroke-width':1,'stroke-dasharray':'3 4'});
  add(svg,'circle',{id:'hour-dot',cx:x(state.hour),cy:y(rows[state.hour]['DC power MW']),r:5,fill:'#21362f',stroke:'#fff','stroke-width':2});
  const pointerHour=e=>{const rect=svg.getBoundingClientRect(),pos=(e.clientX-rect.left)/rect.width*w;state.hour=Math.round(Math.min(23,Math.max(0,(pos-l)/pw*23)));$('hour-range').value=state.hour;updateHourReading(rows);};
  svg.addEventListener('click',pointerHour);
  $('hourly-chart').replaceChildren(svg);
}
function updateHourReading(rows=hourlyRows()){
  const row=rows[state.hour];if(!row)return;
  const time=`${String(state.hour).padStart(2,'0')}:00`;$('hour-label').textContent=time;$('range-time').textContent=time;
  $('hour-power').textContent=`${num(row['DC power MW'])} MW`;
  $('hour-loading').textContent=`${num(row['C6 loading %'])}%`;
  $('hour-loss').textContent=`${num(row['Branch losses MW'])} MW`;
  $('loading-unit').textContent=state.model==='AC'?'Max terminal MVA / line rating':'Active-power MW / line rating';
  $('loss-explanation').textContent=state.model==='AC'?'All branches · active-power losses':'Zero in the DC approximation';
  const x=43+state.hour/23*(840-43-16),y=20+(160-row['DC power MW'])/120*(208-20-34);
  const cursor=$('hour-cursor');if(cursor){cursor.setAttribute('x1',x);cursor.setAttribute('x2',x);$('hour-dot').setAttribute('cx',x);$('hour-dot').setAttribute('cy',y);}
}
function stopDay(){if(dayTimer)clearInterval(dayTimer);dayTimer=null;$('play-day').textContent='Play day ▷';$('play-day').setAttribute('aria-pressed','false');}
$('play-day').addEventListener('click',()=>{if(!data)return;if(dayTimer){stopDay();return;}$('play-day').textContent='Pause day Ⅱ';$('play-day').setAttribute('aria-pressed','true');dayTimer=setInterval(()=>{if(document.hidden)return;state.hour=(state.hour+1)%24;$('hour-range').value=state.hour;updateHourReading();},800);});
$('hour-range').addEventListener('input',e=>{if(!data)return;state.hour=Number(e.target.value);updateHourReading();});
$('pf-control').addEventListener('click',e=>{const b=e.target.closest('[data-pf]');if(!b||!data)return;state.pf=Number(b.dataset.pf);for(const el of $('pf-control').querySelectorAll('button')){const yes=Number(el.dataset.pf)===state.pf;el.classList.toggle('active',yes);el.setAttribute('aria-pressed',String(yes));}updateResults();});
$('effect-select').addEventListener('change',e=>{if(!data)return;state.effect=e.target.value;updateResults();});
$('day-select').addEventListener('change',e=>{if(!data)return;state.day=e.target.value;const rows=selectedEffects();drawDaily(rows);updateDayReading(rows);});
$('model-control').addEventListener('click',e=>{const b=e.target.closest('[data-model]');if(!b||!data)return;state.model=b.dataset.model;for(const el of $('model-control').querySelectorAll('button')){const yes=el.dataset.model===state.model;el.classList.toggle('active',yes);el.setAttribute('aria-pressed',String(yes));}updateHourly();});
$('case-select').addEventListener('change',e=>{if(!data)return;state.case=e.target.value;updateHourly();});
fetch('assets/results.json').then(r=>{if(!r.ok)throw new Error(`Data request: ${r.status}`);return r.json();}).then(d=>{
  data=d;const days=[...new Set(data.selection.map(r=>r.Day))].sort();
  for(const day of days){const o=document.createElement('option');o.value=day;o.textContent=`${shortDate(day)}, 2020`;$('day-select').append(o);}
  $('day-select').value=state.day;updateResults();
}).catch(err=>{console.error('Research data could not be initialized',err);$('load-error').hidden=false;for(const el of document.querySelectorAll('select,input,#pf-control button,#model-control button,#play-day'))el.disabled=true;});
