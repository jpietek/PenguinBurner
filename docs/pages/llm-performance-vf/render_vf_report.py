#!/usr/bin/env python3
"""Render a self-contained report from record_vf_bench.py artifacts."""
import collections, html, json, pathlib, re, statistics, sys
import plotly.graph_objects as go
from plotly.subplots import make_subplots
out=pathlib.Path(sys.argv[1]) if len(sys.argv)>1 else pathlib.Path(__file__).resolve().parent
read=lambda name:json.loads((out/name).read_text())
results=read('results.json'); before=read('curve-before.json'); after=read('curve-after.json'); profile=read('profile.json')
samples=[json.loads(line) for line in (out/'telemetry.jsonl').read_text().splitlines()]
events=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
colors=['#38bdf8','#a78bfa','#fb923c','#f472b6']
labels=[f"{'Prompt pp'+str(r['n_prompt']) if r['n_prompt'] else 'Decode tg'+str(r['n_gen'])} · depth {r['n_depth']}" for r in results]
intervals=[]
for event in events:
    m=re.search(r'benchmark (\d+)/\d+: (prompt|generation) run (\d+)/\d+',event['line'])
    if m:
        case,rep=int(m[1])-1,int(m[3])-1
        start=event['elapsed_s']; end=start+results[case]['samples_ns'][rep]/1e9
        intervals.append(dict(case=case,rep=rep,start=start,end=end))
for s in samples:
    s['case']=next((i['case'] for i in intervals if i['start'] <= s['elapsed_s'] < i['end']),None)
    s['rep']=next((i['rep'] for i in intervals if i['start'] <= s['elapsed_s'] < i['end']),None)
groups=[[s for s in samples if s['case']==c] for c in range(len(results))]
points=sorted([p for p in before['points'] if p['type']==0 and p['voltage_based']==1],key=lambda p:p['voltage_uv'])
unchanged=before['points']==after['points']
offsets_unchanged=all(a['current_offset_khz']==b['current_offset_khz'] for a,b in zip(before['points'],after['points']))
curve_changes=[{'mv':a['voltage_uv']/1000,'before_mhz':a['freq_khz']/1000,'after_mhz':b['freq_khz']/1000} for a,b in zip(before['points'],after['points']) if a['freq_khz'] != b['freq_khz']]
job_unchanged=read('status-before.json')['active_job']==read('status-after.json')['active_job']
def stat(items,key):
    vals=[key(s) for s in items]
    return {'min':min(vals),'median':statistics.median(vals),'max':max(vals)} if vals else None
summary=[]
for c,g in enumerate(groups):
    summary.append({'label':labels[c],'sample_count':len(g),'tokens_s':results[c]['avg_ts'],'tokens_s_stddev':results[c]['stddev_ts'],
        'graphics_mhz':stat(g,lambda s:s['clocks_mhz']['graphics']),'voltage_mv':stat(g,lambda s:s['voltage_mv']),
        'power_w':stat(g,lambda s:s['power_draw_w']),'memory_mhz':stat(g,lambda s:s['clocks_mhz']['memory']),
        'utilization_pct':stat(g,lambda s:s['gpu_utilization_pct']),
        'throttle_masks':dict(collections.Counter(str(s['throttle_reason_mask']) for s in g))})
(out/'analysis.json').write_text(json.dumps({'cases':summary,'curve_unchanged':unchanged,'active_job_unchanged':job_unchanged,'offsets_unchanged':offsets_unchanged,'applied_curve_readback_changes':curve_changes,'measured_intervals':intervals},indent=2)+'\n')
layout=dict(template='plotly_dark',paper_bgcolor='#111c30',plot_bgcolor='#111c30',font=dict(family='system-ui',color='#e2e8f0'),margin=dict(l=65,r=30,t=70,b=60),hovermode='closest')
fig=go.Figure()
fig.add_trace(go.Scatter(x=[p['base_voltage_uv']/1000 for p in points],y=[p['base_freq_khz']/1000 for p in points],name='Driver base reference',mode='lines',line=dict(color='#64748b',dash='dot')))
fig.add_trace(go.Scatter(x=[p['voltage_uv']/1000 for p in points],y=[p['freq_khz']/1000 for p in points],name='Applied gaming Performance curve',mode='lines+markers',marker=dict(size=4),line=dict(color='#f8fafc',width=3)))
fig.add_trace(go.Scatter(x=[p['voltage_uv']/1000 for p in after['points'] if p['type']==0 and p['voltage_based']==1],y=[p['freq_khz']/1000 for p in after['points'] if p['type']==0 and p['voltage_based']==1],name='Applied curve after run',mode='lines',line=dict(color='#4ade80',dash='dash',width=1.5)))
for c,g in enumerate(groups):
    # Aggregate identical voltage/clock bins so residence is visible without hiding the curve.
    bins=collections.defaultdict(list)
    for s in g: bins[(s['voltage_mv'],s['clocks_mhz']['graphics'])].append(s)
    keys=list(bins)
    fig.add_trace(go.Scatter(x=[k[0] for k in keys],y=[k[1] for k in keys],mode='markers',name=labels[c],
        marker=dict(color=colors[c],size=[7+24*(len(bins[k])/max(1,len(g)))**0.5 for k in keys],opacity=.75,line=dict(width=1,color='#111827')),
        customdata=[[len(bins[k]),100*len(bins[k])/len(g),statistics.mean(s['power_draw_w'] for s in bins[k])] for k in keys],
        hovertemplate='%{x:.0f} mV · %{y:.0f} MHz<br>%{customdata[0]} samples (%{customdata[1]:.1f}%)<br>Mean power %{customdata[2]:.1f} W<extra>%{fullData.name}</extra>'))
