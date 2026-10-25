import { useEffect, useState } from 'react';
import { Sparkles, ShieldCheck } from 'lucide-react';
import { post, num } from './api';
import { Button, ErrorBox, Field, Loading, Modal, Toggle } from './components';
import { useAction, useApi } from './hooks';
import { t } from './zh-CN';

export default function DistillDialog({cid,onClose,onStarted}:{cid:number,onClose:()=>void,onStarted:(job:any)=>void}){
 const settings=useApi('/settings'),[mode,setMode]=useState<'full'|'sampled'>('full'),[tier,setTier]=useState('standard'),estimate=useApi(mode==='sampled'?`/contacts/${cid}/distill/estimate?tier=${tier}`:null),[model,setModel]=useState(''),[thinking,setThinking]=useState('max'),[limit,setLimit]=useState(500000),[raw,setRaw]=useState(true),[consent,setConsent]=useState(false),[review,setReview]=useState(false),[startDate,setStartDate]=useState(''),[endDate,setEndDate]=useState(''),[preview,setPreview]=useState<any>(null);
 const action=useAction();useEffect(()=>{if(settings.data){setModel(settings.data.llm.model);setThinking(settings.data.llm.thinking);setRaw(settings.data.privacy.allow_raw&&!settings.data.privacy.summary_only)}},[settings.data]);useEffect(()=>{if(estimate.data)setLimit(estimate.data.suggested_limit)},[estimate.data]);
 const scope={start:startDate?Math.floor(new Date(startDate).getTime()/1000):undefined,end:endDate?Math.floor(new Date(endDate).getTime()/1000):undefined};
 const shared={mode,model,thinking,send_raw:mode==='full'?true:raw,scope};
 async function start(){const job=await action.run(()=>post(`/contacts/${cid}/distill`,{...shared,tier,consent,nuwa_review:review,token_limit:limit}));if(job)onStarted(job)}
 async function dryRun(){const result=await action.run(()=>post(`/contacts/${cid}/distill/dry-run`,shared));if(result)setPreview(result)}
 return <Modal title={t.distillTitle} subtitle={t.distillSub} onClose={onClose}><div className="modal-body">{settings.loading?<Loading/>:<>
 {settings.error&&<ErrorBox error={settings.error}/>}{action.error&&<ErrorBox error={action.error}/>}{!settings.data?.llm.base_url&&<ErrorBox error={t.needModel}/>}
 <div className="form-grid"><Field label={t.model}><input value={model} onChange={e=>setModel(e.target.value)}/></Field><Field label={t.thinking}><select value={thinking} onChange={e=>setThinking(e.target.value)}>{(['low','medium','high','max'] as const).map(x=><option value={x} key={x}>{t[x]}</option>)}</select></Field></div>
 <Field label={t.distillMode}><div className="tier-options"><button className={mode==='full'?'selected':''} onClick={()=>{setMode('full');setRaw(true);setPreview(null)}}>{t.fullMode}</button><button className={mode==='sampled'?'selected':''} onClick={()=>{setMode('sampled');setPreview(null)}}>{t.sampledMode}</button></div></Field>
 {mode==='full'?<p className="notice">{t.fullModeWarning}</p>:<><p className="notice">{t.sampledModeWarning}</p><Field label={t.tier}><div className="tier-options">{(['quick','standard','deep'] as const).map(x=><button key={x} className={x===tier?'selected':''} onClick={()=>setTier(x)}>{t[x]}</button>)}</div></Field></>}
 <div className="form-grid"><Field label={t.startDate}><input type="datetime-local" value={startDate} onChange={e=>setStartDate(e.target.value)}/></Field><Field label={t.endDate}><input type="datetime-local" value={endDate} onChange={e=>setEndDate(e.target.value)}/></Field></div>
 {mode==='sampled'&&estimate.data&&<div className="estimate"><div><span>{t.tokens}</span><strong>≈ {num(estimate.data.estimated_tokens)}</strong></div><div><span>{t.duration}</span><strong>{estimate.data.estimated_minutes.join('–')} {t.minutes}</strong></div><small>{estimate.data.note}</small></div>}
 <Field label={t.tokenLimit}><input type="number" min={10000} value={limit} onChange={e=>setLimit(Number(e.target.value))}/></Field>{mode==='full'?<Toggle label={t.fullRawRequired} checked={true} onChange={()=>{}}/>:<Toggle label={t.rawSend} checked={raw} onChange={setRaw}/>} {!raw&&mode==='sampled'&&<p className="notice">{t.summaryWarning}</p>}
 {preview&&<div className="estimate"><div><span>{t.dryRunMode}</span><strong>{preview.mode}</strong></div><div><span>{t.dryRunChunks}</span><strong>{num(preview.chunks.length)}</strong></div><small>{preview.estimator.approximate?t.dryRunApproximate:t.dryRunExact}</small></div>}
 <Toggle label="女娲阶段人工检查点（调研 / 提炼 / 验证后暂停）" checked={review} onChange={setReview}/><div className="privacy-note"><ShieldCheck size={22}/><p>{t.privacyExplain}</p></div><label className="checkbox"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>{t.consent}</label>
 </>}</div><footer className="modal-footer"><Button onClick={onClose}>{t.cancel}</Button><Button disabled={action.busy} onClick={dryRun}>{t.dryRun}</Button><Button primary disabled={action.busy||!consent||!settings.data?.llm.base_url} onClick={start}><Sparkles size={16}/>{t.start}</Button></footer></Modal>
}
