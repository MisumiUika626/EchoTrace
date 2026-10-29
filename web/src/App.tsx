import { useEffect, useState } from 'react';
import { AudioLines, UsersRound, Download, ListTodo, Settings2, Search, Plus, ArrowUpRight, ChevronRight, ShieldCheck, Database, MessageCircle, Sparkles, PanelLeft, X } from 'lucide-react';
import { api, date, num, post } from './api';
import { Badge, Button, Empty, ErrorBox, Loading, Modal, Message } from './components';
import { useApi } from './hooks';
import { t } from './zh-CN';
import ImportDialog from './ImportDialog';
import Person from './Person';
import { SettingsPage, JobsPage, ExportPage } from './WorkspacePages';

type Route={page:string,cid?:number};
function readRoute():Route{const parts=location.hash.slice(1).split('/');return {page:parts[0]||'people',cid:parts[1]?Number(parts[1]):undefined}}
export default function App(){
 const [route,setRoute]=useState<Route>(readRoute),[importOpen,setImportOpen]=useState(false),[toast,setToast]=useState(''),[query,setQuery]=useState(''),[menu,setMenu]=useState(false),[evidence,setEvidence]=useState<any>(null),[evidenceError,setEvidenceError]=useState('');
 const contacts=useApi<any[]>('/contacts');
 useEffect(()=>{const handler=()=>setRoute(readRoute());window.addEventListener('hashchange',handler);return()=>window.removeEventListener('hashchange',handler)},[]);
 useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(''),4500);return()=>clearTimeout(timer)},[toast]);
 function navigate(page:string,cid?:number){location.hash=page+(cid?'/'+cid:'');setMenu(false)}
 function started(job:any){setImportOpen(false);setToast(t.created);contacts.refresh();navigate('jobs')}
 async function showEvidence(cid:number,id:string|number){setEvidence({loading:true});setEvidenceError('');try{setEvidence(await api(`/contacts/${cid}/messages/${id}`))}catch(e){setEvidenceError((e as Error).message)}}
 const nav=[{id:'people',label:t.people,icon:UsersRound},{id:'export',label:t.export,icon:Download},{id:'jobs',label:t.jobs,icon:ListTodo}];
 const filtered=(contacts.data||[]).filter(p=>(p.display_name+p.uid).toLowerCase().includes(query.toLowerCase()));
 const total=(contacts.data||[]).reduce((n,p)=>n+(p.msg_count||0),0);
 return <div className="app-shell"><aside className={`sidebar ${menu?'open':''}`}><a className="brand" href="#people"><span className="brand-mark"><AudioLines size={25}/></span><div><strong>{t.brand}</strong><small>{t.brandSub}</small></div></a><div className="nav-label">{t.workspace}</div><nav>{nav.map(({id,label,icon:Icon})=><button key={id} className={route.page===id||(id==='people'&&route.page==='person')?'active':''} onClick={()=>navigate(id)}><Icon size={19}/><span>{label}</span>{id==='people'&&!!contacts.data?.length&&<small>{contacts.data.length}</small>}</button>)}</nav>
 <div className="sidebar-note"><div className="echo-orbit"><span/><span/><span/><AudioLines size={30}/></div><h4>{t.memories}</h4><p>{t.localSub}</p></div><div className="sidebar-bottom"><button className={route.page==='settings'?'active':''} onClick={()=>navigate('settings')}><Settings2 size={18}/>{t.settings}</button><div className="local-status"><span className="status-dot"/><span>{t.local}</span><ShieldCheck size={14}/></div></div></aside>
 <div className="workspace"><header className="topbar"><div className="breadcrumbs"><button className="icon-button mobile-menu" onClick={()=>setMenu(!menu)} aria-label={t.workspace}><PanelLeft size={19}/></button><span>{t.workspace}</span><ChevronRight size={14}/><b>{route.page==='person'?t.people:route.page==='settings'?t.settings:nav.find(n=>n.id===route.page)?.label||t.people}</b></div><div className="topbar-right"><span className="privacy-chip"><ShieldCheck size={14}/>{t.privateBadge}</span><span className="user-avatar">{t.me}</span></div></header>
 <main key={route.page+(route.cid||'')}>
 {route.page==='people'&&<><section className="page-heading"><div><span className="eyebrow">{t.brandSub}</span><h1>{t.greeting}<span className="title-dot">.</span></h1><p>{t.greetingSub}</p></div><Button primary onClick={()=>setImportOpen(true)}><Plus size={18}/>{t.import}</Button></section>
 <div className="stats-grid"><div className="stat-card"><span className="stat-icon purple"><UsersRound size={21}/></span><div><span>{t.peopleCount}</span><strong>{num(contacts.data?.length||0)}<small>{t.people}</small></strong></div></div><div className="stat-card"><span className="stat-icon blue"><MessageCircle size={21}/></span><div><span>{t.messagesCount}</span><strong>{num(total)}<small>{t.messagesUnit}</small></strong></div></div><div className="stat-card"><span className="stat-icon amber"><Sparkles size={21}/></span><div><span>{t.distilledCount}</span><strong>{num((contacts.data||[]).filter(p=>p.version).length)}<small>{t.distilled}</small></strong></div></div></div>
 <section className="intro-banner"><div><Badge tone="purple">{t.privateBadge}</Badge><h2>{t.archive}</h2><p>{t.archiveSub}</p><div className="inline-actions"><button className="text-button" onClick={()=>setImportOpen(true)}>{t.import}<ArrowUpRight size={16}/></button><span/><button className="text-button muted" onClick={()=>navigate('export')}>{t.exportQQ}<ArrowUpRight size={16}/></button></div></div><div className="banner-art" aria-hidden="true"><div className="orbit-ring ring-one"/><div className="orbit-ring ring-two"/><div className="art-note note-back"><AudioLines size={25}/><i/><i/><i/></div><div className="art-note note-front"><MessageCircle size={25}/><i/><i/><span><Sparkles size={12}/></span></div><span className="art-star">✦</span></div></section>
 <section><div className="list-heading"><h2>{t.people}<span>{contacts.data?.length||0}</span></h2><div className="search-box"><Search size={17}/><input aria-label={t.searchPeople} placeholder={t.searchPeople} value={query} onChange={e=>setQuery(e.target.value)}/></div></div>
 {contacts.loading?<Loading/>:contacts.error?<ErrorBox error={contacts.error} retry={contacts.refresh}/>:filtered.length?<div className="people-grid">{filtered.map((p,index)=><button className="person-card" key={p.id} onClick={()=>navigate('person',p.id)}><div className="person-card-top"><span className={`avatar avatar-${index%4}`}>{p.display_name.slice(0,1)}</span><ArrowUpRight size={19}/></div><h3>{p.display_name}</h3><p>{num(p.msg_count)} {t.messagesUnit}</p><Badge tone={p.version?'green':'muted'}>{p.version?`${t.distilled} v${p.version}`:t.undistilled}</Badge><footer><span>{t.lastChat}</span><time>{date(p.last_ts)}</time></footer></button>)}</div>:<div className="empty-people"><Empty title={t.emptyTitle} subtitle={t.emptySub} action={<Button onClick={()=>setImportOpen(true)}><Plus size={16}/>{t.import}</Button>}/><p className="fixture-hint">{t.fictionHint}</p></div>}</section><footer className="page-footnote"><ShieldCheck size={14}/>{t.localSub}</footer></>}
 {route.page==='person'&&route.cid&&<Person cid={route.cid} onBack={()=>navigate('people')} onStarted={started} notify={setToast} onEvidence={id=>showEvidence(route.cid!,id)} onRefreshContacts={contacts.refresh}/>}
 {route.page==='settings'&&<SettingsPage notify={setToast}/>}
 {route.page==='jobs'&&<JobsPage onPerson={cid=>{contacts.refresh();navigate('person',cid)}} notify={setToast} onRefresh={contacts.refresh}/>}
 {route.page==='export'&&<ExportPage onImport={()=>setImportOpen(true)} onStarted={started} notify={setToast}/>}
 </main></div>
 {importOpen&&<ImportDialog onClose={()=>setImportOpen(false)} onStarted={started}/>}
 {evidence&&<Modal title={t.evidenceTitle} subtitle={t.jumpHighlight} onClose={()=>setEvidence(null)}><div className="modal-body evidence-context">{evidenceError?<ErrorBox error={evidenceError}/>:evidence.loading?<Loading/>:<>{evidence.context.map((m:any)=><Message key={m.id} message={m} highlight={m.id===evidence.message.id}/>)}<details><summary>{t.raw}</summary><pre>{JSON.stringify(evidence.message.raw,null,2)}</pre></details></>}</div></Modal>}
 {toast&&<div className="toast" role="status"><CheckIcon/>{toast}<button aria-label={t.close} onClick={()=>setToast('')}><X size={15}/></button></div>}
 </div>
}
function CheckIcon(){return <ShieldCheck size={18}/>}
