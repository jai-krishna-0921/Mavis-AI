import { useState } from 'react'
import { useCreateInvite, useInvites, useMe, useRevokeInvite } from '../api/hooks'
import { Modal } from './Modal'
import { CopyButton } from './CopyButton'
import ui from '../styles/ui.module.css'

export function InviteModal({ onClose }: { onClose: () => void }) {
  const invites = useInvites()
  const me = useMe()
  const create = useCreateInvite()
  const revoke = useRevokeInvite()
  const [name, setName] = useState('')
  const left = me.data?.invites_left

  const onCreate = (e: React.FormEvent) => {
    e.preventDefault()
    create.mutate(name.trim() || undefined, { onSuccess: () => setName('') })
  }

  return (
    <Modal title="Invite a friend" onClose={onClose}>
      <p className={ui.lede}>
        Mavis is invite only. Make a link for someone you trust.
        {left !== null && left !== undefined ? ` You can make ${left} more.` : ''}
      </p>
      <form onSubmit={onCreate} style={{ marginTop: 18 }}>
        <div className={ui.field}>
          <label htmlFor="invite-name">Name (optional)</label>
          <input id="invite-name" className={ui.input} value={name} onChange={(e) => setName(e.target.value)} maxLength={60} autoComplete="off" />
        </div>
        <button type="submit" className={`${ui.btn} ${ui.btnPrimary}`} disabled={create.isPending || left === 0}>Create link</button>
        {create.error && <p className={ui.error} role="alert">{create.error.message}</p>}
      </form>

      <h3 style={{ fontSize: 17, margin: '24px 0 8px' }}>Your links</h3>
      {invites.isPending && <p className={ui.status}>Loading</p>}
      {invites.data?.length === 0 && <p className={ui.empty}>No links yet.</p>}
      <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
        {invites.data?.map((inv) => (
          <li key={inv.code} className={ui.row} style={{ flexWrap: 'wrap' }}>
            <div className={ui.rowMain}>
              <div className={ui.rowTitle}>{inv.name || 'Unnamed link'}</div>
              <div className={ui.rowSub}>{inv.link ?? (inv.hint ? `Link ending in ${inv.hint}` : 'Link made earlier')}</div>
              <div className={ui.rowSub}>
                {inv.uses} of {inv.max_uses ?? 'unlimited'} uses
              </div>
            </div>
            <div className={ui.rowActions}>
              {inv.link && <CopyButton text={inv.link} label="Copy link" what={inv.name ? `for ${inv.name}` : inv.code} />}
              <button type="button" className={`${ui.btn} ${ui.btnDanger}`} disabled={revoke.isPending}
                aria-label={`Revoke link ${inv.name || inv.code}`} onClick={() => revoke.mutate(inv.code)}>
                Revoke
              </button>
            </div>
          </li>
        ))}
      </ul>
      {revoke.error && <p className={ui.error} role="alert">{revoke.error.message}</p>}
      <div className="actions" style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20 }}>
        <button type="button" className={ui.btn} onClick={onClose}>Done</button>
      </div>
    </Modal>
  )
}
