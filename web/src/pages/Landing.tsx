import { Link, useSearchParams } from 'react-router-dom'
import { useMe } from '../api/hooks'
import { Weave } from '../components/Weave'
import { BOT_URL } from '../lib/channels'
import styles from './Landing.module.css'

const TASKS = ['dispute an unwarranted bill', 'send birthday gifts to friends', 'chase an invoice', 'book a doctor\'s appointment', 'plan a weekend getaway']

export function Landing() {
  const me = useMe()
  const [params] = useSearchParams()
  const invite = params.get('invite')
  const signIn = invite ? `/login?invite=${encodeURIComponent(invite)}` : '/login'
  return (
    <div className={styles.page}>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className={styles.mark}><Weave size={72} animated title="Mavis AI" /></header>
      <main id="main" className={styles.copy} tabIndex={-1}>
        <h1 className={styles.lead}>Mavis AI is a personal assistant that knows what you are working on and what matters to you.</h1>
        <p>
          There is nothing new to learn. You text it in Telegram or Slack, the places you already talk, and it gets on with it.
          It reads your mail and calendar when you allow it, remembers what you tell it, and asks before it acts.
        </p>
        <p>
          It can help you{' '}
          {TASKS.map((t, i) => (
            <span key={t}><span className={styles.task}>{t}</span>{i < TASKS.length - 2 ? ', ' : i === TASKS.length - 2 ? ', or ' : '.'}</span>
          ))}
        </p>
        <nav className={styles.actions} aria-label="Get started">
          <a className={styles.cta} href={BOT_URL} target="_blank" rel="noopener noreferrer">Text Mavis <span aria-hidden="true">&rsaquo;</span></a>
          {me.data
            ? <Link to="/workspace">Open your workspace</Link>
            : <Link to={signIn}>Sign in</Link>}
        </nav>
      </main>
      <footer className={styles.foot}>
        <span>Mavis AI</span>
      </footer>
    </div>
  )
}
