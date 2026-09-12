import { useEffect, useState } from 'react'
import client, { apiError } from '../api/client'
import { Mail, Send, Check, Pencil, Search, Building2, User, Clock, AlertTriangle, Loader2, Briefcase, ExternalLink, ShieldCheck, ShieldAlert, XCircle } from 'lucide-react'
import { aiOutage, type AIOutage } from '../api/client'

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
  const [jobId, setJobId] = useState<string>('')
  const [openJd, setOpenJd] = useState<number|null>(null)
  const [outage, setOutage] = useState<AIOutage|null>(null)

  const load=async()=>{
    const {data}=await client.get('/api/emails', {params: filter?{status:filter}: {}})
    setEmails(data)
    const j=await client.get('/api/jobs'); setJobs(j.data)
  }
  useEffect(()=>{ load(); const id=setInterval(load, 4000); return ()=>clearInterval(id)},[filter])

  const generate=async()=>{
    if(!company && !jobId) return setNotice({id:-1, kind:'err', text:'Enter a company name (or pick a job) first'})
    setBusyId(-1); setOutage(null)
    try{
      const job = jobs.find(j=>String(j.id)===String(jobId))
      // The endpoint takes a JSON body (EmailGenerate), not query params.
      // job_id is what makes the draft about a real posting — the bucket then
      // shows the title, link and JD excerpt next to the message.
      await client.post('/api/emails/generate', {company: company || (job?.company ?? ''), job_id: jobId ? Number(jobId) : null, department: dept})
      setCompany(''); setJobId(''); setNotice(null); load()
    }catch(e:any){
      const o = aiOutage(e)
      if(o) setOutage(o)
      else setNotice({id:-1, kind:'err', text: apiError(e, 'Could not draft the email')})
    }
    finally{ setBusyId(null) }
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
      // SendRequest is a JSON body: {otp, allow_real}. Sending it as query
      // params made every send fail with a 422 before it reached the gate.
      const res=await client.post(`/api/emails/${id}/send`, {otp: code ?? null, allow_real: true})
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
          <label className="text-xs mono">Job (optional — attaches the JD)</label>
          <select value={jobId} onChange={e=>{ setJobId(e.target.value); const j=jobs.find(x=>String(x.id)===e.target.value); if(j) setCompany(j.company) }} className="block mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 w-64">
            <option value="">No specific posting</option>
            {jobs.map((j:any)=> <option key={j.id} value={j.id}>{j.title} • {j.company}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs mono">Department</label>
          <select value={dept} onChange={e=>setDept(e.target.value)} className="block mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700">
            <option>engineering</option><option>product</option><option>design</option><option>data</option>
          </select>
        </div>
        <button onClick={generate} disabled={busyId===-1} className="px-4 py-2 rounded-full bg-blue-600 text-white text-sm font-medium disabled:opacity-50">
          {busyId===-1 ? 'Drafting…' : 'Find decision maker & draft'}
        </button>
        <div className="ml-auto flex gap-1">
          {['','pending_approval','queued','sent','needs_otp','failed'].map(s=> <button key={s} onClick={()=>setFilter(s)} className={`text-xs px-3 py-1 rounded-full border mono ${filter===s?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-800'}`}>{s||'all'}</button>)}
        </div>
      </div>

      {outage && (
        <div className="p-3 rounded-xl bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-sm">
          <div className="flex items-start gap-2">
            <XCircle className="w-4 h-4 text-red-600 dark:text-red-400 shrink-0 mt-0.5"/>
            <div className="min-w-0">
              <div className="font-medium text-red-800 dark:text-red-200">AI unavailable — no draft was created</div>
              <div className="text-xs mono mt-1 text-red-700 dark:text-red-300">{outage.message}{outage.reason ? ` (${outage.reason})` : ''}</div>
              {outage.fix && <div className="text-xs mono mt-1 text-red-700 dark:text-red-300"><strong>Fix:</strong> {outage.fix}</div>}
            </div>
          </div>
        </div>
      )}

      {/* Errors from the drafting form itself (id -1) belong at page level —
          the per-row notices below are keyed to an existing email. */}
      {notice && notice.id===-1 && (
        <div className={`text-sm mono p-2 rounded-lg border ${
          notice.kind==='err'
            ? 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-900 text-red-700 dark:text-red-300'
            : 'bg-amber-50 dark:bg-amber-950 border-amber-200 dark:border-amber-900 text-amber-700 dark:text-amber-300'
        }`}>{notice.text}</div>
      )}

      <div className="grid gap-3">
        {emails.length===0 ? <div className="card p-8 text-center mono text-sm text-zinc-500">No emails in bucket. Generate for startups/small companies — finds hiring manager via AI + hunter pattern, drafts with SMTP (handles 2FA OTP).</div> :
        emails.map(e=> {
          // Captured once per row so TypeScript keeps the null-check below.
          const rowNotice = notice && notice.id===e.id ? notice : null
          return (
          <div key={e.id} className="card p-4">
            <div className="flex flex-wrap gap-3 items-start justify-between">
              <div className="min-w-0">
                <div className="text-sm font-medium flex items-center gap-2 flex-wrap"><Building2 className="w-4 h-4 text-zinc-400"/>{e.company} <span className="text-zinc-500 font-normal">→ {e.to_name} &lt;{e.to_email}&gt;</span></div>
                <div className="text-xs mono text-zinc-500 flex gap-2 flex-wrap items-center">
                  <span>{e.recipient_type}</span><span>•</span><span>{new Date(e.created_at).toLocaleString()}</span><span>•</span>
                  <span className={`px-2 py-0.5 rounded-full border text-[11px] ${e.status==='sent'?'bg-emerald-50 border-emerald-200 text-emerald-700':'bg-amber-50 border-amber-200 text-amber-700 dark:bg-zinc-800'}`}>{e.status}</span>
                  {e.verified
                    ? <span className="px-2 py-0.5 rounded-full border text-[11px] bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 inline-flex items-center gap-1"><ShieldCheck className="w-3 h-3"/> verified address</span>
                    : <span className="px-2 py-0.5 rounded-full border text-[11px] bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800 inline-flex items-center gap-1" title={`source: ${e.source} • confidence ${Math.round((e.confidence||0)*100)}%`}><ShieldAlert className="w-3 h-3"/> unverified ({e.source})</span>}
                  {e.role_mailbox && <span className="px-2 py-0.5 rounded-full border text-[11px] bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800">generic mailbox</span>}
                  <span className={`px-2 py-0.5 rounded-full border text-[11px] ${e.ai_used ? 'bg-blue-50 border-blue-200 text-blue-700 dark:bg-blue-950 dark:border-blue-800' : 'bg-zinc-100 border-zinc-300 text-zinc-600 dark:bg-zinc-800'}`}>{e.ai_used ? 'AI written' : 'template'}</span>
                </div>
                {e.job ? (
                  <div className="mt-2 text-xs border dark:border-zinc-800 rounded-lg p-2 bg-zinc-50 dark:bg-zinc-800/60">
                    <div className="flex items-center gap-1.5 font-medium text-zinc-700 dark:text-zinc-200">
                      <Briefcase className="w-3.5 h-3.5 text-zinc-400"/> {e.job.title || 'Untitled role'}
                      {e.job.url && <a href={e.job.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 font-normal">posting <ExternalLink className="w-3 h-3"/></a>}
                    </div>
                    {e.job.jd_excerpt ? (
                      <>
                        <button onClick={()=>setOpenJd(openJd===e.id?null:e.id)} className="mt-1 text-[11px] mono text-zinc-500 underline">
                          {openJd===e.id ? 'hide job description' : 'show job description'} ({e.job.jd_length} chars)
                        </button>
                        {openJd===e.id && <pre className="mt-1 whitespace-pre-wrap text-[11px] mono text-zinc-600 dark:text-zinc-300 max-h-48 overflow-auto">{e.job.jd_excerpt}</pre>}
                      </>
                    ) : <div className="mt-1 text-[11px] mono text-amber-600 dark:text-amber-400">No JD attached — this draft is not tied to a posting.</div>}
                  </div>
                ) : (
                  <div className="mt-2 text-[11px] mono text-amber-600 dark:text-amber-400">Not attached to a job posting — pick a job when drafting so the message references the role.</div>
                )}
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
