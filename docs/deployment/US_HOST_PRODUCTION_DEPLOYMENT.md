# US Host Production Deployment

This file is the deployment handoff for the current `aitrans.video` US host.
Keep it in sync whenever the production compose layout changes.

## Canonical Layout

The production root is:

```text
/opt/aivideotrans/
  docker-compose.yml        # only production Compose entrypoint
  config/.env               # production env and secrets
  app/                      # application source tree
  data/                     # persistent projects, jobs, logs, caches
  caddy/                    # Caddy config and state
```

`/opt/aivideotrans/docker-compose.yml` is the only production Compose
entrypoint. Do not deploy from `/opt/aivideotrans/app`.

## Compose Source Of Truth

The repository `docker-compose.yml` is the source of truth for production.
On the server it is copied to:

```text
/opt/aivideotrans/docker-compose.yml
```

The same file also supports local development. Production build paths are
selected through `.env`:

```dotenv
AIVIDEOTRANS_ROOT=/opt/aivideotrans
AIVIDEOTRANS_APP_BUILD_CONTEXT=/opt/aivideotrans/app
AIVIDEOTRANS_NEXT_BUILD_CONTEXT=/opt/aivideotrans/app/frontend-next
AIVIDEOTRANS_GATEWAY_BUILD_CONTEXT=/opt/aivideotrans/app/gateway
```

Host bind mounts should use `${AIVIDEOTRANS_ROOT}/...` so data, config, logs,
and Caddy state stay anchored under the deployment root.

## Deploy

Use this from the US host:

```bash
cd /opt/aivideotrans
docker compose --env-file /opt/aivideotrans/config/.env config -q
docker compose --env-file /opt/aivideotrans/config/.env up -d --build
```

Do not run `docker compose down -v` in production. It can remove persistent
volumes such as PostgreSQL data.

## Verify

After deploy:

```bash
cd /opt/aivideotrans
docker compose --env-file /opt/aivideotrans/config/.env ps
docker compose ls --all
```

`docker compose ls --all` must show only:

```text
/opt/aivideotrans/docker-compose.yml
```

Check container labels if there is any doubt:

```bash
for c in \
  aivideotrans-app \
  aivideotrans-gateway \
  aivideotrans-next \
  aivideotrans-caddy \
  aivideotrans-cloudflared-us \
  aivideotrans-postgres
do
  docker inspect "$c" --format '{{.Name}} {{index .Config.Labels "com.docker.compose.project.config_files"}} {{index .Config.Labels "com.docker.compose.project.working_dir"}}'
done
```

Every container must report:

```text
/opt/aivideotrans/docker-compose.yml /opt/aivideotrans
```

Smoke-test public auth pages:

```bash
curl -L -sS -o /tmp/avt_register.html -w '%{http_code}\n' https://aitrans.video/auth/register
curl -L -sS -o /tmp/avt_login.html -w '%{http_code}\n' https://aitrans.video/auth/login
```

Both should return `200`.

## Frontend Public Env Rule

Any `NEXT_PUBLIC_*` value that the browser needs must be available at Next.js
build time. That means all three places must agree:

- `docker-compose.yml` build args
- `frontend-next/Dockerfile` `ARG` and `ENV`
- `/opt/aivideotrans/config/.env`

Runtime-only env is not enough for client-side Next.js bundles.

For captcha specifically:

- frontend public IDs: `NEXT_PUBLIC_GEETEST_REGISTER_CAPTCHA_ID`,
  `NEXT_PUBLIC_GEETEST_LOGIN_CAPTCHA_ID`
- gateway server values: `AVT_GEETEST_*`

Do not print secret keys in logs or final answers.

## Known Bad Pattern

Do not keep two divergent Compose files:

```text
/opt/aivideotrans/docker-compose.yml
/opt/aivideotrans/app/docker-compose.yml
```

This previously caused containers in the same `COMPOSE_PROJECT_NAME` to be
created from different Compose files. The symptom was `next` running from the
root Compose file while `app` and `gateway` ran from the app Compose file,
which made frontend captcha build args drift from the codebase.

If `/opt/aivideotrans/app/docker-compose.yml` exists, it should either match
the root Compose file exactly or be treated as non-authoritative.
