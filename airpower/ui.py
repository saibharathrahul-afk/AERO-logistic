HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Air Power - Fleet Readiness Decision Support</title>
<style>:root{--bg:#0b1220;--card:#131c2e;--tx:#e6ecf5;--mu:#8da0bd;--ok:#2ecc71;--bad:#ff5c5c;--warn:#f5b942;--ac:#4da3ff}*{box-sizing:border-box}
body{margin:0 auto;font:14px system-ui;background:var(--bg);color:var(--tx);padding:16px;max-width:1150px}h1{font-size:20px}h2{font-size:14px;color:var(--mu);margin:0 0 8px;text-transform:uppercase;letter-spacing:.05em}
.card{background:var(--card);border-radius:10px;padding:14px;margin-bottom:14px;overflow-x:auto}table{width:100%;border-collapse:collapse}td,th{padding:6px 8px;border-bottom:1px solid #22304a;text-align:left;vertical-align:top}
.pill{padding:2px 8px;border-radius:99px;font-size:12px}.bad{background:#4a1d1d;color:var(--bad)}.ok{background:#14361f;color:var(--ok)}.warn{background:#41330f;color:var(--warn)}
button:disabled{opacity:.5;cursor:wait}button,select,input{background:var(--ac);color:#04101f;border:0;border-radius:6px;padding:7px 12px;font-weight:600;margin:2px}button.s{background:#22304a;color:var(--tx)}input{width:70px;background:#0f1828;color:var(--tx);border:1px solid #22304a}select{background:#22304a;color:var(--tx)}
.kpi{display:flex;gap:12px;flex-wrap:wrap}.kpi div{flex:1;min-width:130px;background:#0f1828;border-radius:8px;padding:10px}.kpi b{font-size:22px;display:block}small,label{color:var(--mu)}</style></head><body>
<h1>Fleet Readiness Decision Support <small>· synthetic data · advisory only</small></h1>
<div class="card">Signed in as <select id="key" onchange="load()"><option value="demo-analyst">Analyst (view/plan)</option><option value="demo-officer" selected>Maintenance Officer (can approve)</option><option value="demo-commander">Commander (can approve)</option></select>
</div>
<div id="err" class="pill bad" style="display:none;padding:10px 14px;margin-bottom:14px;white-space:pre-wrap"></div>
<div class="card"><h2>Fleet &amp; maintenance tasks</h2><table id="fleet"></table><table id="tasks" style="margin-top:10px"></table></div>
<div class="card"><h2>Scenario controls (change inputs, then recompute)</h2>
<label>PN-401 stock <input id="s401" type="number" value="0" min="0"></label> <label>PN-401 extra delay (d) <input id="d401" type="number" value="0" min="0"></label>
<label>PN-212 stock <input id="s212" type="number" value="1" min="0"></label> <label>PN-212 extra delay (d) <input id="d212" type="number" value="0" min="0"></label><br>
<label><input type="checkbox" id="x1" style="width:auto"> disable PN-401 urgent indent</label> <label><input type="checkbox" id="x2" style="width:auto"> disable PN-401 cannibalization</label> <label><input type="checkbox" id="x3" style="width:auto"> disable PN-401 expedite repair</label><br>
<button id="go" onclick="run()">Recompute risk &amp; optimize</button></div>
<div class="card"><h2>7-Day Loss Risk Board <small>(rule-based heuristic, not a validated ML model)</small></h2><table id="risk"></table></div>
<div class="card"><h2>14-Day Spare Position <small>(synthetic data)</small></h2><table id="spares14"></table></div>
<div class="card"><h2>14-Day Aircraft Risk <small>(synthetic evidence)</small></h2><table id="risk14"></table></div>
<div class="card"><h2>Spare position (promised → realistic ETA)</h2><table id="parts"></table></div>
<div class="card"><h2>CP-SAT plan</h2><div id="plan"><small>Press "Recompute risk &amp; optimize"</small></div></div>
<div class="card"><h2>What-if comparison <small>(current scenario vs factory data)</small></h2><button id="cmpb" onclick="cmp()">Compare scenarios</button><div id="cmp"></div></div>
<div class="card"><h2>Plan history <small>(click a row to open)</small></h2><table id="hist"></table></div>
<div class="card"><h2>Audit trail <span id="chain"></span></h2><table id="audit"></table></div>
<script>
const $=id=>document.getElementById(id),H=()=>({'Content-Type':'application/json','X-API-Key':$('key').value});
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const body=()=>({stock:{'PN-401':+$('s401').value||0,'PN-212':+$('s212').value||0},delay:{'PN-401':+$('d401').value||0,'PN-212':+$('d212').value||0},
 disable:[$('x1').checked&&'PN-401:urgent_indent',$('x2').checked&&'PN-401:cannibalize',$('x3').checked&&'PN-401:expedite_repair'].filter(Boolean)});
const api=async(u,o)=>{const r=await fetch(u,o);let j=null;try{j=await r.json()}catch(e){}
 if(!r.ok){const d=j&&j.detail;throw new Error(typeof d==='string'?d:d?JSON.stringify(d):'HTTP '+r.status)}return j};
const post=(u,b)=>api(u,{method:'POST',headers:H(),body:JSON.stringify(b||body())});
const get=u=>api(u,{headers:H()});
const err=m=>{const e=$('err');e.textContent=m||'';e.style.display=m?'block':'none'};
const busy=async(id,txt,fn)=>{const b=$(id),t=b.textContent;b.disabled=true;b.textContent=txt;try{err();await fn()}catch(e){err(e.message)}finally{b.disabled=false;b.textContent=t}};
async function load(){
 try{
 const [r,p,a,c,f,h,spares,risk14]=await Promise.all([post('/api/risk'),post('/api/parts'),get('/api/audit'),get('/api/audit/verify'),get('/api/fleet'),get('/api/plans'),get('/api/v1/spares/position'),get('/api/v1/risk/14-day')]);
 $('chain').innerHTML=c.intact?'<span class="pill ok">hash chain intact</span>':'<span class="pill bad">TAMPERING DETECTED</span>';
 $('risk').innerHTML='<tr><th>Aircraft<th>Task<th>Due<th>Window<th>Part<th>No-action finish<th>Cause<th>Heuristic score<th>Why</tr>'+r.map(x=>`<tr><td>${esc(x.aircraft)}<td>${esc(x.kind)}<td>D${esc(x.due)}<td>${esc(x.window)}<td>${esc(x.part)}<td>D${esc(x.baseline_finish)}<td><span class="pill ${x.will_be_lost?'bad':x.at_risk?'warn':'ok'}">${x.will_be_lost?'LOSS: ':''}${esc(x.cause)}</span><td>${esc(x.risk_score)}<td><small>${esc(x.why)}</small></tr>`).join('');
 const partRows = Object.entries(spares.parts).map(([pn,info]) => `<tr><td>${esc(pn)}<td>${esc(info.desc)}<td>${info.daily.map(d=>`<span style='display:inline-block;width:28px'>D${d.day}:${d.end_available}</span>`).join(' ')}</tr>`).join('');
 $('spares14').innerHTML='<tr><th>Part<th>Description<th>Projected daily end-stock</tr>'+partRows;
 $('risk14').innerHTML='<tr><th>Aircraft<th>Task<th>Part<th>Shortage day<th>Available<th>Required<th>Evidence</tr>'+risk14.risks.map(x=>`<tr><td>${esc(x.aircraft)}<td>${esc(x.task)}<td>${esc(x.part)}<td>D${esc(x.first_shortage_day)}<td>${esc(x.available_qty_on_day)}<td>${esc(x.required_qty_on_day)}<td><small>${esc(x.evidence.join(' | '))}</small></tr>`).join('')||'<tr><td colspan="7"><small>No synthetic shortages in the 14-day window</small></tr>';
 $('parts').innerHTML='<tr><th>PN<th>Item<th>On hand<th>Repair<th>Procurement<th>Urgent indent</tr>'+Object.entries(p).map(([k,v])=>`<tr><td>${esc(k)}<td>${esc(v.desc)}<td>${esc(v.on_hand)}<td>${v.repair.map(x=>esc(x.promised)+'→'+esc(x.realistic)).join(', ')||'—'}<td>${v.procurement.map(x=>esc(x.promised)+'→'+esc(x.realistic)).join(', ')||'—'}<td>D${esc(v.urgent_indent.eta)}</tr>`).join('');
 $('fleet').innerHTML='<tr><th>Aircraft<th>Status<th>Flight hours</tr>'+f.aircraft.map(x=>`<tr><td>${esc(x.id)}<td><span class="pill ${x.status==='Serviceable'?'ok':'bad'}">${esc(x.status)}</span><td>${esc(x.hours)}</tr>`).join('');
 $('tasks').innerHTML='<tr><th>Task<th>Aircraft<th>Kind<th>Due<th>Tolerance<th>Duration<th>Part<th>Priority</tr>'+f.tasks.map(x=>`<tr><td>${esc(x.id)}<td>${esc(x.aircraft)}<td>${esc(x.kind)}<td>D${esc(x.due)}<td>+${esc(x.tol)}d<td>${esc(x.dur)}d<td>${esc(x.part)}<td>${esc(x.prio)}</tr>`).join('');
 $('hist').innerHTML='<tr><th>Plan<th>Status<th>Decision<th>By<th>When</tr>'+h.map(x=>`<tr style="cursor:pointer" onclick="openPlan('${esc(x.id)}')"><td>${esc(x.id)}${x.parent?' <small>(from '+esc(x.parent)+')</small>':''}<td>${esc(x.status)}${x.verified===false?' <span class="pill bad">unverified</span>':''}<td><span class="pill ${x.decision==='APPROVED'?'ok':x.decision==='REJECTED'?'bad':'warn'}">${esc(x.decision)}</span><td>${esc(x.decided_by||'—')}<td>${new Date(x.ts*1000).toLocaleString()}</tr>`).join('')||'<tr><td><small>No plans yet</small></tr>';
 $('audit').innerHTML=a.map(x=>`<tr><td>${new Date(x.ts*1000).toLocaleTimeString()}<td>${esc(x.who)} <small>(${esc(x.role)})</small><td>${esc(x.act)}</tr>`).join('')||'<tr><td><small>No events</small></tr>';
 }catch(e){err(e.message)}}
function gantt(pl){
 const A=pl.actions,bays=pl.bays||2,mx=Math.max(12,...A.map(a=>a.finish+2),...A.map(a=>a.limit+2)),L=130,Wd=900,dw=(Wd-L-10)/mx,x=d=>L+d*dw,rh=34,top=22,by=top+A.length*rh+8;
 const col={use_stock:'#2ecc71',await_repair:'#4da3ff',expedite_repair:'#7fd1ff',await_delivery:'#9b8cff',urgent_indent:'#f5b942',cannibalize:'#ff8a5c'};
 let s=`<svg viewBox="0 0 ${Wd} ${by+34}" style="width:100%;min-width:640px;font:11px system-ui">`;
 for(let d=0;d<=mx;d++)s+=`<line x1="${x(d)}" x2="${x(d)}" y1="${top-4}" y2="${by-4}" stroke="#22304a"/><text x="${x(d)+2}" y="12" fill="#8da0bd">D${d}</text>`;
 A.forEach((a,i)=>{const y=top+i*rh;
  s+=`<text x="4" y="${y+21}" fill="#e6ecf5">${esc(a.aircraft)} · ${esc(a.part)}</text>`;
  s+=`<line x1="${x(0)}" x2="${x(a.part_ready_day)}" y1="${y+17}" y2="${y+17}" stroke="#8da0bd" stroke-dasharray="4 3"/>`;
  s+=`<rect x="${x(a.start)}" y="${y+6}" width="${(a.finish-a.start)*dw}" height="22" rx="4" fill="${col[a.action]||'#4da3ff'}"/>`;
  s+=`<text x="${x(a.start)+4}" y="${y+21}" fill="#04101f" font-weight="600">${esc(a.action.replace(/_/g,' '))}</text>`;
  s+=`<line x1="${x(a.due)}" x2="${x(a.due)}" y1="${y+2}" y2="${y+32}" stroke="#f5b942" stroke-width="2"/><line x1="${x(a.limit)}" x2="${x(a.limit)}" y1="${y+2}" y2="${y+32}" stroke="#ff5c5c" stroke-width="2"/>`});
 s+=`<text x="4" y="${by+12}" fill="#8da0bd">Bay use (cap ${bays})</text>`;
 for(let d=0;d<mx;d++){const n=A.filter(a=>a.start<=d&&d<a.finish).length;
  s+=`<rect x="${x(d)+1}" y="${by}" width="${dw-2}" height="18" rx="3" fill="${n>bays?'#ff5c5c':n?'#2a4a78':'#131c2e'}"/><text x="${x(d)+dw/2-3}" y="${by+13}" fill="#e6ecf5">${n||''}</text>`}
 return s+`</svg><small>dashed = waiting for part · amber line = due day · red line = latest approved finish · colour = supply action</small>`}
async function cmp(){await busy('cmpb','Comparing…',async()=>{
 const j=await post('/api/compare',{base:{stock:{},delay:{},disable:[]},alt:body()}),d=j.diff;
 if(!d.comparable){$('cmp').innerHTML=`<p>Factory data: <b>${esc(j.base.status)}</b> · Current scenario: <b>${esc(j.alt.status)}</b></p><small>${[...(j.base.diagnosis||[]),...(j.alt.diagnosis||[])].map(esc).join('<br>')}</small>`;return}
 const dl=(v,u)=>`${v>0?'+':''}${v}${u||''}`;
 $('cmp').innerHTML=`<div class="kpi" style="margin:10px 0"><div><b>${dl(d.delta.on_time)}</b>tasks on time</div><div><b>${dl(d.delta.grounded_days)}</b>aircraft-days grounded</div><div><b>${dl(d.delta.donor_down_days)}</b>donor down days</div><div><b>${dl(d.delta.extra_cost)}</b>modeled extra cost</div><div><b>${dl(d.delta.robustness_pct,' pts')}</b>deadline-hold chance under slip</div></div><table><tr><th>Aircraft<th>Factory data<th>Current scenario<th></tr>`+d.rows.map(x=>`<tr><td>${esc(x.aircraft)}<td>${esc(x.base)}<td>${esc(x.alt)}<td>${x.changed?'<span class="pill warn">changed</span>':'<span class="pill ok">same</span>'}</tr>`).join('')+`</table><small>Each plan is optimal for its own scenario (${j.base.verified&&j.alt.verified?'both independently verified':'WARNING: a plan failed verification'}). Deltas = current scenario minus factory data.</small>`})}
async function rep(id){const w=window.open('','_blank');
 try{const r=await fetch(`/api/plans/${encodeURIComponent(id)}/report`,{headers:H()});if(!r.ok)throw new Error('report failed ('+r.status+')');
  const u=URL.createObjectURL(new Blob([await r.text()],{type:'text/html'}));if(w)w.location=u;else window.open(u)}
 catch(e){if(w)w.close();err(e.message)}}
async function openPlan(id){try{err();render(await get(`/api/plans/${encodeURIComponent(id)}`));window.scrollTo({top:$('plan').offsetTop-20,behavior:'smooth'})}catch(e){err(e.message)}}
async function run(){await busy('go','Solving…',async()=>{const pl=await post('/api/optimize');render(pl);await load()})}
function render(pl){
 window.CUR=pl;
 if(!pl.actions.length){$('plan').innerHTML=`<span class="pill bad">${esc(pl.status)}</span> ${esc(pl.reason)}<ul>${(pl.diagnosis||[]).map(d=>`<li>${esc(d)}</li>`).join('')}</ul>`;return}
 const m=pl.metrics,dc=pl.decision==='APPROVED'?'ok':pl.decision==='REJECTED'?'bad':'warn',pend=pl.decision==='PENDING';
 $('plan').innerHTML=`<span class="pill ok">${esc(pl.status)}</span> <span class="pill ${pl.verified?'ok':'bad'}">${pl.verified?'independently verified':'VERIFICATION FAILED: '+esc(pl.violations.join('; '))}</span> <small>plan ${esc(pl.id)}${pl.parent?' (modified from '+esc(pl.parent)+')':''} · objective ${esc(pl.objective)}</small>
 <div class="kpi" style="margin:10px 0"><div><b>${m.baseline.on_time}/${m.baseline.total} → ${m.plan.on_time}/${m.plan.total}</b>tasks on time (no action → plan)</div><div><b>${m.baseline.grounded_days} → ${m.plan.grounded_days}</b>aircraft-days grounded</div><div><b>${m.plan.deferred}</b>tasks using approved deferral</div><div><b>${m.plan.donor_down_days}</b>donor-aircraft down days</div><div><b>${m.plan.extra_cost}</b>modeled extra cost</div><div><b>${pl.robustness?Math.round(pl.robustness.p_all_within_limits*100)+'%':'n/a'}</b>deadlines hold if supply slips (stress test)</div></div>
 <table><tr><th>Aircraft<th>Action<th>Part ready<th>Work<th>Deferral<th>Slack to limit<th>Rejected alternatives</tr>`+pl.actions.map(a=>`<tr><td>${esc(a.aircraft)} (${esc(a.part)})<td><b>${esc(a.action.replace(/_/g,' '))}</b>${a.donor?' from '+esc(a.donor):''}<td>D${a.part_ready_day}<td>D${a.start}→D${a.finish}<td>${a.deferral_days}d<td>${a.slack_to_limit}d<td><small>${a.rejected.map(r=>esc(r.kind)+': '+esc(r.why)).join('<br>')||'—'}</small></tr>`).join('')+
 `</table><h2 style="margin-top:12px">Maintenance timeline</h2>${gantt(pl)}<p>Human decision: <span class="pill ${dc}">${esc(pl.decision||'PENDING')}</span> ${pend?`<button onclick="dec('${esc(pl.id)}','APPROVED')">Approve</button><button class="s" onclick="mod()">Modify</button><button class="s" onclick="dec('${esc(pl.id)}','REJECTED')">Override</button>`:''} <button class="s" onclick="rep('${esc(pl.id)}')">Report</button> <small>(Analyst role is refused by the server)</small></p><div id="mod"></div>`}
function mod(){const pl=window.CUR;
 $('mod').innerHTML='<h2>Modify plan</h2><table><tr><th>Aircraft<th>Action<th>Start day</tr>'+pl.actions.map(a=>`<tr><td>${esc(a.aircraft)} (${esc(a.part)})<td><select id="ma_${esc(a.task)}">${a.choices.map((c,i)=>`<option value="${i}" ${c.action==a.action&&c.group==a.group?'selected':''}>${esc(c.action.replace(/_/g,' '))}${c.donor?' from '+esc(c.donor):''} (ready D${c.ready})</option>`).join('')}</select><td><input id="ms_${esc(a.task)}" type="number" min="0" value="${a.start}"></tr>`).join('')+'</table><button onclick="submitMod()">Validate &amp; save as new plan</button> <small>The revised plan is rebuilt from scenario data and re-verified; it can only be approved if it passes.</small>'}
async function submitMod(){try{err();const pl=window.CUR,edits={};
 pl.actions.forEach(a=>{const c=a.choices[+$('ma_'+a.task).value];edits[a.task]={action:c.action,group:c.group,donor:c.donor,start:+$('ms_'+a.task).value}});
 const j=await post(`/api/plans/${encodeURIComponent(pl.id)}/modify`,{edits});render(j);load()}catch(e){err(e.message)}}
async function dec(id,d){try{err();const j=await post(`/api/plans/${encodeURIComponent(id)}/decide?decision=${d}`,{});render(j);load()}catch(e){err(e.message)}}
load();
</script></body></html>"""