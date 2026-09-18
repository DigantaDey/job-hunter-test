import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, FileText, Loader2, RefreshCw, ShieldCheck, Upload, AlertTriangle } from 'lucide-react'
import client, { apiError } from '../api/client'

/** Candidate-only onboarding shell. It intentionally does not use Layout: provider,
 * queue, billing and owner controls must never be part of this experience. */
export default function Onboarding() {
  const navigate = useNavigate()
  const input = useRef<HTMLInputElement>(null)
  const [status, setStatus] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const load = async () => { try { const { data } = await client.get('/api/onboarding/status'); setStatus(data); setError('') } catch (e) { setError(apiError(e, 'Could not restore onboarding')) } finally { setLoading(false) } }
  useEffect(() => { void load() }, [])
  useEffect(() => {
    if (!status || !['resume_processing'].includes(status.state)) return
    const id = window.setInterval(() => void load(), 2500)
    return () => window.clearInterval(id)
  }, [status?.state])

  const upload = async (file?: File) => {
    if (!file) return
    setBusy(true); setError('')
    try { const form = new FormData(); form.append('file', file); const { data } = await client.post('/api/onboarding/resume', form, { headers: { 'Content-Type': 'multipart/form-data' } }); setStatus(data) }
    catch (e) { setError(apiError(e, 'Upload failed. Your existing progress was kept.')) }
    finally { setBusy(false) }
  }
  if (loading) return <Shell><div role="status" className="flex items-center gap-2"><Loader2 className="animate-spin"/> Restoring your onboarding…</div></Shell>
  const progress = status?.progress?.percent ?? (status?.state === 'resume_processing' ? 50 : 0)
  const processing = status?.state === 'resume_processing'
  const blocked = status?.state === 'extraction_blocked'
  const review = status?.state === 'profile_review_required'
  return <Shell>
    <div className="max-w-2xl mx-auto">
      <div className="flex justify-between items-center mb-10"><div className="font-semibold text-lg">JobHunter</div><span className="text-xs text-zinc-500">Your private setup</span></div>
      <div className="mb-8"><p className="text-sm text-blue-600 font-medium">STEP 1 OF 3</p><h1 className="text-3xl font-bold mt-2">Build your job-search profile</h1><p className="mt-3 text-zinc-600 dark:text-zinc-400">We use your resume to find relevant roles and keep applications accurate. You stay in control: nothing is submitted without your approval.</p></div>
      {error && <div role="alert" className="mb-4 p-3 rounded-xl border border-red-200 bg-red-50 text-red-700 text-sm">{error}</div>}
      {status?.state === 'awaiting_resume' && <section className="card p-8 text-center"><Upload className="mx-auto w-10 h-10 text-blue-600"/><h2 className="font-semibold text-lg mt-3">Upload your resume</h2><p className="text-sm text-zinc-500 mt-2">PDF or DOCX. We’ll extract only information useful for discovery, matching, and application safety.</p><input ref={input} className="sr-only" type="file" accept=".pdf,.docx,application/pdf" onChange={e => void upload(e.target.files?.[0])} /><button disabled={busy} onClick={() => input.current?.click()} className="mt-6 px-5 py-3 rounded-xl bg-blue-600 text-white font-medium focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50">{busy ? 'Uploading…' : 'Choose resume'}</button><p className="mt-4 text-xs text-zinc-500">You can leave and come back at any time.</p></section>}
      {processing && <section className="card p-8"><div className="flex items-center gap-3"><Loader2 className="animate-spin text-blue-600"/><div><h2 className="font-semibold">Extracting your profile</h2><p className="text-sm text-zinc-500">This continues safely in the background. You may close this page.</p></div></div><div className="mt-6 h-3 rounded-full bg-zinc-100 overflow-hidden"><div className="h-full bg-blue-600 transition-all" style={{width: `${progress}%`}} /></div><div className="mt-2 text-xs text-zinc-500" role="status">{status?.progress?.label || status?.progress?.detail || 'Reading your resume…'} {progress}%</div><button onClick={() => void load()} className="mt-5 text-sm underline inline-flex gap-2 items-center"><RefreshCw className="w-4 h-4"/> Check status</button></section>}
      {blocked && <section className="card p-8"><AlertTriangle className="text-amber-600"/><h2 className="font-semibold mt-3">We couldn’t finish extraction</h2><p className="text-sm text-zinc-600 mt-2">{status.blocked?.message || 'Your saved upload is still here.'}</p>{status.blocked?.retryable && <button onClick={async () => { setBusy(true); try { const {data}=await client.post('/api/onboarding/retry'); setStatus(data) } catch(e){setError(apiError(e,'Retry failed'))} finally{setBusy(false)} }} className="mt-5 px-4 py-2 rounded-xl bg-zinc-900 text-white">{busy ? 'Retrying…' : 'Retry extraction'}</button>}<button onClick={() => input.current?.click()} className="ml-3 mt-5 px-4 py-2 rounded-xl border">Upload a different resume</button><input ref={input} className="sr-only" type="file" accept=".pdf,.docx" onChange={e=>void upload(e.target.files?.[0])}/></section>}
      {review && <section className="card p-8"><Check className="text-emerald-600"/><h2 className="font-semibold text-lg mt-3">Your profile is ready to review</h2><p className="text-sm text-zinc-600 mt-2">We’ll show what was confirmed, uncertain, or missing, with evidence from your resume. Required safety details must be resolved before discovery starts.</p><button onClick={() => navigate('/profile-review')} className="mt-6 px-5 py-3 rounded-xl bg-blue-600 text-white">Review my profile</button></section>}
      <div className="mt-8 flex gap-3 text-xs text-zinc-500"><ShieldCheck className="w-4 h-4"/><span>Your data is used for matching and safe applications. Optional preferences can be skipped; required safety questions cannot.</span></div>
    </div>
  </Shell>
}
function Shell({children}:{children:ReactNode}) { return <main className="min-h-screen bg-zinc-50 dark:bg-zinc-950 text-zinc-900 dark:text-zinc-100 p-5 md:p-10">{children}</main> }
