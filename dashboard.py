"""
📊 Дашборд UYDA Импорт — одна HTML-страница со всеми данными внутри.
Открывается в любом браузере (компьютер, телефон), интернет не нужен: графики (Chart.js) встроены в файл.
Бот собирает данные (bot.dashboard_data) и вызывает build_html(data).
"""

import html
import json
import os

_CHART_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand", "chart.umd.js")
_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"


def _chart_script() -> str:
    try:
        with open(_CHART_JS, encoding="utf-8") as f:
            return "<script>" + f.read().replace("</script", "<\\/script") + "</script>"
    except OSError:
        return f'<script src="{_CDN}"></script>'     # нет встроенной библиотеки — из интернета


CSS = """
:root{color-scheme:light;--bg:#f6f4f1;--card:#ffffff;--ink:#1d1a17;--ink2:#5a544d;--muted:#8b847c;--line:#e7e2dc;
--brand:#ff4900;--s1:#2a78d6;--s2:#eb6834;--good:#1a7f37;--warn:#b7791f;--bad:#c62828;--chip:#f1ede8}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#141312;--card:#1d1c1a;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#97948b;--line:#2f2d2a;--s1:#3987e5;--s2:#d95926;--good:#4cb860;--warn:#d6a03a;
--bad:#ef6b6b;--chip:#27251f}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#141312;--card:#1d1c1a;--ink:#f0efec;--ink2:#c3c2b7;--muted:#97948b;
--line:#2f2d2a;--s1:#3987e5;--s2:#d95926;--good:#4cb860;--warn:#d6a03a;--bad:#ef6b6b;--chip:#27251f}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;-webkit-text-size-adjust:100%}
header{background:#1d1a17;color:#fff;padding:18px 16px 0}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
header .wrap{padding:0}
.brand{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.logo{width:38px;height:38px;border-radius:10px;background:var(--brand);display:grid;place-items:center;font-weight:800;color:#fff}
h1{font-size:20px;margin:0;font-weight:700}.sub{color:#c9c2ba;font-size:13px}
nav{display:flex;gap:4px;overflow-x:auto;margin-top:14px;scrollbar-width:none}nav::-webkit-scrollbar{display:none}
nav button{background:none;border:0;color:#d8d1c9;padding:10px 14px;font:inherit;font-weight:600;border-bottom:3px solid transparent;
cursor:pointer;white-space:nowrap}nav button.on{color:#fff;border-color:var(--brand)}
main.wrap{padding:18px 16px 40px}.tab{display:none}.tab.on{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.kpi .l{color:var(--ink2);font-size:13px}.kpi .v{font-size:26px;font-weight:750;margin-top:2px;font-variant-numeric:tabular-nums}
.kpi .s{color:var(--muted);font-size:12px;margin-top:2px}
.kpi.bad .v{color:var(--bad)}.kpi.warn .v{color:var(--warn)}.kpi.good .v{color:var(--good)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px;min-width:0}
.card h2{font-size:15px;margin:0 0 2px}.card .hint{color:var(--muted);font-size:12px;margin-bottom:10px}
.chart{position:relative;height:280px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.bar button{background:var(--chip);border:1px solid var(--line);color:var(--ink);border-radius:999px;padding:6px 12px;font:inherit;
font-size:13px;cursor:pointer}.bar button.on{background:var(--ink);color:var(--card);border-color:var(--ink)}
input[type=search]{flex:1;min-width:180px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:10px;
padding:9px 12px;font:inherit}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
td.w{white-space:normal;min-width:220px;max-width:420px}
th{position:sticky;top:0;background:var(--card);color:var(--ink2);font-weight:600;cursor:pointer;user-select:none}
th.n,td.n{text-align:right;font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:0}
th .ar{color:var(--muted);font-size:11px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--chip)}
.late{color:var(--bad);font-weight:600}.muted{color:var(--muted)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--ink2);margin-bottom:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
footer{color:var(--muted);font-size:12px;text-align:center;padding:10px 16px 30px}
.empty{color:var(--muted);padding:18px;text-align:center}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const T = D.t;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt = (v, d=0) => v==null||v==='' ? '—' : Number(v).toLocaleString('ru-RU',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// вкладки
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('on', x===b));
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.id===b.dataset.t));
  try { localStorage.setItem('uyda-tab', b.dataset.t); } catch(e) {}
});
try { const t = localStorage.getItem('uyda-tab'); const b = t && document.querySelector(`nav button[data-t="${t}"]`); if (b) b.click(); } catch(e) {}

// KPI
document.getElementById('kpis').innerHTML = D.kpi.map(k =>
  `<div class="kpi ${k.tone||''}"><div class="l">${esc(k.label)}</div><div class="v">${esc(k.value)}</div>${k.sub?`<div class="s">${esc(k.sub)}</div>`:''}</div>`).join('');

// таблицы с поиском и сортировкой
function table(el, cols, rows, searchEl) {
  let sortKey = null, dir = 1, q = '';
  function draw() {
    let r = rows.filter(x => !q || cols.some(c => String(x[c.k]??'').toLowerCase().includes(q)));
    if (sortKey) r = r.slice().sort((a,b) => { const x=a[sortKey], y=b[sortKey];
      if (x==null||x==='') return 1; if (y==null||y==='') return -1;
      return (typeof x==='number' && typeof y==='number' ? x-y : String(x).localeCompare(String(y),'ru')) * dir; });
    el.innerHTML = r.length ? `<table><thead><tr>${cols.map(c=>`<th class="${c.n?'n':''}" data-k="${c.k}">${esc(c.h)} <span class="ar">${sortKey===c.k?(dir>0?'▲':'▼'):''}</span></th>`).join('')}</tr></thead>
      <tbody>${r.map(x=>`<tr>${cols.map(c=>`<td class="${c.n?'n':''} ${c.w?'w':''}">${c.f?c.f(x[c.k],x):esc(x[c.k]??'—')}</td>`).join('')}</tr>`).join('')}</tbody></table>`
      : `<div class="empty">${esc(T.empty)}</div>`;
    el.querySelectorAll('th').forEach(th => th.onclick = () => { const k=th.dataset.k; dir = sortKey===k ? -dir : 1; sortKey=k; draw(); });
  }
  if (searchEl) searchEl.oninput = () => { q = searchEl.value.trim().toLowerCase(); draw(); };
  draw();
}

// графики
function charts() {
  if (!window.Chart) { document.querySelectorAll('.chart').forEach(c => c.innerHTML = `<div class="empty">${esc(T.nochart)}</div>`); return; }
  const ink2 = css('--ink2'), line = css('--line'), s1 = css('--s1'), s2 = css('--s2');
  Chart.defaults.color = ink2; Chart.defaults.font.family = 'system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif';
  Chart.defaults.borderColor = line;
  const tip = {backgroundColor: css('--card'), titleColor: css('--ink'), bodyColor: css('--ink'), borderColor: line, borderWidth: 1,
               padding: 10, boxPadding: 4};
  const m = D.months;
  new Chart(document.getElementById('cMoney'), {type:'bar',
    data:{labels:m.map(x=>x.label), datasets:[
      {label:T.factory, data:m.map(x=>x.factory), backgroundColor:s1, borderRadius:0, stack:'a', borderColor:css('--card'), borderWidth:{top:2}},
      {label:T.overhead, data:m.map(x=>x.overhead), backgroundColor:s2, borderRadius:4, borderSkipped:'start', stack:'a', borderColor:css('--card'), borderWidth:{top:2}}]},
    options:{maintainAspectRatio:false, interaction:{mode:'index', intersect:false},
      plugins:{legend:{display:false}, tooltip:{...tip, callbacks:{label:c=>` ${c.dataset.label}: $${fmt(c.raw)}`,
        footer:it=>{const i=it[0].dataIndex; return `${T.landed}: $${fmt(m[i].factory+m[i].overhead)} · ${T.cargos}: ${m[i].n}`;}}}},
      scales:{x:{stacked:true, grid:{display:false}}, y:{stacked:true, ticks:{callback:v=>'$'+fmt(v)}, grid:{color:line}}}}});
  new Chart(document.getElementById('cPct'), {type:'line',
    data:{labels:m.map(x=>x.label), datasets:[{label:T.pct, data:m.map(x=>x.pct), borderColor:s2, backgroundColor:s2, borderWidth:2,
      pointRadius:4, pointHoverRadius:6, spanGaps:true, tension:.25}]},
    options:{maintainAspectRatio:false, interaction:{mode:'index', intersect:false},
      plugins:{legend:{display:false}, tooltip:{...tip, callbacks:{label:c=>` ${T.pct}: ${fmt(c.raw,1)}%`, footer:it=>`${T.cargos}: ${m[it[0].dataIndex].n}`}}},
      scales:{x:{grid:{display:false}}, y:{beginAtZero:true, ticks:{callback:v=>v+'%'}, grid:{color:line}}}}});
  window._catChart = new Chart(document.getElementById('cCat'), {type:'bar', data:{labels:[], datasets:[
      {label:T.factory, data:[], backgroundColor:s1, borderRadius:0, stack:'a', borderColor:css('--card'), borderWidth:{right:2}},
      {label:T.overhead, data:[], backgroundColor:s2, borderRadius:4, borderSkipped:'start', stack:'a', borderColor:css('--card'), borderWidth:{right:2}}]},
    options:{indexAxis:'y', maintainAspectRatio:false, interaction:{mode:'index', intersect:false},
      plugins:{legend:{display:false}, tooltip:{...tip, callbacks:{label:c=>` ${c.dataset.label}: $${fmt(c.raw)}`,
        footer:it=>{const r=window._catRows[it[0].dataIndex]; return `${T.cargos}: ${r.n} · ${T.pct}: ${fmt(r.pct,1)}%`;}}}},
      scales:{x:{stacked:true, ticks:{callback:v=>'$'+fmt(v)}, grid:{color:line}}, y:{stacked:true, grid:{display:false}}}}});
}

// категории
const catBar = document.getElementById('catBar');
const dims = Object.keys(D.cats);
function showCat(name) {
  catBar.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.d===name));
  const rows = D.cats[name];
  window._catRows = rows.slice(0, 12);
  if (window._catChart) { const ch = window._catChart; ch.data.labels = window._catRows.map(r=>r.label);
    ch.data.datasets[0].data = window._catRows.map(r=>r.factory); ch.data.datasets[1].data = window._catRows.map(r=>r.overhead);
    document.getElementById('cCatBox').style.height = Math.max(200, window._catRows.length*34+60)+'px'; ch.resize(); ch.update(); }
  table(document.getElementById('tCat'), [
    {k:'label', h:name, w:1}, {k:'n', h:T.cargos, n:1}, {k:'factory', h:T.factory+', $', n:1, f:v=>fmt(v)},
    {k:'overhead', h:T.overhead+', $', n:1, f:v=>fmt(v)}, {k:'pct', h:T.pct, n:1, f:v=>v==null?'—':fmt(v,1)+'%'},
    {k:'share', h:T.share, n:1, f:v=>fmt(v,0)+'%'}], rows);
}
catBar.innerHTML = dims.map(d => `<button data-d="${esc(d)}">${esc(d)}</button>`).join('');
catBar.querySelectorAll('button').forEach(b => b.onclick = () => showCat(b.dataset.d));

charts();
if (dims.length) showCat(dims[0]);

// открытые партии
table(document.getElementById('tOpen'), [
  {k:'title', h:T.cargo, w:1, f:(v,x)=>`<b>${esc(v)}</b><div class="muted">${esc(x.regime)} · ${esc(x.country)} · ${esc(x.supplier)}</div>`},
  {k:'date', h:T.date}, {k:'resp', h:T.resp},
  {k:'stage', h:T.stage, n:1, f:(v,x)=>v==null?'—':`<span class="${x.late?'late':''}">${v} / ${x.norm}</span>`},
  {k:'missing', h:T.missing, n:1, f:v=>v?'❗ '+v:'✅'}, {k:'yellow', h:'🟡', n:1, f:v=>v||''},
  {k:'risk', h:T.risk, n:1, f:(v,x)=>v?`${x.risk_icon} ${v}`:''}, {k:'docs', h:'📎', n:1, f:(v,x)=>v?`${v}${x.finals?' · ✅'+x.finals:''}`:''},
  {k:'factory', h:T.factory+', $', n:1, f:v=>fmt(v)}, {k:'overhead', h:T.overhead+', $', n:1, f:v=>fmt(v)},
  {k:'pct', h:T.pct, n:1, f:v=>v==null?'—':fmt(v,1)+'%'}, {k:'landed', h:T.landed+', $', n:1, f:v=>fmt(v)}],
  D.open, document.getElementById('qOpen'));
table(document.getElementById('tRisk'), [
  {k:'score', h:T.score, n:1, f:(v,x)=>`${x.icon} ${v}`}, {k:'kind', h:T.kind}, {k:'title', h:T.about, w:1},
  {k:'why', h:T.why, w:1}, {k:'owner', h:T.owner}, {k:'status', h:T.status}], D.risks, document.getElementById('qRisk'));
table(document.getElementById('tDocs'), [
  {k:'date', h:T.date}, {k:'cargo', h:T.cargo, w:1}, {k:'name', h:T.file, w:1}, {k:'stage', h:T.dstage},
  {k:'from', h:T.from}, {k:'to', h:T.to}, {k:'acc', h:T.acc, w:1}], D.docs, document.getElementById('qDocs'));
"""


