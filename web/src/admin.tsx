/* Shared pieces of the admin app: access data, labels and the check-list picker. */
import type { ReactNode } from 'react'
import { useToast, Icon } from './ui'

export interface DataSet { name: string; label: string; kind: 'everyone' | 'team' | 'custom'; team?: string; description: string; files: number
  who: { everyone: boolean; teams: string[]; roles: string[]; people: string[] } }
export interface Role { name: string; description: string; datasets: string[]; connectors: string[]; builtin: boolean }
export interface Grant { datasets: string[]; connectors: string[]; roles: string[] }
export interface TeamA { link: string; name: string; slug: string; parent: string | null; depth: number; admins: string[]; members: number; dataset: string; grant: Grant }
export interface AccessData { datasets: DataSet[]; roles: Role[]; connectors: Record<string, string>; teams: TeamA[]
  can_give: { datasets: string[]; connectors: string[]; roles: string[]; all: boolean }; company_admin: boolean }
export interface Effective { datasets: Record<string, string[]>; connectors: Record<string, string[]>; teams: string[]; roles: string[] }

export const dsLabel = (a: AccessData | null, n: string) => a?.datasets.find((d) => d.name === n)?.label || n

/* a row of toggle chips: the minimal multi-select used everywhere */
export function Checks({ options, value, onChange, locked = [], empty, lockHint = 'Comes from their team' }: { options: { v: string; label: ReactNode; hint?: string }[]; value: string[]
  onChange: (v: string[]) => void; locked?: string[]; empty?: string; lockHint?: string }) {
  if (!options.length) return <div className="small muted">{empty || 'Nothing to choose.'}</div>
  return <div className="picks">{options.map((o) => {
    const on = value.includes(o.v) || locked.includes(o.v); const lock = locked.includes(o.v)
    return <button type="button" key={o.v} className={`pick${on ? ' on' : ''}${lock ? ' lock' : ''}`} title={lock ? lockHint : o.hint}
      disabled={lock} onClick={() => onChange(on ? value.filter((x) => x !== o.v) : [...value, o.v])}>{on ? '✓ ' : ''}{o.label}</button>
  })}</div>
}

export function Section({ title, sub, right, children }: { title: string; sub?: ReactNode; right?: ReactNode; children: ReactNode }) {
  return <section className="sect"><div className="secthd"><div><h2>{title}</h2>{sub && <div className="small muted">{sub}</div>}</div>{right}</div>{children}</section>
}

/* a small "copy link" button; relative links get this site's address */
export function CopyLink({ url, label = 'Copy link' }: { url: string; label?: string }) {
  const toast = useToast(); const full = url.startsWith('/') ? location.origin + url : url
  return <button type="button" className="btn ghost sm" title={full} onClick={async () => { try { await navigator.clipboard.writeText(full); toast('Link copied') } catch { toast(full) } }}>
    <Icon name="copy" size={14} />{label}</button>
}
