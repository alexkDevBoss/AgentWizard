/**
 * The spine.
 *
 * One column, one continuous rule, every event hanging off it in order. Not a
 * chat transcript with left and right bubbles -- direction is carried by a
 * glyph so the column stays dense and scannable at ten events or two hundred.
 *
 * Silence is drawn at scale (see ../time.ts): a gap between events occupies
 * real vertical space proportional to how long it lasted, and gaps that cover
 * the player's night are hatched. Absence is the thing an operator most needs
 * to notice and the thing a plain log hides.
 */
import type { Player, TimelineEvent } from "../types";
import {
  clockAt,
  dayAt,
  describeDuration,
  gapHeight,
  isSilence,
  minutesBetween,
  spansQuietHours,
} from "../time";

const DIRECTION_GLYPH = { in: "◀", out: "▶" } as const;

/** Which events are the player's own words or a character's, versus plumbing. */
function isFiction(event: TimelineEvent): boolean {
  if (event.note) return false;
  if (event.kind === "system") return false;
  return event.kind === "message" || event.kind === "blocked";
}

function Body({ event }: { event: TimelineEvent }) {
  const text = event.text ?? "";
  if (event.note) return <p className="ev__note">{text}</p>;
  if (isFiction(event)) return <p className="ev__said">{text}</p>;
  return <p className="ev__system">{text}</p>;
}

function Event({ event, player }: { event: TimelineEvent; player: Player }) {
  const blocked = event.kind === "blocked";
  return (
    <div
      className={`ev ev--${event.direction}${blocked ? " ev--blocked" : ""}`}
    >
      <div className="ev__time">{clockAt(event.at, player.timezone)}</div>
      <div className="ev__body">
        <div className="ev__meta">
          <span className="ev__dir">{DIRECTION_GLYPH[event.direction]}</span>
          <span>{event.note ? "operator note" : event.kind}</span>
          {event.channel !== "telegram" && <span>· {event.channel}</span>}
          {blocked && event.reason && (
            <span className="tag tag--blocked">not sent · {event.reason}</span>
          )}
          {event.needs_review && !blocked && (
            <span className="tag tag--flag">needs a look</span>
          )}
          {event.substituted && (
            <span className="tag tag--sub">replaced</span>
          )}
          {event.source?.startsWith("operator:") && <span>· by hand</span>}
        </div>
        <Body event={event} />
        {event.rules?.length ? (
          <div className="ev__meta" style={{ marginTop: 4 }}>
            <span className="tag tag--flag">{event.rules.join(" · ")}</span>
          </div>
        ) : null}
        {event.retry_at && (
          <div className="ev__meta" style={{ marginTop: 4 }}>
            <span>can send from {clockAt(event.retry_at, player.timezone)}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function Gap({
  from,
  to,
  player,
}: {
  from: string;
  to: string;
  player: Player;
}) {
  const minutes = minutesBetween(from, to);
  const night = spansQuietHours(from, to, player.timezone);
  return (
    <div
      className={"gap" + (night ? " gap--night" : "")}
      style={{ height: gapHeight(minutes) }}
      aria-hidden="true"
    >
      <span className="gap__label">
        {night ? "quiet hours · " : ""}
        {describeDuration(minutes)} of silence
      </span>
    </div>
  );
}

export function Timeline({
  player,
  events,
}: {
  player: Player;
  events: TimelineEvent[];
}) {
  // The scroll container is owned by the caller; this renders only the rows.
  if (events.length === 0) {
    return (
      <p className="rail__empty">
        Nothing yet.{" "}
        {player.status === "PENDING"
          ? `Waiting for them to send /start ${player.enrolment_code ?? ""}.`
          : "Send the first message below to begin."}
      </p>
    );
  }

  const rows: React.ReactNode[] = [];
  let lastDay = "";

  events.forEach((event, index) => {
    const day = dayAt(event.at, player.timezone);
    if (day !== lastDay) {
      rows.push(
        <div className="daymark" key={`day-${event.sk}`}>
          {day}
        </div>,
      );
      lastDay = day;
    } else {
      const previous = events[index - 1];
      const minutes = minutesBetween(previous.at, event.at);
      if (isSilence(minutes)) {
        rows.push(
          <Gap
            key={`gap-${event.sk}`}
            from={previous.at}
            to={event.at}
            player={player}
          />,
        );
      }
    }
    rows.push(<Event key={event.sk} event={event} player={player} />);
  });

  return <>{rows}</>;
}
