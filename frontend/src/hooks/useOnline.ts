import { useEffect, useState } from 'react'

/**
 * Live online/offline signal (browser `navigator.onLine` + events).
 *
 * The dashboard renders an honest offline state from this: a banner while the
 * connection is down and a distinct "you're offline" card when there is no
 * data to show — instead of a generic failure that would send the user retrying
 * into a dead network.
 */
export function useOnline(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator === 'undefined' ? true : navigator.onLine !== false
  )

  useEffect(() => {
    const up = () => setOnline(true)
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    return () => {
      window.removeEventListener('online', up)
      window.removeEventListener('offline', down)
    }
  }, [])

  return online
}
