import { useEffect, useState } from 'react'
import client from '../api/client'
import { TrendingUp, ExternalLink, Mail, Briefcase, Loader2, Sparkles } from 'lucide-react'

export default function Funding(){
  const [companies, setCompanies]=useState<any[]>([])
  const [busy, setBusy]=useState<string>('')
  const [msg, setMsg]=useState('')
  const load=()=> client.get('/api/funding/companies').then(r=>setCompanies(r.data))
  useEffect(()=>{ load() },[])

  const process=async(name:string)=>{
    setBusy(name); setMsg('')
    try{
      const {data}=await client.post(`/api/funding/${encodeURIComponent(name)}/process`)
      setMsg(`${data.action}: ${data.message}`)
    } finally{ setBusy('')}
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold flex items-center gap-2"><TrendingUp className="w-5 h-5"/> Funding Radar <span className="text-xs mono font-normal text-zinc-500">up to Series D • open positions or founder cold email</span></h1>
      <div className="card p-4 bg-gradient-to-br from-blue-50 to-violet-50 dark:from-blue-950/30 dark:to-violet-950/30 border-blue-200 dark:border-blue-900">
        <div className="text-sm font-medium flex items-center gap-2"><Sparkles className="w-4 h-4 text-blue-600"/> How it works</div>
        <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">Searches companies that recently raised up to Series D → checks open positions → if available, applies via usual flow; else researches founder & cold-emails about user’s accomplishments to pitch need for the department.</div>
      </div>
      {msg && <div className="p-3 rounded-xl bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-sm mono text-emerald-800 dark:text-emerald-200">{msg}</div>}
      <div className="grid md:grid-cols-2 gap-3">
        {companies.map(c=> (
          <div key={c.name} className="card p-4">
            <div className="flex items-start justify-between">
              <div>
                <div className="text-sm font-semibold">{c.name}</div>
                <div className="text-xs mono text-zinc-500">{c.stage} • raised {c.raised_at} • <a href={`https://${c.website}`} target="_blank" className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400">{c.website} <ExternalLink className="w-3 h-3"/></a></div>
              </div>
              <span className={`text-xs px-2 py-1 rounded-full mono border ${c.has_open_positions?'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950':'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950'}`}>{c.has_open_positions?'open roles':'no opening'}</span>
            </div>
            <div className="mt-3 flex gap-2">
              <button onClick={()=>process(c.name)} disabled={!!busy} className="flex-1 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-xs font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50">
                {busy===c.name ? <Loader2 className="w-3.5 h-3.5 animate-spin"/> : c.has_open_positions ? <Briefcase className="w-3.5 h-3.5"/> : <Mail className="w-3.5 h-3.5"/>} {c.has_open_positions ? 'Apply usual flow' : 'Cold email founder'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
