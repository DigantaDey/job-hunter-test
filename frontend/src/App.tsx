import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Jobs from './pages/Jobs'
import Queues from './pages/Queues'
import Resumes from './pages/Resumes'
import Emails from './pages/Emails'
import Vault from './pages/Vault'
import Settings from './pages/Settings'
import Logs from './pages/Logs'
import Funding from './pages/Funding'

export default function App(){
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout/>}>
          <Route path="/" element={<Dashboard/>} />
          <Route path="/jobs" element={<Jobs/>} />
          <Route path="/queues" element={<Queues/>} />
          <Route path="/resumes" element={<Resumes/>} />
          <Route path="/emails" element={<Emails/>} />
          <Route path="/funding" element={<Funding/>} />
          <Route path="/vault" element={<Vault/>} />
          <Route path="/settings" element={<Settings/>} />
          <Route path="/logs" element={<Logs/>} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
