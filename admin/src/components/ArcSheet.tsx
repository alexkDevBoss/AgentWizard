/**
 * Compose and edit a player's walking adventure.
 *
 * Two states in one sheet. With no arc yet, it asks for a starting point and
 * composes one. With an arc, it is an editor: rewrite anything, swap a stop
 * for another real place nearby, reorder, drop one.
 *
 * Composing is a background job because it takes 25-35 seconds and API
 * Gateway closes the connection at 30 -- so this starts a job and polls it.
 *
 * The route length is recomputed here on every edit, before saving, so the
 * operator watches it change as they swap stops rather than discovering on
 * save that they have built a five-kilometre walk. The server checks it again
 * and is the authority; this is the fast feedback, not the guarantee.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { Advance, Arc, ArcBeat, Waypoint } from "../types";

/** Kept in step with backend/core/arcsmith.py. */
const MAX_ROUTE_M = 4500;
const MAX_LEG_M = 1400;
const MIN_STAGES = 3;
const MAX_STAGES = 6;
const POLL_MS = 2500;

const EARTH_M = 6371008.8;

function metres(a: Waypoint, b: Waypoint): number {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_M * Math.asin(Math.sqrt(h));
}

function route(arc: Arc): { total: number; longest: number } {
  const stops = arc.beats.map((b) => b.waypoint).filter(Boolean) as Waypoint[];
  let total = 0;
  let longest = 0;
  for (let i = 1; i < stops.length; i++) {
    const leg = metres(stops[i - 1], stops[i]);
    total += leg;
    longest = Math.max(longest, leg);
  }
  return { total, longest };
}

/**
 * The last stage must always wait for the operator: an arc that completes
 * itself leaves someone standing in a street with nobody watching. Applied
 * here so reordering or deleting can never produce an arc the server refuses.
 */
function renumber(beats: ArcBeat[]): ArcBeat[] {
  return beats.map((beat, i) => ({
    ...beat,
    order: i + 1,
    advance_on: (i === beats.length - 1
      ? "operator"
      : beat.advance_on === "operator"
        ? "arrival"
        : beat.advance_on) as Advance,
  }));
}

