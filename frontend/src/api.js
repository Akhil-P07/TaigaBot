// Thin wrapper over the bot's /api/* routes. Everything is same-origin and
// cookie-authenticated, so there are no tokens to manage in the browser.

async function request(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })

  // 204 and friends have no body to parse.
  const text = await res.text()
  const data = text ? JSON.parse(text) : {}

  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`)
    err.status = res.status
    throw err
  }
  return data
}

export const api = {
  me: () => request('/api/me'),
  logout: () => request('/api/auth/logout', { method: 'POST' }),

  guilds: () => request('/api/guilds'),
  guild: (id) => request(`/api/guilds/${id}`),

  premiumList: () => request('/api/premium'),
  premiumGrant: (guildId, days, note) =>
    request('/api/premium', { method: 'POST', body: { guildId, days, note } }),
  premiumRevoke: (guildId) => request(`/api/premium/${guildId}`, { method: 'DELETE' }),

  tickets: (scope, status) => {
    const q = new URLSearchParams()
    if (scope) q.set('scope', scope)
    if (status) q.set('status', status)
    const qs = q.toString()
    return request(`/api/tickets${qs ? `?${qs}` : ''}`)
  },
  ticket: (id) => request(`/api/tickets/${id}`),
  ticketCreate: (payload) => request('/api/tickets', { method: 'POST', body: payload }),
  ticketReply: (id, body) =>
    request(`/api/tickets/${id}/reply`, { method: 'POST', body: { body } }),
  ticketStatus: (id, status) =>
    request(`/api/tickets/${id}/status`, { method: 'POST', body: { status } }),
}

export function timeAgo(unixSeconds) {
  if (!unixSeconds) return 'never'
  const secs = Math.floor(Date.now() / 1000) - unixSeconds
  if (secs < 60) return 'just now'
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  return new Date(unixSeconds * 1000).toLocaleDateString()
}

export function formatDate(unixSeconds) {
  if (!unixSeconds) return '—'
  return new Date(unixSeconds * 1000).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}
