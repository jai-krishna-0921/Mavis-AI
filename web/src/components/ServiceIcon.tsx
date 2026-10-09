import {
  AddressBook, CalendarDots, CheckSquare, EnvelopeSimple, FileText, GoogleDriveLogo, Table, VideoCamera, Square,
  type Icon,
} from '@phosphor-icons/react'

const ICONS: Record<string, Icon> = {
  mail: EnvelopeSimple,
  calendar: CalendarDots,
  drive: GoogleDriveLogo,
  docs: FileText,
  sheets: Table,
  tasks: CheckSquare,
  meet: VideoCamera,
  contacts: AddressBook,
}

export const SERVICES = ['mail', 'calendar', 'drive', 'docs', 'sheets', 'tasks', 'meet', 'contacts'] as const

export function ServiceIcon({ name, size = 20 }: { name: string; size?: number }) {
  const I = ICONS[name] ?? Square
  return <I size={size} weight="light" aria-hidden="true" />
}
