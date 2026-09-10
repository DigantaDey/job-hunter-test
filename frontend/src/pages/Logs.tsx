import { useEffect, useState } from 'react'
import client from '../api/client'
import { ScrollText, AlertTriangle, Info, Filter } from 'lucide-react'

export default function Logs(){
  const [logs, setLogs]=useState<any[]>([])
  const [filter, setFilter]=useState('')
  useEffect(()=>{ client.get('/api/logs', {params: filter?{pipeline:filter}:{}}).then(r=>setLogs(r.data)) },[filter])
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold flex items-center gap-2"><ScrollText className="w-5 h-5"/> Error logs <span className="text-xs mono font-normal text-zinc-500">accessible via dashboard</span></h1>
      <div className="card p-3 flex items-center gap-2">
        <Filter className="w-4 h-4 text-zinc-500"/><span className="text-xs mono">Pipeline:</span>
        {['','discovery','application','ai','email','vault','general'].map(p=> <button key={p} onClick={()=>setFilter(p)} className={`text-xs px-3 py-1 rounded-full border mono ${filter===p?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-800'}`}>{p||'all'}</button>)}
      </div>
      <div className="card overflow-hidden">
        <div className="max-h-[70vh] overflow-auto divide-y dark:divide-zinc-800">
          {logs.length===0 ? <div className="p-8 text-center mono text-sm text-zinc-500">No logs yet.</div> :
            logs.map(l=> (
              <div key={l.id} className="p-3 flex gap-3">
                <div className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 ${l.level==='error'?'bg-red-100 dark:bg-red-950 text-red-600':'bg-blue-100 dark:bg-blue-950 text-blue-600'}`}>{l.level==='error'?<AlertTriangle className="w-4 h-4"/>:<Info className="w-4 h-4"/>}</div>
                <div className="min-w-0 flex-1">
                  <div className="text-xs mono text-zinc-500">{new Date(l.timestamp).toLocaleString()} • {l.pipeline} • {l.level} {l.job_id && `• job #${l.job_id}`}</div>
                  <div className="text-sm mt-1">{l.message}</div>
                  {l.meta && Object.keys(l.meta).length>0 && <pre className="mt-1 text-xs mono bg-zinc-50 dark:bg-zinc-800 border dark:border-zinc-700 rounded-lg p-2 overflow-auto">{JSON.stringify(l.meta, null, 2)}</pre>}
                </div>
              </div>
            ))}
        </div>
      </div>
    </div>
  )
}
