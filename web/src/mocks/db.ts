import type { Connector, Invite, Me, Preferences, SourceInfo, VaultItem, VaultSource } from '../api/types'

const now = Date.parse('2026-10-08T09:00:00Z')
const ago = (days: number) => new Date(now - days * 86_400_000).toISOString()

function seedItems(): VaultItem[] {
  const mk = (id: string, kind: VaultItem['kind'], title: string, detail: string, source: VaultSource, trust: VaultItem['trust'], d: number): VaultItem =>
    ({ id, kind, title, detail, source, trust, updated_at: ago(d) })
  return [
    mk('p1', 'person', 'Priya Raman', 'Co-founder. Prefers short messages before 10am.', 'you', 'user', 2),
    mk('p2', 'person', 'Daniel Okoye', 'Accountant at Lark & Finch. Sends invoices on the 1st.', 'gmail', 'medium', 5),
    mk('p3', 'person', 'Meera Iyer', 'Landlord. Rent due on the 5th.', 'gmail', 'high', 9),
    mk('p4', 'person', 'Sam Whitlock', 'Design contractor, shared Slack channel #brand.', 'slack', 'medium', 11),
    mk('p5', 'person', '<img src=x onerror=alert(1)> Mallory', 'Text from an email signature. <script>alert("x")</script> shown as plain text.', 'gmail', 'low', 3),
    mk('o1', 'organisation', 'Lark & Finch LLP', 'Accounting firm, engagement renews in March.', 'gmail', 'medium', 7),
    mk('o2', 'organisation', 'Northwind Studio', 'Client since 2024. Weekly sync on Thursdays.', 'calendar', 'high', 4),
    mk('o3', 'organisation', 'Zento Labs', 'Your company.', 'you', 'user', 30),
    mk('r1', 'project', 'Spring launch', 'Target 14 April. Blockers: pricing page copy.', 'slack', 'medium', 1),
    mk('r2', 'project', 'Tax filing 2026', 'Documents collected, waiting on Daniel.', 'gmail', 'medium', 6),
    mk('r3', 'project', 'Flat move', 'Lease ends 30 November.', 'you', 'user', 14),
    mk('f1', 'fact', 'Lives in Bengaluru', 'Timezone Asia/Kolkata.', 'you', 'user', 40),
    mk('f2', 'fact', 'Flies with Indigo when possible', 'Learned from three past bookings.', 'gmail', 'medium', 20),
    mk('f3', 'fact', 'Standup is at 09:30 on weekdays', 'From a recurring calendar event.', 'calendar', 'high', 8),
    mk('f4', 'fact', 'Dislikes calls before 11am', 'Said in chat.', 'you', 'user', 12),
    mk('f5', 'fact', 'Dentist appointment 21 Oct', 'From a confirmation email.', 'gmail', 'high', 2),
    mk('f6', 'fact', 'Team lunch every other Friday', 'Mentioned in #general.', 'slack', 'low', 3),
    mk('f7', 'fact', 'Allergic to penicillin', 'You told Mavis directly.', 'you', 'user', 90),
    mk('f8', 'fact', 'Prefers window seats', 'Learned from bookings.', 'gmail', 'low', 25),
    mk('f9', 'fact', 'Wife is Anjali', 'You told Mavis directly.', 'you', 'user', 120),
    mk('f10', 'fact', 'Gym on Tuesday and Thursday', 'From calendar.', 'calendar', 'medium', 10),
    mk('f11', 'fact', 'Uses INR as the main currency', 'Profile setting.', 'dashboard', 'user', 60),
    mk('x1', 'preference', 'Short, direct replies', 'No small talk in task updates.', 'you', 'user', 15),
    mk('x2', 'preference', 'Confirm before sending email', 'Always ask first.', 'you', 'user', 15),
    mk('x3', 'preference', 'Book aisle seats on flights over 3 hours', '', 'you', 'user', 33),
  ]
}

export type MockDb = ReturnType<typeof createDb>

export function createDb() {
  const me: Me = {
    user_id: 'u_123', name: 'Jai Krishna', timezone: 'Asia/Kolkata', currency: 'INR',
    channels: {
      telegram: { connected: true, label: '@jaikrishna', open_url: 'https://t.me/Mavis247_bot' },
      slack: { connected: true, label: 'Zento Labs workspace' },
    },
    email: 'jai@example.com', csrf: 'csrf-mock-token', invites_left: 3,
  }
  const connectors: Connector[] = [
    {
      id: 'google', name: 'Google Workspace', description: 'Mail, calendar, drive, docs and more, read and act on your behalf.',
      status: 'active', account: 'jai@example.com',
      scopes_granted: ['mail', 'calendar', 'drive', 'docs', 'sheets', 'tasks'], missing_scopes: ['meet', 'contacts'],
      connected_at: ago(40),
    },
    {
      id: 'slack', name: 'Slack', description: 'Read channels you invite Mavis to and message you there.',
      status: 'none', account: null, scopes_granted: [], missing_scopes: [], connected_at: null,
    },
  ]
  const preferences: Preferences = {
    name: 'Jai Krishna', timezone: 'Asia/Kolkata', quiet_hours: { start: '22:00', end: '07:00' },
    proactive_channel: 'telegram', morning_checkin_time: '08:30', language_register_opt_out: false,
  }
  const invites: Invite[] = [
    { code: 'k3m9x2', link: 'https://mavis.example.com/?invite=k3m9x2', name: 'Priya', uses: 1, max_uses: 5 },
  ]
  let items = seedItems()
  let pollCount = 0
  let nextInvite = 1
  return {
    me, connectors, preferences, invites,
    get items() { return items },
    set items(v: VaultItem[]) { items = v },
    suppressed: [] as string[],
    deleted: false,
    loggedIn: true,
    pollsUntilOk: 3,
    get pollCount() { return pollCount },
    bumpPoll() { pollCount += 1; return pollCount },
    resetPoll() { pollCount = 0 },
    newInviteId() { return `inv${nextInvite++}` },
    sources(): SourceInfo[] {
      const by = new Map<VaultSource, number>()
      for (const i of items) by.set(i.source, (by.get(i.source) ?? 0) + 1)
      return [...by.entries()].map(([source, count]) => ({ source, count, last_sync: source === 'you' || source === 'dashboard' ? null : ago(0.2) }))
    },
  }
}

const holder = { current: createDb() }
export const db = () => holder.current
export const resetDb = () => { holder.current = createDb() }
