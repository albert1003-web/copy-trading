import { useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { api } from './api'
import Dashboard from './pages/Dashboard'
import Trades from './pages/Trades'
import Watchlist from './pages/Watchlist'
import Leaderboard from './pages/Leaderboard'
import Positions from './pages/Positions'
import Agents from './pages/Agents'
import Pipeline from './pages/Pipeline'

const NAV = [
  { to: '/', label: 'Dashboard' },
  { to: '/trades', label: 'Trades' },
  { to: '/watchlist', label: 'Watchlist' },
  { to: '/leaderboard', label: 'Leaderboard' },
  { to: '/positions', label: 'Positions' },
  { to: '/agents', label: 'Agents' },
  { to: '/pipeline', label: 'Pipeline' },
]

export default function App() {
  const [stopped, setStopped] = useState(false)

  const quit = async () => {
    if (!confirm('Quit Trade Tracker?')) return
    await api.post('/shutdown').catch(() => {})
    setStopped(true)
  }

  if (stopped) {
    return (
      <main className="stopped">
        <h1>Trade Tracker has stopped</h1>
        <p className="muted">You can close this tab. Open the app again to restart it.</p>
      </main>
    )
  }

  return (
    <div className="layout">
      <header>
        <span className="brand">Trade Tracker</span>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === '/'}>{n.label}</NavLink>
          ))}
        </nav>
        <button className="ghost" onClick={quit}>Quit</button>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/trades" element={<Trades />} />
          <Route path="/watchlist" element={<Watchlist />} />
          <Route path="/leaderboard" element={<Leaderboard />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/pipeline" element={<Pipeline />} />
          <Route path="*" element={<p className="muted">Page not found.</p>} />
        </Routes>
      </main>
    </div>
  )
}
