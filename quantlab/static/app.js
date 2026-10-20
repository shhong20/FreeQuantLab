"use strict";
// All text originating in the SQLite ledger is written via textContent,
// never through innerHTML. No remote scripts, CDN, credentials or live orders.
const $ = (id) => document.getElementById(id);
const colors = ["#53d4ad","#7d9af6","#f8bc73","#da91ce","#67c6ec","#d2d779","#dd876f","#94baaa"];
let latest = null;
let selected = "all";
const money = (x) => x === null || x === undefined ? "—" : Number(x).toLocaleString("ko-KR",{maximumFractionDigits:0})+"원";
const pct = (x) => x === null || x === undefined ? "—" : (x >= 0 ? "+" : "")+(x*100).toFixed(2)+"%";
const compact = (x) => x === null || x === undefined ? "—" : Number(x).toLocaleString("ko-KR");
const kst = (iso) => iso ? new Date(iso).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "—";
function element(tag, classname, value) {
  const node = document.createElement(tag);
  if (classname) node.className = classname;
  if (value !== undefined) node.textContent = String(value);
  return node;
}
function clear(node){while(node.firstChild)node.removeChild(node.firstChild);}
function td(row, content, klass="") {
  const cell = element("td",klass);
  if (content instanceof Node) cell.appendChild(content);
  else cell.textContent=String(content);
  row.appendChild(cell);
  return cell;
}
function badge(text, klass){
  return element("span","state-badge "+klass,text);
}
const labels={
  buy_hold_20pct:"Buy & Hold · 20%",
  trend_ma20_80:"Trend · MA20/80",
  factor_r5_gt0:"Factor · r5 > 0",
};
const states={
  incubating:"등록 완료 · 관찰 대기",
  awaiting_cycle:"오늘 시세 확인 전",
  recorded:"오늘 기록 완료",
  overdue:"예정 실행 지연",
  halted:"가상 손실 한도 정지",
};
function updateMetrics(info){
  const s=info.summary;
  $("metric-tracks").textContent=compact(s.tracks);
  $("metric-assets").textContent=compact(s.assets)+"개 자산 · BTC / ETH";
  $("metric-events").textContent=compact(s.recorded_events);
  $("metric-orders").textContent=compact(s.virtual_orders);
  $("metric-alerts").textContent=compact(s.critical_alerts+s.warnings);
  $("metric-alert-label").textContent=s.critical_alerts+"개 중대 · "+s.warnings+"개 주의";
  const severity=s.critical_alerts?"주의: 가상계좌 정지":s.warnings?"운영 점검 필요":s.recorded_events?"정상 관찰 중":"첫 관측 대기 중";
  $("head-status").textContent=severity;
  $("last-refreshed").textContent="업데이트 "+kst(info.generated_utc);
}
function updateAlerts(info){
  const s=info.summary;
  const all=$("alerts-list"); clear(all);
  $("alerts-count").textContent=info.alerts.length+"개 알림";
  $("last-run").textContent=s.last_run ? kst(s.last_run)+" · "+s.last_run_status : "실행 기록 없음";
  $("health-summary").textContent=info.alerts.length
    ? "관찰이 필요한 운영 항목이 "+info.alerts.length+"건 있습니다. 거래는 활성화되지 않습니다."
    : "시스템 이상 없음 · 모든 모의 투자 기록은 읽기 전용으로 확인하고 있습니다.";
  if (!info.alerts.length){
    all.appendChild(element("div","alert ok-alert","✓  활성화된 위험 경고가 없습니다. 다음 자동 실행을 대기합니다."));
  }
  for(const item of info.alerts.slice(0,12)){
    const card=element("div","alert "+item.severity);
    card.appendChild(element("div","alert-title",item.code));
    card.appendChild(element("div","alert-desc",item.message+" "+(item.symbol||"")+" "+(item.strategy||"")));
    all.appendChild(card);
  }
}
function renderAccounts(info){
  const target=$("tracks-table");clear(target);
  if(!info.tracks.length) {
    const r=element("tr");td(r,"등록된 가상 전략이 없습니다.","waiting").colSpan=7;target.appendChild(r);return;
  }
  for(const track of info.tracks){
    if(selected!=="all"&&track.symbol!==selected)continue;
    const r=element("tr");
    const asset=element("div","asset-strong",track.symbol.replace("KRW-",""));
    const sub=element("div","track-sub",labels[track.strategy]||track.strategy);
    const c=element("div");c.appendChild(asset);c.appendChild(sub);
    td(r,c);
    td(r,badge(states[track.state]||track.state,track.state));
    td(r,money(track.recorded_equity),"align-right");
    td(r,pct(track.observed_return),"align-right "+(track.observed_return>0?"positive":track.observed_return<0?"negative":""));
    td(r,pct(track.max_recorded_drawdown),"align-right "+(track.max_recorded_drawdown<0?"negative":""));
    td(r,compact(track.simulated_orders),"align-right");
    td(r,track.last_decision_date||"첫 관측 대기");
    target.appendChild(r);
  }
}
function renderEvents(info){
  const target=$("events-table");clear(target);
  const relevant=info.recent_events.filter(x=>selected==="all"||x.symbol===selected).slice(0,12);
  if(!relevant.length){const r=element("tr");td(r,"첫 미래 시세 관측 이후 거래 내역이 표시됩니다.","waiting").colSpan=5;target.appendChild(r);return;}
  for(const item of relevant){
    const r=element("tr");
    td(r,item.date);
    td(r,item.symbol.replace("KRW-",""));
    td(r,labels[item.strategy]||item.strategy);
    td(r,badge(item.action,item.action==="HALT"?"halted":"recorded"));
    td(r,money(item.price),"align-right");
    target.appendChild(r);
  }
}
function renderRuns(info){
  const box=$("runs");clear(box);
  if(!info.recent_runs.length){box.appendChild(element("div","waiting","아직 실행 기록이 없습니다."));return;}
  for(const run of info.recent_runs.slice(0,7)){
    const line=element("div","run");
    line.appendChild(element("div","run-dot "+(run.status==="OK"?"":"failed")));
    const main=element("div","run-main");
    main.appendChild(element("b","",run.status==="OK"?"정상 실행 완료":"오류 발생 · 확인 필요"));
    main.appendChild(element("small","",run.errors.length?run.errors.map(x=>x.asset+" "+x.error).join("; "):"자동 조회 / 중복 방지 동작"));
    line.appendChild(main);
    line.appendChild(element("time","",kst(run.created_at)));
    box.appendChild(line);
  }
}
function drawChart(info){
  const points=info.tracks.filter(t=>(selected==="all"||t.symbol===selected)&&t.events>0);
  const legend=$("chart-legend");clear(legend);
  const empty=$("chart-empty");
  const canvas=$("equity-chart");
  canvas.hidden=false;
  const width=Math.max(1,canvas.clientWidth);
  const height=Math.max(1,canvas.clientHeight);
  const dpr=Math.min(window.devicePixelRatio||1,2);
  canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
  const ctx=canvas.getContext("2d");if(!ctx)return;
  ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);
  if(!points.length){empty.hidden=false;canvas.hidden=true;return;}
  empty.hidden=true;canvas.hidden=false;
  const all=points.flatMap(p=>p.equity_points.map(v=>v.equity));
  let min=Math.min(...all),max=Math.max(...all);
  const extra=Math.max(2000,(max-min)*.15);
  min-=extra;max+=extra;
  const l=62,r=9,t=14,b=27,w=width-l-r,h=height-t-b;
  ctx.strokeStyle="#223543";ctx.lineWidth=1;ctx.fillStyle="#708b9e";ctx.font="10px system-ui";
  for(let i=0;i<=4;i++){
    const y=t+h*i/4;
    ctx.beginPath();ctx.moveTo(l,y);ctx.lineTo(width-r,y);ctx.stroke();
    ctx.textAlign="right";
    ctx.fillText(Math.round(max-(max-min)*i/4).toLocaleString("ko-KR"),l-8,y+3);
  }
  const dates=Array.from(new Set(points.flatMap(p=>p.equity_points.map(x=>x.date)))).sort();
  ctx.textAlign="center";ctx.fillStyle="#748da0";
  ctx.fillText(dates[0]||"",l+5,height-6);
  ctx.fillText(dates[dates.length-1]||"",width-r-24,height-6);
  for(let j=0;j<points.length;j++){
    const p=points[j],color=colors[j%colors.length];
    ctx.strokeStyle=color;ctx.lineWidth=2;ctx.lineCap="round";ctx.lineJoin="round";ctx.beginPath();
    p.equity_points.forEach((v,i)=>{
      const x=l+(dates.indexOf(v.date)/Math.max(1,dates.length-1))*w;
      const y=t+(max-v.equity)/(max-min)*h;
      if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
    });ctx.stroke();
    const lab=element("div","legend-item");
    const block=element("div","legend-color");block.style.backgroundColor=color;
    lab.appendChild(block);
    lab.appendChild(element("span","",p.symbol.replace("KRW-","")+" / "+(labels[p.strategy]||p.strategy)));
    legend.appendChild(lab);
  }
}
function render(data){latest=data;updateMetrics(data);updateAlerts(data);renderAccounts(data);renderEvents(data);renderRuns(data);drawChart(data);}
async function refreshReportsAndAlerts() {
  const [reportResult, alertsResult] = await Promise.allSettled([
    fetch("/api/latest-report",{cache:"no-store"}),
    fetch("/api/alert-history",{cache:"no-store"}),
  ]);
  const target=$("report-rows");clear(target);
  if(reportResult.status==="fulfilled" && reportResult.value.ok) {
    try {
      const data=await reportResult.value.json();
      $("report-date").textContent=data.report_date_kst || "작성일 미확인";
      const reviewById=new Map((data.manual_review?.strategies||[]).map(x=>[x.track_id,x]));
      for(const entry of (data.strategies||[])){
        const row=element("tr");
        td(row,entry.symbol.replace("KRW-","")+" / "+(labels[entry.strategy]||entry.strategy));
        td(row,compact(entry.recorded_days),"align-right");
        td(row,pct(entry.observed_total_return),"align-right");
        td(row,entry.vs_buy_hold_return_pp==null?"—":
          (entry.vs_buy_hold_return_pp>=0?"+":"")+
          entry.vs_buy_hold_return_pp.toFixed(2)+" pp","align-right");
        const review=reviewById.get(entry.track_id);
        td(row,review?.research_maturity==="READY_FOR_HUMAN_REVIEW"?"수동 검토 가능":"평가 자료 부족");
        target.appendChild(row);
      }
      if(!target.children.length){const r=element("tr");td(r,"등록된 연구 전략 없음","waiting").colSpan=5;target.appendChild(r);}
    }catch(error){const r=element("tr");td(r,"보고서 형식 확인 필요","waiting").colSpan=5;target.appendChild(r);}
  }else{
    $("report-date").textContent="작성 대기";
    const r=element("tr");
    td(r,"다음 정기 보고서 생성 이후 표시됩니다.","waiting").colSpan=4;
    target.appendChild(r);
  }
  const history=$("alert-history");clear(history);
  if(alertsResult.status==="fulfilled" && alertsResult.value.ok) {
    try {
      const data=await alertsResult.value.json();
      const events=data.events||[];
      if(!events.length){history.appendChild(element("div","waiting","아직 발생한 운영 장애 알림이 없습니다."));}
      for(const ev of events.slice(0,10)){
        const line=element("div","run");
        line.appendChild(element("div","run-dot "+(ev.kind==="resolved"?"":ev.severity==="critical"?"failed":"")));
        const main=element("div","run-main");
        main.appendChild(element("b","",ev.code+" · "+(ev.kind==="resolved"?"복구":ev.kind==="triggered"?"발생":ev.kind)));
        main.appendChild(element("small","",ev.message+" / "+ev.delivery));
        line.appendChild(main);line.appendChild(element("time","",kst(ev.time)));
        history.appendChild(line);
      }
    }catch(error){history.appendChild(element("div","waiting","알림 이력 형식 확인 필요"));}
  }else{history.appendChild(element("div","waiting","경고 기록 연결 대기 중"));}
}
async function updateProspectiveProtocol(){
  try {
    const res=await fetch("/api/protocol-status",{cache:"no-store"});
    if(!res.ok)throw new Error("Protocol status unavailable");
    const data=await res.json();
    const locked=data.status==="LOCKED_UNTIL_FUTURE_CUTOFF";
    $("protocol-state").textContent=locked?"미래 평가 잠금":data.status==="OBSERVED_FOR_MANUAL_REVIEW"?"관측 완료 · 수동 검토":"검증 보류";
    $("protocol-detail").textContent=locked
      ? data.first_eligible_decision_utc+" ~ "+data.last_eligible_decision_utc+
        " UTC 일봉 · 평가 기간 종료 전에는 수익률 비교 비공개"
      : "결과가 공개되더라도 자동 투자 승인이나 통계적 유의성 확정은 불가능합니다.";
    $("protocol-end").textContent=data.evaluation_not_before_utc
      ? kst(data.evaluation_not_before_utc)+" 이후":"평가 대기";
    $("protocol-caution").textContent="해시 "+(data.protocol_sha256||"").slice(0,12)+
      " · 공개 대시보드가 있어 완전한 블라인드 검증은 아닙니다.";
  }catch(error){
    $("protocol-state").textContent="조회 불가";
    $("protocol-detail").textContent="평가 원장/사전 등록 계획을 확인할 수 없습니다.";
    $("protocol-end").textContent="—";
  }
}
async function reload(){
  const button=$("refresh");button.disabled=true;
  try{
    const res=await fetch("/api/status",{cache:"no-store"});
    if(!res.ok)throw new Error("상태 "+res.status);
    render(await res.json());
    $("connection").textContent="● 로컬 데이터 연결됨";$("connection").classList.remove("error");
  }catch(error){
    $("connection").textContent="● 원장 조회 실패";$("connection").classList.add("error");
    $("head-status").textContent="모니터링 데이터 확인 필요";
    $("health-summary").textContent="읽기 전용 SQLite 원장에 접근하지 못했습니다. 서비스 및 파일 경로를 확인하세요.";
  }finally{button.disabled=false;await Promise.all([refreshReportsAndAlerts(),updateProspectiveProtocol()]);}
}
document.querySelectorAll("[data-symbol]").forEach(b=>b.addEventListener("click",()=>{
  selected=b.dataset.symbol;
  document.querySelectorAll("[data-symbol]").forEach(x=>x.classList.toggle("active",x===b));
  if(latest)render(latest);
}));
$("refresh").addEventListener("click",reload);
window.addEventListener("resize",()=>{if(latest)drawChart(latest);});
reload();
setInterval(reload,60000);
