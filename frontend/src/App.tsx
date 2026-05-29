import { useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ChangeEvent, FormEvent } from 'react'
import "./index.css"

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:5000'
const MAX_DURATION_SECONDS = 60
const STEM_ORDER = ['vocals', 'drums', 'bass', 'other'] as const

type StemName = (typeof STEM_ORDER)[number]

type StemTrack = {
  id: StemName | string
  name: string
  url: string
}

type UploadStatus = 'idle' | 'ready' | 'processing' | 'complete' | 'error'

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

const labelStem = (name: string) =>
  name
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())

const formatBytes = (bytes: number) => {
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }

  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

const extractUrl = (value: unknown): string | null => {
  if (typeof value === 'string') {
    return value
  }

  if (!isObject(value)) {
    return null
  }

  const candidate =
    value.url ?? value.audioUrl ?? value.href ?? value.src ?? value.file ?? value.data
  return typeof candidate === 'string' ? candidate : null
}

const toPlayableUrl = (value: string) => {
  if (value.startsWith('http') || value.startsWith('blob:') || value.startsWith('data:')) {
    return value
  }

  return `${API_BASE.replace(/\/$/, '')}/${value.replace(/^\//, '')}`
}

const normalizeStems = (payload: unknown): StemTrack[] => {
  const source = isObject(payload) && 'stems' in payload ? payload.stems : payload

  if (Array.isArray(source)) {
    return source
      .map((item, index) => {
        const url = extractUrl(item)
        if (!url) {
          return null
        }

        const rawName =
          isObject(item) && typeof item.name === 'string'
            ? item.name
            : STEM_ORDER[index] ?? `stem ${index + 1}`
        return { id: rawName, name: labelStem(rawName), url: toPlayableUrl(url) }
      })
      .filter((track): track is StemTrack => track !== null)
  }

  if (!isObject(source)) {
    return []
  }

  return Object.entries(source)
    .map(([name, value]) => {
      const url = extractUrl(value)
      return url ? { id: name, name: labelStem(name), url: toPlayableUrl(url) } : null
    })
    .filter((track): track is StemTrack => track !== null)
    .sort((left, right) => {
      const leftIndex = STEM_ORDER.indexOf(left.id as StemName)
      const rightIndex = STEM_ORDER.indexOf(right.id as StemName)
      return (leftIndex === -1 ? 99 : leftIndex) - (rightIndex === -1 ? 99 : rightIndex)
    })
}

const validateAudioDuration = (file: File) =>
  new Promise<number>((resolve, reject) => {
    const audio = new Audio()
    const objectUrl = URL.createObjectURL(file)

    audio.preload = 'metadata'
    audio.onloadedmetadata = () => {
      URL.revokeObjectURL(objectUrl)
      resolve(audio.duration)
    }
    audio.onerror = () => {
      URL.revokeObjectURL(objectUrl)
      reject(new Error('Unable to read this audio file. Please try another clip.'))
    }
    audio.src = objectUrl
  })

