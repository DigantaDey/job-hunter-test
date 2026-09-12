import { useCallback, useEffect, useState } from 'react'
import client, { apiError, aiOutage, type AIOutage } from '../api/client'
import { Users, Plus, Sparkles, Target, Brain, RefreshCw, Loader2, Check, Trash2, Pencil, XCircle, BadgeCheck } from 'lucide-react'

/**
 * User personas — one job-search track each.
 *
 * Each persona carries its own search context, preferences and learned memory,
 * and drives discovery → scoring → resume → outreach for that track, so running
 * "Data Analyst" and "Data Scientist" in parallel never blurs into one generic
 * search. The portrait is rebuilt from observed evidence only.
 */
type Persona = {
  id: number
  name: string
  target_role: string
  is_active: boolean
  is_default: boolean
  search_context: Record<string, any>
  preferences: Record<string, any>
  portrait: {
    headline?: string
    identity?: string
    strengths?: string[]
    gaps?: string[]
    positioning?: string
    search_directives?: string[]
    outreach_angle?: string
  }
  portrait_at?: string | null
  stats?: Record<string, any>
  memory?: {
    signals?: number
    maturity?: string
    engagement?: Record<string, number>
    observed_titles?: Record<string, number>
    observed_skills?: Record<string, number>
  }
}

