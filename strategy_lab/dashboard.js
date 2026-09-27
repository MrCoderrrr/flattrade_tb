(()=>{'use strict';
const ids=['nfv1','nfv2','nfv3','mcxv1','mcxv2','mcxv3'];
const labels={nfv1:'Original NIFTY · KAMA / ADX',nfv2:'Selective NIFTY range spreads',nfv3:'Active NIFTY indicators · hedged',mcxv1:'Original MCX · KAMA / EMA',mcxv2:'Selective MCX trend spreads',mcxv3:'ATM straddle · KAMA / EMA · leg stops'};
const $=id=>document.getElementById(id), money=n=>Number.isFinite(Number(n))?'₹'+Number(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
const signed=n=>{const v=Number(n)||0;return (v>0?'+':'')+money(v)};
const percent=(n,capital)=>Number(capital)>0?((Number(n)||0)/Number(capital)*100).toFixed(2)+'%':'—';
const color=(el,n)=>{el.classList.toggle('positive',Number(n)>0);el.classList.toggle('negative',Number(n)<0);el.classList.toggle('neutral',Number(n)===0)};
const text=(el,value)=>{const s=String(value??'—');if(el.textContent!==s)el.textContent=s};
let status=null,chain=null,monthOffset=0,busy=false,activeTab='dashboard',toastTimer;
let pinResolver=null;
function requestPin(title,explanation){if(pinResolver)return Promise.resolve(null);text($('pin-title'),title);text($('pin-explanation'),explanation);$('action-pin').value='';$('pin-dialog').showModal();$('action-pin').focus();return new Promise(resolve=>pinResolver=resolve)}
function closePin(value){$('pin-dialog').close();if(pinResolver){const resolve=pinResolver;pinResolver=null;resolve(value)}}
function submitPin(){const value=$('action-pin').value;if(!/^\d{4}$/.test(value)){text($('pin-explanation'),'Enter exactly four digits to continue.');$('action-pin').focus();return}closePin(value)}$('pin-submit').onclick=submitPin;$('action-pin').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();submitPin()}};$('pin-cancel').onclick=$('pin-cancel-x').onclick=()=>closePin(null);$('pin-dialog').oncancel=e=>{e.preventDefault();closePin(null)};
function notify(message,error=false){const el=$('toast');text(el,message);el.classList.toggle('error',error);el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,5000)}
async function api(path,payload){const options={credentials:'same-origin',cache:'no-store',headers:{}};if(payload!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(payload)}const response=await fetch(path,options);let data={};try{data=await response.json()}catch{}if(!response.ok){const e=new Error(data.error||'Server request failed');e.status=response.status;throw e}return data}
function themePreference(){try{return window.localStorage.getItem('desk-theme')}catch{return null}}
function applyTheme(name){document.documentElement.dataset.theme=name;try{window.localStorage.setItem('desk-theme',name)}catch{}$('light-toggle').checked=name==='light'}
applyTheme(themePreference()==='light'?'light':'dark');
function tab(name){activeTab=name;document.querySelectorAll('.nav button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===name)));document.querySelectorAll('.page').forEach(p=>p.classList.toggle('active',p.id==='page-'+name));if(name==='chain')loadChain()}
document.querySelectorAll('.nav button').forEach(b=>b.addEventListener('click',()=>tab(b.dataset.tab)));
$('theme').onclick=()=>applyTheme(document.documentElement.dataset.theme==='light'?'dark':'light');$('light-toggle').onchange=e=>applyTheme(e.target.checked?'light':'dark');
async function submitLogin(){const pin=$('pin').value;if(!/^\d{4}$/.test(pin)){text($('login-error'),'Enter exactly four digits.');return}try{await api('/api/login',{pin});$('pin').value='';$('login-error').textContent='';$('login').hidden=true;await loadStatus();if($('login').hidden===false)text($('login-error'),'PIN accepted, but the browser session did not persist. Refresh and try again.')}catch(err){text($('login-error'),err.message)}}$('login-submit').onclick=submitLogin;$('pin').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();submitLogin()}};
async function loadStatus(){try{const data=await api('/api/status');status=data;$('login').hidden=true;$('connection-dot').classList.add('on');text($('connection'),'Connected');render()}catch(err){$('connection-dot').classList.remove('on');text($('connection'),'Offline');if(err.status===401){$('login').hidden=false;status=null}else notify(err.message,true)}}
async function change(path,payload,success){if(busy)return false;busy=true;try{await api(path,payload);notify(success);await loadStatus();return true}catch(err){notify(err.message,true);await loadStatus();return false}finally{busy=false}}
function market(id){return id.startsWith('nf')?'NIFTY':'MCX'}
function dateIST(value){if(!value)return'—';const d=new Date(value);return Number.isNaN(d.getTime())?'—':new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false}).format(d)}
function node(tag,cls,value){const e=document.createElement(tag);if(cls)e.className=cls;if(value!==undefined)e.textContent=String(value);return e}
function render(){const a=status.account||{}, sessions=status.sessions||{},capital=a.configured_capital||a.capital; text($('asof'),'Updated '+dateIST(status.server_time));text($('today-pnl'),signed(a.daily_pnl));color($('today-pnl'),a.daily_pnl);text($('today-return'),percent(a.daily_pnl,capital));color($('today-return'),a.daily_pnl);text($('account-capital'),money(capital));
const schedules=status.schedules||{},open=[];let active=0;for(const m of ['NIFTY','MCX']){const s=sessions[m]||{};if((s.state&&!['STOPPED','SESSION_COMPLETE'].includes(s.state))||schedules[m])active++;for(const p of s.positions||[])open.push({market:m,strategy:s.strategy_id,mode:s.mode,...p})}
text($('active-count'),active);text($('active-badge'),active+' ON');const activeBox=$('active-strategy-cards');activeBox.replaceChildren();for(const m of ['NIFTY','MCX']){const s=sessions[m]||{},planned=schedules[m],running=s.state&&!['STOPPED','SESSION_COMPLETE'].includes(s.state);if(!running&&!planned)continue;const card=node('div','active-strategy'),left=node('div'),right=node('div','value'),spec=(status.strategies||[]).find(x=>x.id===planned?.strategy_id);left.append(node('strong','',(running?s.strategy_id:planned.strategy_id).toUpperCase()),node('small','',running?m+' · '+(s.mode||'paper').toUpperCase()+' · '+s.state:m+' · PAPER · SCHEDULED '+planned.scheduled_for+' '+(spec?.entry_start||'')+' IST'));right.append(node('div','',running?signed(s.net_pnl):'Waiting'),node('small','',running?percent(s.net_pnl,s.capital||capital)+' return':'Starts in its session window'));if(running)color(right,s.net_pnl);card.append(left,right);activeBox.append(card)}if(!active)activeBox.append(node('div','empty','No strategy is on right now.'));
text($('open-count'),open.length);text($('positions-badge'),open.length+' OPEN');const box=$('active-trades');box.replaceChildren();if(!open.length){box.className='empty';box.textContent='No open positions right now.'}else{box.className='';for(const p of open){const row=node('div','trade');const name=node('div','');name.append(node('strong','',p.symbol||'Option'),node('small','',p.strategy+' · '+p.market+' · '+(p.mode||'paper').toUpperCase()));const pnl=node('div','right');pnl.append(node('div','',signed(p.unrealized_pnl)),node('small','',percent(p.unrealized_pnl,capital)));row.append(name,node('div','',p.side+' · '+p.quantity),node('div','pricecol',money(p.mark_price)),pnl);color(pnl,p.unrealized_pnl);box.append(row)}}
const events=(status.events||[]).slice(-8).reverse(),evbox=$('events');evbox.replaceChildren();if(!events.length)evbox.append(node('div','empty','No activity recorded yet.'));for(const e of events){const r=node('div','event');r.append(node('time','',dateIST(e.timestamp)),node('span','',e.message));evbox.append(r)}
text($('live-reason'),status.live_permission?'Permission is ON. '+(status.live_reason||'Broker execution unavailable'):'Permission is OFF. Enable it here before requesting live trading.');$('live-toggle').checked=!!status.live_permission;$('live-toggle').disabled=busy;
if(document.activeElement!==$('capital'))$('capital').value=String(capital||200000);
renderStrategies();renderHistory();}
function renderStrategies(){
  const list=$('strategy-list'),sessions=status.sessions||{},schedules=status.schedules||{},specs=new Map((status.strategies||[]).map(x=>[x.id,x]));
  for(const id of ids){
    let card=$('card-'+id);
    if(!card){
      card=node('article','card strategy');card.id='card-'+id;
      const name=node('div','namecol');name.append(node('div','name',id.toUpperCase()),node('div','desc',labels[id]));
      const size=node('div','sizecol');size.append(node('div','metriclabel','Multiplier'));
      const inp=node('input');inp.type='number';inp.min='1';inp.max='100';inp.step='1';inp.value='1';inp.id='mult-'+id;inp.setAttribute('aria-label',id+' multiplier');size.append(inp);
      const pnlcol=node('div','pnlcol');pnlcol.append(node('div','metriclabel','Today P&L / return'),node('div','metricval'));
      const feed=node('div','feedcol');feed.append(node('div','metriclabel','Session'),node('div','metricval'));
      const mode=node('div','modecol');const paperLabel=node('label');const paper=node('input');paper.type='checkbox';paper.checked=true;paper.id='paper-'+id;paper.setAttribute('aria-label',id+' paper trading');paperLabel.append(paper,node('span','','Paper'));mode.append(paperLabel,node('small','','Uncheck for live'));
      const sw=node('div','switchcol');const label=node('label','switch'),input=node('input');input.type='checkbox';input.id='toggle-'+id;input.setAttribute('aria-label','Start or stop '+id);label.append(input,node('span'));sw.append(label,node('small','',''));
      card.append(name,size,pnlcol,feed,mode,sw);list.append(card);input.onchange=()=>toggleStrategy(id,input.checked);
    }
    const spec=specs.get(id)||{},s=sessions[market(id)]||{},planned=schedules[market(id)],scheduled=planned?.strategy_id===id,owns=s.strategy_id===id&&!!s.date;
    const working=scheduled||(owns&&!['STOPPED','SESSION_COMPLETE'].includes(s.state)&&!s.stop_requested),archived=spec.enabled===false,competing=(planned&&!scheduled)||(!owns&&s.state&&!['STOPPED','SESSION_COMPLETE'].includes(s.state));
    const sw=$('toggle-'+id),inp=$('mult-'+id),paper=$('paper-'+id);sw.checked=working;sw.disabled=archived||busy||competing||(owns&&s.state==='EXIT_PENDING');inp.disabled=archived||working||busy||competing;paper.disabled=archived||working||busy||competing;
    if(working)paper.checked=(scheduled?planned.mode:s.mode)!=='live';
    const badge=card.querySelector('.name span');if(!badge)card.querySelector('.name').append(node('span','',archived?'ARCHIVED':id.endsWith('v3')?'ACTIVE':'PAPER'));else text(badge,archived?'ARCHIVED':id.endsWith('v3')?'ACTIVE':'PAPER');
    if((owns||scheduled)&&document.activeElement!==inp)inp.value=String(scheduled?planned.multiplier:s.multiplier||1);
    const pnl=card.querySelector('.pnlcol .metricval'),capital=s.capital||status.account?.configured_capital;
    text(pnl,owns?signed(s.net_pnl)+' · '+percent(s.net_pnl,capital):'—');color(pnl,owns?s.net_pnl:0);
    text(card.querySelector('.feedcol .metricval'),scheduled?'Scheduled '+planned.scheduled_for:owns?(s.state||'—'):'Idle');
    text(card.querySelector('.switchcol small'),archived?'Legacy':scheduled?'Scheduled':owns?(s.stop_requested?'Exit pending':s.state||'Idle'):'Off');
    card.title=scheduled?'Starts '+planned.scheduled_for+' at '+spec.entry_start+' IST':owns?(s.reason||''):archived?'Original version is archived in the current controller':'';
  }
}
async function toggleStrategy(id,on){
  const input=$('toggle-'+id);if(!status){input.checked=false;return}
  if(!on){const ok=await change('/api/stop',{market:market(id)},id+' switched off; any pending exit remains visible.');if(!ok)input.checked=true;return}
  const multiplier=Number($('mult-'+id).value),capital=Number(status.account?.configured_capital||0);
  if(!Number.isInteger(multiplier)||multiplier<1||multiplier>100||!Number.isFinite(capital)||capital<multiplier*200000){notify('Save account capital in Settings; each multiplier needs at least ₹2,00,000.',true);input.checked=false;return}
  const mode=$('paper-'+id).checked?'paper':'live',payload={market:market(id),strategy_id:id,mode,multiplier,capital};
  if(mode==='live'){
    if(!status.live_permission){notify('Enable account-wide live permission in Settings first.',true);input.checked=false;return}
    const pin=await requestPin('Confirm live request',id.toUpperCase()+' · '+multiplier+'× · real orders would be placed if a live executor is available.');
    if(!pin){input.checked=false;return}payload.pin=pin;
  }
  const ok=await change('/api/start',payload,id+' '+mode+' request accepted; the card shows its start time.');if(!ok)input.checked=false;
}
function monthKey(offset){const now=new Date(),ist=new Date(now.toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));ist.setDate(1);ist.setMonth(ist.getMonth()+offset);return ist.getFullYear()+'-'+String(ist.getMonth()+1).padStart(2,'0')}
function selectedRows(id){const key=monthKey(monthOffset);return(status?.strategy_history||[]).filter(x=>x.strategy_id===id&&String(x.date||'').startsWith(key)).sort((a,b)=>a.date.localeCompare(b.date))}
function renderHistory(){const box=$('history-cards');box.replaceChildren();for(const id of ids){const rows=selectedRows(id),total=rows.reduce((sum,r)=>sum+(Number(r.net_pnl)||0),0),capital=Math.max(0,...rows.map(r=>Number(r.capital)||0)),card=node('button','card historycard');card.type='button';card.append(node('h3','',id.toUpperCase()),node('div','value',signed(total)));color(card.querySelector('.value'),total);const foot=node('div','foot');foot.append(node('span','',rows.length+' recorded day'+(rows.length===1?'':'s')),node('span','',capital?(total/capital*100).toFixed(2)+'% return':'—'));card.append(foot);card.onclick=()=>openHistory(id);box.append(card)}}
function openHistory(id){const rows=selectedRows(id),box=$('dialog-days');text($('dialog-title'),id.toUpperCase()+' · '+(monthOffset===0?'This month':'Last month'));box.replaceChildren();if(!rows.length)box.append(node('div','empty','No strategy-specific records for this month.'));for(const r of rows){const line=node('div','dailyrow');line.append(node('span','',r.date+' · '+(r.mode||'paper').toUpperCase()+' · '+r.entries+' entr'+(r.entries===1?'y':'ies')),node('strong','',signed(r.net_pnl)+' · '+percent(r.net_pnl,r.capital)));color(line.lastChild,r.net_pnl);box.append(line)}const total=rows.reduce((n,r)=>n+(Number(r.net_pnl)||0),0),capital=Math.max(0,...rows.map(r=>Number(r.capital)||0));text($('dialog-total'),signed(total));color($('dialog-total'),total);text($('dialog-return'),capital?(total/capital*100).toFixed(2)+'% of highest configured capital':'No return calculated');$('history-dialog').showModal()}
$('dialog-x').onclick=$('dialog-close').onclick=()=>$('history-dialog').close();$('month-current').onclick=()=>setMonth(0);$('month-previous').onclick=()=>setMonth(-1);function setMonth(value){monthOffset=value;$('month-current').classList.toggle('active',value===0);$('month-previous').classList.toggle('active',value===-1);if(status)renderHistory()}
async function loadChain(){if(activeTab!=='chain'||!status)return;try{chain=await api('/api/chain');renderChain()}catch(err){text($('chain-state'),'Unavailable');text($('chain-note'),err.message)}}
function fmt(n){return n!==null&&n!==undefined&&n!==''&&Number.isFinite(Number(n))?Number(n).toFixed(2):'—'}
function renderChain(){text($('chain-spot'),fmt(chain.spot));text($('chain-expiry'),chain.expiry||'—');text($('chain-state'),chain.ready?'Streaming':'Waiting');text($('chain-note'),(chain.reason||chain.source||'Broker feed pending')+' · Rows show actual quote age; blank prices mean unavailable.');const box=$('chain-rows');box.replaceChildren();if(!chain.rows?.length){const tr=node('tr');const td=node('td','empty',chain.reason||'No fresh contracts');td.colSpan=9;tr.append(td);box.append(tr);return}const closest=chain.rows.reduce((a,b)=>Math.abs(a.strike-chain.spot)<Math.abs(b.strike-chain.spot)?a:b).strike;for(const r of chain.rows){const tr=node('tr',r.strike===closest?'atm':'');const ce=r.ce||{},pe=r.pe||{};for(const [value,cls] of [[fmt(ce.bid),ce.stale?'stale':''],[fmt(ce.ask),ce.stale?'stale':''],[fmt(ce.last),ce.stale?'stale':''],[ce.age_seconds==null?'—':ce.age_seconds+'s',ce.stale?'stale':''],[r.strike,'strike'],[pe.age_seconds==null?'—':pe.age_seconds+'s',pe.stale?'stale':''],[fmt(pe.last),pe.stale?'stale':''],[fmt(pe.bid),pe.stale?'stale':''],[fmt(pe.ask),pe.stale?'stale':'']])tr.append(node('td',cls,value));box.append(tr)}}
$('emergency').onclick=async()=>{if(!confirm('Emergency stop all strategies? Open positions may need fresh quotes to exit.'))return;await change('/api/kill',{},'Emergency stop requested. Verify every position reaches zero.')};$('logout').onclick=async()=>{try{await api('/api/logout',{})}catch{}status=null;$('login').hidden=false;$('pin').focus()};
$('save-capital').onclick=async()=>{const capital=Number($('capital').value);if(!Number.isFinite(capital)||capital<200000){notify('Enter account capital of at least ₹2,00,000.',true);return}await change('/api/settings',{capital},'Shared account capital saved.')};
$('live-toggle').onchange=async e=>{const enabled=e.target.checked;let pin;if(enabled){pin=await requestPin('Allow live requests','Enter your dashboard PIN to enable account-wide live permission. This does not itself place orders.');if(!pin){e.target.checked=false;return}}const payload={live_permission:enabled};if(enabled)payload.pin=pin;const ok=await change('/api/settings',payload,'Live request permission '+(enabled?'enabled.':'disabled.'));if(!ok)e.target.checked=!enabled};
async function poll(){if(!document.hidden)await loadStatus();setTimeout(poll,document.hidden?15000:2000)}async function pollChain(){if(!document.hidden)await loadChain();setTimeout(pollChain,document.hidden?10000:1000)}
poll();pollChain();
})();
