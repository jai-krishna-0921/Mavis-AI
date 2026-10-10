import { useState } from 'react'
import { LinkSimple, Trash } from '@phosphor-icons/react'
import { useCreateInvite, useInvites, useMe, useRevokeInvite } from '../api/hooks'
import { Modal } from './Modal'
import { CopyButton } from './CopyButton'
import { EmptyState, SkeletonRows } from './Ui'
import { useToast } from './Toast'

export function InviteModal({ onClose }: { onClose: () => void }) {
  const invites = useInvites()
  const me = useMe()
  const create = useCreateInvite()
  const revoke = useRevokeInvite()
  const toast = useToast()
  const [name, setName] = useState('')
  // the full link exists only in the create response (the server keeps a hash), so it is held here
  const [fresh, setFresh] = useState<{ link: string; name: string | null } | null>(null)
  const left = me.data?.invites_left

  const onCreate = (e: React.FormEvent) => {
    e.preventDefault()
    create.mutate(name.trim() || undefined, {
      onSuccess: (inv) => {
        setName('')
        if (inv.link) setFresh({ link: inv.link, name: inv.name ?? null })
        toast('Invite link created')
      },
    })
  }

  return (
    <Modal title="Invite a friend" onClose={onClose}>
      <p className="text-muted">
        Mavis is invite only. Make a link for someone you trust.
        {left !== null && left !== undefined ? ` You can make ${left} more.` : ''}
      </p>
      <form onSubmit={onCreate} className="mt-5">
        <div className="field">
          <label htmlFor="invite-name">Name (optional)</label>
          <input id="invite-name" className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} autoComplete="off" />
        </div>
        <button type="submit" className="btn btn-primary" disabled={create.isPending || left === 0}>Create link</button>
        {create.error && <p className="error" role="alert">{create.error.message}</p>}
      </form>
      {fresh && (
        <div className="mt-5 rounded-2xl border border-line bg-raised p-4" role="status" aria-live="polite">
          <div className="text-sm font-bold">{fresh.name ? `Link for ${fresh.name}` : 'Your new link'}</div>
          <input readOnly value={fresh.link} aria-label="New invite link" className="input mt-2 w-full font-mono text-sm"
            onFocus={(e) => e.currentTarget.select()} />
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <CopyButton text={fresh.link} label="Copy link" what={fresh.name ? `for ${fresh.name}` : 'invite'} />
            <span className="text-sm text-muted">Copy it now. For security it is shown only once.</span>
          </div>
        </div>
      )}

      <h3 className="mb-2 mt-8 text-base font-bold">Your links</h3>
      {invites.isPending && <SkeletonRows rows={2} />}
      {invites.data?.length === 0 && <EmptyState icon={<LinkSimple size={24} weight="light" />} title="No links yet">Links you make show up here.</EmptyState>}
      <ul className="m-0 list-none p-0">
        {invites.data?.map((inv) => (
          <li key={inv.code} className="row flex-wrap">
            <div className="min-w-0 flex-1 basis-[12rem]">
              <div className="row-title">{inv.name || 'Unnamed link'}</div>
              <div className="row-sub">{inv.link ?? (inv.hint ? `Link ending in ${inv.hint}` : 'Link made earlier')}</div>
              <div className="row-sub tnum">
                {inv.uses} of {inv.max_uses ?? 'unlimited'} uses
              </div>
            </div>
            <div className="row-actions">
              {inv.link && <CopyButton text={inv.link} label="Copy link" what={inv.name ? `for ${inv.name}` : inv.code} />}
              <button type="button" className="btn btn-sm btn-danger" disabled={revoke.isPending}
                aria-label={`Revoke link ${inv.name || inv.code}`}
                onClick={() => revoke.mutate(inv.code, { onSuccess: () => toast('Link revoked') })}>
                <Trash size={14} weight="light" aria-hidden="true" /> Revoke
              </button>
            </div>
          </li>
        ))}
      </ul>
      {revoke.error && <p className="error" role="alert">{revoke.error.message}</p>}
      <div className="mt-6 flex justify-end">
        <button type="button" className="btn" onClick={onClose}>Done</button>
      </div>
    </Modal>
  )
}
