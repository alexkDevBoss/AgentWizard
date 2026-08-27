/**
 * The roster rail.
 *
 * Strips, not cards: full-bleed rows divided by hairlines, status carried on
 * the leading edge, so a column of ten reads as one instrument rather than ten
 * separate objects. Sorted by staleness, because the operator's first question
 * every time they look is "who has gone quiet?".
 */
import type { Player } from "../types";
import { since } from "../time";

/** Anything past this without contact is worth the operator's eye. */
const STALE_MINUTES = 6 * 60;

function isStale(player: Player): boolean {
  if (player.status !== "ACTIVE") return false;
  if (!player.last_contact_at) return true;
  return (Date.now() - Date.parse(player.last_contact_at)) / 60000 > STALE_MINUTES;
}

function Quota({ used }: { used: number }) {
  return (
    <span className="quota" title={`${used} of 6 story messages used today`}>
      {Array.from({ length: 6 }, (_, i) => (
        <span
          key={i}
          className={
            "quota__cell" +
            (i < used ? " quota__cell--used" : "") +
            (used >= 6 && i === 5 ? " quota__cell--last" : "")
          }
        />
      ))}
    </span>
  );
}

export function Roster({
  players,
  selectedId,
  flagged,
  onSelect,
  onAdd,
}: {
  players: Player[];
  selectedId: string | null;
  flagged: Set<string>;
  onSelect: (id: string) => void;
  onAdd: () => void;
}) {
  return (
    <nav className="rail" aria-label="Players">
      <div className="rail__head">
        <span>Roster · {players.length}</span>
        <button className="rail__add" onClick={onAdd}>
          Add player
        </button>
      </div>

      {players.length === 0 ? (
        <p className="rail__empty">
          No players yet. Add one to mint an enrolment code, then give them the
          code to send to the bot.
        </p>
      ) : (
        players.map((player) => (
          <button
            key={player.player_id}
            className={`strip strip--${player.status.toLowerCase()}`}
            aria-current={player.player_id === selectedId}
            onClick={() => onSelect(player.player_id)}
          >
            <span className="strip__top">
              <span className="strip__name">{player.display_name}</span>
              <span
                className={
                  "strip__since" + (isStale(player) ? " strip__since--stale" : "")
                }
              >
                {since(player.last_contact_at)}
              </span>
            </span>
            <span className="strip__bottom">
              <span>{player.status}</span>
              {flagged.has(player.player_id) && (
                <span className="strip__flag">● needs a look</span>
              )}
              {player.status === "PENDING" && player.enrolment_code && (
                <span className="code">{player.enrolment_code}</span>
              )}
              <Quota used={player.quota_used_today} />
            </span>
          </button>
        ))
      )}
    </nav>
  );
}
