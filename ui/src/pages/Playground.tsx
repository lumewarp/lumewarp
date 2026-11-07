import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { streamText, type ModelMessage } from "ai";
import { ChevronDownIcon, ChevronRightIcon, SendIcon, SquareIcon, Trash2Icon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { playgroundProvider, type PlaygroundKey, type ResponseInfo } from "../api/playground";
import { PageHeader } from "../components/Bits";
import { SelectField } from "../components/Form";
import { JsonEditor } from "../components/SchemaForm";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { ComboboxField } from "../components/ui/combobox-field";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "../components/ui/empty";
import { HelpTip } from "../components/ui/help-tip";
import { Textarea } from "../components/ui/textarea";
import { useMe } from "../hooks";
import type { Values } from "../lib/schema";
import { PageLoading } from "./Access";

interface Turn {
  id: number;
  role: "user" | "assistant";
  text: string;
  reasoning: string;
  error?: string;
  requestId?: string | null;
  usage?: { input?: number; output?: number; total?: number };
  /** Time to the first streamed token, and output tokens per second after it (needs reported usage). */
  ttftMs?: number;
  tps?: number;
  streaming?: boolean;
  /** A user turn whose reply failed: shown, but not sent again. */
  skip?: boolean;
}

const STORE = "lumewarp.playground";

function loadChoice(): { key?: string; model?: string } {
  try {
    return JSON.parse(localStorage.getItem(STORE) ?? "{}") as { key?: string; model?: string };
  } catch {
    return {};
  }
}

function saveChoice(key: string | undefined, model: string | undefined) {
  try {
    localStorage.setItem(STORE, JSON.stringify({ key, model }));
  } catch {
    // Storage is a convenience only.
  }
}

export function Playground() {
  const me = useMe();
  const breakGlass = me.data?.kind === "break_glass";
  const info = useQuery({ queryKey: ["playground"], queryFn: api.playground, staleTime: 30_000, enabled: !breakGlass });
  // Fills the main container's height (header and its padding aside), so the transcript scrolls inside it.
  return (
    <div className="flex flex-col lg:h-[calc(100dvh-6.75rem)] lg:min-h-[32rem]">
      <PageHeader title="Playground" />
      {breakGlass ? (
        <Alert data-testid="playground-break-glass">
          <AlertDescription>The playground is not available in the emergency session: it chats as one of your own keys, and that session owns none.</AlertDescription>
        </Alert>
      ) : info.isPending ? (
        <PageLoading />
      ) : info.error ? (
        <Alert variant="destructive" data-testid="state-error">
          <AlertDescription>{info.error.message}</AlertDescription>
        </Alert>
      ) : !info.data.enabled ? (
        <Alert data-testid="playground-disabled">
          <AlertDescription>The playground isn't configured on this installation.</AlertDescription>
        </Alert>
      ) : info.data.keys.length === 0 ? (
        <Empty className="border" data-testid="playground-no-keys">
          <EmptyHeader>
            <EmptyTitle>No usable key</EmptyTitle>
            <EmptyDescription>
              The playground uses one of your own active keys. Create one in <Link to="/my-keys">My keys</Link>.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Chat keys={info.data.keys} />
      )}
    </div>
  );
}

function Chat({ keys }: { keys: PlaygroundKey[] }) {
  const saved = useMemo(loadChoice, []);
  const [keyId, setKeyId] = useState(() => (keys.some((k) => k.id === saved.key) ? saved.key! : keys[0]!.id));
  const key = keys.find((k) => k.id === keyId) ?? keys[0]!;
  const [model, setModel] = useState(() => (key.models.includes(saved.model ?? "") ? saved.model! : (key.models[0] ?? "")));
  const [extra, setExtra] = useState<Values>({});
  const [extraProblem, setExtraProblem] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const nextId = useRef(1);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    saveChoice(keyId, model);
  }, [keyId, model]);
  useEffect(
    () => () => {
      abort.current?.abort();
    },
    [],
  );
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [turns]);

  const chooseKey = (id: string) => {
    setKeyId(id);
    const next = keys.find((k) => k.id === id);
    if (next && !next.models.includes(model)) setModel(next.models[0] ?? "");
  };

  const patch = (id: number, change: Partial<Turn>) =>
    setTurns((all) => all.map((t) => (t.id === id ? { ...t, ...change } : t)));

  async function send() {
    const text = draft.trim();
    if (!text || busy || !model || extraProblem) return;
    const user: Turn = { id: nextId.current++, role: "user", text, reasoning: "" };
    const reply: Turn = { id: nextId.current++, role: "assistant", text: "", reasoning: "", streaming: true };
    const history: ModelMessage[] = [
      ...turns.filter((t) => !t.skip && !t.error && t.text).map((t) => ({ role: t.role, content: t.text }) as ModelMessage),
      { role: "user", content: text },
    ];
    setTurns((all) => [...all, user, reply]);
    setDraft("");
    setBusy(true);
    const controller = new AbortController();
    abort.current = controller;
    const seen: ResponseInfo = {};
    let answer = "";
    let thinking = "";
    const started = performance.now();
    let first: number | undefined;
    let output: number | undefined;
    const timing = () => {
      if (first === undefined) return {};
      const seconds = (performance.now() - first) / 1000;
      return { ttftMs: first - started, tps: output !== undefined && seconds > 0 ? output / seconds : undefined };
    };
    try {
      const result = streamText({
        model: playgroundProvider(key.id, seen, extra).chatModel(model),
        messages: history,
        abortSignal: controller.signal,
        maxRetries: 0,
      });
      for await (const part of result.fullStream) {
        if ((part.type === "text-delta" || part.type === "reasoning-delta") && part.text && first === undefined) {
          first = performance.now();
        }
        if (part.type === "text-delta") {
          // Models often open with blank lines or spaces; they are not part of the answer.
          answer = (answer + part.text).trimStart();
          patch(reply.id, { text: answer });
        } else if (part.type === "reasoning-delta") {
          thinking = (thinking + part.text).trimStart();
          patch(reply.id, { reasoning: thinking });
        } else if (part.type === "finish") {
          const u = part.totalUsage;
          output = u.outputTokens;
          if (u.totalTokens !== undefined || u.inputTokens !== undefined) {
            patch(reply.id, { usage: { input: u.inputTokens, output: u.outputTokens, total: u.totalTokens } });
          }
        } else if (part.type === "error") {
          throw part.error;
        }
      }
      patch(reply.id, { streaming: false, requestId: seen.requestId, ...timing() });
    } catch (err) {
      if (controller.signal.aborted) {
        patch(reply.id, { streaming: false, requestId: seen.requestId, ...timing() });
      } else {
        const message = seen.error ?? (err instanceof Error ? err.message : String(err));
        patch(reply.id, { streaming: false, error: message, requestId: seen.requestId });
        patch(user.id, { skip: true });
      }
    } finally {
      setBusy(false);
      abort.current = null;
    }
  }

  return (
    <div className="grid min-h-0 min-w-0 flex-1 gap-4 lg:grid-cols-[22rem_minmax(0,1fr)] lg:grid-rows-[minmax(0,1fr)]">
      <Card className="min-h-0 gap-4 overflow-y-auto p-4" data-testid="playground-settings">
        <SelectField
          label="Key"
          hint="One of your own keys; the request is limited and metered as that key."
          value={keyId}
          onChange={chooseKey}
          options={keys.map((k) => ({ value: k.id, label: `${k.name} · ${k.team_name}` }))}
        />
        <ComboboxField
          label="Model"
          value={model}
          onChange={setModel}
          options={key.models.map((m) => ({ value: m, label: m }))}
          placeholder={key.models.length ? "Select a model…" : "No models"}
          disabled={key.models.length === 0}
        />
        <JsonEditor
          label="Request body (JSON)"
          hint="Merged into every request; model, messages and stream are set by the playground."
          value={extra}
          onChange={setExtra}
          onProblem={setExtraProblem}
        />
      </Card>

      <Card className="h-[70dvh] min-w-0 gap-0 p-0 lg:h-auto" data-testid="playground-chat">
        <div className="relative flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4" role="log" aria-label="Transcript" aria-live="polite">
          {turns.length === 0 && <p className="m-auto text-sm text-muted-foreground">Send a message to start.</p>}
          {turns.map((t) => (
            <Message key={t.id} turn={t} />
          ))}
          <div ref={end} />
        </div>
        <div className="flex flex-col gap-2 border-t p-4">
          <Textarea
            aria-label="Message"
            rows={2}
            placeholder="Message"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setTurns([])} disabled={busy || turns.length === 0}>
              <Trash2Icon aria-hidden="true" /> Clear
            </Button>
            {busy ? (
              <Button type="button" variant="outline" onClick={() => abort.current?.abort()}>
                <SquareIcon aria-hidden="true" /> Stop
              </Button>
            ) : (
              <Button type="button" onClick={() => void send()} disabled={!draft.trim() || !model || !!extraProblem}>
                <SendIcon aria-hidden="true" /> Send
              </Button>
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}

function Message({ turn }: { turn: Turn }) {
  const [open, setOpen] = useState(false);
  if (turn.role === "user") {
    return (
      <div className="ml-auto max-w-[85%] rounded-lg bg-primary px-3 py-2 text-sm whitespace-pre-wrap text-primary-foreground" data-testid="turn-user">
        {turn.text}
      </div>
    );
  }
  const usage = turn.usage;
  const details = [
    turn.requestId && `Request id ${turn.requestId}`,
    usage && `${usage.input ?? "?"} in · ${usage.output ?? "?"} out${usage.total !== undefined ? ` · ${usage.total} total` : ""} tokens`,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="flex max-w-[85%] min-w-0 flex-col gap-1.5" data-testid="turn-assistant">
      {turn.reasoning && (
        <div className="text-xs text-muted-foreground">
          <Button type="button" variant="ghost" size="sm" className="-ml-2 h-6 px-2 text-xs" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? <ChevronDownIcon aria-hidden="true" /> : <ChevronRightIcon aria-hidden="true" />} Reasoning
          </Button>
          {open && <p className="m-0 border-l-2 pl-3 whitespace-pre-wrap" data-testid="reasoning-text">{turn.reasoning}</p>}
        </div>
      )}
      {(turn.text || turn.streaming) && (
        <div className="rounded-lg bg-muted px-3 py-2 text-sm whitespace-pre-wrap" data-testid="assistant-text">
          {turn.text || "…"}
        </div>
      )}
      {turn.error && (
        <Alert variant="destructive" data-testid="turn-error">
          <AlertDescription>{turn.error}</AlertDescription>
        </Alert>
      )}
      {!turn.streaming && (turn.ttftMs !== undefined || turn.requestId || usage) && (
        <p className="m-0 flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground" data-testid="turn-meta">
          {turn.ttftMs !== undefined && <span>TTFT {Math.round(turn.ttftMs)} ms</span>}
          {turn.tps !== undefined && <span>{turn.tps.toFixed(1)} tok/s</span>}
          {details && <HelpTip text={details} id={`turn-${turn.id}-details`} className="-ml-2" />}
        </p>
      )}
    </div>
  );
}