export function ArcSheet({
  playerName,
  arc: existing,
  beatOrder,
  onCompose,
  onPoll,
  onSave,
  onPlaces,
  onClose,
}: {
  playerName: string;
  arc: Arc | null;
  beatOrder: number;
  onCompose: (at: string, theme: string) => Promise<string>;
  onPoll: (
    jobId: string,
  ) => Promise<{ status: string; arc?: Arc; candidates?: Waypoint[]; error?: string }>;
  onSave: (arc: Arc, restart: boolean) => Promise<void>;
  onPlaces: (at: string) => Promise<Waypoint[]>;
  onClose: () => void;
}) {
  const [arc, setArc] = useState<Arc | null>(existing);
  const [candidates, setCandidates] = useState<Waypoint[]>([]);
  const [at, setAt] = useState("");
  const [theme, setTheme] = useState("");
  const [composing, setComposing] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const jobRef = useRef<string | null>(null);

  const { total, longest } = useMemo(
    () => (arc ? route(arc) : { total: 0, longest: 0 }),
    [arc],
  );
  const tooFar = total > MAX_ROUTE_M || longest > MAX_LEG_M;

  // Poll the composition job. The operator is watching a spinner, so the
  // elapsed count runs alongside it -- half a minute of silence with no
  // number moving reads as broken.
  useEffect(() => {
    if (!composing || !jobRef.current) return;
    let live = true;
    const started = Date.now();
    const timer = setInterval(() => {
      setElapsed(Math.round((Date.now() - started) / 1000));
      void onPoll(jobRef.current as string)
        .then((job) => {
          if (!live || job.status === "running") return;
          setComposing(false);
          if (job.status === "failed" || !job.arc) {
            setError(job.error || "composing failed");
            return;
          }
          setArc(job.arc);
          setCandidates(job.candidates || []);
          setDirty(true);
        })
        .catch((e: Error) => {
          if (!live) return;
          setComposing(false);
          setError(e.message);
        });
    }, POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [composing, onPoll]);

  async function compose() {
    if (!at.trim() || composing) return;
    setError(null);
    setElapsed(0);
    try {
      jobRef.current = await onCompose(at.trim(), theme.trim());
      setComposing(true);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function loadAlternatives() {
    const anchor = arc?.beats.find((b) => b.waypoint)?.waypoint;
    const where = at.trim() || (anchor ? `${anchor.lat},${anchor.lon}` : "");
    if (!where) return;
    try {
      setCandidates(await onPlaces(where));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function edit(index: number, patch: Partial<ArcBeat>) {
    if (!arc) return;
    const beats = arc.beats.map((b, i) => (i === index ? { ...b, ...patch } : b));
    setArc({ ...arc, beats: renumber(beats) });
    setDirty(true);
  }

  function move(index: number, by: -1 | 1) {
    if (!arc) return;
    const next = [...arc.beats];
    const target = index + by;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setArc({ ...arc, beats: renumber(next) });
    setDirty(true);
  }

  function drop(index: number) {
    if (!arc || arc.beats.length <= MIN_STAGES) return;
    setArc({ ...arc, beats: renumber(arc.beats.filter((_, i) => i !== index)) });
    setDirty(true);
  }

  async function save(restart: boolean) {
    if (!arc || saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(arc, restart);
      setDirty(false);
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const used = new Set(arc?.beats.map((b) => b.waypoint?.osm_id).filter(Boolean));

  return (
    <div className="veil" onClick={onClose}>
      <div className="sheet sheet--wide" onClick={(e) => e.stopPropagation()}>
        <div className="arc__top">
          <h2 style={{ margin: 0 }}>
            {arc ? arc.title : `An adventure for ${playerName}`}
          </h2>
          {arc && (
            <span className={`arc__route ${tooFar ? "arc__route--over" : ""}`}>
              {(total / 1000).toFixed(1)} km · {arc.beats.length} stops · longest
              leg {Math.round(longest)} m
            </span>
          )}
        </div>

        {!arc && (
          <>
            <p className="arc__lede">
              The stops have to be real places, so this looks up what is actually
              near them on OpenStreetMap and writes a story around what it finds.
            </p>
            <label className="field">
              <span>Starting point</span>
              <input
                value={at}
                onChange={(e) => setAt(e.target.value)}
                placeholder="32.0640,34.7740"
                autoFocus
              />
            </label>
            <label className="field">
              <span>Anything they asked for (optional)</span>
              <input
                value={theme}
                onChange={(e) => setTheme(e.target.value)}
                placeholder="something about architecture, an hour or so"
              />
            </label>
            <p className="arc__note">
              Coverage is good in a city centre and thin in a suburb. If there is
              not enough mapped nearby, it will say so rather than invent
              somewhere.
            </p>
          </>
        )}

        {composing && (
          <div className="arc__waiting">
            Writing the adventure… {elapsed}s
            <span className="arc__note">
              This takes about half a minute. You can leave this open.
            </span>
          </div>
        )}

        {error && <p className="gate__error">{error}</p>}

        {arc && (
          <div className="arc__body">
            <label className="field">
              <span>Title</span>
              <input
                value={arc.title}
                onChange={(e) => {
                  setArc({ ...arc, title: e.target.value });
                  setDirty(true);
                }}
              />
            </label>
            <label className="field">
              <span>Premise</span>
              <textarea
                rows={4}
                value={arc.premise}
                onChange={(e) => {
                  setArc({ ...arc, premise: e.target.value });
                  setDirty(true);
                }}
              />
            </label>
            <label className="field">
              <span>Tone</span>
              <textarea
                rows={2}
                value={arc.tone}
                onChange={(e) => {
                  setArc({ ...arc, tone: e.target.value });
                  setDirty(true);
                }}
              />
            </label>
            {arc.characters[0] && (
              <label className="field">
                <span>{arc.characters[0].name} — voice</span>
                <textarea
                  rows={3}
                  value={arc.characters[0].voice}
                  onChange={(e) => {
                    setArc({
                      ...arc,
                      characters: [
                        { ...arc.characters[0], voice: e.target.value },
                        ...arc.characters.slice(1),
                      ],
                    });
                    setDirty(true);
                  }}
                />
              </label>
            )}

            <div className="arc__stops">
              {arc.beats.map((beat, i) => {
                const here = beatOrder === beat.order;
                return (
                  <div
                    key={beat.beat_id}
                    className={`stop ${here ? "stop--here" : ""}`}
                  >
                    <div className="stop__head">
                      <span className="stop__n">{beat.order}</span>
                      <input
                        className="stop__title"
                        value={beat.title}
                        onChange={(e) => edit(i, { title: e.target.value })}
                      />
                      <button
                        className="act"
                        onClick={() => move(i, -1)}
                        disabled={i === 0}
                        title="Earlier"
                      >
                        ↑
                      </button>
                      <button
                        className="act"
                        onClick={() => move(i, 1)}
                        disabled={i === arc.beats.length - 1}
                        title="Later"
                      >
                        ↓
                      </button>
                      <button
                        className="act act--danger"
                        onClick={() => drop(i)}
                        disabled={arc.beats.length <= MIN_STAGES}
                        title={
                          arc.beats.length <= MIN_STAGES
                            ? `${MIN_STAGES} stops is the minimum`
                            : "Remove this stop"
                        }
                      >
                        ✕
                      </button>
                    </div>

                    <label className="field">
                      <span>Place</span>
                      <select
                        value={beat.waypoint?.osm_id || ""}
                        onChange={(e) => {
                          const found = candidates.find(
                            (c) => c.osm_id === e.target.value,
                          );
                          if (found) edit(i, { waypoint: found });
                        }}
                        onFocus={() => {
                          if (!candidates.length) void loadAlternatives();
                        }}
                      >
                        {beat.waypoint &&
                          !candidates.some(
                            (c) => c.osm_id === beat.waypoint?.osm_id,
                          ) && (
                            <option value={beat.waypoint.osm_id}>
                              {beat.waypoint.name} ({beat.waypoint.kind})
                            </option>
                          )}
                        {candidates.map((c) => (
                          <option
                            key={c.osm_id}
                            value={c.osm_id}
                            disabled={
                              used.has(c.osm_id) &&
                              c.osm_id !== beat.waypoint?.osm_id
                            }
                          >
                            {c.name} ({c.kind.replace(/_/g, " ")})
                            {used.has(c.osm_id) &&
                            c.osm_id !== beat.waypoint?.osm_id
                              ? " — already used"
                              : ""}
                          </option>
                        ))}
                      </select>
                    </label>

                    <label className="field">
                      <span>What ends this stop</span>
                      <select
                        value={beat.advance_on}
                        disabled={i === arc.beats.length - 1}
                        onChange={(e) =>
                          edit(i, { advance_on: e.target.value as Advance })
                        }
                      >
                        <option value="arrival">
                          They share their location from there
                        </option>
                        <option value="photo">
                          They send a photograph from there
                        </option>
                        {i === arc.beats.length - 1 && (
                          <option value="operator">
                            You end it — last stop
                          </option>
                        )}
                      </select>
                    </label>

                    <label className="field">
                      <span>What happens here</span>
                      <textarea
                        rows={3}
                        value={beat.goal}
                        onChange={(e) => edit(i, { goal: e.target.value })}
                      />
                    </label>
                    <label className="field">
                      <span>How the character points them there</span>
                      <textarea
                        rows={2}
                        value={beat.hint || ""}
                        onChange={(e) => edit(i, { hint: e.target.value })}
                      />
                    </label>
                  </div>
                );
              })}
            </div>

            {tooFar && (
              <p className="gate__error">
                {total > MAX_ROUTE_M
                  ? `That is ${(total / 1000).toFixed(1)} km of walking, over the ${MAX_ROUTE_M / 1000} km limit.`
                  : `One leg is ${Math.round(longest)} m, over the ${MAX_LEG_M} m limit.`}{" "}
                Swap a stop for something closer.
              </p>
            )}
            {arc.beats.length > MAX_STAGES && (
              <p className="gate__error">
                {arc.beats.length} stops is more than {MAX_STAGES}.
              </p>
            )}
          </div>
        )}

        <div className="sheet__actions">
          <button className="act" onClick={onClose}>
            {dirty ? "Discard" : "Close"}
          </button>
          {!arc && (
            <button
              className="composer__send"
              onClick={() => void compose()}
              disabled={!at.trim() || composing}
            >
              {composing ? "Writing…" : "Compose"}
            </button>
          )}
          {arc && (
            <>
              <button
                className="act"
                onClick={() => {
                  setArc(null);
                  setCandidates([]);
                  setError(null);
                }}
                disabled={saving || composing}
                title="Throw this away and write a different one"
              >
                Start over
              </button>
              <button
                className="act"
                onClick={() => void save(true)}
                disabled={saving || tooFar}
                title="Save and send them back to the first stop"
              >
                Save &amp; restart
              </button>
              <button
                className="composer__send"
                onClick={() => void save(false)}
                disabled={saving || tooFar}
              >
                {saving ? "Saving…" : "Save"}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
