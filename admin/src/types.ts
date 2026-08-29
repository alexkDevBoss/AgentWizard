export type PlayerStatus = "ACTIVE" | "PAUSED" | "STOPPED" | "PENDING";

export interface Player {
  player_id: string;
  display_name: string;
  timezone: string;
  status: PlayerStatus;
  arc_id: string | null;
  telegram_chat_id: number | null;
  enrolment_code: string | null;
  created_at: string;
  last_contact_at: string | null;
  stopped_at: string | null;
  stop_reason: string | null;
  calls_placed: number;
  notes: string | null;
  quota_used_today: number;
  arc_started_at: string | null;
  /** Which stage of a walking arc they are on, 1-based. */
  beat_order: number;
}

/** One row of a player's timeline. Extra keys vary by event kind. */
export interface TimelineEvent {
  sk: string;
  at: string;
  direction: "in" | "out";
  channel: "telegram" | "email" | "voice" | "admin";
  kind:
    | "message"
    | "photo"
    | "location"
    | "command"
    | "system"
    | "blocked"
    | "error";
  text?: string;
  reason?: string;
  rules?: string[];
  retry_at?: string;
  needs_review?: boolean;
  /** Geofence result. The coordinates that produced it were never stored. */
  arrived?: boolean;
  distance_band?: string;
  waypoint?: string;
  beat_id?: string;
  note?: boolean;
  source?: string;
  substituted?: boolean;
  quota_used?: number;
  message_kind?: string;
  photo_file_id?: string;
}

export interface PlayerDetail {
  player: Player;
  timeline: TimelineEvent[];
}

export interface SendResult {
  sent: boolean;
  reason: string | null;
  retry_at: string | null;
  text: string | null;
}

/** A real place, from OpenStreetMap. Never invented by the model. */
export interface Waypoint {
  osm_id: string;
  name: string;
  kind: string;
  lat: number;
  lon: number;
  fence_m: number;
}

/** What moves the story past a stage. Every value but `reply` is verified. */
export type Advance = "day" | "arrival" | "photo" | "reply" | "operator";

export interface ArcBeat {
  beat_id: string;
  order: number;
  title: string;
  goal: string;
  advance_on: Advance;
  opens_with?: string;
  player_may?: string;
  channel?: string;
  hint?: string;
  waypoint?: Waypoint | null;
}

export interface Arc {
  arc_id: string;
  kind: "days" | "stages";
  title: string;
  premise: string;
  tone: string;
  duration_days: number;
  notes?: string;
  characters: { name: string; role: string; voice: string }[];
  beats: ArcBeat[];
}

/** A background composition. The console polls this until it settles. */
export interface ArcJob {
  job_id: string;
  status: "running" | "done" | "failed";
  arc?: Arc;
  candidates?: Waypoint[];
  route_m?: number;
  error?: string;
  updated_at?: string;
}

export interface AppConfig {
  apiBaseUrl: string;
  userPoolId: string;
  clientId: string;
  loginDomain: string;
  region: string;
  env: string;
}
