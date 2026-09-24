# Running this on a server

A 2 vCPU / 4 GB VPS is comfortable and about €4 a month. Measured on a running stack: ddiver
itself uses 56 MB, the whole observability side another 280 MB. The limits in the compose files
add up to a 2.3 GB ceiling, so even if everything here misbehaves at once, a second project on
the same box still has room.

## First time

```bash
scp deploy/bootstrap.sh root@YOUR_SERVER:/tmp/
ssh root@YOUR_SERVER 'bash /tmp/bootstrap.sh'
```

It installs Docker, turns on a firewall that allows nothing but SSH, disables password logins,
adds unattended security updates and a time daemon, creates a 2 GB swapfile so `docker build`
cannot run the box out of memory, and makes a `deploy` user.

It prints a deploy key the first time. Add it to the repository under **Settings → Deploy keys**
(read-only is enough) and run it again — then it clones.

Then, in order:

```bash
scp .env root@YOUR_SERVER:/srv/ddiver/.env         # not in git, and must not be

ssh root@YOUR_SERVER
chown deploy:deploy /srv/ddiver/.env && chmod 600 /srv/ddiver/.env
sudo -u deploy -i                                  # `ssh deploy@` runs the deploy, see below
cd /srv/ddiver
docker compose run --rm ddiver login --phone +380…
docker compose run --rm ddiver login --code 12345
docker compose run --rm ddiver login --password-file /dev/stdin   # or --password

docker compose -f docker-compose.yml -f deploy/docker-compose.observability.yml up -d
```

The detour through `root` is not decoration: the `deploy` user's `authorized_keys` carries a
forced command, so an SSH session as `deploy` runs `deploy.sh` and exits. There is no shell on
that account to get to, which is the point of it.

Sign in **on the server**, not locally. A session file copied between machines is the one thing
that reliably gets it invalidated, and a session whose first and only address is the server's
is the least interesting thing an anti-fraud system will see that day.

## Deploying

Push to `main`. CI runs ruff, mypy and the tests; only if all of them pass does it SSH in and
run `deploy/deploy.sh`, which fetches, builds, restarts and then waits for the container to
report healthy.

One thing to know about that script: SSH starts it from the checkout as it is *before* the
fetch, so a commit that changes `deploy.sh` is deployed by the previous version of it, and the
new one runs from the next deploy onwards. The script says so in its own output when it
notices. If a change to it has to take effect immediately, run a second deploy by hand. A failed build changes nothing. A build that succeeds but comes up unhealthy is
rolled back to the previous commit automatically — unless the container was *already* unhealthy
before the deploy, in which case rolling back would only hide an expired session.

Four repository secrets make it work; `bootstrap.sh` prints all four when it finishes:

| Secret | What it is |
|---|---|
| `DEPLOY_HOST` | the server's address |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_SSH_KEY` | the private half of the key made for CI |
| `DEPLOY_HOST_KEY` | the server's host key, so the first connection cannot be intercepted |

That key can do exactly one thing. Its `authorized_keys` entry forces `deploy.sh` and forbids a
pty, agent forwarding and port forwarding, so a leaked secret buys an attacker a deployment of
your own code and nothing else.

To deploy by hand, or to see why an automated one failed:

```bash
ssh -i ~/.ssh/ddiver_ci deploy@YOUR_SERVER
```

That key can do nothing else, so it needs no command — whatever you type is ignored and
`deploy.sh` runs. To pin a particular commit, name it and nothing else:

```bash
ssh -i ~/.ssh/ddiver_ci deploy@YOUR_SERVER "$(git rev-parse origin/main)"
```

Anything else — reading logs, `ddiver login`, a shell — goes through `root` and then
`sudo -u deploy -i`.

## Sharing the box with other projects

Nothing here claims a public port. The bot needs no inbound connection at all, and Grafana,
Prometheus and Loki bind to `127.0.0.1`, reached over an SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 root@YOUR_SERVER
```

As `root`, not `deploy`: the deploy key forbids port forwarding, so the tunnel would be
refused before the dashboard ever loaded.

| Port (localhost only) | What |
|---|---|
| 3000 | Grafana |
| 9090 | Prometheus |
| 3100 | Loki |

The compose project is named `ddiver`, so its containers, volumes and network are all namespaced
under that name and cannot be confused with another project's. Everything lives in
`/srv/ddiver`; put the next project in `/srv/whatever` and give it its own `name:`.

When a second project needs ports 80 and 443, do not give them to it directly. Add one shared
reverse proxy — Caddy is the least work, with automatic certificates — on an external Docker
network that every project attaches to. Each project then answers on its own hostname, and
nothing has to negotiate for a port. Until that day, the three ports above are private to the
tunnel and collide with nobody.

## When something is wrong

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.observability.yml ps
docker logs --tail 100 ddiver
docker exec ddiver ddiver health
```

`ddiver health` exits non-zero when the heartbeat is stale, the transport is down or the
listener has not answered a ping. If it says the session is degraded, no restart will help —
that is a `ddiver login` on the server.
