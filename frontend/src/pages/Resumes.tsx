import { useEffect, useState } from 'react'
import client from '../api/client'
import { Upload, FileText, Wand2, Tag, Download, Eye, Brain, ShieldCheck, Sparkles, Layers, Check, Loader2 } from 'lucide-react'

export default function Resumes(){
  const [resumes, setResumes] = useState<any[]>([])
  const [profile, setProfile] = useState<any>(null)
  const [layout, setLayout] = useState<any>(null)
  const [jobs, setJobs] = useState<any[]>([])
  const [selectedJob, setSelectedJob] = useState('')
  const [strict, setStrict] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [preview, setPreview] = useState<any>(null)
  const [editTags, setEditTags] = useState<Record<number,string>>({})

  const load = async()=>{
    const {data}=await client.get('/api/resumes')
    setResumes(data)
    try{ const p=await client.get('/api/profile/current'); setProfile(p.data); setLayout(p.data.layout)}catch{}
    const j=await client.get('/api/jobs'); setJobs(j.data)
  }
  useEffect(()=>{ load() },[])

  const upload = async(e:any)=>{
    const file=e.target.files[0]
    if(!file) return
    const fd=new FormData(); fd.append('file', file)
    await client.post('/api/resume/upload', fd)
    load()
  }
  const generate = async()=>{
    if(!selectedJob) return alert('Pick a job')
    setGenerating(true)
    try{
      const {data}=await client.post('/api/resumes/generate', null, {params:{job_id: selectedJob, strict_skeleton: strict}})
      setPreview(data)
      load()
    }finally{ setGenerating(false)}
  }
  const saveTags = async(id:number)=>{
    await client.put(`/api/resumes/${id}/tags`, JSON.parse(editTags[id] || '[]'))
    load()
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2"><FileText className="w-5 h-5"/> Resume Studio</h1>

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card p-5 space-y-4">
          <h3 className="font-medium flex items-center gap-2"><Upload className="w-4 h-4"/> Master resume</h3>
          <label className="block border-2 border-dashed dark:border-zinc-700 rounded-xl p-6 text-center cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/50">
            <Upload className="w-6 h-6 mx-auto text-zinc-400"/>
            <div className="text-sm mono mt-2">Upload master resume (PDF/DOCX)</div>
            <div className="text-xs text-zinc-500">AI extracts profile + layout (margins, bullets, colors, hyperlinks…)</div>
            <input type="file" accept=".pdf,.docx,.doc" onChange={upload} className="hidden"/>
          </label>
          {profile && (
            <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3 space-y-2">
              <div className="text-sm font-medium">{profile.data.name} • {profile.data.email}</div>
              <div className="text-xs mono text-zinc-600 dark:text-zinc-400 line-clamp-3">{profile.data.summary?.slice(0,220)}</div>
              <div className="flex flex-wrap gap-1">{profile.data.skills?.slice(0,8).map((s:string)=> <span key={s} className="text-[11px] px-2 py-1 rounded-full bg-white dark:bg-zinc-900 border mono">{s}</span>)}</div>
              <details className="text-xs mono">
                <summary className="cursor-pointer text-zinc-500">Layout JSON (margins, bullets, colors…)</summary>
                <pre className="mt-2 bg-white dark:bg-zinc-900 border dark:border-zinc-700 rounded-lg p-2 overflow-auto max-h-40 text-[11px]">{JSON.stringify(layout, null, 2)}</pre>
              </details>
            </div>
          )}
          <div className="pt-2 border-t dark:border-zinc-800">
            <h4 className="text-sm font-medium flex items-center gap-2"><Wand2 className="w-4 h-4"/> Generate tailored resume</h4>
            <div className="mt-2 space-y-2">
              <select value={selectedJob} onChange={e=>setSelectedJob(e.target.value)} className="w-full border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700">
                <option value="">Pick a job…</option>
                {jobs.map(j=> <option key={j.id} value={j.id}>{j.title} • {j.company} (score {j.score})</option>)}
              </select>
              <label className="flex items-center gap-2 text-xs mono"><input type="checkbox" checked={strict} onChange={e=>setStrict(e.target.checked)}/> Strictly maintain uploaded skeleton</label>
              <label className="flex items-center gap-2 text-xs mono"><input type="checkbox" checked={!strict} onChange={e=>setStrict(!e.target.checked)}/> AI-generated ATS-friendly format</label>
              <button onClick={generate} disabled={generating} className="w-full py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50">
                {generating ? <Loader2 className="w-4 h-4 animate-spin"/> : <Sparkles className="w-4 h-4"/>} Generate with JD fact guard
              </button>
              <div className="text-[11px] mono text-zinc-500 flex items-start gap-1"><ShieldCheck className="w-3.5 h-3.5 mt-0.5 shrink-0"/> Adds memory + JD facts, never hallucinates new companies/dates/skills. You approve before upload.</div>
              {preview && (
                <div className="bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 rounded-xl p-3">
                  <div className="text-xs mono font-medium text-emerald-800 dark:text-emerald-200">Generated • score {preview.score} • {preview.reason}</div>
                  <div className="text-xs mono mt-1">Tags: {preview.tags.join(', ')}</div>
                  <div className="mt-2 flex gap-2">
                    <a href={preview.files.docx} className="text-xs px-3 py-1 rounded-full bg-white dark:bg-zinc-900 border inline-flex items-center gap-1"><Download className="w-3 h-3"/> DOCX</a>
                    <a href={preview.files.pdf} className="text-xs px-3 py-1 rounded-full bg-white dark:bg-zinc-900 border inline-flex items-center gap-1"><Download className="w-3 h-3"/> PDF</a>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="lg:col-span-2 card p-5">
          <div className="flex items-center justify-between">
            <h3 className="font-medium flex items-center gap-2"><Layers className="w-4 h-4"/> All resumes • auto-tagged</h3>
            <span className="text-xs mono text-zinc-500">{resumes.length} files</span>
          </div>
          <div className="mt-3 grid md:grid-cols-2 gap-3">
            {resumes.map(r=> (
              <div key={r.id} className="border dark:border-zinc-800 rounded-xl p-3 bg-white dark:bg-zinc-900">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate flex items-center gap-2"><FileText className="w-4 h-4 text-zinc-400"/>{r.filename}</div>
                    <div className="text-xs mono text-zinc-500">{r.type} • {new Date(r.created_at).toLocaleDateString()} • {r.tags.join(', ') || 'no tags'}</div>
                  </div>
                  <span className={`text-[11px] px-2 py-1 rounded-full mono border ${r.type==='master'?'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900': 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300'}`}>{r.type}</span>
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {r.tags.map((t:string)=> <span key={t} className="text-[11px] px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 mono inline-flex items-center gap-1"><Tag className="w-3 h-3"/>{t}</span>)}
                </div>
                <div className="mt-3 flex gap-2">
                  <a href={`/api/resumes/${r.id}/download?format=docx`} className="flex-1 py-1.5 rounded-full border dark:border-zinc-700 text-xs text-center inline-flex items-center justify-center gap-1"><Download className="w-3 h-3"/> DOCX</a>
                  <a href={`/api/resumes/${r.id}/download?format=pdf`} className="flex-1 py-1.5 rounded-full border dark:border-zinc-700 text-xs text-center inline-flex items-center justify-center gap-1"><Download className="w-3 h-3"/> PDF</a>
                </div>
                <div className="mt-2 flex gap-1">
                  <input placeholder='["backend","python"]' value={editTags[r.id] ?? JSON.stringify(r.tags)} onChange={e=>setEditTags({...editTags,[r.id]:e.target.value})} className="flex-1 border rounded-full px-2 py-1 text-xs mono bg-white dark:bg-zinc-800 dark:border-zinc-700"/>
                  <button onClick={()=>saveTags(r.id)} className="px-2 py-1 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-xs"><Check className="w-3 h-3"/></button>
                </div>
                <div className="mt-2 text-[11px] mono text-zinc-500">Before portal upload: approve / download / choose master / pick tagged resume • scoring decides reuse vs new</div>
              </div>
            ))}
          </div>
          {resumes.length===0 && <div className="py-12 text-center mono text-sm text-zinc-500">No resumes yet. Upload master to start.</div>}
        </div>
      </div>

      <div className="card p-5">
        <h3 className="font-medium flex items-center gap-2"><Brain className="w-4 h-4"/> How resume decisions work</h3>
        <div className="mt-3 grid md:grid-cols-3 gap-3 text-sm">
          <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3"><div className="font-medium mono text-xs">Scoring</div><div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">Hybrid heuristic (TF-IDF cosine + coverage) and AI scoring. If score &lt;60, skip generation; if &gt;75 and existing resume similarity &gt;0.85, reuse tagged resume.</div></div>
          <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3"><div className="font-medium mono text-xs">Fact guard</div><div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">Prompt includes “never invent companies, degrees, dates, skills not in profile”. Only rephrase existing bullets to match JD.</div></div>
          <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3"><div className="font-medium mono text-xs">Approval</div><div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">Before upload, you see preview → approve / download / upload polished version → system uses that. All generated resumes auto-tagged.</div></div>
        </div>
      </div>
    </div>
  )
}
