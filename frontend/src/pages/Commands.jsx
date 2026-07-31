import { useEffect, useState } from 'react'
import { Empty, Page, Spinner } from '../components/Layout.jsx'

export default function Commands() {
  const [commands, setCommands] = useState(null)

  useEffect(() => {
    fetch('/api/commands', { credentials: 'same-origin' })
      .then((r) => r.json())
      .then((d) => setCommands(d.commands || []))
      .catch(() => setCommands([]))
  }, [])

  return (
    <Page narrow>
      <div className="page-head">
        <h2>Commands</h2>
      </div>
      <p className="muted">
        Slash commands registered on this instance. Moderation commands require the
        Eboard role; everything else is open to verified members.
      </p>

      {commands === null && <Spinner />}
      {commands?.length === 0 && (
        <Empty>No commands are registered yet — the bot may still be starting up.</Empty>
      )}
      {commands?.map(({ name, description }) => (
        <div className="cmd" key={name}>
          <code>{name}</code>
          <span>{description}</span>
        </div>
      ))}
    </Page>
  )
}
