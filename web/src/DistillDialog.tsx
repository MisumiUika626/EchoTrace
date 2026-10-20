import { useEffect, useState } from 'react';
import { Sparkles, ShieldCheck } from 'lucide-react';
import { post, num } from './api';
import { Button, ErrorBox, Field, Loading, Modal, Toggle } from './components';
import { useAction, useApi } from './hooks';
import { t } from './zh-CN';

export default function DistillDialog({cid,onClose,onStarted}:{cid:number,onClose:()=>void,onStarted:(job:any)=>void}){
 const settings=useApi('/settings'),[tier,setTier]=useState('standard'),estimate=useApi(`/contacts/${cid}/distill/estimate?tier=${tier}`),[model,setModel]=useState(''),[thinking,setThinking]=useState('max'),[limit,setLimit]=useState(500000),[raw,setRaw]=useState(true),[consent,setConsent]=useState(false),[review,setReview]=useState(false),[startDate,setStartDate]=useState(''),[endDate,setEndDate]=useState('');
 const action=useAction();useEffect(()=>{if(settings.data){setModel(settings.data.llm.model);setThinking(settings.data.llm.thinking);setRaw(settings.data.privacy.allow_raw&&!settings.data.privacy.summary_only)}},[settings.data]);useEffect(()=>{if(estimate.data)setLimit(estimate.data.suggested_limit)},[estimate.data]);
 async function start(){const job=await action.run(()=>post(`/contacts/${cid}/distill`,{model,thinking,tier,send_raw:raw,consent,nuwa_review:review,token_limit:limit,scope:{start:startDate?Math.floor(new Date(startDate).getTime()/1000):undefined,end:endDate?Math.floor(new Date(endDate).getTime()/1000):undefined}}));if(job)onStarted(job)}
 return <Modal title={t.distillTitle} subtitle={t.distillSub} onClose={onClose}><div className="modal-body">{settings.loading?<Loading/>:<>
 {settings.error&&<ErrorBox error={settings.error}/>}{action.error&&<ErrorBox error={action.error}/>}{!settings.data?.llm.base_url&&<ErrorBox error={t.needModel}/>}
 <div className="form-grid"><Field label={t.model}><input value={model} onChange={e=>setModel(e.target.value)}/></Field><Field label={t.thinking}><select value={thinking} onChange={e=>setThinking(e.target.value)}>{(['low','medium','high','max'] as const).map(x=><option value={x} key={x}>{t[x]}</option>)}</select></Field></div>
 <Field label={t.tier}><div className="tier-options">{(['quick','standard','deep'] as const).map(x=><button key={x} className={x===tier?'selected':''} onClick={()=>setTier(x)}>{t[x]}</button>)}</div></Field>
 <div className="form-grid"><Field label={t.startDate}><input type="datetime-local" value={startDate} onChange={e=>setStartDate(e.target.value)}/></Field><Field label={t.endDate}><input type="datetime-local" value={endDate} onChange={e=>setEndDate(e.target.value)}/></Field></div>
 {estimate.data&&<div className="estimate"><div><span>{t.tokens}</span><strong>≈ {num(estimate.data.estimated_tokens)}</strong></div><div><span>{t.duration}</span><strong>{estimate.data.estimated_minutes.join('–')} {t.minutes}</strong></div><small>{estimate.data.note}</small></div>}
 <Field label={t.tokenLimit}><input type="number" min={10000} value={limit} onChange={e=>setLimit(Number(e.target.value))}/></Field><Toggle label={t.rawSend} checked={raw} onChange={setRaw}/>{!raw&&<p className="notice">{t.summaryWarning}</p>}
 <Toggle label="女娲阶段人工检查点（调研 / 提炼 / 验证后暂停）" checked={review} onChange={setReview}/><div className="privacy-note"><ShieldCheck size={22}/><p>{t.privacyExplain}</p></div><label className="checkbox"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>{t.consent}</label>
 </>}</div><footer className="modal-footer"><Button onClick={onClose}>{t.cancel}</Button><Button primary disabled={action.busy||!consent||!settings.data?.llm.base_url} onClick={start}><Sparkles size={16}/>{t.start}</Button></footer></Modal>
}
