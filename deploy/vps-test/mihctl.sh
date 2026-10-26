#!/usr/bin/env bash
# Manage the MIRTVONLINE test stand: preflight, deploy, backup, restore, rollback, uninstall.
# Touches only objects named mirtvonline-test*: one container, one volume, one network, and
# images tagged mirtvonline/mih:test-*. Nothing else on the host is read for writing or changed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STATE="$HERE/state"
LAST_BACKUP=
BACKUPS="$HERE/backups"
CONTAINER=mirtvonline-test
VOLUME=mirtvonline-test-data
NETWORK=mirtvonline-test-net
IMAGE_REPO=mirtvonline/mih

cd "$HERE"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

env_value() {
  # Read one KEY=value from .env without sourcing it.
  [[ -f .env ]] || return 0
  sed -n "s/^$1=//p" .env | tail -n 1
}

compose() {
  local image
  image="$(cat "$STATE/current_image" 2>/dev/null || true)"
  [[ -n "$image" ]] || die "nothing deployed yet: run ./mihctl.sh deploy"
  MIH_IMAGE="$image" docker compose -f compose.yml "$@"
}

container_exists() { docker container inspect "$CONTAINER" >/dev/null 2>&1; }

wait_healthy() {
  local status
  for _ in $(seq 1 30); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || true)"
    [[ "$status" == healthy ]] && { say "container is healthy"; return 0; }
    sleep 2
  done
  docker logs --tail 50 "$CONTAINER" >&2 || true
  die "container did not become healthy (last status: ${status:-none})"
}

