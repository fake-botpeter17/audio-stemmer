import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ChangeEvent, DragEvent, FormEvent } from "react";
import "./index.css";

const API_BASE = (import.meta.env.VITE_API_BASE?.trim() ?? "").replace(
  /\/$/,
  "",
);
const MAX_DURATION_SECONDS = 60;
const DOWNLOAD_PROGRESS_FALLBACK_STEPS = 4;
const ACCEPTED_MEDIA_TYPES =
  "audio/*,video/mp4,video/quicktime,.m4a,.mp4,.mov,.aac,.wav,.mp3,.flac,.ogg,.webm";
const ACCEPTED_MEDIA_EXTENSIONS = new Set([
  ".aac",
  ".aif",
  ".aiff",
  ".flac",
  ".m4a",
  ".mov",
  ".mp3",
  ".mp4",
  ".ogg",
  ".opus",
  ".wav",
  ".webm",
]);
const STEM_ORDER = ["vocals", "drums", "bass", "other"] as const;
const STATUS_POLL_INTERVAL_MS = 2_000;
const STATUS_TIMEOUT_MS = 20 * 60 * 1_000;

type StemName = (typeof STEM_ORDER)[number];

type StemTrack = {
  id: StemName | string;
  name: string;
  url: string;
};

type UploadStatus =
  | "idle"
  | "ready"
  | "queued"
  | "processing"
  | "downloading"
  | "complete"
  | "error";
type JobStatus = "queued" | "processing" | "complete" | "error";

type DownloadProgress = {
  completed: number;
  total: number;
  currentStem: string | null;
};

type JobStatusResponse = {
  job_id?: string;
  status?: JobStatus | boolean;
  complete?: boolean;
  error?: string;
  stems?: unknown;
};

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

const labelStem = (name: string) =>
  name
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());

const formatBytes = (bytes: number) => {
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }

  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

const fileExtension = (filename: string) => {
  const extension = filename.toLowerCase().match(/\.[^.]+$/);
  return extension?.[0] ?? "";
};

const isAcceptedMediaFile = (file: File) =>
  file.type.startsWith("audio/") ||
  file.type === "video/mp4" ||
  file.type === "video/quicktime" ||
  ACCEPTED_MEDIA_EXTENSIONS.has(fileExtension(file.name));

const hasDraggedFiles = (dataTransfer: DataTransfer) =>
  Array.from(dataTransfer.types).includes("Files");

const formatPlaybackTime = (seconds: number) => {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "0:00";
  }

  const wholeSeconds = Math.floor(seconds);
  return `${Math.floor(wholeSeconds / 60)}:${(wholeSeconds % 60)
    .toString()
    .padStart(2, "0")}`;
};

const getApiUrl = (path: string) => {
  const normalizedPath = path.replace(/^\//, "");
  return API_BASE ? `${API_BASE}/${normalizedPath}` : `/${normalizedPath}`;
};

const extractUrl = (value: unknown): string | null => {
  if (typeof value === "string") {
    return value;
  }

  if (!isObject(value)) {
    return null;
  }

  const candidate =
    value.url ??
    value.audioUrl ??
    value.href ??
    value.src ??
    value.file ??
    value.data;
  return typeof candidate === "string" ? candidate : null;
};

const toPlayableUrl = (value: string) => {
  if (value.startsWith("blob:") || value.startsWith("data:")) {
    return value;
  }

  if (value.startsWith("http")) {
    const url = new URL(value);
    return getApiUrl(`${url.pathname}${url.search}${url.hash}`);
  }

  return getApiUrl(value);
};

const normalizeStems = (payload: unknown): StemTrack[] => {
  const source =
    isObject(payload) && "stems" in payload ? payload.stems : payload;

  if (Array.isArray(source)) {
    return source
      .map((item, index) => {
        const url = extractUrl(item);
        if (!url) {
          return null;
        }

        const rawName =
          isObject(item) && typeof item.name === "string"
            ? item.name
            : (STEM_ORDER[index] ?? `stem ${index + 1}`);
        return {
          id: rawName,
          name: labelStem(rawName),
          url: toPlayableUrl(url),
        };
      })
      .filter((track): track is StemTrack => track !== null);
  }

  if (!isObject(source)) {
    return [];
  }

  return Object.entries(source)
    .map(([name, value]) => {
      const url = extractUrl(value);
      return url
        ? { id: name, name: labelStem(name), url: toPlayableUrl(url) }
        : null;
    })
    .filter((track): track is StemTrack => track !== null)
    .sort((left, right) => {
      const leftIndex = STEM_ORDER.indexOf(left.id as StemName);
      const rightIndex = STEM_ORDER.indexOf(right.id as StemName);
      return (
        (leftIndex === -1 ? 99 : leftIndex) -
        (rightIndex === -1 ? 99 : rightIndex)
      );
    });
};

const sleep = (milliseconds: number) =>
  new Promise<void>((resolve) => {
    window.setTimeout(resolve, milliseconds);
  });

const createJobId = () => {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }

  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
};