export default function Personas() {
  const [personas, setPersonas] = useState<Persona[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [suggestions, setSuggestions] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [outage, setOutage] = useState<AIOutage | null>(null)
  const [notice, setNotice] = useState('')
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ name: '', target_role: '', keywords: '' })

  const load = useCallback(async () => {
    try {
      const { data } = await client.get('/api/personas')
      setPersonas(data.personas || [])
      setActiveId(data.active_id ?? null)
    } catch (e: any) {
      setNotice(apiError(e, 'Could not load personas'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const run = async (label: string, fn: () => Promise<any>, okMessage?: string) => {
    setBusy(label); setOutage(null); setNotice('')
    try {
      const result = await fn()
      if (okMessage) setNotice(okMessage)
      await load()
      return result
    } catch (e: any) {
      const o = aiOutage(e)
      if (o) setOutage(o)
      else setNotice(apiError(e, 'Action failed'))
    } finally { setBusy('') }
  }

  const suggest = () => run('suggest', () => client.get('/api/personas/suggestions')
    .then(r => setSuggestions(r.data.tracks || [])), 'Suggestions refreshed')

  const create = () => run('create', async () => {
    if (!form.name.trim()) { setNotice('Give the track a name first'); return }
    await client.post('/api/personas', {
      name: form.name.trim(),
      target_role: form.target_role.trim(),
      keywords: form.keywords.split(',').map(k => k.trim()).filter(Boolean),
    })
    setForm({ name: '', target_role: '', keywords: '' })
    setCreating(false)
  }, 'Track created')

  const createFromSuggestion = (t: any) => run(`create-${t.name}`, () => client.post('/api/personas', {
    name: t.name, target_role: t.target_role, keywords: t.keywords, industries: t.industries, seniority: t.seniority,
  }), `Track "${t.name}" created`)

  const reflect = (p: Persona) => run(`reflect-${p.id}`,
    () => client.post(`/api/personas/${p.id}/reflect`, null, { params: { force: true } }),
    'Portrait rebuilt from your activity')

  const activate = (p: Persona) => run(`activate-${p.id}`, () => client.post(`/api/personas/${p.id}/activate`),
    `"${p.name}" is now the active track`)

  const updateKeywords = (p: Persona, keywords: string) => run(`kw-${p.id}`,
    () => client.put(`/api/personas/${p.id}`, { keywords: keywords.split(',').map(k => k.trim()).filter(Boolean) }),
    'Keywords updated')

  const remove = (p: Persona) => {
    if (!window.confirm(`Delete the "${p.name}" track? Its memory is removed too.`)) return
    run(`del-${p.id}`, () => client.delete(`/api/personas/${p.id}`), 'Track deleted')
  }

  if (loading) return <div className="p-8 mono text-sm text-zinc-500">Loading personas…</div>

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2">
          <Users className="w-5 h-5" /> Personas
          <span className="text-xs mono font-normal text-zinc-500">one job-search track each • learns as you use it</span>
        </h1>
        <div className="flex gap-2">
          <button onClick={suggest} disabled={!!busy} className="px-3 py-2 rounded-full border dark:border-zinc-700 text-xs inline-flex items-center gap-1.5 bg-white dark:bg-zinc-800 disabled:opacity-50">
            {busy === 'suggest' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />} Suggest tracks from my resume
          </button>
          <button onClick={() => setCreating(v => !v)} className="px-4 py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center gap-2">
            <Plus className="w-4 h-4" /> New track
          </button>
        </div>
      </div>

      <div className="card p-4 bg-gradient-to-br from-blue-50 to-violet-50 dark:from-blue-950/30 dark:to-violet-950/30 border-blue-200 dark:border-blue-900">
        <div className="text-sm font-medium flex items-center gap-2"><Brain className="w-4 h-4 text-blue-600" /> Why personas</div>
        <p className="text-xs mono text-zinc-600 dark:text-zinc-300 mt-1 leading-relaxed">
          A persona is the system's model of you <em>for one target role</em>. Every scored job, application, reply and
          edit records a signal against the active track, and the portrait below is rebuilt from that evidence — never
          invented. Discovery, scoring, resume tailoring and outreach all read the active track, so two searches
          (say Data Analyst and Data Scientist) stay distinct instead of averaging into one generic profile.
        </p>
      </div>

      {outage && (
        <div className="p-3 rounded-xl bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-sm">
          <div className="flex items-start gap-2">
            <XCircle className="w-4 h-4 text-red-600 dark:text-red-400 shrink-0 mt-0.5" />
            <div className="min-w-0">
              <div className="font-medium text-red-800 dark:text-red-200">AI unavailable — nothing was generated</div>
              <div className="text-xs mono mt-1 text-red-700 dark:text-red-300">{outage.message}{outage.reason ? ` (${outage.reason})` : ''}</div>
              {outage.fix && <div className="text-xs mono mt-1 text-red-700 dark:text-red-300"><strong>Fix:</strong> {outage.fix}</div>}
            </div>
          </div>
        </div>
      )}
      {notice && <div className="p-3 rounded-xl bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 text-sm mono text-blue-700 dark:text-blue-300">{notice}</div>}

      {creating && (
        <div className="card p-4 grid md:grid-cols-[1fr_1fr_2fr_auto] gap-2 items-end">
          <div>
            <label className="text-xs mono">Track name</label>
            <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="Data Analyst" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" />
          </div>
          <div>
            <label className="text-xs mono">Target role</label>
            <input value={form.target_role} onChange={e => setForm({ ...form, target_role: e.target.value })} placeholder="Senior Data Analyst" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" />
          </div>
          <div>
            <label className="text-xs mono">Keywords (comma-separated)</label>
            <input value={form.keywords} onChange={e => setForm({ ...form, keywords: e.target.value })} placeholder="sql, tableau, product analytics" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" />
          </div>
          <button onClick={create} disabled={busy === 'create'} className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm font-medium inline-flex items-center gap-1 disabled:opacity-50">
            {busy === 'create' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />} Create
          </button>
        </div>
      )}

      {suggestions.length > 0 && (
        <div className="card p-4">
          <div className="text-sm font-medium flex items-center gap-2"><Sparkles className="w-4 h-4" /> Suggested tracks from your resume</div>
          <div className="mt-2 grid md:grid-cols-2 gap-2">
            {suggestions.map((t: any) => (
              <div key={t.name} className="border dark:border-zinc-800 rounded-xl p-3 bg-white dark:bg-zinc-900">
                <div className="text-sm font-medium">{t.name} <span className="text-zinc-500 font-normal">— {t.target_role}</span></div>
                <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">{t.why}</div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {(t.keywords || []).slice(0, 6).map((k: string) => <span key={k} className="text-[11px] px-2 py-0.5 rounded-full bg-zinc-100 dark:bg-zinc-800 mono">{k}</span>)}
                </div>
                <button onClick={() => createFromSuggestion(t)} disabled={busy === `create-${t.name}`} className="mt-2 px-3 py-1 rounded-full bg-blue-600 text-white text-xs inline-flex items-center gap-1 disabled:opacity-50">
                  {busy === `create-${t.name}` ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />} Add this track
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {personas.length === 0 ? (
        <div className="card p-12 text-center">
          <Users className="w-8 h-8 mx-auto text-zinc-300 dark:text-zinc-700" />
          <div className="mono text-sm text-zinc-500 mt-3">No tracks yet — upload a master resume, then create one per target role.</div>
        </div>
      ) : (
        <div className="grid lg:grid-cols-2 gap-3">
          {personas.map(p => (
            <div key={p.id} className={`card p-4 flex flex-col ${p.is_active ? 'border-2 border-blue-300 dark:border-blue-800' : ''}`}>
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm font-semibold flex items-center gap-2">
                    {p.name}
                    {p.is_active && <span className="text-[11px] px-2 py-0.5 rounded-full bg-blue-600 text-white mono">active</span>}
                    {p.is_default && <span className="text-[11px] px-2 py-0.5 rounded-full bg-zinc-200 dark:bg-zinc-700 mono">default</span>}
                  </div>
                  <div className="text-xs mono text-zinc-500 flex items-center gap-1"><Target className="w-3 h-3" /> {p.target_role || 'no target role'}</div>
                </div>
                <div className="flex gap-1">
                  {!p.is_active && (
                    <button onClick={() => activate(p)} disabled={busy === `activate-${p.id}`} className="px-2 py-1 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-[11px] disabled:opacity-50">Make active</button>
                  )}
                  <button onClick={() => remove(p)} disabled={busy === `del-${p.id}`} className="p-1.5 rounded-full hover:bg-red-50 dark:hover:bg-red-950 text-red-500" title="Delete track"><Trash2 className="w-3.5 h-3.5" /></button>
                </div>
              </div>

              <div className="mt-3 text-[11px] mono flex flex-wrap gap-x-3 gap-y-1 text-zinc-500">
                <span>signals {p.memory?.signals ?? 0} ({p.memory?.maturity || 'new'})</span>
                <span>applied {p.memory?.engagement?.applied ?? 0}</span>
                <span>replies {p.memory?.engagement?.reply_received ?? 0}</span>
                <span>interviews {p.memory?.engagement?.interview ?? 0}</span>
                {p.stats?.reply_rate ? <span>reply rate {(p.stats.reply_rate * 100).toFixed(0)}%</span> : null}
              </div>

              <KeywordsEditor value={(p.search_context?.keywords || []).join(', ')} onSave={v => updateKeywords(p, v)} busy={busy === `kw-${p.id}`} />

              <div className="mt-3 flex-1">
                <div className="flex items-center justify-between">
                  <div className="text-xs mono font-medium flex items-center gap-1"><Brain className="w-3.5 h-3.5" /> Portrait {p.portrait_at ? <span className="text-zinc-500 font-normal">• {new Date(p.portrait_at).toLocaleDateString()}</span> : null}</div>
                  <button onClick={() => reflect(p)} disabled={busy === `reflect-${p.id}`} className="text-[11px] px-2 py-1 rounded-full border dark:border-zinc-700 inline-flex items-center gap-1 bg-white dark:bg-zinc-800 disabled:opacity-50">
                    {busy === `reflect-${p.id}` ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />} Rebuild from evidence
                  </button>
                </div>
                {p.portrait?.identity ? (
                  <div className="mt-2 rounded-xl border dark:border-zinc-800 p-3 bg-zinc-50 dark:bg-zinc-800/60 space-y-2">
                    {p.portrait.headline && <div className="text-sm font-medium">{p.portrait.headline}</div>}
                    <p className="text-xs text-zinc-600 dark:text-zinc-300 leading-relaxed">{p.portrait.identity}</p>
                    {(p.portrait.strengths || []).length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {(p.portrait.strengths || []).map(s => <span key={s} className="text-[11px] px-2 py-0.5 rounded-full bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300 mono">{s}</span>)}
                      </div>
                    )}
                    {(p.portrait.gaps || []).length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {(p.portrait.gaps || []).map(g => <span key={g} className="text-[11px] px-2 py-0.5 rounded-full bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 text-amber-700 dark:text-amber-300 mono">gap: {g}</span>)}
                      </div>
                    )}
                    {(p.portrait.search_directives || []).length > 0 && (
                      <div className="text-[11px] mono text-zinc-500">
                        discovery: {(p.portrait.search_directives || []).join(' • ')}
                      </div>
                    )}
                    {p.portrait.outreach_angle && <div className="text-[11px] mono text-zinc-500">outreach hook: {p.portrait.outreach_angle}</div>}
                  </div>
                ) : (
                  <div className="mt-2 text-xs mono text-zinc-500">
                    No portrait yet — it is written from your recorded activity, so use the product for a few days
                    (or click Rebuild once you have some signals) and it will describe you on this track.
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function KeywordsEditor({ value, onSave, busy }: { value: string; onSave: (v: string) => void; busy: boolean }) {
  const [draft, setDraft] = useState(value)
  useEffect(() => { setDraft(value) }, [value])
  return (
    <div className="mt-2">
      <label className="text-[11px] mono text-zinc-500">Search keywords for this track</label>
      <div className="mt-1 flex gap-1">
        <input value={draft} onChange={e => setDraft(e.target.value)} placeholder="data analyst, sql, tableau" className="flex-1 border rounded-full px-3 py-1.5 text-xs mono bg-white dark:bg-zinc-800 dark:border-zinc-700" />
        <button onClick={() => onSave(draft)} disabled={busy || draft === value} className="px-2 py-1 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 disabled:opacity-40" title="Save keywords">
          <Pencil className="w-3 h-3" />
        </button>
      </div>
    </div>
  )
}
