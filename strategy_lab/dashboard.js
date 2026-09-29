(()=>{'use strict';
const $=id=>document.getElementById(id), money=n=>Number.isFinite(Number(n))?'₹'+Number(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
const signed=n=>{const v=Number(n)||0;return (v>0?'+':'')+money(v)};
const percent=(n,capital)=>Number(capital)>0?((Number(n)||0)/Number(capital)*100).toFixed(2)+'%':'—';
const pctLabel=value=>node('small','pnl-percent','('+value+')');
function setPnl(el,amount,capital,suffix=''){el.replaceChildren(node('span','',signed(amount)),pctLabel(percent(amount,capital)+(suffix?' '+suffix:'')));color(el,amount)}
const color=(el,n)=>{el.classList.toggle('positive',Number(n)>0);el.classList.toggle('negative',Number(n)<0);el.classList.toggle('neutral',Number(n)===0)};
const text=(el,value)=>{const s=String(value??'—');if(el.textContent!==s)el.textContent=s};
let status=null,chain=null,analyticsSummary=null,detailData=null,detailStrategy=null,detailMonth=null,detailDay=null,routeInitialized=false,monthOffset=0,busy=false,activeTab='dashboard',toastTimer;
let pinResolver=null;
let authRevision=0,loginRequired=false,loginBusy=false;
function requestPin(title,explanation){if(pinResolver)return Promise.resolve(null);text($('pin-title'),title);text($('pin-explanation'),explanation);$('action-pin').value='';$('pin-dialog').showModal();$('action-pin').focus();return new Promise(resolve=>pinResolver=resolve)}
function closePin(value){$('pin-dialog').close();if(pinResolver){const resolve=pinResolver;pinResolver=null;resolve(value)}}
function submitPin(){const value=$('action-pin').value;if(!/^\d{4}$/.test(value)){text($('pin-explanation'),'Enter exactly four digits to continue.');$('action-pin').focus();return}closePin(value)}$('pin-submit').onclick=submitPin;$('action-pin').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();submitPin()}};$('pin-cancel').onclick=$('pin-cancel-x').onclick=()=>closePin(null);$('pin-dialog').oncancel=e=>{e.preventDefault();closePin(null)};
function notify(message,error=false){const el=$('toast');text(el,message);el.classList.toggle('error',error);el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,5000)}
async function api(path,payload){const options={credentials:'same-origin',cache:'no-store',headers:{}};if(payload!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(payload)}const response=await fetch(path,options);let data={};try{data=await response.json()}catch{}if(!response.ok){const e=new Error(data.error||'Server request failed');e.status=response.status;throw e}return data}
function themePreference(){try{return window.localStorage.getItem('desk-theme')}catch{return null}}
function applyTheme(name){document.documentElement.dataset.theme=name;try{window.localStorage.setItem('desk-theme',name)}catch{}$('light-toggle').checked=name==='light'}
applyTheme(themePreference()==='light'?'light':'dark');
$('broker-auth-transport-note').hidden=location.protocol!=='http:'||['localhost','127.0.0.1'].includes(location.hostname);
function tab(name){activeTab=name;if(name!=='strategy-detail'&&location.pathname.startsWith('/strategy/'))window.history?.pushState?.({},'','/');document.querySelectorAll('.nav button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===name)));document.querySelectorAll('.page').forEach(p=>p.classList.toggle('active',p.id==='page-'+name));if(name==='chain')loadChain();if(name==='settings')loadBrokerAuth();if(name==='analytics')loadAnalytics();if(name==='strategy-detail')loadDetail()}
document.querySelectorAll('.nav button').forEach(b=>b.addEventListener('click',()=>tab(b.dataset.tab)));
$('theme').onclick=()=>applyTheme(document.documentElement.dataset.theme==='light'?'dark':'light');$('light-toggle').onchange=e=>applyTheme(e.target.checked?'light':'dark');
async function submitLogin(){if(loginBusy)return;const pin=$('pin').value;if(!/^\d{4}$/.test(pin)){text($('login-error'),'Enter exactly four digits.');return}loginBusy=true;$('login-submit').disabled=true;try{await api('/api/login',{pin});authRevision++;loginRequired=false;$('pin').value='';$('login-error').textContent='';$('login').hidden=true;await loadStatus();if(loginRequired)text($('login-error'),'PIN accepted, but the browser session did not persist. Refresh and try again.')}catch(err){text($('login-error'),err.message)}finally{loginBusy=false;$('login-submit').disabled=false}}$('login-submit').onclick=submitLogin;$('pin').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();submitLogin()}};
async function loadStatus(){const revision=authRevision;try{const data=await api('/api/status');if(revision!==authRevision)return;status=data;if(!loginRequired)$('login').hidden=true;$('connection-dot').classList.add('on');text($('connection'),'Connected');render();if(!routeInitialized){routeInitialized=true;const id=location.pathname.startsWith('/strategy/')?location.pathname.slice('/strategy/'.length):'';if(strategy(id))openDetail(id,false)}}catch(err){if(revision!==authRevision)return;$('connection-dot').classList.remove('on');text($('connection'),'Offline');if(err.status===401){loginRequired=true;$('login').hidden=false;status=null}else notify(err.message,true)}}
async function change(path,payload,success){if(busy)return false;busy=true;try{await api(path,payload);notify(success);await loadStatus();return true}catch(err){notify(err.message,true);await loadStatus();return false}finally{busy=false}}
function strategy(id){return(status?.strategies||[]).find(spec=>spec.id===id)}
const STRATEGY_LABELS={nfv1:'1',nfv3:'2',nfv5:'3',mcxv1:'1',mcxv3:'2'};
function strategyLabel(id){return STRATEGY_LABELS[id]||String(id||'').toUpperCase()}
function strategyFullName(id){const spec=strategy(id),marketName=spec?.market||(String(id||'').startsWith('n')?'NIFTY':'MCX');return marketName+' '+strategyLabel(id)}
function displayEvent(message){return String(message||'').replace(/\b(nfv1|nfv3|nfv5|mcxv1|mcxv3)\b/gi,id=>'Strategy '+strategyLabel(id.toLowerCase()))}
function market(id){return strategy(id)?.market}
function strategyIds(){return(status?.strategies||[]).map(spec=>spec.id)}
function marketOrder(markets){return [...new Set(markets)].sort((a,b)=>{const order={NIFTY:0,MCX:1};return(order[a]??2)-(order[b]??2)||a.localeCompare(b)})}
function marketSection(parent,market,count,listClass){const section=node('section','market-section'),head=node('div','market-head'),heading=node('h2','',market),badge=node('span','badge',count+' strateg'+(count===1?'y':'ies')),cards=node('div',listClass);head.append(heading,badge);section.append(head,cards);parent.append(section);return cards}
function dateIST(value){if(!value)return'—';const d=new Date(value);return Number.isNaN(d.getTime())?'—':new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false}).format(d)}
function node(tag,cls,value){const e=document.createElement(tag);if(cls)e.className=cls;if(value!==undefined)e.textContent=String(value);return e}
function premiumStop(position){
  if(position.leg_stop!==null&&position.leg_stop!==undefined)return {label:position.trail_armed?'TSL':'SL',value:money(position.leg_stop)};
  return {label:'SL / TSL',value:position.side==='BUY'?'Hedge held':'Basket stop only'};
}
function terminalNumber(value,digits=2){return value===null||value===undefined||value===''||!Number.isFinite(Number(value))?'—':Number(value).toFixed(digits)}
function terminalBias(direction){return Number(direction)>0?'▲ UP':Number(direction)<0?'▼ DOWN':'━ FLAT'}
function terminalPanel(session,id,spec,capital){
  const signal=session.signal||{},indicators=signal.indicators||{},positions=session.positions||[],panel=node('section','strategy-terminal');
  const heading=node('div','terminal-heading'),title=node('div','terminal-title'),statusChip=node('span','terminal-state',String(session.state||'RUNNING').replaceAll('_',' '));
  title.append(node('span','terminal-lamp'),node('strong','',strategyFullName(id)+' · '+(spec?.description||'Strategy monitor')));
  heading.append(title,statusChip);panel.append(heading);
  const short=positions.find(p=>p.side==='SELL'),strike=short?.contract?.strike;
  const spot=indicators.close??signal.spot,trend=signal.direction??indicators.trend;
  const adx=indicators.adx7_1m??indicators.adx7_5m??indicators.adx;
  const flow=indicators.flow_score??signal.score;
  const stats=[['SPOT',terminalNumber(spot)],['SHORT STRIKE',strike===undefined?'—':terminalNumber(strike,0)],['BIAS',terminalBias(trend)],['SIGNAL',signal.eligible?'READY':'WAIT'],['ADX',terminalNumber(adx,1)],['KAMA',terminalNumber(indicators.kama)],['KAMA Δ',terminalNumber(indicators.kama_slope,3)],['EMA FAST',terminalNumber(indicators.ema8??indicators.ema9)],['EMA SLOW',terminalNumber(indicators.ema21)],['RSI',terminalNumber(indicators.rsi14,1)],['ATR',terminalNumber(indicators.atr14??indicators.atr)],['EFFICIENCY',terminalNumber(indicators.efficiency??indicators.efficiency5,3)],['FLOW',terminalNumber(flow,2)],['QUALITY',terminalNumber(signal.quality,2)],['ANCHOR',terminalNumber(indicators.anchor)],['RISK LIMIT',session.stop_loss?money(session.stop_loss):'—'],['TARGET',session.take_profit?money(session.take_profit):'—'],['CAPITAL',money(session.capital||capital)]];
  const feedAge=session.feed_age_seconds,age=feedAge===null||feedAge===undefined?'—':Number(feedAge).toFixed(1)+'s';
  const signalBar=indicators.last_bar_open||signal.timestamp||session.feed_timestamp;
  const barTime=signalBar?dateIST(signalBar):'—';
  const statsGrid=node('div','terminal-stats');
  for(const [label,value] of stats){const cell=node('div','terminal-stat');cell.append(node('small','',label),node('strong','',value));if(label==='BIAS')cell.classList.add(Number(trend)>0?'positive':Number(trend)<0?'negative':'neutral');statsGrid.append(cell)}
  panel.append(statsGrid);
  const positionWrap=node('div','terminal-positions-wrap'),table=node('table','terminal-positions'),thead=node('thead'),header=node('tr');
  for(const label of ['LEG','STRIKE','SIDE','QTY','ENTRY','LTP','BEST','SL / TSL','PNL'])header.append(node('th','',label));thead.append(header);const body=node('tbody');
  if(!positions.length){const row=node('tr'),cell=node('td','terminal-empty','No open positions · monitoring for the next entry signal');cell.colSpan=9;row.append(cell);body.append(row)}
  for(const p of positions){const row=node('tr'),contract=p.contract||{},leg=contract.option_type||String(p.symbol||'').match(/(?:CE|PE)$/)?.[0]||'LEG',stop=premiumStop(p);const values=[leg,contract.strike??'—',p.side||'—',p.quantity??'—',money(p.entry_price),money(p.mark_price),money(p.best_mark??p.entry_price),stop.label+' '+stop.value];for(const value of values)row.append(node('td','',value));const pnl=node('td','',signed(p.unrealized_pnl));color(pnl,p.unrealized_pnl);row.append(pnl);body.append(row)}
  table.append(thead,body);positionWrap.append(table);panel.append(positionWrap);
  const footer=node('div','terminal-footer'),pnlGrid=node('div','terminal-pnl');
  for(const [label,value] of [['REALIZED',session.realized_pnl],['UNREALIZED',session.unrealized_pnl],['NET MTM',session.net_pnl]]){const item=node('div','terminal-pnl-item');item.append(node('small','',label),node('strong','',signed(value)));color(item.lastChild,value);pnlGrid.append(item)}
  const returnItem=node('div','terminal-pnl-item');returnItem.append(node('small','','RETURN'),node('strong','',`(${percent(session.net_pnl,session.capital||capital)})`));pnlGrid.append(returnItem);
  const meta=node('div','terminal-meta');meta.append(node('span','',String(session.mode||'paper').toUpperCase()+' · '+(session.entries||0)+' ENTRIES'),node('span','','FEED '+age),node('span','',barTime));
  footer.append(pnlGrid,meta);panel.append(footer);
  panel.append(node('div','terminal-reason',(session.reason||signal.reason||'Awaiting strategy update')+' · '+(signal.eligible?'SIGNAL READY':'SIGNAL WAIT')));
  return panel;
}
function render(){const a=status.account||{}, sessions=status.sessions||{},capital=a.configured_capital||a.capital; text($('asof'),'Updated '+dateIST(status.server_time));text($('today-pnl'),signed(a.daily_pnl));color($('today-pnl'),a.daily_pnl);text($('today-return'),'('+percent(a.daily_pnl,status.paper_capital_basis||capital)+')');color($('today-return'),a.daily_pnl);text($('account-capital'),money(capital));
const schedules=status.schedules||{},open=[];let active=0;for(const [id,s] of Object.entries(sessions)){const spec=strategy(s.strategy_id||id.replace(':legacy',''));for(const p of s.positions||[])open.push({market:spec?.market||'—',strategy:s.strategy_id||id,mode:s.mode,...p})}
text($('paper-banner'),'PAPER · Simulated fills');
text($('active-count'),0);const activeBox=$('active-strategy-cards');activeBox.replaceChildren();for(const [id,planned] of Object.entries(schedules)){if(sessions[id]&&sessions[id].state&&!['STOPPED','SESSION_COMPLETE'].includes(sessions[id].state))continue;const spec=strategy(planned.strategy_id),card=node('div','active-strategy'),left=node('div'),right=node('div','value');left.append(node('strong','',strategyFullName(planned.strategy_id)),node('small','',(spec?.market||'—')+' · PAPER · SCHEDULED '+planned.scheduled_for+' '+(spec?.entry_start||'')+' IST'));right.append(node('div','','Waiting'),node('small','','Starts in its session window'));card.append(left,right);activeBox.append(card);active++}for(const [id,s] of Object.entries(sessions)){const running=s.state&&!['STOPPED','SESSION_COMPLETE'].includes(s.state);if(!running)continue;const strategyId=s.strategy_id||id.replace(':legacy',''),spec=strategy(strategyId),card=node('div','active-strategy'),left=node('div'),right=node('div','value');left.append(node('strong','',strategyFullName(strategyId)),node('small','',(spec?.market||'—')+' · '+(s.mode||'paper').toUpperCase()+' · '+s.state));right.append(node('div','',signed(s.net_pnl)));const returnLabel=node('small','');returnLabel.textContent='('+percent(s.net_pnl,s.capital||capital)+') return';right.append(returnLabel);color(right,s.net_pnl);card.append(left,right);const runtime=node('article','active-runtime');runtime.append(card,terminalPanel(s,strategyId,spec,capital));activeBox.append(runtime);active++}text($('active-count'),active);text($('active-badge'),active+' ON');if(!active)activeBox.append(node('div','empty','No strategy is on right now.'));
text($('open-count'),open.length);text($('positions-badge'),open.length+' OPEN');const box=$('active-trades');box.replaceChildren();if(!open.length){box.className='empty';box.textContent='No open positions right now.'}else{box.className='';for(const p of open){const row=node('div','trade');const name=node('div','');name.append(node('strong','',p.symbol||'Option'),node('small','',strategyLabel(p.strategy)+' · '+p.market+' · '+(p.mode||'paper').toUpperCase()));const pnl=node('div','right');pnl.append(node('div','',signed(p.unrealized_pnl)),pctLabel(percent(p.unrealized_pnl,capital)));const premium=node('div','premiumcol');
const stop=premiumStop(p);for(const [label,value] of [['Entry',money(p.entry_price)],['Current',money(p.mark_price)],['Best',money(p.best_mark??p.entry_price)],[stop.label,stop.value]]){const cell=node('div','premiumcell');cell.append(node('small','',label),node('strong','',value));premium.append(cell)}
pnl.className='tradepnl';row.append(name,node('div','',p.side+' · '+p.quantity),premium,pnl);color(pnl,p.unrealized_pnl);box.append(row)}}
text($('live-reason'),status.live_permission?'Permission is ON. '+(status.live_reason||'Broker execution unavailable'):'Permission is OFF. Enable it here before requesting live trading.');$('live-toggle').checked=!!status.live_permission;$('live-toggle').disabled=busy;
if(document.activeElement!==$('capital'))$('capital').value=String(capital||200000);
renderStrategies();renderHistory();if(activeTab==='strategy-detail')renderDetailPositions()}
function renderStrategies(){
  const list=$('strategy-list'),sessions=status.sessions||{},schedules=status.schedules||{},specs=status.strategies||[];
  for(const card of list.querySelectorAll('.strategy'))if(!specs.some(spec=>card.id==='card-'+spec.id))card.remove();
  for(const section of list.querySelectorAll('.market-section'))if(!specs.some(spec=>spec.market===section.dataset.market))section.remove();
  for(const group of marketOrder(specs.map(spec=>spec.market))){
    let section=[...list.querySelectorAll('.market-section')].find(item=>item.dataset.market===group);
    if(!section){marketSection(list,group,specs.filter(spec=>spec.market===group).length,'strategylist');section=list.lastElementChild;section.dataset.market=group}
    const groupCount=specs.filter(spec=>spec.market===group).length;
    text(section.querySelector('.badge'),groupCount+' strateg'+(groupCount===1?'y':'ies'));
    for(const spec of specs.filter(spec=>spec.market===group)){
    const id=spec.id;
    let card=$('card-'+id);
    if(!card){
      card=node('article','card strategy');card.id='card-'+id;
      const name=node('div','namecol');name.append(node('div','name',strategyLabel(id)),node('div','desc',spec.description||''));const statsLink=node('button','statslink','View stats →');statsLink.type='button';statsLink.onclick=()=>openDetail(id);name.append(statsLink);
      const size=node('div','sizecol');size.append(node('div','metriclabel','Multiplier'));
      const inp=node('input');inp.type='number';inp.min='1';inp.max='100';inp.step='1';inp.value='1';inp.id='mult-'+id;inp.setAttribute('aria-label',id+' multiplier');size.append(inp);
      const pnlcol=node('div','pnlcol');pnlcol.append(node('div','metriclabel','Today P&L / return'),node('div','metricval'));
      const feed=node('div','feedcol');feed.append(node('div','metriclabel','Session'),node('div','metricval'));
      const mode=node('div','modecol');const paperLabel=node('label');const paper=node('input');paper.type='checkbox';paper.checked=true;paper.id='paper-'+id;paper.setAttribute('aria-label',id+' paper trading');paperLabel.append(paper,node('span','','Paper'));mode.append(paperLabel,node('small','','Uncheck for live'));
      const sw=node('div','switchcol');const label=node('label','switch'),input=node('input');input.type='checkbox';input.id='toggle-'+id;input.setAttribute('aria-label','Start or stop '+id);label.append(input,node('span'));sw.append(label,node('small','',''));
      card.append(name,size,pnlcol,feed,mode,sw);section.lastElementChild.append(card);input.onchange=()=>toggleStrategy(id,input.checked);
    }
    if(card.parentElement!==section.lastElementChild)section.lastElementChild.append(card);
    const s=sessions[id]||sessions[id+':legacy']||((sessions[spec.market]||{}).strategy_id===id?sessions[spec.market]:{})||{},planned=schedules[id]||((schedules[spec.market]||{}).strategy_id===id?schedules[spec.market]:null),scheduled=!!planned,owns=s.strategy_id===id&&!!s.date;
    const patternDetails=owns&&s.pattern?' · '+s.pattern+' · rank '+Number(s.pattern_score).toFixed(2)+' · stop '+Number(s.underlying_stop).toFixed(1)+' · target '+Number(s.underlying_target).toFixed(1):'';
    const flowDetails=owns&&id==='nfv5'?' · '+(s.v5_state||'WAITING')+' · flow '+(s.signal?.eligible?Number(s.signal.score).toFixed(1):'waiting'):'';
    text(card.querySelector('.namecol .desc'),(spec.description||'')+patternDetails+flowDetails);
    const working=scheduled||(owns&&!['STOPPED','SESSION_COMPLETE'].includes(s.state)&&!s.stop_requested),archived=spec.enabled===false;
    const sw=$('toggle-'+id),inp=$('mult-'+id),paper=$('paper-'+id);sw.checked=working;sw.disabled=archived||busy||(owns&&['EXIT_PENDING','HEDGE_HOLD'].includes(s.state));inp.disabled=archived||working||busy;paper.disabled=archived||working||busy;
    if(working)paper.checked=(scheduled?planned.mode:s.mode)!=='live';
    const badge=card.querySelector('.name span');if(!badge)card.querySelector('.name').append(node('span','',archived?'ARCHIVED':'PAPER'));else text(badge,archived?'ARCHIVED':'PAPER');
    if((owns||scheduled)&&document.activeElement!==inp)inp.value=String(scheduled?planned.multiplier:s.multiplier||1);
    const pnl=card.querySelector('.pnlcol .metricval'),capital=s.capital||status.account?.configured_capital;
    if(owns)setPnl(pnl,s.net_pnl,capital);else text(pnl,'—');
    text(card.querySelector('.feedcol .metricval'),scheduled?'Scheduled '+planned.scheduled_for:owns?(s.state||'—')+(s.pattern?' · '+s.pattern:'')+(id==='nfv5'&&s.v5_state?' · '+s.v5_state:''):'Idle');
    text(card.querySelector('.switchcol small'),archived?'Legacy':scheduled?'Scheduled':owns?(s.state==='HEDGE_HOLD'?'Hedge held':s.stop_requested?'Exit pending':s.state||'Idle'):'Off');
    card.title=scheduled?'Starts '+planned.scheduled_for+' at '+spec.entry_start+' IST':owns?(s.pattern?'Pattern '+s.pattern+' · score '+s.pattern_score+' · stop '+s.underlying_stop+' · target '+s.underlying_target+' · '+(s.reason||''):(s.reason||'')):archived?'This version is archived in the current controller':'';
    }
  }
}
async function toggleStrategy(id,on){
  const input=$('toggle-'+id);if(!status){input.checked=false;return}
  const selectedMarket=market(id);if(!selectedMarket){notify('Strategy is no longer available.',true);input.checked=false;return}
  if(!on){const ok=await change('/api/stop',{market:selectedMarket,strategy_id:id},id+' switched off; any pending exit remains visible.');if(!ok)input.checked=true;return}
  const multiplier=Number($('mult-'+id).value),capital=Number(status.account?.configured_capital||0);
  if(!Number.isInteger(multiplier)||multiplier<1||multiplier>100||!Number.isFinite(capital)||capital<multiplier*200000){notify('Save account capital in Settings; each multiplier needs at least ₹2,00,000.',true);input.checked=false;return}
  const mode=$('paper-'+id).checked?'paper':'live',payload={market:selectedMarket,strategy_id:id,mode,multiplier,capital};
  if(mode==='live'){
    if(!status.live_permission){notify('Enable account-wide live permission in Settings first.',true);input.checked=false;return}
    const pin=await requestPin('Confirm live request',strategyFullName(id)+' · '+multiplier+'× · real orders would be placed if a live executor is available.');
    if(!pin){input.checked=false;return}payload.pin=pin;
  }
  const ok=await change('/api/start',payload,id+' '+mode+' request accepted; the card shows its start time.');if(!ok)input.checked=false;
}
function monthKey(offset){const now=new Date(),ist=new Date(now.toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));ist.setDate(1);ist.setMonth(ist.getMonth()+offset);return ist.getFullYear()+'-'+String(ist.getMonth()+1).padStart(2,'0')}
function selectedRows(id){const key=monthKey(monthOffset);return(status?.strategy_history||[]).filter(x=>x.strategy_id===id&&String(x.date||'').startsWith(key)).sort((a,b)=>a.date.localeCompare(b.date))}
function renderHistory(){const box=$('history-cards');box.replaceChildren();const records=status?.strategy_history||[],ids=strategyIds(),marketOf=id=>strategy(id)?.market||records.find(row=>row.strategy_id===id)?.market||'OTHER';for(const group of marketOrder(ids.map(marketOf))){const groupIds=ids.filter(id=>marketOf(id)===group),cards=marketSection(box,group,groupIds.length,'historygrid');for(const id of groupIds){const rows=selectedRows(id),total=rows.reduce((sum,r)=>sum+(Number(r.net_pnl)||0),0),capital=Math.max(0,...rows.map(r=>Number(r.capital)||0)),card=node('button','card historycard');card.type='button';card.append(node('h3','',strategyLabel(id)),node('div','value',signed(total)));color(card.querySelector('.value'),total);const foot=node('div','foot');foot.append(node('span','',rows.length+' recorded day'+(rows.length===1?'':'s')));const ret=node('span');if(capital)ret.append(pctLabel((total/capital*100).toFixed(2)+'%'),node('span','',' return'));else ret.textContent='—';foot.append(ret);card.append(foot);card.onclick=()=>openHistory(id);cards.append(card)}}}
function openHistory(id){const rows=selectedRows(id),box=$('dialog-days');text($('dialog-title'),strategyFullName(id)+' · '+(monthOffset===0?'This month':'Last month'));box.replaceChildren();if(!rows.length)box.append(node('div','empty','No strategy-specific records for this month.'));for(const r of rows){const line=node('div','dailyrow'),amount=node('strong','');amount.append(node('span','',signed(r.net_pnl)),pctLabel(percent(r.net_pnl,r.capital)));line.append(node('span','',r.date+' · '+(r.mode||'paper').toUpperCase()+' · '+r.entries+' entr'+(r.entries===1?'y':'ies')),amount);color(amount,r.net_pnl);box.append(line)}const total=rows.reduce((n,r)=>n+(Number(r.net_pnl)||0),0),capital=Math.max(0,...rows.map(r=>Number(r.capital)||0));text($('dialog-total'),signed(total));color($('dialog-total'),total);const returnEl=$('dialog-return');if(capital){returnEl.textContent='('+((total/capital)*100).toFixed(2)+'%) of highest configured capital';returnEl.classList.add('pnl-percent')}else{text(returnEl,'No return calculated');returnEl.classList.remove('pnl-percent')}$('history-dialog').showModal()}
$('dialog-x').onclick=$('dialog-close').onclick=()=>$('history-dialog').close();$('month-current').onclick=()=>setMonth(0);$('month-previous').onclick=()=>setMonth(-1);function setMonth(value){monthOffset=value;$('month-current').classList.toggle('active',value===0);$('month-previous').classList.toggle('active',value===-1);if(status)renderHistory()}
async function loadChain(){if(activeTab!=='chain'||!status)return;try{chain=await api('/api/chain');renderChain()}catch(err){text($('chain-state'),'Unavailable');text($('chain-note'),err.message)}}
function fmt(n){return n!==null&&n!==undefined&&n!==''&&Number.isFinite(Number(n))?Number(n).toFixed(2):'—'}
function renderChain(){text($('chain-spot'),fmt(chain.spot));text($('chain-expiry'),chain.expiry||'—');text($('chain-state'),chain.ready?'Streaming':'Waiting');text($('chain-note'),(chain.reason||chain.source||'Broker feed pending')+' · Rows show actual quote age; blank prices mean unavailable.');const box=$('chain-rows');box.replaceChildren();if(!chain.rows?.length){const tr=node('tr');const td=node('td','empty',chain.reason||'No fresh contracts');td.colSpan=9;tr.append(td);box.append(tr);return}const closest=chain.rows.reduce((a,b)=>Math.abs(a.strike-chain.spot)<Math.abs(b.strike-chain.spot)?a:b).strike;for(const r of chain.rows){const tr=node('tr',r.strike===closest?'atm':'');const ce=r.ce||{},pe=r.pe||{};for(const [value,cls] of [[fmt(ce.bid),ce.stale?'stale':''],[fmt(ce.ask),ce.stale?'stale':''],[fmt(ce.last),ce.stale?'stale':''],[ce.age_seconds==null?'—':ce.age_seconds+'s',ce.stale?'stale':''],[r.strike,'strike'],[pe.age_seconds==null?'—':pe.age_seconds+'s',pe.stale?'stale':''],[fmt(pe.last),pe.stale?'stale':''],[fmt(pe.bid),pe.stale?'stale':''],[fmt(pe.ask),pe.stale?'stale':'']])tr.append(node('td',cls,value));box.append(tr)}}
 $('emergency').onclick=async()=>{if(!confirm('Emergency stop all strategies? Open positions may need fresh quotes to exit.'))return;await change('/api/kill',{},'Emergency stop requested. Verify every position reaches zero.')};$('logout').onclick=async()=>{try{await api('/api/logout',{})}catch{}authRevision++;loginRequired=true;status=null;$('login').hidden=false;$('pin').focus()};