const parseJobStatus = (payload: unknown): JobStatusResponse => {
  if (typeof payload === "boolean") {
    return { status: payload ? "complete" : "processing", complete: payload };
  }

  if (!isObject(payload)) {
    throw new Error("The backend returned an unreadable job status.");
  }

  const rawStatus = payload.status;
  const normalizedStatus =
    typeof rawStatus === "boolean"
      ? rawStatus
        ? "complete"
        : "processing"
      : rawStatus;

  return {
    job_id: typeof payload.job_id === "string" ? payload.job_id : undefined,
    status:
      normalizedStatus === "queued" ||
      normalizedStatus === "processing" ||
      normalizedStatus === "complete" ||
      normalizedStatus === "error"
        ? normalizedStatus
        : undefined,
    complete:
      typeof payload.complete === "boolean" ? payload.complete : undefined,
    error: typeof payload.error === "string" ? payload.error : undefined,
    stems: payload.stems,
  };
};

const fetchJobStatus = async (jobId: string): Promise<JobStatusResponse> => {
  const response = await fetch(getApiUrl(`/get-job-status/${jobId}`));

  if (!response.ok) {
    throw new Error(`Status check failed with status ${response.status}.`);
  }

  return parseJobStatus(await response.json());
};

const waitForJobCompletion = async (jobId: string) => {
  const deadline = Date.now() + STATUS_TIMEOUT_MS;

  while (Date.now() < deadline) {
    const jobStatus = await fetchJobStatus(jobId);

    if (jobStatus.error || jobStatus.status === "error") {
      throw new Error(
        jobStatus.error ?? "The backend could not finish this stem job.",
      );
    }

    if (jobStatus.complete || jobStatus.status === "complete") {
      return jobStatus;
    }

    await sleep(STATUS_POLL_INTERVAL_MS);
  }

  throw new Error("Timed out waiting for the backend to finish this stem job.");
};

