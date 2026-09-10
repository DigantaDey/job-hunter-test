import { useEffect, useState } from 'react'
import client, { apiError } from '../api/client'
import { Mail, Send, Check, Pencil, Search, Building2, User, Clock, AlertTriangle, Loader2 } from 'lucide-react'

/** A per-email message: a blocked send or a missing disclosure, with an action. */
type Notice = { id: number; kind: 'err' | 'warn'; text: string; consent?: string }

export default function Emails(){
  const [emails, setEmails]=useState<any[]>([])
  const [filter, setFilter]=useState('')
  const [company, setCompany]=useState('')
  const [dept, setDept]=useState('engineering')
  const [jobs, setJobs]=useState<any[]>([])
  const [otpFor, setOtpFor]=useState<number|null>(null)
  const [otp, setOtp]=useState('')
  const [notice, setNotice]=useState<Notice|null>(null)
  const [busyId, setBusyId]=useState<number|null>(null)

  const load=async()=>{
    const {data}=await client.get('/api/emails', {params: filter?{status:filter}: {}})
    setEmails(data)
    const j=await client.get('/api/jobs'); setJobs(j.data)
  }
  useEffect(()=>{ load(); const id=setInterval(load, 4000); return ()=>clearInterval(id)},[filter])

  const generate=async()=>{
    if(!company) return alert('Enter company')
    await client.post('/api/emails/generate', null, {params:{company, department: dept}})
    setCompany(''); load()
  }
  const update=async(e:any, field:string, value:string)=>{
    await client.put(`/api/emails/${e.id}`, {[field]: value})
    load()
  }
  const approve=async(id:number)=>{ await client.post(`/api/emails/${id}/approve`); load()}

  /** Record one disclosure (the user clicks it — never accepted silently). */
  const acceptDisclosure=async(id:number, kind:string)=>{
    setBusyId(id)
    try{
      await client.post('/api/account/consent', {[kind]: true})
      setNotice(null)
      await send(id)
    }catch(e:any){ setNotice({id, kind:'err', text: apiError(e, 'Could not record consent')}) }
    finally{ setBusyId(null) }
  }

  const send=async(id:number, code?:string)=>{
    setBusyId(id)
    try{
      const res=await client.post(`/api/emails/${id}/send`, null, {params: code?{otp:code}:{}})
      if(res.data.blocked){
        // The compliance gate refused it: say which gate, instead of looking like nothing happened.
        const blockers:string[]=res.data.compliance?.blockers || []
        setNotice({id, kind:'warn', text: `Not sent — ${blockers.join(', ') || 'blocked by the compliance gate'}`})
        return
      }
      if(res.data.needs_otp){ setOtpFor(id); setOtp(''); setNotice(null); return }
      setOtpFor(null); setOtp(''); setNotice(null); load()
    }catch(e:any){
      const detail=e?.response?.data?.detail
      if(detail?.code==='consent_required'){
        // 403 from the consent gate: offer the exact disclosure that is missing.
        setNotice({id, kind:'err', text: detail.message || `Accept the '${detail.consent}' disclosure to send.`, consent: detail.consent})
        return
      }
      setNotice({id, kind:'err', text: apiError(e, 'Send failed')})
    }finally{ setBusyId(null) }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold flex items-center gap-2"><Mail className="w-5 h-5"/> Email Bucket <span className="text-xs mono font-normal text-zinc-500">approval required • queued for sending • editable</span></h1>

      <div className="card p-4 flex flex-wrap gap-2 items-end">
        <div>
          <label className="text-xs mono">Company</label>
          <input value={company} onChange={e=>setCompany(e.target.value)} placeholder="e.g. Linear, Stealth Startup" className="block mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 w-64"/>
        </div>
        <div>
          <label className="text-xs mono">Department</label>
          <select value={dept} onChange={e=>setDept(e.target.value)} className="block mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700">
            <option>engineering</option><option>product</option><option>design</option><option>data</option>
          </select>
        </div>
        <button onClick={generate} className="px-4 py-2 rounded-full bg-blue-600 text-white text-sm font-medium">Find decision maker & draft</button>
        <div className="ml-auto flex gap-1">
          {['','pending_approval','queued','sent','needs_otp','failed'].map(s=> <button key={s} onClick={()=>setFilter(s)} className={`text-xs px-3 py-1 rounded-full border mono ${filter===s?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-800'}`}>{s||'all'}</button>)}
        </div>
      </div>

      <div className="grid gap-3">
        {emails.length===0 ? <div className="card p-8 text-center mono text-sm text-zinc-500">No emails in bucket. Generate for startups/small companies — finds hiring manager via AI + hunter pattern, drafts with SMTP (handles 2FA OTP).</div> :
        emails.map(e=> {
          // Captured once per row so TypeScript keeps the null-check below.
          const rowNotice = notice && notice.id===e.id ? notice : null
          return (
          <div key={e.id} className="card p-4">
            <div className="flex flex-wrap gap-3 items-start justify-between">
              <div>
                <div className="text-sm font-medium flex items-center gap-2"><Building2 className="w-4 h-4 text-zinc-400"/>{e.company} <span className="text-zinc-500 font-normal">→ {e.to_name} &lt;{e.to_email}&gt;</span></div>
                <div className="text-xs mono text-zinc-500 flex gap-2"><span>{e.recipient_type}</span><span>•</span><span>{new Date(e.created_at).toLocaleString()}</span><span>•</span><span className={`px-2 py-0.5 rounded-full border text-[11px] ${e.status==='sent'?'bg-emerald-50 border-emerald-200 text-emerald-700':'bg-amber-50 border-amber-200 text-amber-700 dark:bg-zinc-800'}`}>{e.status}</span></div>
              </div>
              <div className="flex gap-2">
                {e.status==='pending_approval' && <button onClick={()=>approve(e.id)} className="px-3 py-1.5 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-xs inline-flex items-center gap-1"><Check className="w-3.5 h-3.5"/> Approve → queue</button>}
                {(e.status==='queued' || e.status==='needs_otp') && <button onClick={()=>send(e.id)} disabled={busyId===e.id} className="px-3 py-1.5 rounded-full bg-blue-600 text-white text-xs inline-flex items-center gap-1 disabled:opacity-50">{busyId===e.id ? <Loader2 className="w-3.5 h-3.5 animate-spin"/> : <Send className="w-3.5 h-3.5"/>} Send via SMTP</button>}
              </div>
            </div>
            <div className="mt-3 grid lg:grid-cols-[1fr_1.4fr] gap-3">
              <div>
                <label className="text-xs mono">Subject (editable, cut/copy/paste)</label>
                <input defaultValue={e.subject} onBlur={ev=>update(e,'subject',ev.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
              </div>
              <div>
                <label className="text-xs mono">To</label>
                <input defaultValue={e.to_email} onBlur={ev=>update(e,'to_email',ev.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
              </div>
            </div>
            <div className="mt-3">
              <label className="text-xs mono flex items-center gap-1"><Pencil className="w-3.5 h-3.5"/> Body (editable)</label>
              <textarea defaultValue={e.body} onBlur={ev=>update(e,'body',ev.target.value)} rows={6} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 leading-relaxed"/>
              {e.status==='needs_otp' && <div className="mt-2 text-xs mono p-2 rounded-lg bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 flex gap-2"><AlertTriangle className="w-3.5 h-3.5 shrink-0"/> SMTP 2FA needed — enter the OTP / app password from your provider (or verify on another device).</div>}
              {rowNotice && (
                <div className={`mt-2 text-xs mono p-2 rounded-lg border flex flex-wrap items-center gap-2 ${
                  rowNotice.kind==='warn'
                    ? 'bg-amber-50 dark:bg-amber-950 border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200'
                    : 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-900 text-red-700 dark:text-red-300'}`}>
                  <AlertTriangle className="w-3.5 h-3.5 shrink-0"/>
                  <span className="flex-1">{rowNotice.text}</span>
                  {rowNotice.consent && (
                    <button onClick={()=>acceptDisclosure(e.id, rowNotice.consent!)} disabled={busyId===e.id}
                            className="px-3 py-1 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-[11px] inline-flex items-center gap-1 disabled:opacity-50">
                      {busyId===e.id && <Loader2 className="w-3 h-3 animate-spin"/>}
                      Accept the “{rowNotice.consent}” disclosure &amp; send
                    </button>
                  )}
                  <button onClick={()=>setNotice(null)} className="text-[11px] underline opacity-70">dismiss</button>
                </div>
              )}
              {otpFor===e.id && (
                <div className="mt-2 flex gap-2">
                  <input value={otp} onChange={ev=>setOtp(ev.target.value)} placeholder="OTP / app password" type="password" className="flex-1 border rounded-xl px-3 py-2 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
                  <button onClick={()=>send(e.id, otp)} disabled={!otp} className="px-4 py-2 rounded-full bg-blue-600 text-white text-xs font-medium inline-flex items-center gap-1 disabled:opacity-50"><Send className="w-3.5 h-3.5"/> Retry with OTP</button>
                </div>
              )}
            </div>
          </div>
          )
        })}
      </div>
    </div>
  )
}
