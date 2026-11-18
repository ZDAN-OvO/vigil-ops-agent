import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchApi } from './api'
import type { Snapshot } from './api'

type Connection = 'connecting' | 'connected' | 'reconnecting'

export function useConversation() {
  const [snapshot, setSnapshot] = useState<Snapshot>()
  const [connection, setConnection] = useState<Connection>('connecting')
  const reload = useRef<() => Promise<void>>(async () => {})
  const refresh = useCallback(() => reload.current(), [])

  useEffect(() => {
    let stopped = false
    let source: EventSource | undefined
    let listeningTo: string | undefined
    let running: Promise<void> | undefined
    let again = false
    let reconnectTimer: number | undefined
    let refreshTimer: number | undefined
    let pollTimer: number | undefined

    function scheduleReconnect() {
      if (stopped || reconnectTimer !== undefined) return
      setConnection('reconnecting')
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined
        void pull()
      }, 1500)
    }

    function subscribe(id: string, after: number) {
      source?.close()
      listeningTo = id
      const active = new EventSource(`/api/conversations/${encodeURIComponent(id)}/events?after=${after}`)
      source = active
      active.onopen = () => {
        if (!stopped && active === source) setConnection('connected')
      }
      active.onmessage = () => {
        if (refreshTimer !== undefined) return
        refreshTimer = window.setTimeout(() => {
          refreshTimer = undefined
          void pull()
        }, 250)
      }
      active.onerror = () => {
        if (active !== source) return
        active.close()
        source = undefined
        listeningTo = undefined
        scheduleReconnect()
      }
    }

    async function read() {
      try {
        const next = await fetchApi<Snapshot>('/conversations/current')
        if (stopped) return
        setSnapshot(next)
        const conversation = next.conversation
        if (conversation && listeningTo !== conversation.id) {
          subscribe(conversation.id, next.last_event_id ?? 0)
        } else if (!conversation) {
          source?.close()
          source = undefined
          listeningTo = undefined
          setConnection('connected')
        } else if (source?.readyState === EventSource.OPEN) {
          setConnection('connected')
        }
        window.clearTimeout(pollTimer)
        if (conversation && (!conversation.finished || conversation.page_state === 'needs_verification')) {
          pollTimer = window.setTimeout(() => void pull(), 10000)
        }
      } catch {
        if (!stopped) {
          source?.close()
          source = undefined
          listeningTo = undefined
          scheduleReconnect()
        }
      }
    }

    function pull(): Promise<void> {
      if (running) {
        again = true
        return running
      }
      running = (async () => {
        do {
          again = false
          await read()
        } while (again && !stopped)
      })().finally(() => { running = undefined })
      return running
    }

    reload.current = pull
    void pull()
    return () => {
      stopped = true
      source?.close()
      window.clearTimeout(reconnectTimer)
      window.clearTimeout(refreshTimer)
      window.clearTimeout(pollTimer)
    }
  }, [])

  return { snapshot, connection, refresh }
}
