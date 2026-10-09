# Secrets runbook

Where things live: every key is a SecureString at `/mavis/prod/<KEY>` in SSM Parameter Store (ap-south-1, standard tier, AWS managed key `alias/aws/ssm`, tag `Project=mavis`). The box renders them to `/run/mavis/mavis.env` (tmpfs, root, mode 600) via `mavis-secrets.service` at boot and during every deploy. There is no `.env` on the box. Nothing here ever prints a value; do not paste values into chat, tickets or shell history.

## Everyday commands (laptop, profile `cashfree`)

```bash
deploy/aws/secrets.sh list                         # names and last-modified only
deploy/aws/secrets.sh push                         # dry run: which owner keys in your local .env differ
deploy/aws/secrets.sh push --apply                 # write only the changed ones
printf '%s' "$VALUE" | deploy/aws/secrets.sh set KEY --apply   # one key from stdin (also for hand-added keys)
deploy/aws/secrets.sh render-remote                # re-render /run/mavis/mavis.env on the box
deploy/aws/compose.sh ps                           # compose on the box with the env file
```

## Rotate a key

Applies to owner keys (`OLLAMA_API_KEY`, `TAVILY_API_KEY`, `TELEGRAM_BOT_TOKEN`, Google and Slack client secrets, ...).

1. Create the new key at the provider.
2. Put it in your local `.env`, then `deploy/aws/secrets.sh push` (check the plan) and `push --apply`. Or `printf '%s' "$NEW" | deploy/aws/secrets.sh set KEY --apply`.
3. `deploy/aws/deploy.sh` (renders and recreates the containers with the new value). For a quick change without a rebuild: `deploy/aws/secrets.sh render-remote`, then `deploy/aws/compose.sh up -d` (recreates only the services whose environment changed).
4. Revoke the old key at the provider.

`NATIVE_TOKEN_KEK` rotates only through `NATIVE_TOKEN_KEK_PREVIOUS` (old value there, new value in `NATIVE_TOKEN_KEK`) so sealed grants stay readable; read the connector notes before doing it.

## What never to rotate (or delete)

- `NATIVE_TOKEN_KEK`: wraps every stored Google and Slack token. Replacing or losing it disconnects every user, who must reconnect.
- `POSTGRES_PASSWORD` and `NEO4J_PASSWORD`: the databases keep the password they were first created with; changing only the parameter locks the api out. Changing them needs `ALTER USER` / `neo4j-admin` first.
- `TELEGRAM_WEBHOOK_SECRET`: changing it breaks the registered webhook until `deploy/aws/webhook.sh set` runs again (cheap, but do it deliberately).

`deploy.sh` creates these only when missing and `secrets.sh set` refuses them unless `--force-rotate-generated`. Do not delete the parameters in the console. Deletion is unrecoverable; SSM is the only copy.

## Recover or rebuild the box

1. `deploy/aws/provision.sh`, `deploy/aws/iam-role.sh --apply` (the role needs the `mavis-secrets-read` policy and the instance profile), `deploy/aws/bootstrap.sh`.
2. `deploy/aws/deploy.sh --no-webhook`: reads SSM, renders, builds. Nothing is regenerated because the keys exist. Restore database data separately (`/var/backups/mavis` dumps).
3. Reboot of a healthy box needs nothing: the unit recreates `/run/mavis/mavis.env` and containers restart on their own. If the unit failed (`systemctl status mavis-secrets`), check the instance role and the network, then `sudo systemctl restart mavis-secrets`.
4. If the rendered file is missing and you need compose now: `sudo /usr/local/sbin/mavis-secrets render`.

`render` refuses to overwrite the file when any of `POSTGRES_PASSWORD`, `NEO4J_PASSWORD`, `TELEGRAM_WEBHOOK_SECRET`, `NATIVE_TOKEN_KEK` is missing from SSM, so a partial store can never start the stack with a blank key.

## One-time migration from the old /opt/mavis/.env

`iam-role.sh --apply`, `secrets.sh seed-from-box` (plan), `secrets.sh seed-from-box --apply`, `bootstrap.sh`, `deploy.sh`. `deploy.sh` refuses to continue if the old file holds a key SSM lacks, and deletes the old file (shred, then rm) only after the new stack is up.
