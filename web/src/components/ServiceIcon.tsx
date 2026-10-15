// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
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

export function ServiceIcon({ name, size = 20 }: { name: string; size?: number }) {
  const I = ICONS[name] ?? Square
  return <I size={size} weight="light" aria-hidden="true" />
}
