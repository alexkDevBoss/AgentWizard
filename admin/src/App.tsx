/**
 * The console.
 *
 * One screen: roster on the left, the selected player's spine in the middle,
 * the composer docked underneath. No navigation, because an operator watching
 * a live story should never be more than one click from any player.
 *
 * Polling rather than websockets: at ten players a five-second refresh is a
 * rounding error in cost, and it cannot silently stop working the way a
 * dropped socket can.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { User } from "oidc-client-ts";
import { Api, ApiError } from "./api";
import { initAuth, resolveUser, signIn, signOut } from "./auth";
import { Composer } from "./components/Composer";
import { NewPlayer } from "./components/NewPlayer";
import { PlayerHeader } from "./components/PlayerHeader";
import { Roster } from "./components/Roster";
import { Timeline } from "./components/Timeline";
import type { AppConfig, Player, TimelineEvent } from "./types";

const REFRESH_MS = 5000;

export function App({ config }: { config: AppConfig }) {
  const [user, setUser] = useState<User | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  const [players, setPlayers] = useState<Player[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<{
    player: Player;
    timeline: TimelineEvent[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);

  const spineRef = useRef<HTMLDivElement>(null);
  const lastEventKey = useRef<string>("");

  const api = useMemo(
    () => new Api(config, () => user?.id_token),
    [config, user],
  );

  useEffect(() => {
    initAuth(config);
    resolveUser()
      .then(setUser)
      .catch((e: Error) => setAuthError(e.message))
      .finally(() => setReady(true));
  }, [config]);

  const handle = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) {
      setUser(null);
      return;
    }
    setError((e as Error).message);
  }, []);

  // Roster poll.
  useEffect(() => {
    if (!user) return;
    let live = true;
    const tick = () =>
      api
        .players()
        .then((next) => {
          if (!live) return;
          setPlayers(next);
          setError(null);
          setSelectedId((current) =>
            current ?? (next.length ? next[0].player_id : null),
          );
        })
        .catch(handle);
    void tick();
    const timer = setInterval(tick, REFRESH_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [api, user, handle]);

  // Selected player poll.
  useEffect(() => {
    if (!user || !selectedId) {
      setDetail(null);
      return;
    }
    let live = true;
    const tick = () =>
      api
        .player(selectedId)
        .then((next) => live && setDetail(next))
        .catch(handle);
    void tick();
    const timer = setInterval(tick, REFRESH_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [api, user, selectedId, handle]);

  // Follow the tail only when something new actually arrives, so the operator
  // can scroll back through history without being yanked forward.
  useEffect(() => {
    const events = detail?.timeline ?? [];
    const key = `${detail?.player.player_id}:${events.at(-1)?.sk ?? ""}`;
    if (key !== lastEventKey.current) {
      lastEventKey.current = key;
      const node = spineRef.current;
      if (node) node.scrollTop = node.scrollHeight;
    }
  }, [detail]);

  const flagged = useMemo(() => {
    const set = new Set<string>();
    if (detail?.timeline.some((e) => e.needs_review)) {
      set.add(detail.player.player_id);
    }
    return set;
  }, [detail]);

  async function withRefresh<T>(work: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    try {
      const result = await work();
      if (selectedId) setDetail(await api.player(selectedId));
      setPlayers(await api.players());
      return result;
    } catch (e) {
      handle(e);
    } finally {
      setBusy(false);
    }
  }

  async function control(action: "stop" | "pause" | "resume") {
    if (!detail) return;
    if (action === "stop") {
      const reason = window.prompt(
        `Stop ${detail.player.display_name}'s story? This cannot be resumed.\n\nWhy? (recorded on the timeline)`,
        "operator stopped",
      );
      if (reason === null) return;
      await withRefresh(() => api.control(detail.player.player_id, "stop", reason));
      return;
    }
    await withRefresh(() => api.control(detail.player.player_id, action));
  }

  async function remove() {
    if (!detail) return;
    const typed = window.prompt(
      `Permanently delete ${detail.player.display_name} and their entire timeline.\n\nType their name to confirm.`,
    );
    if (typed !== detail.player.display_name) return;
    await withRefresh(() => api.deletePlayer(detail.player.player_id));
    setSelectedId(null);
  }

  if (!ready) return <div className="blank">Loading</div>;

  if (!user) {
    return (
      <div className="gate">
        <div className="gate__mark">Handler</div>
        <h1 className="gate__title">
          The console for a story that is already running.
        </h1>
        {authError && <p className="gate__error">{authError}</p>}
        <button className="composer__send" onClick={() => void signIn()}>
          Sign in
        </button>
      </div>
    );
  }

  return (
    <div className="desk">
      <div className="bar">
        <span className="bar__mark">Handler</span>
        <span className="bar__env">{config.env}</span>
        <span>quiet hours 22:00–08:00 local</span>
        <span className="bar__spacer" />
        <span>{user.profile.email ?? user.profile.sub}</span>
        <button className="bar__link" onClick={() => void signOut()}>
          Sign out
        </button>
      </div>

      <Roster
        players={players}
        selectedId={selectedId}
        flagged={flagged}
        onSelect={setSelectedId}
        onAdd={() => setAdding(true)}
      />

      <main className="main">
        {error && <div className="banner">{error}</div>}
        {detail ? (
          <>
            <PlayerHeader
              player={detail.player}
              busy={busy}
              onControl={(a) => void control(a)}
              onDelete={() => void remove()}
            />
            <div className="spine" ref={spineRef}>
              <Timeline player={detail.player} events={detail.timeline} />
            </div>
            <Composer
              player={detail.player}
              onSend={async (text) => {
                const result = await api.sendAsCharacter(
                  detail.player.player_id,
                  text,
                );
                await withRefresh(async () => result);
                return result;
              }}
              onNote={async (text) => {
                await api.addNote(detail.player.player_id, text);
                await withRefresh(async () => undefined);
              }}
            />
          </>
        ) : (
          <div className="blank">
            {players.length ? "Select a player" : "No players yet"}
          </div>
        )}
      </main>

      {adding && (
        <NewPlayer
          onClose={() => setAdding(false)}
          onCreate={async (body) => {
            const created = await api.createPlayer(body);
            setPlayers(await api.players());
            return created;
          }}
        />
      )}
    </div>
  );
}
