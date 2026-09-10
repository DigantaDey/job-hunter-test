import { useEffect, useState } from 'react'
import client from '../api/client'
import { Mail, Send, Check, Pencil, Search, Building2, User, Clock, AlertTriangle } from 'lucide-react'

export default function Emails(){
  const [emails, setEmails]=useState<any[]>([])
  const [filter, setFilter]=useState('')
  const [company, setCompany]=useState('')
  const [dept, setDept]=useState('engineering')
  const [jobs, setJobs]=useState<any[]>([])

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
  const send=async(id:number, otp?:string)=>{
    const res=await client.post(`/api/emails/${id}/send`, null, {params: otp?{otp}:{}})
    if(res.data.needs_otp){
      const code=prompt('SMTP 2FA required — enter OTP / app password:')
      if(code) send(id, code)
    } else load()
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
          {['','pending_approval','queued','sent','needs_otp'].map(s=> <button key={s} onClick={()=>setFilter(s)} className={`text-xs px-3 py-1 rounded-full border mono ${filter===s?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-800'}`}>{s||'all'}</button>)}
        </div>
      </div>

      <div className="grid gap-3">
        {emails.length===0 ? <div className="card p-8 text-center mono text-sm text-zinc-500">No emails in bucket. Generate for startups/small companies — finds hiring manager via AI + hunter pattern, drafts with SMTP (handles 2FA OTP).</div> :
        emails.map(e=> (
          <div key={e.id} className="card p-4">
            <div className="flex flex-wrap gap-3 items-start justify-between">
              <div>
                <div className="text-sm font-medium flex items-center gap-2"><Building2 className="w-4 h-4 text-zinc-400"/>{e.company} <span className="text-zinc-500 font-normal">→ {e.to_name} &lt;{e.to_email}&gt;</span></div>
                <div className="text-xs mono text-zinc-500 flex gap-2"><span>{e.recipient_type}</span><span>•</span><span>{new Date(e.created_at).toLocaleString()}</span><span>•</span><span className={`px-2 py-0.5 rounded-full border text-[11px] ${e.status==='sent'?'bg-emerald-50 border-emerald-200 text-emerald-700':'bg-amber-50 border-amber-200 text-amber-700 dark:bg-zinc-800'}`}>{e.status}</span></div>
              </div>
              <div className="flex gap-2">
                {e.status==='pending_approval' && <button onClick={()=>approve(e.id)} className="px-3 py-1.5 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-xs inline-flex items-center gap-1"><Check className="w-3.5 h-3.5"/> Approve → queue</button>}
                {(e.status==='queued' || e.status==='needs_otp') && <button onClick={()=>send(e.id)} className="px-3 py-1.5 rounded-full bg-blue-600 text-white text-xs inline-flex items-center gap-1"><Send className="w-3.5 h-3.5"/> Send via SMTP</button>}
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
              {e.status==='needs_otp' && <div className="mt-2 text-xs mono p-2 rounded-lg bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 flex gap-2"><AlertTriangle className="w-3.5 h-3.5"/> SMTP 2FA needed — click Send and provide OTP / verify on device. Handles app passwords.</div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
