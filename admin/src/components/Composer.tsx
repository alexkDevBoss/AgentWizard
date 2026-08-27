/**
 * Speak as the character, or write a note to yourself.
 *
 * This is the whole Wizard-of-Oz pilot in one control, so it is honest about
 * what happens: the same gates that will govern the story engine govern this
 * box, and when one refuses, the refusal is shown here rather than swallowed.
 * The operator finds out that a message did not go out at the moment they
 * pressed send, not two days later reading a timeline.
 */
import { useState } from "react";
import type { Player, SendResult } from "../types";
import { clockAt } from "../time";

type Mode = "character" | "note";

const REFUSAL_COPY: Record<string, string> = {
  quiet_hours: "Held — it is quiet hours where they are.",
  rate_limited: "Held — they have had all six story messages today.",
  player_stopped: "Not sent — this player stopped the story.",
  player_paused: "Not sent — this player is paused. Resume first.",
  no_channel: "Not sent — no Telegram chat is bound to this player yet.",
  validation_failed:
    "A content rule matched, so a safe line went out instead. The original is on the timeline.",
};

export function Composer({
  player,
  onSend,
  onNote,
}: {
  player: Player;
  onSend: (text: string) => Promise<SendResult>;
  onNote: (text: string) => Promise<void>;
}) {
  const [mode, setMode] = useState<Mode>("character");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<{ text: string; refused: boolean } | null>(
    null,
  );

  const canSend = text.trim().length > 0 && !busy;

  async function submit() {
    if (!canSend) return;
    setBusy(true);
    setHint(null);
    try {
      if (mode === "note") {
        await onNote(text.trim());
        setText("");
        setHint({ text: "Note added to the timeline.", refused: false });
        return;
      }

      const result = await onSend(text.trim());
      if (result.sent && !result.reason) {
        setText("");
        setHint({ text: "Sent.", refused: false });
      } else if (result.sent) {
        setText("");
        setHint({
          text: REFUSAL_COPY[result.reason!] ?? `Sent, with ${result.reason}.`,
          refused: true,
        });
      } else {
        const base = REFUSAL_COPY[result.reason!] ?? `Not sent — ${result.reason}.`;
        const when = result.retry_at
          ? ` Try again from ${clockAt(result.retry_at, player.timezone)} their time.`
          : "";
        setHint({ text: base + when, refused: true });
      }
    } catch (error) {
      setHint({ text: (error as Error).message, refused: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="composer">
      <div className="composer__mode">
        <button
          className="composer__tab"
          aria-pressed={mode === "character"}
          onClick={() => setMode("character")}
        >
          Speak as the handler
        </button>
        <button
          className="composer__tab"
          aria-pressed={mode === "note"}
          onClick={() => setMode("note")}
        >
          Note to self
        </button>
      </div>

      <div className="composer__row">
        <textarea
          className={
            "composer__input" + (mode === "note" ? " composer__input--note" : "")
          }
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder={
            mode === "character"
              ? `Write what the handler says to ${player.display_name}…`
              : "Something to remember about this player. They never see it."
          }
          disabled={busy}
        />
        <button
          className="composer__send"
          onClick={() => void submit()}
          disabled={!canSend}
        >
          {mode === "note" ? "Add note" : "Send"}
        </button>
      </div>

      <div
        className={
          "composer__hint" + (hint?.refused ? " composer__hint--refused" : "")
        }
        role="status"
      >
        {hint?.text ?? (mode === "character" ? "⌘/Ctrl + Enter to send." : "")}
      </div>
    </div>
  );
}