port_in_use() {
  if command -v ss >/dev/null; then
    ss -ltnH | awk '{print $4}' | grep -Eq "[:.]$2\$"
  else
    # No ss: try to connect on loopback and on the publish address.
    (exec 3<>"/dev/tcp/127.0.0.1/$2") 2>/dev/null || (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null
  fi
}

cmd_preflight() {
  # Read-only checks: nothing is created, started or changed.
  local ok=1 port addr
  port="$(env_value MIH_PUBLISH_PORT)"; port="${port:-18765}"
  addr="$(env_value MIH_PUBLISH_ADDR)"; addr="${addr:-127.0.0.1}"

  say "== docker"
  docker version --format 'engine {{.Server.Version}}' || { say "FAIL: docker is not reachable"; ok=0; }
  docker compose version || { say "FAIL: docker compose v2 is missing"; ok=0; }

  say "== .env"
  if [[ ! -f .env ]]; then
    say "FAIL: no .env, run: cp .env.example .env && chmod 600 .env"; ok=0
  else
    local token; token="$(env_value MIH_API_TOKEN)"
    (( ${#token} >= 24 )) || { say "FAIL: MIH_API_TOKEN is shorter than 24 characters"; ok=0; }
    [[ "$(stat -c %a .env)" == 600 ]] || say "WARN: chmod 600 .env"
  fi
  if [[ "$addr" == 0.0.0.0 || "$addr" == "::" ]]; then
    say "FAIL: MIH_PUBLISH_ADDR=$addr would expose the API on every interface"; ok=0
  fi
  say "publish address: $addr:$port"

  say "== port $port"
  if port_in_use "$addr" "$port"; then
    if container_exists && docker port "$CONTAINER" 2>/dev/null | grep -q ":$port\$"; then
      say "in use by $CONTAINER itself (already deployed)"
    else
      say "FAIL: port $port is taken by another service, pick another MIH_PUBLISH_PORT"; ok=0
    fi
  else
    say "free"
  fi

  say "== names"
  for kind in container volume network; do
    local name; name="$CONTAINER"; [[ $kind == volume ]] && name=$VOLUME; [[ $kind == network ]] && name=$NETWORK
    if docker "$kind" inspect "$name" >/dev/null 2>&1; then
      local labels=.Labels owner; [[ $kind == container ]] && labels=.Config.Labels
      owner="$(docker "$kind" inspect -f "{{index $labels \"com.docker.compose.project\"}}" "$name" 2>/dev/null || true)"
      if [[ "$owner" == mirtvonline-test ]]; then say "$kind $name: ours"; else say "FAIL: $kind $name exists and is not ours"; ok=0; fi
    else
      say "$kind $name: free"
    fi
  done

  say "== resources"
  free -h | sed -n '1,2p' || true
  df -h "$HERE" /var/lib/docker 2>/dev/null | awk 'NR==1 || !seen[$1]++' || true
  nproc 2>/dev/null | sed 's/^/cpus: /' || true

  say "== tailscale"
  if command -v tailscale >/dev/null; then tailscale ip -4 2>/dev/null | sed 's/^/tailscale ipv4: /' || true
  else say "tailscale CLI not found (fine if access goes through an SSH tunnel)"; fi

  (( ok )) && say "PREFLIGHT OK" || die "preflight found problems, see FAIL lines above"
}

cmd_deploy() {
  cmd_preflight
  mkdir -p "$STATE"
  local rev tag previous
  rev="$(git -C "$REPO" rev-parse --short=12 HEAD)"
  [[ -z "$(git -C "$REPO" status --porcelain -- src pyproject.toml Dockerfile)" ]] || rev="$rev-dirty"
  tag="$IMAGE_REPO:test-$rev"
  previous="$(cat "$STATE/current_image" 2>/dev/null || true)"

  say "== build $tag"
  # --pull refreshes the base image for security fixes; MIH_BUILD_PULL=0 reuses the local one.
  local pull=--pull; [[ "${MIH_BUILD_PULL:-1}" == 0 ]] && pull=
  docker build $pull -t "$tag" "$REPO"

  if container_exists; then
    say "== backup before upgrade"
    cmd_backup
    printf '%s\n' "$LAST_BACKUP" >"$STATE/pre_upgrade_backup"
  fi
  if [[ -n "$previous" && "$previous" != "$tag" ]]; then
    printf '%s\n' "$previous" >"$STATE/previous_image"
  fi
  printf '%s\n' "$tag" >"$STATE/current_image"
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$tag" >>"$STATE/history"

  say "== start"
  compose up -d --remove-orphans
  wait_healthy
  cmd_status
}

cmd_status() {
  compose ps
  local port addr token
  port="$(env_value MIH_PUBLISH_PORT)"; port="${port:-18765}"
  addr="$(env_value MIH_PUBLISH_ADDR)"; addr="${addr:-127.0.0.1}"
  token="$(env_value MIH_API_TOKEN)"
  say "== live";  curl -fsS "http://$addr:$port/api/v1/health/live"; echo
  say "== ready"; curl -fsS -H "Authorization: Bearer $token" "http://$addr:$port/api/v1/health/ready"; echo
  say "== stats"; docker exec "$CONTAINER" mih stats
}

cmd_backup() {
  container_exists || die "container $CONTAINER does not exist"
  mkdir -p "$BACKUPS"
  local ts tmp out
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="$(mktemp -d)"
  out="$BACKUPS/mih-$ts.tar.gz"
  # SQLite online backup: consistent even while the service writes.
  docker exec "$CONTAINER" python -c "
import sqlite3
src = sqlite3.connect('/data/mih.sqlite3')
dst = sqlite3.connect('/data/.backup.sqlite3')
src.backup(dst)
dst.close()
"
  docker cp "$CONTAINER:/data/.backup.sqlite3" "$tmp/mih.sqlite3"
  docker exec "$CONTAINER" rm -f /data/.backup.sqlite3
  docker cp "$CONTAINER:/data/snapshots" "$tmp/snapshots" 2>/dev/null || mkdir -p "$tmp/snapshots"
  cat "$STATE/current_image" >"$tmp/image" 2>/dev/null || true
  tar -C "$tmp" -czf "$out" .
  rm -rf "$tmp"
  LAST_BACKUP="$out"
  say "backup: $out ($(du -h "$out" | cut -f1))"
  prune_backups
}

prune_backups() {
  # Keep the newest MIH_BACKUP_KEEP archives (default 5) and always the pre-upgrade one,
  # which rollback needs.
  local keep pinned
  keep="$(env_value MIH_BACKUP_KEEP)"; keep="${keep:-5}"
  [[ "$keep" =~ ^[1-9][0-9]*$ ]] || die "MIH_BACKUP_KEEP must be a positive number"
  pinned="$(cat "$STATE/pre_upgrade_backup" 2>/dev/null || true)"
  ls -1t "$BACKUPS"/mih-*.tar.gz 2>/dev/null | tail -n +"$((keep + 1))" | while read -r old; do
    [[ "$old" == "$pinned" ]] && continue
    rm -f "$old" && say "removed old backup: $old"
  done
}

cmd_restore() {
  local file="${1:-}"
  [[ -n "$file" ]] || file="$(ls -1t "$BACKUPS"/mih-*.tar.gz 2>/dev/null | head -n 1 || true)"
  [[ -f "$file" ]] || die "no backup file; usage: ./mihctl.sh restore backups/mih-<time>.tar.gz"
  say "restoring $file into volume $VOLUME"
  compose stop mih
  # Unpack with the stand's own image, as the service user, into the stand's own volume.
  compose run --rm --no-deps -T -v "$(cd "$(dirname "$file")" && pwd)/$(basename "$file"):/restore.tar.gz:ro" \
    --entrypoint python mih -c "
import pathlib, shutil, tarfile
data = pathlib.Path('/data')
for name in ('mih.sqlite3', 'mih.sqlite3-wal', 'mih.sqlite3-shm'):
    (data / name).unlink(missing_ok=True)
shutil.rmtree(data / 'snapshots', ignore_errors=True)
with tarfile.open('/restore.tar.gz') as tar:
    tar.extractall(data, members=[m for m in tar.getmembers() if m.name != './image'], filter='data')
print('restored')
"
  compose up -d
  wait_healthy
}

cmd_rollback() {
  local previous
  previous="$(cat "$STATE/previous_image" 2>/dev/null || true)"
  [[ -n "$previous" ]] || die "no previous image recorded"
  docker image inspect "$previous" >/dev/null 2>&1 || die "image $previous is gone"
  local backup
  backup="$(cat "$STATE/pre_upgrade_backup" 2>/dev/null || true)"
  say "rolling back to $previous"
  cp "$STATE/current_image" "$STATE/rolled_back_from"
  printf '%s\n' "$previous" >"$STATE/current_image"
  printf '%s rollback %s\n' "$(date -u +%FT%TZ)" "$previous" >>"$STATE/history"
  # The newer image may have migrated the database forward; the pre-upgrade backup matches
  # the older image, so restore it along with the image.
  if [[ -f "$backup" ]]; then cmd_restore "$backup"; else compose up -d; wait_healthy; fi
  rm -f "$STATE/previous_image" "$STATE/pre_upgrade_backup"
}

cmd_uninstall() {
  [[ "${1:-}" == --yes ]] || die "removes the container, network and data volume of the stand; run with --yes (make a backup first)"
  if [[ -f "$STATE/current_image" ]]; then compose down --volumes; fi
  docker image ls "$IMAGE_REPO" --format '{{.Repository}}:{{.Tag}}' | grep ":test-" | xargs -r docker image rm
  rm -rf "$STATE"
  say "removed; backups are kept in $BACKUPS"
}

case "${1:-}" in
  preflight) cmd_preflight ;;
  deploy) cmd_deploy ;;
  status) cmd_status ;;
  logs) shift; compose logs --tail "${1:-200}" mih ;;
  backup) cmd_backup ;;
  restore) shift; cmd_restore "${1:-}" ;;
  rollback) cmd_rollback ;;
  uninstall) shift; cmd_uninstall "${1:-}" ;;
  *) say "usage: ./mihctl.sh preflight|deploy|status|logs [n]|backup|restore [file]|rollback|uninstall --yes"; exit 1 ;;
esac