def build_html(data: dict) -> str:
    t = data["t"]
    e = html.escape
    blob = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    tabs = [("ov", t["tab_overview"]), ("op", t["tab_open"]), ("ct", t["tab_cats"]), ("rk", t["tab_risks"]),
            ("dc", t["tab_docs"])]
    nav = "".join(f'<button data-t="{k}" class="{"on" if i == 0 else ""}">{e(v)}</button>' for i, (k, v) in enumerate(tabs))
    legend = (f'<div class="legend"><span><i style="background:var(--s1)"></i>{e(t["factory"])}</span>'
              f'<span><i style="background:var(--s2)"></i>{e(t["overhead"])}</span></div>')
    return f"""<!doctype html>
<html lang="{e(data.get('lang', 'ru'))}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(t['title'])}</title><style>{CSS}</style></head>
<body>
<header><div class="wrap"><div class="brand"><div class="logo">U</div><div>
<h1>{e(t['title'])}</h1><div class="sub">{e(t['generated'])}</div></div></div>
<nav>{nav}</nav></div></header>
<main class="wrap">
<section class="tab on" id="ov">
  <div class="kpis" id="kpis"></div>
  <div class="grid2">
    <div class="card"><h2>{e(t['ch_money'])}</h2><div class="hint">{e(t['ch_money_hint'])}</div>{legend}
      <div class="chart"><canvas id="cMoney" role="img" aria-label="{e(t['ch_money'])}"></canvas></div></div>
    <div class="card"><h2>{e(t['ch_pct'])}</h2><div class="hint">{e(t['ch_pct_hint'])}</div>
      <div class="chart"><canvas id="cPct" role="img" aria-label="{e(t['ch_pct'])}"></canvas></div></div>
  </div>
</section>
<section class="tab" id="op">
  <div class="bar"><input type="search" id="qOpen" placeholder="{e(t['search'])}"></div>
  <div class="tw" id="tOpen"></div>
</section>
<section class="tab" id="ct">
  <div class="card"><h2>{e(t['ch_cat'])}</h2><div class="hint">{e(t['ch_cat_hint'])}</div>
    <div class="bar" id="catBar"></div>{legend}
    <div class="chart" id="cCatBox"><canvas id="cCat" role="img" aria-label="{e(t['ch_cat'])}"></canvas></div></div>
  <div class="tw" id="tCat"></div>
</section>
<section class="tab" id="rk">
  <div class="bar"><input type="search" id="qRisk" placeholder="{e(t['search'])}"></div>
  <div class="tw" id="tRisk"></div>
</section>
<section class="tab" id="dc">
  <div class="bar"><input type="search" id="qDocs" placeholder="{e(t['search'])}"></div>
  <div class="tw" id="tDocs"></div>
</section>
</main>
<footer>{e(t['footer'])}</footer>
<script type="application/json" id="data">{blob}</script>
{_chart_script()}
<script>{JS}</script>
</body></html>"""
