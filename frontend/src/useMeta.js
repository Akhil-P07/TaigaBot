import { useEffect, useState } from 'react'
import { fetchMeta } from './api.js'

/** Public site metadata: inviteUrl, githubUrl, clubUrl, serverCount. */
export function useMeta() {
  const [meta, setMeta] = useState({})

  useEffect(() => {
    let alive = true
    fetchMeta().then((m) => { if (alive) setMeta(m) })
    return () => { alive = false }
  }, [])

  return meta
}
