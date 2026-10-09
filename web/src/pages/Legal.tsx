import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Wordmark } from '../components/Logo'

const UPDATED = '9 October 2026'
const CONTACT = 'mavis-support@googlegroups.com'

function LegalPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <main className="min-h-screen bg-bg px-5 py-12 text-ink">
      <div className="mx-auto max-w-3xl">
        <Link to="/" aria-label="Mavis AI home" className="no-underline"><Wordmark size={32} /></Link>
        <h1 className="mt-12 text-4xl font-bold tracking-tight">{title}</h1>
        <p className="mt-2 text-sm text-muted">Last updated {UPDATED}</p>
        <div className="legal mt-10 space-y-8 leading-relaxed text-ink [&_h2]:text-xl [&_h2]:font-bold [&_h2]:mb-3 [&_li]:ml-5 [&_li]:list-disc [&_li]:mb-1 [&_p]:mb-3">
          {children}
        </div>
        <footer className="mt-16 flex flex-wrap gap-6 border-t border-line pt-6 text-sm text-muted">
          <Link to="/privacy" className="no-underline hover:text-ink">Privacy Policy</Link>
          <Link to="/terms" className="no-underline hover:text-ink">Terms of Service</Link>
          <Link to="/" className="no-underline hover:text-ink">Home</Link>
        </footer>
      </div>
    </main>
  )
}

export function PrivacyPolicy() {
  return (
    <LegalPage title="Privacy Policy">
      <section>
        <p>
          Mavis AI ("Mavis", "we") is a personal assistant you talk to in Telegram and Slack. This policy explains what
          data Mavis uses, why, where it is kept, and how you control it. Questions: <a href={`mailto:${CONTACT}`}>{CONTACT}</a>.
        </p>
      </section>
      <section>
        <h2>What Mavis uses</h2>
        <ul>
          <li>Your messages to Mavis in Telegram or Slack, and the files you send it.</li>
          <li>Basic account details: your Telegram or Slack name and ID, time zone, and the email address of an account you connect.</li>
          <li>
            If you connect Google: the Gmail, Google Calendar, Google Drive, Docs, Sheets, Contacts, Tasks and Meet data
            covered by the permissions you approve.
          </li>
          <li>If you connect Slack: the channels and direct messages you can already read, and your Slack profile.</li>
        </ul>
        <p>Mavis only connects an account after you approve it on Google's or Slack's own page. You choose which permissions to grant.</p>
      </section>
      <section>
        <h2>How Mavis uses it</h2>
        <ul>
          <li>To answer you, summarise and search your mail, calendar, files and messages, and keep reminders and to-dos.</li>
          <li>To build a private knowledge layer about your people, projects and preferences, so its help fits you.</li>
          <li>To take actions you ask for, such as drafting or sending an email or a Slack message. Anything sent to other people needs your approval first.</li>
        </ul>
        <p>
          Content from email, Slack and the web is treated as information, never as instructions to Mavis. Passwords,
          one-time codes, card and bank numbers and similar secrets are masked before anything is stored.
        </p>
      </section>
      <section>
        <h2>Google user data</h2>
        <p>
          Mavis AI's use and transfer of information received from Google APIs to any other app will adhere to the{' '}
          <a href="https://developers.google.com/terms/api-services-user-data-policy" target="_blank" rel="noopener noreferrer">
            Google API Services User Data Policy
          </a>
          , including the Limited Use requirements.
        </p>
        <ul>
          <li>Google user data is used only to provide and improve the features you use in Mavis.</li>
          <li>It is never sold, never used for advertising, and never used to train general AI models.</li>
          <li>People do not read it, except with your permission, to investigate abuse or security issues, or where the law requires.</li>
          <li>It is shared only with the service providers below, solely to run Mavis for you.</li>
        </ul>
      </section>
      <section>
        <h2>Service providers</h2>
        <ul>
          <li>Amazon Web Services (Mumbai region) hosts Mavis and stores its data.</li>
          <li>An AI model provider (Ollama Cloud) processes the text needed to generate each reply. It is not used to train their models on your data.</li>
          <li>Telegram and Slack deliver messages between you and Mavis.</li>
        </ul>
      </section>
      <section>
        <h2>Storage and security</h2>
        <p>
          Data is stored per user and kept separate from other users. Access tokens for connected accounts are encrypted.
          Connections use HTTPS. No system is perfectly secure, and we will tell affected users promptly about a breach that affects them.
        </p>
      </section>
      <section>
        <h2>Your controls</h2>
        <ul>
          <li>See and correct what Mavis knows in the Vault on the dashboard, or ask it to forget something.</li>
          <li>Disconnect an account with /disconnect or on the dashboard. By default, what Mavis learned from it is forgotten.</li>
          <li>Clear your chat with /clear, or delete your account and all its data with /delete_me or on the dashboard.</li>
          <li>
            Revoke Mavis's Google access at any time at{' '}
            <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener noreferrer">myaccount.google.com/permissions</a>.
          </li>
        </ul>
        <p>When you delete your account, your data is removed from our systems, except where the law requires us to keep a record.</p>
      </section>
      <section>
        <h2>Children</h2>
        <p>Mavis is not for children under 13, and we do not knowingly collect their data.</p>
      </section>
      <section>
        <h2>Changes</h2>
        <p>If this policy changes in a meaningful way, we will tell you in Mavis before the change applies.</p>
      </section>
    </LegalPage>
  )
}

export function TermsOfService() {
  return (
    <LegalPage title="Terms of Service">
      <section>
        <p>
          These terms cover your use of Mavis AI. By using Mavis you agree to them. Questions:{' '}
          <a href={`mailto:${CONTACT}`}>{CONTACT}</a>.
        </p>
      </section>
      <section>
        <h2>The service</h2>
        <p>
          Mavis is an AI assistant offered by invitation, as is, while it is being built. Features may change. Mavis can
          make mistakes: check important information, and review anything before you approve it being sent.
        </p>
      </section>
      <section>
        <h2>Your account</h2>
        <ul>
          <li>You are responsible for what happens in your account and for the accounts you connect.</li>
          <li>Only connect accounts you are allowed to use, and follow the rules of Google, Slack and Telegram.</li>
        </ul>
      </section>
      <section>
        <h2>Acceptable use</h2>
        <p>Do not use Mavis to break the law, harm or harass others, send spam, or try to access data that is not yours.</p>
      </section>
      <section>
        <h2>Your data</h2>
        <p>
          Your data stays yours. We use it only to run Mavis for you, as described in the{' '}
          <Link to="/privacy">Privacy Policy</Link>.
        </p>
      </section>
      <section>
        <h2>Ending use</h2>
        <p>
          You can stop at any time and delete your account with /delete_me. We may suspend accounts that break these
          terms or put the service or other users at risk.
        </p>
      </section>
      <section>
        <h2>Liability</h2>
        <p>
          To the extent the law allows, Mavis is provided without warranties, and we are not liable for indirect or
          consequential losses arising from its use.
        </p>
      </section>
    </LegalPage>
  )
}
