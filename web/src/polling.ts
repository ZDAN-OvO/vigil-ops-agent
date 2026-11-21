import { useCallback, useEffect, useRef } from "react";

export function usePolling(task: (signal: AbortSignal) => Promise<void>, delay: number) {
  const wake = useRef<() => void>(null);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let timer: number | undefined;
    let running = false;
    let queued = false;

    async function run() {
      window.clearTimeout(timer);
      if (signal.aborted) return;
      if (running) {
        queued = true;
        return;
      }

      running = true;
      try {
        do {
          queued = false;
          await task(signal);
        } while (queued && !signal.aborted);
      } finally {
        running = false;
        if (!signal.aborted) timer = window.setTimeout(() => void run(), delay);
      }
    }

    wake.current = () => void run();
    wake.current();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
      wake.current = null;
    };
  }, [task, delay]);

  return useCallback(() => wake.current?.(), []);
}
