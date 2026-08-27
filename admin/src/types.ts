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

export interface AppConfig {
  apiBaseUrl: string;
  userPoolId: string;
  clientId: string;
  loginDomain: string;
  region: string;
  env: string;
}
