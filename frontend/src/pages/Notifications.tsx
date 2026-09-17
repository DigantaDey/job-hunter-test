import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import client from '../api/client'
import { Bell, CheckCheck, Trash2 } from 'lucide-react'

export default function Notifications() {
  const [notifs, setNotifs] = useState<any[]>([])
  const [filter, setFilter] = useState<'all'|'unread'>('all')
  // cancelled-ref guard (same pattern as Dashboard): the async read below must
  // never setState after the page has unmounted.
  const mounted = useRef(true)
  useEffect(()=>{
    mounted.current = true
    return ()=>{ mounted.current = false }
  },[])

  const load = ()=>{
    client.get(`/api/notifications?unread_only=${filter==='unread'}`).then(r=>{ if(mounted.current) setNotifs(r.data) }).catch(()=>{})
  }
  useEffect(()=>{ load() },[filter])

  const markAll = async ()=>{
    await client.post('/api/notifications/read-all')
    load()
  }

  return (
    <div className="space-y-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold flex items-center gap-2"><Bell className="w-5 h-5"/> Notifications</h1>
        <div className="flex gap-2">
          <button onClick={()=>setFilter('all')} className={`px-3 py-1 rounded-full text-xs border ${filter==='all'?'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900':''}`}>All</button>
          <button onClick={()=>setFilter('unread')} className={`px-3 py-1 rounded-full text-xs border ${filter==='unread'?'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900':''}`}>Unread</button>
          <button onClick={markAll} className="px-3 py-1 rounded-full text-xs border flex items-center gap-1"><CheckCheck className="w-3 h-3"/>Mark all read</button>
        </div>
      </div>

      <div className="space-y-2">
        {notifs.map(n=>(
          <div key={n.id} className={`card p-4 flex gap-3 ${!n.read?'bg-blue-50/50 dark:bg-blue-950/20 border-blue-200 dark:border-blue-800':''}`}>
            <div className="flex-1">
              <div className="text-sm font-medium">{n.title}</div>
              <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">{n.body}</div>
              <div className="text-[11px] mono text-zinc-500 mt-1">{n.kind} • {new Date(n.created_at).toLocaleString()}</div>
              {/* Internal links (/...) route client-side via <Link> — a bare
                  <a href> would force a full page reload. External links
                  (http...) open in a new tab. */}
              {n.link && (typeof n.link === 'string' && n.link.startsWith('/') ? (
                <Link to={n.link} className="text-xs text-blue-600 dark:text-blue-400 mt-1 inline-block">{n.link} →</Link>
              ) : (
                <a href={n.link} target="_blank" rel="noreferrer" className="text-xs text-blue-600 dark:text-blue-400 mt-1 inline-block">{n.link} ↗</a>
              ))}
            </div>
            <div className="flex flex-col gap-1">
              {!n.read && <button onClick={async()=>{ await client.post(`/api/notifications/${n.id}/read`); load()}} className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800"><CheckCheck className="w-4 h-4"/></button>}
              <button onClick={async()=>{ await client.delete(`/api/notifications/${n.id}`); load()}} className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800"><Trash2 className="w-4 h-4 text-zinc-500"/></button>
            </div>
          </div>
        ))}
        {notifs.length===0 && <div className="card p-8 text-center text-sm mono text-zinc-500">No notifications</div>}
      </div>

      <div className="card p-4">
        <h3 className="font-medium text-sm">Preferences</h3>
        <div className="mt-2 text-xs mono text-zinc-500">Granular notification controls: high-match alerts, automation failures, follow-up reminders, interview reminders, new replies, weekly summaries. Configure in Settings → Notifications.</div>
      </div>
    </div>
  )
}
