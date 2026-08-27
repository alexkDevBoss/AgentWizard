/**
 * Time on the spine.
 *
 * The timeline draws silence at scale: a four-hour gap between events occupies
 * visibly more of the column than a four-minute one. Linear scaling is
 * unusable across a seven-day arc, so gap height grows logarithmically -- big
 * enough that absence is obvious, bounded so a single overnight silence does
 * not push the rest of the story off-screen.
 */

/** Gaps shorter than this are ordinary conversational rhythm, not silence. */
const SILENCE_THRESHOLD_MIN = 8;
const MIN_GAP_PX = 26;
const MAX_GAP_PX = 150;

export const QUIET_START_HOUR = 22;
export const QUIET_END_HOUR = 8;

export function minutesBetween(earlier: string, later: string): number {
  return (Date.parse(later) - Date.parse(earlier)) / 60000;
}

export function gapHeight(minutes: number): number {
  const growth = Math.log2(minutes / SILENCE_THRESHOLD_MIN) * 30;
  return Math.round(Math.min(MAX_GAP_PX, MIN_GAP_PX + Math.max(0, growth)));
}

export function isSilence(minutes: number): boolean {
  return minutes >= SILENCE_THRESHOLD_MIN;
}

/** The hour of day at `iso`, in the player's own timezone. */
export function localHour(iso: string, timeZone: string): number {
  try {
    return Number(
      new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit",
        hour12: false,
        timeZone,
      }).format(new Date(iso)),
    );
  } catch {
    return new Date(iso).getUTCHours();
  }
}

export function inQuietHours(iso: string, timeZone: string): boolean {
  const hour = localHour(iso, timeZone);
  return hour >= QUIET_START_HOUR || hour < QUIET_END_HOUR;
}

/**
 * Whether a gap covers the player's night.
 *
 * Checked at the midpoint rather than either end: a silence that starts at
 * 21:50 and ends at 08:10 is the night, and labelling it by its first minute
 * would call it evening.
 */
export function spansQuietHours(
  from: string,
  to: string,
  timeZone: string,
): boolean {
  const midpoint = new Date(
    (Date.parse(from) + Date.parse(to)) / 2,
  ).toISOString();
  return inQuietHours(midpoint, timeZone);
}

export function clockAt(iso: string, timeZone: string): string {
  try {
    return new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone,
    }).format(new Date(iso));
  } catch {
    return iso.slice(11, 16);
  }
}

export function dayAt(iso: string, timeZone: string): string {
  try {
    return new Intl.DateTimeFormat("en-GB", {
      weekday: "short",
      day: "numeric",
      month: "short",
      timeZone,
    }).format(new Date(iso));
  } catch {
    return iso.slice(0, 10);
  }
}

/** "4h 20m", "3d", "just now" -- for the roster's staleness column. */
export function since(iso: string | null): string {
  if (!iso) return "never";
  const minutes = Math.max(0, (Date.now() - Date.parse(iso)) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${Math.floor(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 24) {
    const h = Math.floor(hours);
    const m = Math.floor(minutes - h * 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  return `${Math.floor(hours / 24)}d`;
}

export function describeDuration(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)} minutes`;
  const hours = minutes / 60;
  if (hours < 24) {
    const h = Math.floor(hours);
    const m = Math.round(minutes - h * 60);
    return m ? `${h}h ${m}m` : `${h} hours`;
  }
  const days = Math.floor(hours / 24);
  const remainder = Math.round(hours - days * 24);
  return remainder ? `${days}d ${remainder}h` : `${days} days`;
}
