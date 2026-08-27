/**
 * Add a player and mint their enrolment code.
 *
 * Enrolment is operator-driven by design: there is no sign-up page, and the
 * code created here is the only way a chat can bind to a player.
 */
import { useState } from "react";

/** Offered up front so the common cases need no typing; any IANA name works. */
const COMMON_ZONES = [
  "Asia/Jerusalem",
  "Europe/London",
  "Europe/Berlin",
  "America/New_York",
  "America/Los_Angeles",
  "UTC",
];

export function NewPlayer({
  onCreate,
  onClose,
}: {
  onCreate: (body: {
    display_name: string;
    timezone: string;
    notes?: string;
  }) => Promise<{ enrolment_code: string }>;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  );
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function create() {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await onCreate({
        display_name: name.trim(),
        timezone,
        notes: notes.trim() || undefined,
      });
      setCode(result.enrolment_code);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const zones = COMMON_ZONES.includes(timezone)
    ? COMMON_ZONES
    : [timezone, ...COMMON_ZONES];

  return (
    <div className="veil" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        {code ? (
          <>
            <h2>{name} is ready</h2>
            <p style={{ color: "var(--chalk-dim)", marginTop: 0 }}>
              Ask them to send this to the bot. It works once.
            </p>
            <p
              className="code"
              style={{ fontSize: 26, letterSpacing: "0.14em", margin: "14px 0" }}
            >
              /start {code}
            </p>
            <div className="sheet__actions">
              <button className="act" onClick={onClose}>
                Done
              </button>
            </div>
          </>
        ) : (
          <>
            <h2>Add a player</h2>
            <label className="field">
              <span>Name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="What you'll call them here"
                autoFocus
              />
            </label>
            <label className="field">
              <span>Timezone</span>
              <select
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
              >
                {zones.map((zone) => (
                  <option key={zone} value={zone}>
                    {zone}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Notes (optional)</span>
              <input
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                placeholder="Anything you want to remember"
              />
            </label>
            <p
              style={{
                color: "var(--chalk-faint)",
                fontFamily: "var(--mono)",
                fontSize: 10.5,
                margin: 0,
              }}
            >
              Quiet hours are 22:00–08:00 in their timezone. Getting this wrong
              means messaging them at night.
            </p>
            {error && (
              <p className="gate__error" style={{ marginBottom: 0 }}>
                {error}
              </p>
            )}
            <div className="sheet__actions">
              <button className="act" onClick={onClose}>
                Cancel
              </button>
              <button
                className="composer__send"
                onClick={() => void create()}
                disabled={!name.trim() || busy}
              >
                Create
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
