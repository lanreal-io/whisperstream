import { useCallback, useEffect, useRef, useState } from "react";
import { hasVoiceSample } from "./voiceSamples";

/**
 * Single-player voice preview hook for the Agent Speak voice list
 * (design.md's "Play/pause state machine" table). Plays the bundled
 * pre-rendered clips at `frontend/public/voice-samples/`; makes no backend
 * call, so it must never import `lib/api`. Free of modal-specific props so
 * a PR 2 setup step can own it without the modal being open.
 *
 * One module-scoped HTMLAudioElement backs every hook instance across the
 * app, matching the locked "single-player" constraint: playing a new voice
 * always pauses and resets whatever was playing first.
 */

export type SpeechEngine = "kokoro" | "supertonic";

const MIN_PLAYBACK_RATE = 0.5;
const MAX_PLAYBACK_RATE = 2.0;

export function voiceSamplePath(engine: SpeechEngine, id: string | number): string {
  return `/voice-samples/${engine}-${id}.ogg`;
}

export function voiceKey(engine: SpeechEngine, id: string | number): string {
  return `${engine}/${id}`;
}

function clampPlaybackRate(rate: number): number {
  return Math.min(MAX_PLAYBACK_RATE, Math.max(MIN_PLAYBACK_RATE, rate));
}

let sharedAudio: HTMLAudioElement | null = null;

function getSharedAudio(): HTMLAudioElement {
  if (!sharedAudio) {
    sharedAudio = new Audio();
  }
  return sharedAudio;
}

export interface UseVoicePreview {
  playingKey: string | null;
  error: boolean;
  toggle: (engine: SpeechEngine, id: string | number) => void;
  stopAll: () => void;
}

export function useVoicePreview(speed: number, volume: number): UseVoicePreview {
  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const playingKeyRef = useRef<string | null>(null);
  // The row whose clip is currently loaded into the shared element, even
  // while paused. Lets a paused row resume from its kept position instead
  // of being treated as a fresh start (which would reset currentTime).
  const loadedKeyRef = useRef<string | null>(null);

  useEffect(() => {
    playingKeyRef.current = playingKey;
  }, [playingKey]);

  // Live speed/volume writes so both sliders demonstrate themselves mid-clip.
  useEffect(() => {
    const audio = getSharedAudio();
    audio.playbackRate = clampPlaybackRate(speed);
    audio.volume = volume;
  }, [speed, volume]);

  const stopAll = useCallback(() => {
    const audio = getSharedAudio();
    audio.pause();
    audio.currentTime = 0;
    loadedKeyRef.current = null;
    setPlayingKey(null);
  }, []);

  // `ended` returns control to play; `error` never throws and just clears state.
  useEffect(() => {
    const audio = getSharedAudio();

    function handleEnded() {
      audio.currentTime = 0;
      loadedKeyRef.current = null;
      setPlayingKey(null);
    }

    function handleError() {
      loadedKeyRef.current = null;
      setPlayingKey(null);
      setError(true);
    }

    audio.addEventListener("ended", handleEnded);
    audio.addEventListener("error", handleError);
    return () => {
      audio.removeEventListener("ended", handleEnded);
      audio.removeEventListener("error", handleError);
    };
  }, []);

  // Unmount tears everything down: a modal that keeps talking after it
  // closes is a bug.
  useEffect(() => {
    return () => {
      stopAll();
    };
  }, [stopAll]);

  const toggle = useCallback(
    (engine: SpeechEngine, id: string | number) => {
      if (!hasVoiceSample(engine, id)) return;
      const audio = getSharedAudio();
      const key = voiceKey(engine, id);

      if (playingKeyRef.current === key) {
        // Pause the currently playing row: keep position so play resumes.
        audio.pause();
        setPlayingKey(null);
        return;
      }

      if (playingKeyRef.current !== null) {
        // A different row is playing: only one clip is ever audible.
        audio.pause();
        audio.currentTime = 0;
      }

      if (loadedKeyRef.current !== key) {
        // Not the row already loaded (idle or a genuine switch): load fresh.
        audio.currentTime = 0;
        audio.src = voiceSamplePath(engine, id);
        loadedKeyRef.current = key;
      }
      // Otherwise the same row is loaded and paused: resume from its kept position.

      setError(false);
      audio.playbackRate = clampPlaybackRate(speed);
      audio.volume = volume;

      try {
        const playResult = audio.play();
        if (playResult && typeof playResult.catch === "function") {
          playResult.catch(() => {
            setPlayingKey(null);
            setError(true);
          });
        }
      } catch {
        setPlayingKey(null);
        setError(true);
        return;
      }

      setPlayingKey(key);
    },
    [speed, volume],
  );

  return { playingKey, error, toggle, stopAll };
}
