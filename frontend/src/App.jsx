import { Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth.jsx'
import { Page, Spinner } from './components/Layout.jsx'

import Landing from './pages/Landing.jsx'
import Commands from './pages/Commands.jsx'
import Setup from './pages/Setup.jsx'
import Legal from './pages/Legal.jsx'
import Dashboard from './pages/Dashboard.jsx'
import ServerDetail from './pages/ServerDetail.jsx'
import AdminPremium from './pages/AdminPremium.jsx'
import Tickets from './pages/Tickets.jsx'
import TicketDetail from './pages/TicketDetail.jsx'

/** Gate a route behind sign-in (and optionally master privileges). */
function Protected({ children, ownerOnly = false }) {
  const { user, loading } = useAuth()
  if (loading) return <Page><Spinner /></Page>
  if (!user?.authenticated) return <Navigate to="/" replace />
  if (ownerOnly && !user.isOwner) return <Navigate to="/dashboard" replace />
  return children
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/commands" element={<Commands />} />
        <Route path="/setup" element={<Setup />} />
        <Route path="/terms" element={<Legal kind="terms" />} />
        <Route path="/privacy" element={<Legal kind="privacy" />} />

        <Route path="/dashboard" element={<Protected><Dashboard /></Protected>} />
        <Route path="/servers/:guildId" element={<Protected><ServerDetail /></Protected>} />
        <Route path="/tickets" element={<Protected><Tickets /></Protected>} />
        <Route path="/tickets/:ticketId" element={<Protected><TicketDetail /></Protected>} />
        <Route
          path="/admin/premium"
          element={<Protected ownerOnly><AdminPremium /></Protected>}
        />

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AuthProvider>
  )
}