$('save-capital').onclick=async()=>{const capital=Number($('capital').value);if(!Number.isFinite(capital)||capital<200000){notify('Enter account capital of at least ₹2,00,000.',true);return}await change('/api/settings',{capital},'Shared account capital saved.')};
$('live-toggle').onchange=async e=>{const enabled=e.target.checked;let pin;if(enabled){pin=await requestPin('Allow live requests','Enter your dashboard PIN to enable account-wide live permission. This does not itself place orders.');if(!pin){e.target.checked=false;return}}const payload={live_permission:enabled};if(enabled)payload.pin=pin;const ok=await change('/api/settings',payload,'Live request permission '+(enabled?'enabled.':'disabled.'));if(!ok)e.target.checked=!enabled};
async function loadBrokerAuth(){if(!status)return;try{const data=await api('/api/auth/status');const state=!data.configured?'Credentials unavailable':data.saved_today?'Token saved today':data.token_present?'Renew token':'Login needed';text($('broker-auth-state'),state);text($('broker-auth-time'),data.last_updated?'Saved '+dateIST(data.last_updated):'No token saved');const link=$('broker-auth-link');link.hidden=!data.configured||!data.auth_url;$('broker-auth-submit').disabled=!data.configured}catch(err){text($('broker-auth-state'),'Status unavailable');text($('broker-auth-message'),err.message)}}
let brokerTokenBusy=false;
async function syncBrokerToken(){if(brokerTokenBusy)return;const code=$('broker-auth-code').value.trim();if(!code){text($('broker-auth-message'),'Paste the redirect URL or request code first.');return}brokerTokenBusy=true;const button=$('broker-auth-submit');button.disabled=true;text($('broker-auth-message'),'Exchanging request code with Flattrade…');try{await api('/api/auth/token',{url_or_code:code});$('broker-auth-code').value='';text($('broker-auth-message'),'Token saved. The read-only option feed will reconnect using it.');notify('Flattrade token saved on the server.');await loadBrokerAuth()}catch(err){text($('broker-auth-message'),err.message);notify(err.message,true)}finally{brokerTokenBusy=false;button.disabled=false}}
$('broker-auth-submit').onclick=syncBrokerToken;
$('broker-auth-code').addEventListener('paste',()=>setTimeout(syncBrokerToken,0));
function metricCard(label,value,tone=0){const card=node('div','card');card.append(node('small','',label),node('strong','',value));color(card.lastChild,tone);return card}
function plot(values,lineId,zeroId){const line=$(lineId),zero=$(zeroId);if(!values.length){line.setAttribute('points','');zero.setAttribute('y1','120');zero.setAttribute('y2','120');return}const low=Math.min(0,...values),high=Math.max(0,...values),span=Math.max(high-low,1),y=v=>220-(v-low)*200/span;zero.setAttribute('y1',String(y(0)));zero.setAttribute('y2',String(y(0)));line.setAttribute('points',values.map((value,index)=>(12+index*776/Math.max(values.length-1,1)).toFixed(1)+','+y(value).toFixed(1)).join(' '));line.style.stroke=values.at(-1)<0?'var(--red)':'var(--teal)'}
async function loadAnalytics(){if(activeTab!=='analytics'||!status)return;try{analyticsSummary=await api('/api/analytics/summary');renderAnalytics()}catch(err){text($('analytics-note'),err.message)}}
function renderAnalytics(){
  const data=analyticsSummary||{};
  const daily=(data.daily||[]).filter(row=>row.day.startsWith(monthKey(0))).reverse();
  const total=daily.reduce((sum,row)=>sum+Number(row.pnl||0),0),days=daily.length;
  const cumulative=[];daily.reduce((sum,row)=>{const next=sum+Number(row.pnl||0);cumulative.push(next);return next},0);
  plot(cumulative,'combined-line','combined-zero');text($('combined-month'),monthKey(0));
  const metrics=$('combined-metrics');
  metrics.replaceChildren(metricCard('Month P&L',signed(total),total),metricCard('Recorded days',days),
    metricCard('Positive days',daily.filter(row=>row.pnl>0).length),
    metricCard('Average daily return',days?(daily.reduce((sum,row)=>sum+Number(row.return_pct||0),0)/days).toFixed(2)+'%':'—'));
  text($('combined-note'),days?'Paper P&L after modeled costs.':'No paper strategy days recorded this month.');
  text($('analytics-updated'),data.last_capture?'Updated '+dateIST(data.last_capture):'Collector waiting');
  const cards=$('analytics-strategy-cards');cards.replaceChildren();
  const allSpecs=status.strategies||[];
  for(const group of marketOrder(allSpecs.map(spec=>spec.market))){
    const specs=allSpecs.filter(spec=>spec.market===group),groupCards=marketSection(cards,group,specs.length,'analytics-card-grid');
    for(const spec of specs){
      const row=(data.strategy_rollup||[]).find(item=>item.strategy_id===spec.id),card=node('button','card analytics-card');
      card.type='button';card.append(node('small','',spec.market+' · paper'),node('strong','',strategyLabel(spec.id)),
        node('div','value',row?signed(row.total_pnl):'No recorded sessions'),
        node('small','',row?row.days+' days · '+row.win_days+' positive · mean daily '+row.mean_daily_return_pct.toFixed(2)+'%':'Start this strategy in paper mode to collect results'));
      if(row)color(card.querySelector('.value'),row.total_pnl);card.onclick=()=>openDetail(spec.id);groupCards.append(card);
    }
  }
}
function detailSessions(){return Object.entries(status?.sessions||{}).filter(([key,session])=>
  (session.strategy_id===detailStrategy||key===detailStrategy||key===detailStrategy+':legacy')&&Array.isArray(session.positions)).map(([,session])=>session)}
