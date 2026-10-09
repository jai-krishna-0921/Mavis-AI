// Simple stroke glyphs for each Google Workspace service (our own drawings, not vendor logos).
const PATHS: Record<string, string> = {
  mail: 'M3 6h18v12H3z M3 7l9 6 9-6',
  calendar: 'M4 5h16v15H4z M4 10h16 M8 3v4 M16 3v4',
  drive: 'M12 4l8 14H4z M8.5 11h7',
  docs: 'M6 3h9l3 3v15H6z M9 11h6 M9 15h6',
  sheets: 'M5 4h14v16H5z M5 9h14 M5 14h14 M11 4v16',
  tasks: 'M5 12l4 4 10-10',
  meet: 'M3 7h12v10H3z M15 11l6-3v8l-6-3',
  contacts: 'M12 11a3.5 3.5 0 100-7 3.5 3.5 0 000 7z M5 20c0-4 3-6 7-6s7 2 7 6',
}

export const SERVICES = ['mail', 'calendar', 'drive', 'docs', 'sheets', 'tasks', 'meet', 'contacts'] as const

export function ServiceIcon({ name, size = 20 }: { name: string; size?: number }) {
  const d = PATHS[name] ?? 'M4 4h16v16H4z'
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  )
}