const validateMediaDuration = (file: File) =>
  new Promise<number>((resolve, reject) => {
    const media = document.createElement(
      file.type.startsWith("video/") ? "video" : "audio",
    );
    const objectUrl = URL.createObjectURL(file);

    media.preload = "metadata";
    media.onloadedmetadata = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(media.duration);
    };
    media.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(
        new Error(
          "Unable to read this media file. Try an MP3, M4A, MP4, WAV, FLAC, or another ffmpeg-compatible clip.",
        ),
      );
    };
    media.src = objectUrl;
  });

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [status, setStatus] = useState<UploadStatus>("idle");
  const [error, setError] = useState("");
  const [stems, setStems] = useState<StemTrack[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [volumes, setVolumes] = useState<Record<string, number>>({});
  const [playbackTime, setPlaybackTime] = useState(0);
  const [playbackDuration, setPlaybackDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isDraggingMedia, setIsDraggingMedia] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<DownloadProgress>({
    completed: 0,
    total: 0,
    currentStem: null,
  });
  const audioRefs = useRef<Record<string, HTMLAudioElement | null>>({});
  const dragDepth = useRef(0);
  const stemObjectUrls = useRef<string[]>([]);

  const releaseDownloadedStemUrls = useCallback(() => {
    for (const objectUrl of stemObjectUrls.current) {
      URL.revokeObjectURL(objectUrl);
    }

    stemObjectUrls.current = [];
  }, []);

  useEffect(() => releaseDownloadedStemUrls, [releaseDownloadedStemUrls]);

  const downloadStemAudio = useCallback(
    async (tracks: StemTrack[]) => {
      const downloadedUrls: string[] = [];
      const downloadedTracks: StemTrack[] = [];

      setDownloadProgress({
        completed: 0,
        total: tracks.length,
        currentStem: null,
      });

      try {
        for (const [index, track] of tracks.entries()) {
          const completedBeforeTrack = index;
          setDownloadProgress({
            completed: completedBeforeTrack,
            total: tracks.length,
            currentStem: track.name,
          });

          const response = await fetch(track.url);

          if (!response.ok) {
            throw new Error(
              `Could not download ${track.name} with status ${response.status}.`,
            );
          }

          const contentLength = Number(response.headers.get("content-length"));
          const body = response.body;
          let blob: Blob;

          if (body && Number.isFinite(contentLength) && contentLength > 0) {
            const reader = body.getReader();
            const chunks: BlobPart[] = [];
            let receivedBytes = 0;

            while (true) {
              const { done, value } = await reader.read();
              if (done) {
                break;
              }

              if (value) {
                chunks.push(value);
                receivedBytes += value.byteLength;
                setDownloadProgress({
                  completed:
                    completedBeforeTrack + receivedBytes / contentLength,
                  total: tracks.length,
                  currentStem: track.name,
                });
              }
            }

            blob = new Blob(chunks, {
              type: response.headers.get("content-type") ?? "audio/mpeg",
            });
          } else {
            setDownloadProgress({
              completed:
                completedBeforeTrack + 1 / DOWNLOAD_PROGRESS_FALLBACK_STEPS,
              total: tracks.length,
              currentStem: track.name,
            });
            blob = await response.blob();
          }

          const objectUrl = URL.createObjectURL(blob);
          downloadedUrls.push(objectUrl);
          downloadedTracks.push({ ...track, url: objectUrl });
          setDownloadProgress({
            completed: index + 1,
            total: tracks.length,
            currentStem: track.name,
          });
        }

        releaseDownloadedStemUrls();
        stemObjectUrls.current = downloadedUrls;
        setDownloadProgress({
          completed: tracks.length,
          total: tracks.length,
          currentStem: null,
        });
        return downloadedTracks;
      } catch (downloadError) {
        for (const objectUrl of downloadedUrls) {
          URL.revokeObjectURL(objectUrl);
        }

        throw downloadError;
      }
    },
    [releaseDownloadedStemUrls],
  );

  const durationLabel = useMemo(() => {
    if (duration === null || Number.isNaN(duration)) {
      return "Up to 1:00";
    }

    return `${Math.floor(duration / 60)}:${Math.floor(duration % 60)
      .toString()
      .padStart(2, "0")}`;
  }, [duration]);

  const statusSteps = [
    { label: "Upload", active: file !== null || status === "complete" },
    {
      label: "Separate",
      active:
        status === "queued" ||
        status === "processing" ||
        status === "downloading" ||
        status === "complete",
    },
    {
      label: "Download",
      active: status === "downloading" || status === "complete",
    },
    { label: "Mix", active: status === "complete" },
  ];

  const getStemPlayers = useCallback(
    () =>
      stems
        .map((stem) => audioRefs.current[stem.id])
        .filter((player): player is HTMLAudioElement => player !== null),
    [stems],
  );

  const syncPlaybackPosition = useCallback((player: HTMLAudioElement) => {
    setPlaybackTime(player.currentTime);

    if (Number.isFinite(player.duration)) {
      setPlaybackDuration(player.duration);
    }
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (!isPlaying || stems.length === 0) {
        return;
      }

      const players = getStemPlayers();
      const leader = players[0];
      if (!leader) {
        return;
      }

      syncPlaybackPosition(leader);

      for (const player of players.slice(1)) {
        if (Math.abs(player.currentTime - leader.currentTime) > 0.12) {
          player.currentTime = leader.currentTime;
        }
      }
    }, 500);

    return () => window.clearInterval(interval);
  }, [getStemPlayers, isPlaying, stems.length, syncPlaybackPosition]);

  const handleMediaFile = async (selectedFile: File | null) => {
    dragDepth.current = 0;
    setIsDraggingMedia(false);
    setError("");
    releaseDownloadedStemUrls();
    setStems([]);
    setJobId(null);
    setPlaybackTime(0);
    setPlaybackDuration(0);
    setIsPlaying(false);

    if (!selectedFile) {
      setFile(null);
      setDuration(null);
      setStatus("idle");
      return;
    }

    if (!isAcceptedMediaFile(selectedFile)) {
      setFile(null);
      setDuration(null);
      setStatus("error");
      setError(
        "Please upload an audio or video file that can be converted to MP3.",
      );
      return;
    }

    try {
      const selectedDuration = await validateMediaDuration(selectedFile);
      if (selectedDuration > MAX_DURATION_SECONDS + 0.25) {
        setFile(null);
        setDuration(selectedDuration);
        setStatus("error");
        setError("Please trim the clip to 1 minute or less before stemming.");
        return;
      }

      setFile(selectedFile);
      setDuration(selectedDuration);
      setStatus("ready");
    } catch (validationError) {
      setFile(null);
      setDuration(null);
      setStatus("error");
      setError(
        validationError instanceof Error
          ? validationError.message
          : "Unable to validate this audio clip.",
      );
    }
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    void handleMediaFile(event.target.files?.[0] ?? null);
  };

  const handleDropZoneDragEnter = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();

    if (isBusy || !hasDraggedFiles(event.dataTransfer)) {
      return;
    }

    dragDepth.current += 1;
    event.dataTransfer.dropEffect = "copy";
    setIsDraggingMedia(true);
  };

  const handleDropZoneDragOver = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();

    if (!isBusy && hasDraggedFiles(event.dataTransfer)) {
      event.dataTransfer.dropEffect = "copy";
    }
  };

  const handleDropZoneDragLeave = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();

    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) {
      setIsDraggingMedia(false);
    }
  };

  const handleDropZoneDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();

    if (isBusy) {
      return;
    }

    void handleMediaFile(event.dataTransfer.files.item(0));
  };

  const handleStemAudio = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    if (!file) {
      setError("Choose an audio clip first.");
      setStatus("error");
      return;
    }

    setStatus("processing");
    setError("");
    releaseDownloadedStemUrls();
    setStems([]);
    setPlaybackTime(0);
    setPlaybackDuration(0);
    setIsPlaying(false);
    setDownloadProgress({ completed: 0, total: 0, currentStem: null });

    const nextJobId = createJobId();
    const formData = new FormData();
    formData.append("audio", file);

    try {
      setJobId(nextJobId);
      const response = await fetch(getApiUrl(`/stem/${nextJobId}`), {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        throw new Error(`Stem request failed with status ${response.status}.`);
      }

      setStatus("queued");
      const completedJob = await waitForJobCompletion(nextJobId);
      const nextStems = normalizeStems(completedJob.stems).slice(0, 4);
      if (nextStems.length !== 4) {
        throw new Error("The backend did not return four playable stems.");
      }

      setStatus("downloading");
      const playableStems = await downloadStemAudio(nextStems);

      setStems(playableStems);
      setVolumes(Object.fromEntries(playableStems.map((stem) => [stem.id, 1])));
      setPlaybackTime(0);
      setPlaybackDuration(0);
      setStatus("complete");
    } catch (requestError) {
      setStatus("error");
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Something went wrong while stemming your audio.",
      );
    }
  };

  const resetStemmer = () => {
    for (const player of Object.values(audioRefs.current)) {
      player?.pause();
    }

    setFile(null);
    setDuration(null);
    setStatus("idle");
    setError("");
    releaseDownloadedStemUrls();
    setStems([]);
    setJobId(null);
    setPlaybackTime(0);
    setPlaybackDuration(0);
    setVolumes({});
    setIsPlaying(false);
    setIsDraggingMedia(false);
    dragDepth.current = 0;
    setDownloadProgress({ completed: 0, total: 0, currentStem: null });
    audioRefs.current = {};
  };

  const isBusy =
    status === "queued" || status === "processing" || status === "downloading";
  const downloadPercent =
    downloadProgress.total > 0
      ? Math.min(
          100,
          Math.max(
            0,
            (downloadProgress.completed / downloadProgress.total) * 100,
          ),
        )
      : 0;

  const togglePlayback = async () => {
    const players = getStemPlayers();
    const leader = players[0];

    if (status !== "complete" || !leader) {
      return;
    }

    if (isPlaying) {
      players.forEach((player) => player.pause());
      setIsPlaying(false);
      return;
    }

    const startAt = leader.ended ? 0 : leader.currentTime;
    players.forEach((player) => {
      player.currentTime = startAt;
      player.volume = volumes[player.dataset.stemId ?? ""] ?? 1;
    });

    await Promise.all(players.map((player) => player.play()));
    setIsPlaying(true);
  };

  const updateVolume = (stemId: string, nextVolume: number) => {
    setVolumes((currentVolumes) => ({
      ...currentVolumes,
      [stemId]: nextVolume,
    }));

    const player = audioRefs.current[stemId];
    if (player) {
      player.volume = nextVolume;
    }
  };

  const handleTrackEnded = () => {
    const players = getStemPlayers();
    if (players.every((player) => player.ended || player.paused)) {
      setIsPlaying(false);
    }
  };

  const seekPlayback = (nextTime: number) => {
    const safeTime = Math.min(
      Math.max(nextTime, 0),
      playbackDuration || nextTime,
    );

    for (const player of getStemPlayers()) {
      player.currentTime = safeTime;
    }

    setPlaybackTime(safeTime);
  };

  return (
    <main className={`app-shell app-shell--${status}`}>
      <section className="hero-card" aria-labelledby="page-title">
        <div className="orb orb--left" />
        <div className="orb orb--right" />

        <div className="eyebrow">
          <span className="eyebrow-dot" />
          AI powered stem separation
        </div>
        <h1 id="page-title">Split any short clip into four mix-ready stems.</h1>
        <p className="hero-copy">
          Upload a clip up to one minute long, send it to the stemmer, then
          audition the separated tracks in sync with individual fades.
        </p>

        <div className="status-rail" aria-label="Stem separation progress">
          {statusSteps.map((step) => (
            <span
              className={
                step.active ? "status-pill status-pill--active" : "status-pill"
              }
              key={step.label}
            >
              {step.label}
            </span>
          ))}
        </div>

        {status !== "complete" ? (
          <form className="upload-panel" onSubmit={handleStemAudio}>
            <label
              className={
                isDraggingMedia ? "drop-zone drop-zone--dragging" : "drop-zone"
              }
              onDragEnter={handleDropZoneDragEnter}
              onDragLeave={handleDropZoneDragLeave}
              onDragOver={handleDropZoneDragOver}
              onDrop={handleDropZoneDrop}
            >
              <input
                type="file"
                accept={ACCEPTED_MEDIA_TYPES}
                onChange={handleFileChange}
                disabled={isBusy}
              />
              <span className="drop-icon">♫</span>
              <strong>
                {file
                  ? file.name
                  : isDraggingMedia
                    ? "Release to upload this clip"
                    : "Drop in an audio or video clip"}
              </strong>
              <small>
                {file
                  ? `${durationLabel} selected`
                  : "MP3, M4A, MP4, WAV, FLAC, OGG • 60 seconds max"}
              </small>
              <span className="wave-preview" aria-hidden="true">
                {Array.from({ length: 22 }).map((_, index) => (
                  <span
                    key={index}
                    style={
                      {
                        "--bar": index,
                        "--bar-height": `${18 + (index % 6) * 7}px`,
                      } as CSSProperties
                    }
                  />
                ))}
              </span>
              {file ? (
                <span className="file-chip">
                  Ready to stem • {formatBytes(file.size)} •{" "}
                  {file.type || "audio"}
                </span>
              ) : null}
            </label>

            <button
              className="primary-button"
              type="submit"
              disabled={!file || isBusy}
            >
              {isBusy ? "Preparing stems…" : "Stem audio"}
            </button>
          </form>
        ) : (
          <section className="stems-panel" aria-label="Separated stems">
            <div className="transport-card">
              <button
                className="play-button"
                type="button"
                onClick={togglePlayback}
                disabled={status !== "complete"}
              >
                <span className="play-glyph">{isPlaying ? "Ⅱ" : "▶"}</span>
                <span>{isPlaying ? "Pause all" : "Play all"}</span>
              </button>
              <p>
                All four stems launch together and stay synchronized while you
                blend the mix.
              </p>
              {jobId ? (
                <small className="job-chip">Job ID: {jobId}</small>
              ) : null}
              <span
                className={
                  isPlaying ? "live-badge live-badge--on" : "live-badge"
                }
              >
                {isPlaying ? "Live mix" : "Ready"}
              </span>
            </div>

            <label className="seek-card">
              <span>{formatPlaybackTime(playbackTime)}</span>
              <input
                type="range"
                min="0"
                max={playbackDuration || 0}
                step="0.01"
                value={Math.min(playbackTime, playbackDuration || playbackTime)}
                disabled={playbackDuration === 0}
                onChange={(event) => seekPlayback(Number(event.target.value))}
                style={
                  {
                    "--seek": `${
                      playbackDuration > 0
                        ? Math.min((playbackTime / playbackDuration) * 100, 100)
                        : 0
                    }%`,
                  } as CSSProperties
                }
              />
              <span>{formatPlaybackTime(playbackDuration)}</span>
            </label>

            <div className="stem-list">
              {stems.map((stem, index) => (
                <article
                  className="stem-card"
                  key={stem.id}
                  style={{ animationDelay: `${index * 90}ms` }}
                >
                  <div className="stem-heading">
                    <span className="stem-number">0{index + 1}</span>
                    <h2>{stem.name}</h2>
                    <span className="stem-meter" aria-hidden="true">
                      <span />
                      <span />
                      <span />
                      <span />
                    </span>
                  </div>
                  <audio
                    ref={(element) => {
                      audioRefs.current[stem.id] = element;
                    }}
                    data-stem-id={stem.id}
                    src={stem.url}
                    preload="auto"
                    onLoadedMetadata={(event) =>
                      syncPlaybackPosition(event.currentTarget)
                    }
                    onTimeUpdate={(event) => {
                      if (index === 0) {
                        syncPlaybackPosition(event.currentTarget);
                      }
                    }}
                    onEnded={handleTrackEnded}
                  />
                  <label className="volume-control">
                    <span>Fade</span>
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.01"
                      value={volumes[stem.id] ?? 1}
                      onChange={(event) =>
                        updateVolume(stem.id, Number(event.target.value))
                      }
                      style={
                        {
                          "--volume": `${(volumes[stem.id] ?? 1) * 100}%`,
                        } as CSSProperties
                      }
                    />
                    <strong>
                      {Math.round((volumes[stem.id] ?? 1) * 100)}%
                    </strong>
                  </label>
                </article>
              ))}
            </div>

            <div className="result-actions">
              {jobId ? (
                <a
                  className="secondary-button"
                  href={getApiUrl(`/get-stems/${jobId}`)}
                >
                  Download stems zip
                </a>
              ) : null}
              <button
                className="secondary-button"
                type="button"
                onClick={resetStemmer}
              >
                Stem another audio
              </button>
            </div>
          </section>
        )}

        {isBusy ? (
          <div className="processing-card" role="status" aria-live="polite">
            <div className="loader" />
            <div>
              <strong>
                {status === "downloading"
                  ? "Downloading stems for playback…"
                  : "StemmerAI is listening closely…"}
              </strong>
              <span>
                {status === "downloading"
                  ? downloadProgress.currentStem
                    ? `Downloading ${downloadProgress.currentStem} stem for playback.`
                    : "The stems are ready. Preparing local playback now."
                  : `Job ${jobId?.slice(0, 8) ?? "pending"} is running in the background. Keep this tab open while the backend renders each stem.`}
              </span>
              {status === "downloading" ? (
                <div
                  className="download-progress"
                  aria-label="Stem download progress"
                  aria-valuemax={100}
                  aria-valuemin={0}
                  aria-valuenow={Math.round(downloadPercent)}
                  role="progressbar"
                >
                  <span
                    className="download-progress__bar"
                    style={
                      {
                        "--download-progress": `${downloadPercent}%`,
                      } as CSSProperties
                    }
                  />
                  <small>{Math.round(downloadPercent)}% downloaded</small>
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        {error ? <p className="error-message">{error}</p> : null}
      </section>
    </main>
  );
}