fig.add_annotation(x=925,y=3000,text='Saved anchor: 925 mV / 3000 MHz',showarrow=True,ax=-100,ay=-65)
fig.update_layout(**layout,height=660,title='Gaming V/F curve + measured LLM operating points',xaxis_title='Core voltage (mV)',yaxis_title='Graphics clock (MHz)',legend=dict(orientation='h',y=-.20),
    updatemenus=[dict(type='buttons',direction='right',x=1,xanchor='right',y=1.04,showactive=False,bgcolor='#23334a',font=dict(color='#e2e8f0'),buttons=[dict(label='Working region',method='relayout',args=[{'xaxis.range':[775,1050],'yaxis.range':[1200,3150]}]),dict(label='Full curve',method='relayout',args=[{'xaxis.autorange':True,'yaxis.autorange':True}])])])
fig.update_xaxes(range=[775,1050]); fig.update_yaxes(range=[1200,3150])
curve_html=fig.to_html(full_html=False,include_plotlyjs=True,div_id='curve',config={'responsive':True,'displaylogo':False})
timeline=make_subplots(rows=4,cols=1,shared_xaxes=True,vertical_spacing=.045,subplot_titles=('Core clock','Core voltage','Board power','GPU utilization / memory clock'),specs=[[{}],[{}],[{}],[{'secondary_y':True}]])
series=[('Core MHz',lambda s:s['clocks_mhz']['graphics'],'#f8fafc'),('Voltage mV',lambda s:s['voltage_mv'],'#fbbf24'),('Power W',lambda s:s['power_draw_w'],'#4ade80'),('GPU utilization %',lambda s:s['gpu_utilization_pct'],'#38bdf8')]
for row,(name,key,color) in enumerate(series,1):
    timeline.add_trace(go.Scatter(x=[s['elapsed_s'] for s in samples],y=[key(s) for s in samples],name=name,line=dict(color=color,width=1.5)),row=row,col=1)
    timeline.update_yaxes(title_text=name,row=row,col=1)
timeline.add_trace(go.Scatter(x=[s['elapsed_s'] for s in samples],y=[s['clocks_mhz']['memory'] for s in samples],name='Memory MHz',line=dict(color='#a78bfa',width=1)),row=4,col=1,secondary_y=True)
timeline.update_yaxes(title_text='Memory MHz',row=4,col=1,secondary_y=True)
for i in intervals:
    timeline.add_vrect(x0=i['start'],x1=i['end'],fillcolor=colors[i['case']],opacity=.1,line_width=0,row='all',col=1)