export default function App() {
  const [file, setFile] = useState<File | null>(null)
  const [duration, setDuration] = useState<number | null>(null)
  const [status, setStatus] = useState<UploadStatus>('idle')
  const [error, setError] = useState('')
  const [stems, setStems] = useState<StemTrack[]>([])
  const [volumes, setVolumes] = useState<Record<string, number>>({})
  const [isPlaying, setIsPlaying] = useState(false)
  const audioRefs = useRef<Record<string, HTMLAudioElement | null>>({})

  const durationLabel = useMemo(() => {
    if (duration === null || Number.isNaN(duration)) {
      return 'Up to 1:00'
    }

    return `${Math.floor(duration / 60)}:${Math.floor(duration % 60).toString().padStart(2, '0')}`
  }, [duration])

  const statusSteps = [
    { label: 'Upload', active: file !== null || status === 'complete' },
    { label: 'Separate', active: status === 'processing' || status === 'complete' },
    { label: 'Mix', active: status === 'complete' },
  ]

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (!isPlaying || stems.length === 0) {
        return
      }

      const players = stems
        .map((stem) => audioRefs.current[stem.id])
        .filter((player): player is HTMLAudioElement => player !== null)
      const leader = players[0]
      if (!leader) {
        return
      }

      for (const player of players.slice(1)) {
        if (Math.abs(player.currentTime - leader.currentTime) > 0.12) {
          player.currentTime = leader.currentTime
        }
      }
    }, 500)

    return () => window.clearInterval(interval)
  }, [isPlaying, stems])

  const handleFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFile = event.target.files?.[0] ?? null
    setError('')
    setStems([])
    setIsPlaying(false)

    if (!selectedFile) {
      setFile(null)
      setDuration(null)
      setStatus('idle')
      return
    }

    if (!selectedFile.type.startsWith('audio/')) {
      setFile(null)
      setDuration(null)
      setStatus('error')
      setError('Please upload a valid audio file.')
      return
    }

    try {
      const selectedDuration = await validateAudioDuration(selectedFile)
      if (selectedDuration > MAX_DURATION_SECONDS + 0.25) {
        setFile(null)
        setDuration(selectedDuration)
        setStatus('error')
        setError('Please trim the clip to 1 minute or less before stemming.')
        return
      }

      setFile(selectedFile)
      setDuration(selectedDuration)
      setStatus('ready')
    } catch (validationError) {
      setFile(null)
      setDuration(null)
      setStatus('error')
      setError(
        validationError instanceof Error
          ? validationError.message
          : 'Unable to validate this audio clip.',
      )
    }
  }

  const handleStemAudio = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (!file) {
      setError('Choose an audio clip first.')
      setStatus('error')
      return
    }

    setStatus('processing')
    setError('')
    setStems([])
    setIsPlaying(false)

    const formData = new FormData()
    formData.append('audio', file)

    try {
      const response = await fetch(`${API_BASE.replace(/\/$/, '')}/stem`, {
        method: 'POST',
        body: formData,
      })

      if (!response.ok) {
        throw new Error(`Stem request failed with status ${response.status}.`)
      }

      const payload = await response.json()
      const nextStems = normalizeStems(payload).slice(0, 4)
      if (nextStems.length !== 4) {
        throw new Error('The backend did not return four playable stems.')
      }

      setStems(nextStems)
      setVolumes(Object.fromEntries(nextStems.map((stem) => [stem.id, 1])))
      setStatus('complete')
    } catch (requestError) {
      setStatus('error')
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Something went wrong while stemming your audio.',
      )
    }
  }

  const resetStemmer = () => {
    for (const player of Object.values(audioRefs.current)) {
      player?.pause()
    }

    setFile(null)
    setDuration(null)
    setStatus('idle')
    setError('')
    setStems([])
    setVolumes({})
    setIsPlaying(false)
    audioRefs.current = {}
  }

  const togglePlayback = async () => {
    const players = stems
      .map((stem) => audioRefs.current[stem.id])
      .filter((player): player is HTMLAudioElement => player !== null)
    const leader = players[0]

    if (!leader) {
      return
    }

    if (isPlaying) {
      players.forEach((player) => player.pause())
      setIsPlaying(false)
      return
    }

    const startAt = leader.ended ? 0 : leader.currentTime
    players.forEach((player) => {
      player.currentTime = startAt
      player.volume = volumes[player.dataset.stemId ?? ''] ?? 1
    })

    await Promise.all(players.map((player) => player.play()))
    setIsPlaying(true)
  }

  const updateVolume = (stemId: string, nextVolume: number) => {
    setVolumes((currentVolumes) => ({ ...currentVolumes, [stemId]: nextVolume }))

    const player = audioRefs.current[stemId]
    if (player) {
      player.volume = nextVolume
    }
  }

  const handleTrackEnded = () => {
    const players = stems
      .map((stem) => audioRefs.current[stem.id])
      .filter((player): player is HTMLAudioElement => player !== null)
    if (players.every((player) => player.ended || player.paused)) {
      setIsPlaying(false)
    }
  }

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
          Upload a clip up to one minute long, send it to the stemmer, then audition the separated
          tracks in sync with individual fades.
        </p>

        <div className="status-rail" aria-label="Stem separation progress">
          {statusSteps.map((step) => (
            <span className={step.active ? 'status-pill status-pill--active' : 'status-pill'} key={step.label}>
              {step.label}
            </span>
          ))}
        </div>

        {status !== 'complete' ? (
          <form className="upload-panel" onSubmit={handleStemAudio}>
            <label className="drop-zone">
              <input
                type="file"
                accept="audio/*"
                onChange={handleFileChange}
                disabled={status === 'processing'}
              />
              <span className="drop-icon">♫</span>
              <strong>{file ? file.name : 'Drop in an audio clip'}</strong>
              <small>{file ? `${durationLabel} selected` : 'WAV, MP3, M4A, FLAC • 60 seconds max'}</small>
              <span className="wave-preview" aria-hidden="true">
                {Array.from({ length: 22 }).map((_, index) => (
                  <span
                    key={index}
                    style={
                      {
                        '--bar': index,
                        '--bar-height': `${18 + (index % 6) * 7}px`,
                      } as CSSProperties
                    }
                  />
                ))}
              </span>
              {file ? (
                <span className="file-chip">
                  Ready to stem • {formatBytes(file.size)} • {file.type || 'audio'}
                </span>
              ) : null}
            </label>

            <button className="primary-button" type="submit" disabled={!file || status === 'processing'}>
              {status === 'processing' ? 'Separating stems…' : 'Stem audio'}
            </button>
          </form>
        ) : (
          <section className="stems-panel" aria-label="Separated stems">
            <div className="transport-card">
              <button className="play-button" type="button" onClick={togglePlayback}>
                <span className="play-glyph">{isPlaying ? 'Ⅱ' : '▶'}</span>
                <span>{isPlaying ? 'Pause all' : 'Play all'}</span>
              </button>
              <p>All four stems launch together and stay synchronized while you blend the mix.</p>
              <span className={isPlaying ? 'live-badge live-badge--on' : 'live-badge'}>
                {isPlaying ? 'Live mix' : 'Ready'}
              </span>
            </div>

            <div className="stem-list">
              {stems.map((stem, index) => (
                <article className="stem-card" key={stem.id} style={{ animationDelay: `${index * 90}ms` }}>
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
                      audioRefs.current[stem.id] = element
                    }}
                    data-stem-id={stem.id}
                    src={stem.url}
                    preload="auto"
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
                      style={{ '--volume': `${(volumes[stem.id] ?? 1) * 100}%` } as CSSProperties}
                    />
                    <strong>{Math.round((volumes[stem.id] ?? 1) * 100)}%</strong>
                  </label>
                </article>
              ))}
            </div>

            <button className="secondary-button" type="button" onClick={resetStemmer}>
              Stem another audio
            </button>
          </section>
        )}

        {status === 'processing' ? (
          <div className="processing-card" role="status" aria-live="polite">
            <div className="loader" />
            <div>
              <strong>StemmerAI is listening closely…</strong>
              <span>
                This can take a little while. Keep this tab open while the backend renders each stem.
              </span>
            </div>
          </div>
        ) : null}

        {error ? <p className="error-message">{error}</p> : null}
      </section>
    </main>
  )
}