function detailCapital(sessions,month){
  const historical=(status?.strategy_history||[]).filter(row=>row.strategy_id===detailStrategy&&String(row.date||'').startsWith(month));
  return Math.max(0,...historical.map(row=>Number(row.capital)||0),...sessions.filter(session=>String(session.date||'').startsWith(month)).map(session=>Number(session.capital)||0))||Number(status?.account?.configured_capital)||0;
}
function closedLegs(trades){
  const inventory=new Map(),closed=[];
  for(const fill of trades){
    const side=String(fill.side||'').toUpperCase(),symbol=String(fill.symbol||'');
    let remaining=Number(fill.quantity)||0;
    const price=Number(fill.price),cost=Number(fill.cost)||0;
    if(!symbol||!['BUY','SELL'].includes(side)||remaining<=0||!Number.isFinite(price))continue;
    const fullQuantity=remaining,queue=inventory.get(symbol)||[];
    while(remaining>0&&queue.length&&queue[0].side!==side){
      const entry=queue[0],quantity=Math.min(remaining,entry.remaining);
      const gross=(entry.side==='SELL'?entry.price-price:price-entry.price)*quantity;
      const net=gross-entry.costPerUnit*quantity-cost*quantity/fullQuantity;
      closed.push({ts:fill.ts||fill.timestamp,side:entry.side,symbol,quantity,entryPrice:entry.price,exitPrice:price,pnl:net});
      entry.remaining-=quantity;remaining-=quantity;
      if(entry.remaining<=0)queue.shift();
    }
    if(remaining>0)queue.push({side,price,remaining,costPerUnit:cost/fullQuantity});
    inventory.set(symbol,queue);
  }
  return closed.reverse();
}
function renderClosedLegs(){
  const sessions=detailSessions(),today=status?.today,selected=detailDay||(detailMonth===monthKey(0)?today:detailData?.day);
  const live=selected===today;
  const trades=live?sessions.flatMap(session=>session.date===today?(session.trades||[]):[]):detailData?.day===selected?(detailData.trades||[]):[];
  const resetAt=live?Math.max(0,...sessions.filter(session=>session.date===today).map(session=>Date.parse(session.pnl_reset_at||'')||0)):0;
  const legs=closedLegs([...trades].sort((a,b)=>String(a.ts||a.timestamp||'').localeCompare(String(b.ts||b.timestamp||'')))).filter(leg=>!resetAt||(Date.parse(leg.ts||'')||0)>=resetAt);
  const capital=detailCapital(sessions,detailMonth||monthKey(0)),table=$('detail-closed');table.replaceChildren();
  text($('detail-closed-count'),legs.length+' closed');
  text($('detail-closed-state'),selected?selected+' · '+(live?'updating every second':'recorded fills'):'Select a day below');
  if(!legs.length){const row=node('tr'),cell=node('td','empty',trades.length?'No matched closed legs for this day.':'No closed legs for this day.');cell.colSpan=7;row.append(cell);table.append(row);return}
  for(const leg of legs){const row=node('tr');for(const value of [dateIST(leg.ts),leg.side,leg.symbol,leg.quantity,money(leg.entryPrice),money(leg.exitPrice)])row.append(node('td','',value));const result=node('td');result.append(node('span','',signed(leg.pnl)),pctLabel(percent(leg.pnl,capital)));color(result,leg.pnl);row.append(result);table.append(row)}
}
function openDetail(id,push=true){if(!strategy(id))return;detailStrategy=id;detailMonth=monthKey(0);detailDay=status?.today||null;detailData=null;if(push)window.history?.pushState?.({},'', '/strategy/'+id);tab('strategy-detail');renderDetailPositions()}
function renderDetailPositions(){
  if(!status||!detailStrategy)return;
  const matches=detailSessions(),open=matches.flatMap(session=>session.positions.map(position=>({position,session})));
  const table=$('detail-positions');table.replaceChildren();
  text($('detail-open-count'),open.length+' open');
  const scheduled=status.schedules?.[detailStrategy];
  const current=matches.find(session=>session.positions.length)||matches[0];
  const entryFills=(current?.trades||[]).filter(fill=>fill.reason==='ENTRY'&&fill.side==='SELL');
  const lastEntry=entryFills.at(-1)?.timestamp;
  const entryLegs=lastEntry?entryFills.filter(fill=>fill.timestamp===lastEntry).length:0;
  const origin=entryLegs===2?'Opened 2-leg ATM straddle at '+dateIST(lastEntry)+' · ':'';
  text($('detail-open-state'),open.length?origin+open.length+' leg'+(open.length===1?' remains':'s open')+' · '+(current?.state||'open'):
    scheduled?'Paper session scheduled for '+scheduled.scheduled_for:
    current?(current.state||'Idle')+(current.reason?' · '+current.reason:''):'No paper session running');
  if(!open.length){const row=node('tr'),cell=node('td','empty','No open positions for this strategy.');cell.colSpan=8;row.append(cell);table.append(row);text($('detail-open-note'),scheduled?'Positions will appear here when its session starts.':'Open legs appear here as soon as a paper basket enters.')}else{
    for(const {position:p,session} of open){
      const row=node('tr');
      const stop=premiumStop(p);
      const values=[p.side,p.symbol,p.quantity,money(p.entry_price),money(p.mark_price),money(p.best_mark??p.entry_price),stop.label+' · '+stop.value];
      for(const value of values)row.append(node('td','',value));
      const result=node('td');result.append(node('span','',signed(p.unrealized_pnl)),pctLabel(percent(p.unrealized_pnl,session.capital)));color(result,p.unrealized_pnl);row.append(result);table.append(row);
    }
    const stale=matches.some(session=>session.positions.length&&session.valuation_stale);
    text($('detail-open-note'),stale?'Some marks are stale; displayed leg P&L is the last known paper valuation.':'Leg P&L is simulated mark-to-market before basket-level costs. Percentages use the strategy allocation.');
  }
  renderClosedLegs();
  renderDetailMetrics();
}
function renderDetailMetrics(){
  const data=detailData||{},rows=data.daily||[],sessions=detailSessions(),today=status?.today;
  const current=sessions.find(session=>session.date===today&&session.state&&!['STOPPED','SESSION_COMPLETE'].includes(session.state))||sessions.find(session=>session.date===today);
  const capital=detailCapital(sessions,data.month||detailMonth||monthKey(0));
  const todayPnl=current?Number(current.net_pnl)||0:Number(rows.find(row=>row.day===today)?.pnl)||0;
  let monthPnl=rows.reduce((sum,row)=>sum+(Number(row.pnl)||0),0);
  if(current&&String(today||'').startsWith(data.month||detailMonth||monthKey(0)))monthPnl+=todayPnl-(Number(rows.find(row=>row.day===today)?.pnl)||0);
  const unrealized=current?Number(current.unrealized_pnl)||0:0;
  const realized=todayPnl-unrealized;
  const metric=(label,value,n)=>{const card=node('div','card'),title=node('small','',label),amount=node('strong','');amount.append(node('span','',signed(value)),pctLabel(percent(value,n)));color(amount,value);card.append(title,amount);return card};
  $('detail-metrics').replaceChildren(metric('Today P&L',todayPnl,current?.capital||capital),metric('Closed + costs',realized,current?.capital||capital),metric('Open P&L',unrealized,current?.capital||capital),metric('Month P&L',monthPnl,capital));
}
async function loadDetail(){if(activeTab!=='strategy-detail'||!detailStrategy||!status)return;try{const query='?month='+encodeURIComponent(detailMonth||monthKey(0))+(detailDay?'&day='+encodeURIComponent(detailDay):'');detailData=await api('/api/analytics/strategy/'+encodeURIComponent(detailStrategy)+query);renderDetail()}catch(err){text($('detail-chart-note'),err.message)}}
function renderDetail(){
  const data=detailData||{},rows=data.daily||[],selected=rows.find(row=>row.day===data.day),title=(data.strategy_id?strategyFullName(data.strategy_id):'Strategy');
  text($('detail-title'),title);
  text($('detail-subtitle'),(data.market||'—')+' · independent paper ledger · '+(strategy(data.strategy_id)?.description||''));
  text($('detail-month'),data.month||'—');
  renderDetailMetrics();renderClosedLegs();
  plot((data.curve||[]).map(row=>Number(row.pnl||0)),'detail-line','detail-zero');
  text($('detail-day-label'),data.day||'No selected day');
  text($('detail-chart-note'),data.intraday_available?data.curve.length+' plotted samples · net simulated P&L after modeled costs':selected?'Daily ledger exists; intraday curve was not recorded for this older session.':'No paper session in this month.');
  const table=$('detail-daily');table.replaceChildren();
  if(!rows.length){const tr=node('tr'),td=node('td','empty','No daily results in this month.');td.colSpan=4;tr.append(td);table.append(tr)}
  for(const item of [...rows].reverse()){
    const tr=node('tr');tr.dataset.day=item.day;
    for(const value of [item.day,signed(item.pnl)])tr.append(node('td','',value));const ret=node('td');ret.append(pctLabel(Number(item.return_pct).toFixed(2)+'%'));tr.append(ret,node('td','',item.entries));
    color(tr.children[1],item.pnl);tr.onclick=()=>{detailDay=item.day;loadDetail()};
    if(item.day===data.day)tr.style.background='var(--panel2)';table.append(tr);
  }
}
function shiftDetailMonth(offset){const [year,month]=(detailMonth||monthKey(0)).split('-').map(Number),date=new Date(year,month-1+offset,1);detailMonth=date.getFullYear()+'-'+String(date.getMonth()+1).padStart(2,'0');detailDay=detailMonth===monthKey(0)?status?.today||null:null;loadDetail()}
$('detail-back').onclick=()=>tab('analytics');$('detail-prev').onclick=()=>shiftDetailMonth(-1);$('detail-next').onclick=()=>shiftDetailMonth(1);window.addEventListener('popstate',()=>{const id=location.pathname.startsWith('/strategy/')?location.pathname.slice('/strategy/'.length):'';if(strategy(id))openDetail(id,false);else tab('analytics')});
async function poll(){if(!document.hidden)await loadStatus();setTimeout(poll,document.hidden?15000:1000)}async function pollChain(){if(!document.hidden)await loadChain();setTimeout(pollChain,document.hidden?10000:1000)}
async function pollAnalytics(){if(!document.hidden&&activeTab==='analytics')await loadAnalytics();if(!document.hidden&&activeTab==='strategy-detail'&&detailData?.day===status?.today)await loadDetail();setTimeout(pollAnalytics,15000)}
async function pollBrokerAuth(){if(!document.hidden&&activeTab==='settings')await loadBrokerAuth();setTimeout(pollBrokerAuth,60000)}
poll();pollChain();pollAnalytics();pollBrokerAuth();
})();