timeline.update_layout(**{**layout,'hovermode':'x unified'},height=900,title='Measured run timeline — shaded regions are timed benchmark repetitions',legend=dict(orientation='h',y=-.1))
timeline.update_xaxes(title_text='Seconds since telemetry start',row=4,col=1)
time_html=timeline.to_html(full_html=False,include_plotlyjs=False,div_id='timeline',config={'responsive':True,'displaylogo':False})
def span(d,dec=0):return f"{d['median']:.{dec}f} <small>({d['min']:.{dec}f}–{d['max']:.{dec}f})</small>" if d else 'No samples'
rows=''.join('<tr>'+''.join(f'<td>{v}</td>' for v in [html.escape(s['label']),f"{s['tokens_s']:.1f} ± {s['tokens_s_stddev']:.1f}",span(s['graphics_mhz']),span(s['voltage_mv']),span(s['power_w'],1),span(s['utilization_pct']),str(s['sample_count'])])+'</tr>' for s in summary)
run=read('run.json')
report=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RTX 5080 · Performance curve × LLM</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#091120;color:#e2e8f0;font:16px/1.55 system-ui}}main{{max-width:1400px;margin:auto;padding:32px}}h1{{font-size:34px;line-height:1.15;margin:8px 0 18px}}h2{{font-size:21px}}p{{max-width:1100px}}.eyebrow{{color:#38bdf8;letter-spacing:.12em;font-size:12px}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}}.card,.panel{{background:#111c30;border:1px solid #23334a;border-radius:12px;padding:18px}}.card b{{display:block;font-size:24px}}.card span,small,.muted{{color:#9aacbf}}.panel{{margin:20px 0;padding:12px;overflow-x:auto}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #23334a;white-space:nowrap}}th{{color:#9aacbf}}a{{color:#38bdf8}}code{{overflow-wrap:anywhere}}@media(max-width:700px){{main{{padding:12px}}.cards{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:26px}}.plotly-graph-div{{min-width:750px}}}}</style></head><body><main>
<div class="eyebrow">LOCAL MEASUREMENT · {html.escape(run['start_utc'])}</div><h1>Where the LLM lands on your gaming curve</h1><p>NVIDIA GeForce RTX 5080 · Qwen3-8B Q4_K_M · llama.cpp CUDA · full GPU offload · Flash Attention. The white line is the V/F curve read from the running PenguinBurner daemon immediately before the benchmark. Colored bubbles show observed operating points during timed LLM work.</p>
<div class="cards"><div class="card"><b>3000 MHz</b><span>Saved Performance anchor @ 925 mV</span></div><div class="card"><b>3030 MHz</b><span>Configured upper curve ceiling</span></div><div class="card"><b>390 W</b><span>Configured board power limit</span></div><div class="card"><b>+4000 MHz</b><span>Configured memory offset</span></div></div>
<div class="panel">{curve_html}</div><p class="muted">Bubble size reflects sample share within each case; hover for voltage, clock, residence and power. Click legend entries to isolate cases, drag to zoom, or select Full curve. Driver base reference is the reported base V/F table, not a stock-performance benchmark. GPU operating points may differ from the static V/F table because of power states and firmware limits.</p>
<h2>Throughput and operating points</h2><p class="muted">Five repetitions per case. Tokens/s shows mean ± standard deviation. Telemetry shows median (minimum–maximum) across timed repetitions only. Prompt and decode rates measure different work and should not be compared directly.</p><div class="panel table-wrap"><table><thead><tr><th>Workload</th><th>Tokens/s</th><th>Core MHz</th><th>mV</th><th>Watts</th><th>GPU %</th><th>Samples</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="panel">{time_html}</div><h2>What this run shows</h2><p><b>Decode settles on the rising shoulder, around 915 mV and 2805–2812 MHz; prompt processing sits closer to the knee, around 925–930 mV and 2902–2940 MHz.</b> The saved 3000 MHz anchor is not a guaranteed operating clock for every workload. This run does not reproduce a severe low-clock collapse, and without a stock baseline it cannot establish whether the curve reduces token throughput.</p><p>The driver reports software power-cap flag 0x4 in 71/72 depth-0 decode samples and 78/82 depth-4096 decode samples. Reported board power is typically about 259 W during decode against a configured 390 W limit. That flag is evidence of a driver-reported limiting condition, but these measurements do not establish why it is asserted; averaged power is not an instantaneous limit measurement.</p><h2>How to read this result</h2><p>Prompt processing uses 2048 tokens; decode generates 256 tokens with one sequence. Depth 4096 means 4096 tokens of prior context. Context preparation and warmup are visible on the timeline but excluded from colored V/F bubbles and telemetry summaries. These are synthetic llama-bench token workloads, not an application response-quality test.</p>
<p>Sampling target: 100 ms through the daemon's read-only telemetry API. Timed windows combine receipt of benchmark progress messages with native per-repetition durations; boundaries are approximate at telemetry resolution. This captures sampled operating points, not every instantaneous transition. Power and utilization readings can have their own averaging windows.</p>
<p><b>Active daemon job unchanged: {job_unchanged}. Applied offsets unchanged: {offsets_unchanged}.</b> The driver-reported applied curve changed slightly at {len(curve_changes)} points (7–15 MHz), while the 925 mV anchor and upper ceiling remained unchanged. Both curve readbacks are shown; the cause of the small readback changes was not isolated. No profile, clock, voltage, fan or power setting was changed by the recorder. This run characterizes this RTX 5080 under its current Performance profile; it does not establish the RTX 3090 user's behavior or quantify a loss against stock. A stock comparison would be needed to measure that loss.</p>
<h2>Reproducible evidence</h2><p><a href="telemetry.csv">Telemetry CSV</a> · <a href="telemetry.jsonl">Raw daemon telemetry</a> · <a href="results.json">Benchmark results</a> · <a href="analysis.json">Phase intervals and summaries</a> · <a href="curve-before.json">Applied curve before</a> · <a href="curve-after.json">Applied curve after</a> · <a href="profile.json">Profile settings extract</a> · <a href="benchmark.log">Benchmark log</a> · <a href="installation.json">Model/build hashes</a> · <a href="record_vf_bench.py">Recorder</a> · <a href="render_vf_report.py">Report generator</a></p><p class="muted">Public evidence omits machine identifiers and uses ${{HOME}} in place of the local home path. Profile and daemon status files contain only fields needed for this report. Charts and Plotly JavaScript are embedded in this HTML; no network access is required. Raw evidence links refer to companion files.</p><details><summary>Exact benchmark command</summary><p><code>{html.escape(' '.join(run['command']))}</code></p></details></main></body></html>'''
(out/'index.html').write_text('\n'.join(line.rstrip() for line in report.splitlines())+'\n')
import shutil
if pathlib.Path(__file__).resolve() != (out/'render_vf_report.py').resolve():
    shutil.copy2(__file__,out/'render_vf_report.py')
print(json.dumps({'report':str(out/'index.html'),'curve_unchanged':unchanged,'active_job_unchanged':job_unchanged,'cases':summary},indent=2))
