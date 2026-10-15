// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
import { GoogleLogo, Plugs, SlackLogo, type Icon } from '@phosphor-icons/react'

const ICONS: Record<string, { Icon: Icon; weight: 'bold' | 'light' }> = {
  google: { Icon: GoogleLogo, weight: 'bold' },
  slack: { Icon: SlackLogo, weight: 'light' },
}

export function ConnectorIcon({ icon, size = 24 }: { icon: string; size?: number }) {
  const { Icon: I, weight } = ICONS[icon] ?? { Icon: Plugs, weight: 'light' as const }
  return <I size={size} weight={weight} aria-hidden="true" />
}
