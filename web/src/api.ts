import { t } from './zh-CN';
export class ApiError extends Error { constructor(public code:string,message:string){super(message);} }
export async function api<T=any>(path:string,init:RequestInit={}):Promise<T>{
  let response:Response;
  try { response=await fetch('/api'+path,{...init,headers:{...(init.body && !(init.body instanceof FormData)?{'Content-Type':'application/json'}:{}),...init.headers}}); }
  catch { throw new ApiError('NETWORK',t.offline); }
  const body=await response.json();
  if(!body.ok) throw new ApiError(body.error?.code || 'UNKNOWN',body.error?.message || t.unknownError);
  return body.data;
}
export const post=<T=any>(path:string,data:unknown={})=>api<T>(path,{method:'POST',body:JSON.stringify(data)});
export const put=<T=any>(path:string,data:unknown)=>api<T>(path,{method:'PUT',body:JSON.stringify(data)});
export const date=(ts?:number|null)=>ts?new Date(ts*1000).toLocaleDateString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'}):t.noTime;
export const datetime=(ts:number)=>new Date(ts*1000).toLocaleString('zh-CN',{hour12:false});
export const num=(n:number=0)=>n.toLocaleString('zh-CN');
