/**
 * Thin client for the admin API.
 *
 * Every call carries the Cognito ID token. A 401 means the session expired
 * mid-shift; the caller sends the operator back to sign-in rather than showing
 * a stale console.
 */
import type { AppConfig, Player, PlayerDetail, SendResult } from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly body: unknown = null,
  ) {
    super(message);
  }
}

export class Api {
  constructor(
    private readonly config: AppConfig,
    private readonly token: () => string | undefined,
  ) {}

  private async call<T>(
    path: string,
    init: RequestInit = {},
    // Some routes answer 409 with a meaningful body (a refused send). Those
    // are results, not failures, and the caller wants to read them.
    treatAsResult: number[] = [],
  ): Promise<T> {
    const token = this.token();
    const response = await fetch(`${this.config.apiBaseUrl}${path}`, {
      ...init,
      headers: {
        "content-type": "application/json",
        ...(token ? { authorization: token } : {}),
        ...(init.headers || {}),
      },
    });

    const raw = await response.text();
    const body = raw ? JSON.parse(raw) : null;

    if (response.ok || treatAsResult.includes(response.status)) {
      return body as T;
    }
    throw new ApiError(
      response.status,
      body?.error || `${init.method || "GET"} ${path} failed (${response.status})`,
      body,
    );
  }

  me = () => this.call<{ operator: string }>("/admin/me");

  players = () =>
    this.call<{ players: Player[] }>("/admin/players").then((r) => r.players);

  player = (id: string) => this.call<PlayerDetail>(`/admin/players/${id}`);

  createPlayer = (body: {
    display_name: string;
    timezone: string;
    notes?: string;
  }) =>
    this.call<{ player: Player; enrolment_code: string }>("/admin/players", {
      method: "POST",
      body: JSON.stringify(body),
    });

  /** Speak as the character. A refusal comes back as a result, not an error. */
  sendAsCharacter = (id: string, text: string) =>
    this.call<SendResult>(
      `/admin/players/${id}/message`,
      { method: "POST", body: JSON.stringify({ text }) },
      [409],
    );

  addNote = (id: string, text: string) =>
    this.call<{ added: boolean }>(`/admin/players/${id}/note`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });

  control = (id: string, action: "stop" | "pause" | "resume", reason?: string) =>
    this.call<{ player: Player; acknowledged: boolean; reason: string | null }>(
      `/admin/players/${id}/${action}`,
      { method: "POST", body: JSON.stringify(reason ? { reason } : {}) },
    );

  deletePlayer = (id: string) =>
    this.call<{ deleted: boolean; rows: number }>(`/admin/players/${id}`, {
      method: "DELETE",
    });
}
