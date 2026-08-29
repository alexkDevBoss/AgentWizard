/**
 * Who this player is and what the operator can do to them right now.
 *
 * Controls disable themselves rather than failing: resume is dead unless the
 * player is paused, stop is dead once they are stopped. A control that cannot
 * work should say so before it is pressed.
 */
import type { Arc, Player } from "../types";
import { clockAt, inQuietHours, since } from "../time";

export function PlayerHeader({
  player,
  arc,
  busy,
  onControl,
  onDelete,
  onArc,
}: {
  player: Player;
  arc: Arc | null;
  busy: boolean;
  onControl: (action: "stop" | "pause" | "resume") => void;
  onDelete: () => void;
  onArc: () => void;
}) {
  const quiet = inQuietHours(new Date().toISOString(), player.timezone);
  const stopped = player.status === "STOPPED";

  return (
    <header className="head">
      <div>
        <h1 className="head__name">{player.display_name}</h1>
        <div className="head__facts">
          <span className={`pill pill--${player.status}`}>{player.status}</span>
          <span>
            <b>{clockAt(new Date().toISOString(), player.timezone)}</b> local
            {quiet ? " · quiet hours" : ""}
          </span>
          <span>{player.timezone}</span>
          <span>
            last heard <b>{since(player.last_contact_at)}</b>
          </span>
          <span>
            <b>{player.quota_used_today}</b>/6 today
          </span>
          {player.status === "PENDING" && player.enrolment_code && (
            <span>
              code <b className="code">{player.enrolment_code}</b>
            </span>
          )}
          {arc?.kind === "stages" && (
            <span>
              stop <b>{player.beat_order}</b>/{arc.beats.length} ·{" "}
              {arc.beats[player.beat_order - 1]?.waypoint?.name ?? "—"}
            </span>
          )}
          {stopped && player.stop_reason && <span>{player.stop_reason}</span>}
        </div>
      </div>

      <div className="head__actions">
        <button className="act" disabled={busy} onClick={onArc}>
          {arc ? "Edit adventure" : "Compose adventure"}
        </button>
        <button
          className="act"
          disabled={busy || stopped || player.status !== "ACTIVE"}
          onClick={() => onControl("pause")}
        >
          Pause
        </button>
        <button
          className="act"
          disabled={busy || player.status !== "PAUSED"}
          onClick={() => onControl("resume")}
        >
          Resume
        </button>
        <button
          className="act act--halt"
          disabled={busy || stopped}
          onClick={() => onControl("stop")}
        >
          Stop
        </button>
        <button className="act act--danger" disabled={busy} onClick={onDelete}>
          Delete
        </button>
      </div>
    </header>
  );
}
