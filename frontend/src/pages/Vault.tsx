import { useEffect, useRef, useState } from 'react'
import client, { downloadVaultCsv } from '../api/client'
import { Vault as VaultIcon, Download, Trash2, Shield, ExternalLink, KeyRound } from 'lucide-react'

export default function Vault(){
  const [entries, setEntries]=useState<any[]>([])
  // cancelled-ref guard (same pattern as Dashboard): the async read below must
  // never setState after the page has unmounted.
  const mounted = useRef(true)
  useEffect(()=>{
    mounted.current = true
    return ()=>{ mounted.current = false }
  },[])
  const load=()=> client.get('/api/vault').then(r=>{ if(mounted.current) setEntries(r.data) })
  useEffect(()=>{ load() },[])

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold flex items-center gap-2"><VaultIcon className="w-5 h-5"/> Vault <span className="text-xs mono font-normal text-zinc-500">auto-created credentials • Chrome & Apple Keychain compatible</span></h1>

      <div className="card p-5">
        <div className="flex flex-wrap gap-2">
          <button onClick={()=>void downloadVaultCsv('chrome')} className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm inline-flex items-center gap-2 focus:outline-none focus:ring-2 focus:ring-blue-500"><Download className="w-4 h-4"/> Download Chrome CSV</button>
          <button onClick={()=>void downloadVaultCsv('apple')} className="px-4 py-2 rounded-full border dark:border-zinc-700 text-sm inline-flex items-center gap-2 focus:outline-none focus:ring-2 focus:ring-blue-500"><Download className="w-4 h-4"/> Download Apple Keychain CSV</button>
          <button onClick={async()=>{ if(confirm('Delete ALL credentials forever? This cannot be undone.')){ await client.delete('/api/vault'); load() } }} className="px-4 py-2 rounded-full bg-red-600 text-white text-sm inline-flex items-center gap-2"><Trash2 className="w-4 h-4"/> Delete all forever</button>
        </div>
        <div className="mt-3 text-xs mono text-zinc-500 flex items-start gap-2"><Shield className="w-4 h-4 shrink-0"/> Passwords encrypted (Fernet). Exports: Chrome format (name,url,username,password) • Apple format (Title,URL,Username,Password,Notes,OTPAuth). Choose your platform import.</div>
      </div>

      <div className="card overflow-hidden">
        <div className="p-3 border-b dark:border-zinc-800 flex items-center justify-between"><span className="text-sm font-medium mono">{entries.length} credentials</span><span className="text-xs mono text-zinc-500 inline-flex items-center gap-1"><KeyRound className="w-3.5 h-3.5"/> auto-generated for Workday/Lever/Greenhouse</span></div>
        {entries.length===0 ? <div className="p-8 text-center mono text-sm text-zinc-500">No credentials yet. Apply to a Workday/Lever job to auto-create one.</div> :
          <div className="divide-y dark:divide-zinc-800">
            {entries.map((e:any)=> (
              <div key={e.id} className="p-4 flex items-center gap-3">
                <div className="w-9 h-9 rounded-xl bg-zinc-900 dark:bg-zinc-800 text-white flex items-center justify-center text-xs font-bold">{e.domain.slice(0,2).toUpperCase()}</div>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium flex items-center gap-2">{e.domain} <ExternalLink className="w-3 h-3 text-zinc-400"/></div>
                  <div className="text-xs mono text-zinc-500 truncate">{e.username} • {new Date(e.created_at).toLocaleString()}</div>
                </div>
                <span className="text-xs mono px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800">password • encrypted</span>
              </div>
            ))}
          </div>
        }
      </div>
    </div>
  )
}
