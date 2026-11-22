import { type FormEvent, useLayoutEffect, useRef, useState } from "react";
import type { Session } from "./api";
import { errorMessage } from "./api";

export function SessionTitle({ session, canWrite, save }: {
  session: Session | null;
  canWrite: boolean;
  save: (title: string) => Promise<void>;
}) {
  const title = session ? session.title || session.id : "选择会话";
  const [draft, setDraft] = useState<string>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const edit = useRef<HTMLButtonElement>(null);
  const restoreFocus = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const editing = draft !== undefined;

  useLayoutEffect(() => {
    if (editing) {
      input.current?.focus();
      input.current?.select();
    } else if (restoreFocus.current) {
      (restoreFocus.current.isConnected ? restoreFocus.current : edit.current)?.focus();
      restoreFocus.current = null;
    }
  }, [editing]);

  function start(button: HTMLButtonElement) {
    restoreFocus.current = button;
    setDraft(title);
  }

  function stop() {
    setDraft(undefined);
    setError("");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canWrite || !draft?.trim() || inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    setError("");
    try {
      await save(draft);
      stop();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  }

  return <>
    <div className="title-row" hidden={editing}>
      <h1 id={editing ? undefined : "session-title"}>
        {session && canWrite ? <button type="button" className="title-edit" ref={edit}
          title="编辑会话名称" onClick={(event) => start(event.currentTarget)}>{title}</button> : title}
      </h1>
      {session && canWrite && <button type="button" className="session-control"
        onClick={(event) => start(event.currentTarget)}>改名</button>}
    </div>
    {editing && <form className="title-form" onSubmit={(event) => void submit(event)} onKeyDown={(event) => {
      if (event.key === "Escape" && !inFlight.current) {
        event.preventDefault();
        stop();
      }
    }}>
      <label id="session-title" className="visually-hidden" htmlFor="title-input">会话名称</label>
      <input id="title-input" type="text" value={draft} ref={input} disabled={saving}
        onChange={(event) => setDraft(event.currentTarget.value)} />
      <button type="submit" className="session-control" disabled={!canWrite || !draft?.trim() || saving}>
        {saving ? "保存中…" : "保存"}
      </button>
      <button type="button" className="session-control" disabled={saving} onClick={stop}>取消</button>
      <p className="notice" role="status">{error}</p>
    </form>}
  </>;
}
