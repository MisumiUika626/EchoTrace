import { Badge } from './components';

export function NuwaCheckpoints({checkpoints}:{checkpoints:any}) {
 if (!checkpoints) return null;
 return <div className="nuwa-checkpoints">{Object.entries(checkpoints).map(([key,item]:[string,any])=><details key={key}><summary>{item.title}</summary><pre>{JSON.stringify(item.summary,null,2)}</pre></details>)}</div>;
}

export default function NuwaReport({report}:{report:any}) {
 if (!report) return <section className="panel nuwa-report"><h3>女娲流程</h3><p className="muted">此版本尚无完整女娲流程报告。使用「模型重跑 Skill」补齐六维调研、筛选、独立验证和精炼。</p></section>;
 const validation=report.validation;
 return <section className="panel nuwa-report">
  <div className="section-heading"><h3>女娲 · 本地语料流程</h3><Badge tone={report.status==='passed'?'green':'amber'}>{report.status==='passed'?'验证通过':'已交付 · 有薄弱项'}</Badge></div>
  <p className="footnote">{report.note}</p>
  <div className="nuwa-counts"><span>{report.models} 个心智模型</span><span>{report.heuristics} 条启发式</span><span>修复 {report.repair_rounds}/2 轮</span></div>
  <details open><summary>六维调研摘要</summary><div className="nuwa-table"><table><thead><tr><th>维度</th><th>发现 / 消息</th><th>薄弱处</th></tr></thead><tbody>{report.research.map((r:any)=><tr key={r.dimension}><td>{r.dimension}</td><td>{r.findings} / {r.sources}</td><td>{r.gaps.join('；')||'未报告'}</td></tr>)}</tbody></table></div></details>
  <details><summary>候选模型三重检验与降级</summary>{report.selection.length?report.selection.map((s:any,i:number)=><article className="claim" key={i}><b>{s.name} · {s.action==='model'?'保留模型':s.action==='heuristic'?'降为启发式':'舍弃'}</b><p>{s.reason}</p></article>):<p className="muted">证据不足，没有可验证的候选模型。</p>}</details>
  <details open><summary>独立验证 · 已知 / 未知 / 表达</summary>{validation.checks.map((c:any)=><p key={c.id} className="quality-row"><span className={c.passed?'green':'amber'}>{c.passed?'✓':'!'}</span><span>{c.reason}</span></p>)}<details><summary>查看测试问题与实际回答</summary>{validation.answers.map((a:any)=><article className="claim" key={a.id}><b>{a.id}</b><p>{validation.plan.known.find((q:any)=>q.id===a.id)?.question||(a.id==='edge'?validation.plan.edge_question:validation.plan.voice_question)}</p><p>{a.text}</p></article>)}</details></details>
  <details><summary>双视角精炼 · {report.refinement.applied.length} 项已应用</summary>{report.refinement.reviews.map((r:any)=><article key={r.perspective}><h4>{r.perspective==='optimizer'?'结构与工作流评审':'触发与角色规则评审'}</h4>{r.scores.map((s:any)=><p key={s.dimension}>{s.dimension}：{s.score}/10 · {s.reason}</p>)}{r.dry_runs.map((s:string,i:number)=><p key={i} className="footnote">{s}</p>)}</article>)}{report.refinement.applied.map((s:any,i:number)=><p key={i}>已应用：{s.text}（{s.reason}）</p>)}{report.refinement.rejected.length>0&&<p className="notice">部分建议未通过后续验证，已保留精炼前版本。</p>}</details>
  {report.gaps.length>0&&<details><summary>证据缺口与适用边界</summary><ul>{report.gaps.map((s:string,i:number)=><li key={i}>{s}</li>)}</ul></details>}
 </section>;
}
