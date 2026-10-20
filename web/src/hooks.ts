import { useCallback, useEffect, useState } from 'react';
import { api } from './api';
export function useApi<T=any>(path:string|null){
 const [data,setData]=useState<T|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true),[tick,setTick]=useState(0);
 const refresh=useCallback(()=>setTick(v=>v+1),[]);
 useEffect(()=>{let active=true;setError('');if(!path){setData(null);setLoading(false);return;}setLoading(true);api<T>(path).then(d=>{if(active)setData(d)}).catch(e=>{if(active)setError(e.message)}).finally(()=>{if(active)setLoading(false)});return()=>{active=false}},[path,tick]);
 return {data,error,loading,refresh,setData};
}
export function useAction(){const [busy,setBusy]=useState(false),[error,setError]=useState('');const run=async<T,>(fn:()=>Promise<T>):Promise<T|null>=>{setBusy(true);setError('');try{return await fn()}catch(e){setError((e as Error).message);return null}finally{setBusy(false)}};return {busy,error,run,setError};}
