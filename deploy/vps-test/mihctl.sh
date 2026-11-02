#!/usr/bin/env bash
# Manage the MIRTVONLINE test stand: preflight, deploy, update, autoupdate, backup, restore,
# rollback, public, uninstall.
# Touches only objects named mirtvonline-test*: one container, one volume, one network, and
# images tagged mirtvonline/mih:test-*. Nothing else on the host is changed, except this node's
# Tailscale Funnel entry with `public on|off`, which the owner runs on purpose.
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

to_mib() {
  # 3g, 1536m, 512M, 2048k or plain bytes -> MiB; empty when the value is not understood.
  local v="${1,,}"
  case "$v" in
    *g) awk -v n="${v%g}" 'BEGIN{if (n ~ /^[0-9.]+$/) printf "%d\n", n*1024}' ;;
    *m) awk -v n="${v%m}" 'BEGIN{if (n ~ /^[0-9.]+$/) printf "%d\n", n}' ;;
    *k) awk -v n="${v%k}" 'BEGIN{if (n ~ /^[0-9.]+$/) printf "%d\n", n/1024}' ;;
    *) awk -v n="${v%b}" 'BEGIN{if (n ~ /^[0-9]+$/) printf "%d\n", n/1048576}' ;;
  esac
}

check_limits() {
  # Warn (never fail) when the stand's caps leave the other services on this host less than
  # one CPU or less than half of the memory. Limits are caps, not reservations.
  local cpus mem mem_mib host_cpus host_mib
  cpus="$(env_value MIH_CPUS)"; cpus="${cpus:-2.0}"
  mem="$(env_value MIH_MEM_LIMIT)"; mem="${mem:-3g}"
  mem_mib="$(to_mib "$mem")"
  host_cpus="$(nproc 2>/dev/null || echo 0)"
  host_mib="$(awk '/^MemTotal:/{printf "%d\n", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
  say "limits: cpus $cpus of $host_cpus, memory $mem of ${host_mib} MiB"
  if (( host_cpus > 0 )) && awk -v c="$cpus" -v h="$host_cpus" 'BEGIN{exit !(c > h - 1)}'; then
    say "WARN: MIH_CPUS=$cpus leaves other services less than one CPU"
  fi
  if [[ -z "$mem_mib" ]]; then
    say "WARN: MIH_MEM_LIMIT=$mem is not understood (use e.g. 3g or 1536m)"
  elif (( host_mib > 0 && mem_mib * 2 > host_mib )); then
    say "WARN: MIH_MEM_LIMIT=$mem is more than half of this host's memory"
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

  local mcp_port; mcp_port="$(env_value MIH_MCP_PUBLISH_PORT)"; mcp_port="${mcp_port:-18766}"
  local proxy_port; proxy_port="$(env_value MIH_PROXY_PORT)"; proxy_port="${proxy_port:-18767}"
  local p var
  for p in "$port" "$mcp_port" "$proxy_port"; do
    var=MIH_PUBLISH_PORT; [[ "$p" == "$mcp_port" ]] && var=MIH_MCP_PUBLISH_PORT
    [[ "$p" == "$proxy_port" ]] && var=MIH_PROXY_PORT
    say "== port $p"
    if port_in_use "$addr" "$p"; then
      if container_exists && docker port "$CONTAINER" 2>/dev/null | grep -q ":$p\$"; then
        say "in use by $CONTAINER itself (already deployed)"
      else
        say "FAIL: port $p is taken by another service, pick another $var"; ok=0
      fi
    else
      say "free"
    fi
  done
  say "MCP address: 127.0.0.1:$mcp_port (loopback only)"

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
  check_limits


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

notify_owner() {
  # One push through the stand's own ntfy settings; silent if pushes are off or the
  # container is down (the update log keeps the message either way).
  container_exists || return 0
  docker exec "$CONTAINER" mih notify-send --level "${3:-warning}" "$1" "${2:-}" >/dev/null 2>&1 || true
}

ci_state() {
  # Prints success, pending or failed for the GitHub checks of commit $1; "success" when
  # MIH_UPDATE_REQUIRE_CI=0. Uses the public API: no token, 60 calls an hour are plenty.
  local require; require="$(env_value MIH_UPDATE_REQUIRE_CI)"
  [[ "${require:-1}" == 0 ]] && { echo success; return; }
  local slug json
  slug="$(env_value MIH_UPDATE_REPO)"
  [[ -n "$slug" ]] || slug="$(git -C "$REPO" remote get-url origin \
    | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')"
  [[ "$slug" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || { echo failed; return; }
  json="$(curl -fsS -H 'Accept: application/vnd.github+json' \
    "${MIH_GITHUB_API:-https://api.github.com}/repos/$slug/commits/$1/check-runs?per_page=100")" \
    || { echo pending; return; }
  python3 -c '
import json, sys
runs = json.load(sys.stdin).get("check_runs", [])
if not runs or any(r["status"] != "completed" for r in runs):
    print("pending")
elif all(r["conclusion"] in ("success", "skipped", "neutral") for r in runs):
    print("success")
else:
    print("failed")
' <<<"$json"
}

cmd_update() {
  # Bring the stand to the newest commit of MIH_UPDATE_BRANCH (default main), only once
  # GitHub CI passed for it. Works after a history rewrite too (reset, not merge). If the new
  # version does not come up healthy, the previous image, database and commit come back and
  # the owner gets one push; that commit is then skipped until a newer one appears.
  local auto=0; [[ "${1:-}" == --auto ]] && auto=1
  mkdir -p "$STATE"
  exec 9>"$STATE/update.lock"
  flock -n 9 || { say "another update is running"; return 0; }
  local branch current target
  branch="$(env_value MIH_UPDATE_BRANCH)"; branch="${branch:-main}"
  [[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]] \
    || die "the stand's clone has local changes to tracked files; update refuses to overwrite them (see git -C $REPO status)"
  git -C "$REPO" fetch --quiet origin "$branch"
  current="$(git -C "$REPO" rev-parse HEAD)"
  target="$(git -C "$REPO" rev-parse "origin/$branch")"
  if [[ "$current" == "$target" ]]; then say "up to date: ${current:0:12}"; return 0; fi
  if (( auto )) && [[ "$(cat "$STATE/bad_commit" 2>/dev/null || true)" == "$target" ]]; then
    say "skipping ${target:0:12}: it failed before; waiting for a newer commit"; return 0
  fi
  case "$(ci_state "$target")" in
    success) ;;
    pending) say "CI for ${target:0:12} has not finished yet; try later"; return 0 ;;
    *)
      say "CI failed for ${target:0:12}; not deploying it"
      if (( auto )) && [[ "$(cat "$STATE/bad_commit" 2>/dev/null || true)" != "$target" ]]; then
        printf '%s\n' "$target" >"$STATE/bad_commit"
        notify_owner "Стенд не обновлён" "Проверки GitHub не прошли для ${target:0:12}. Стенд остаётся на ${current:0:12}."
      fi
      return 0 ;;
  esac
  say "== update ${current:0:12} -> ${target:0:12}"
  printf '%s\n' "$current" >"$STATE/previous_commit"
  local image_before; image_before="$(cat "$STATE/current_image" 2>/dev/null || true)"
  git -C "$REPO" reset --quiet --hard "$target"
  # Run the new script: the update may have changed mihctl.sh itself.
  if "${MIH_UPDATE_DEPLOY:-$HERE/mihctl.sh}" deploy; then
    rm -f "$STATE/bad_commit"
    printf '%s update %s\n' "$(date -u +%FT%TZ)" "${target:0:12}" >>"$STATE/history"
    say "updated to ${target:0:12}"
    return 0
  fi
  say "== the new version did not start; going back to ${current:0:12}"
  git -C "$REPO" reset --quiet --hard "$current"
  printf '%s\n' "$target" >"$STATE/bad_commit"
  # Roll back only if the new image was started; a failed build left the old one running.
  if [[ "$(cat "$STATE/current_image" 2>/dev/null || true)" != "$image_before" && -s "$STATE/previous_image" ]]; then
    "${MIH_UPDATE_ROLLBACK:-$HERE/mihctl.sh}" rollback || true
  fi
  notify_owner "Стенд: обновление откатилось" "Версия ${target:0:12} не запустилась, стенд вернулся на ${current:0:12}. Подробности: deploy/vps-test/state/update.log" error
  return 1
}

AUTOUPDATE_MARK="# mirtvonline-test autoupdate"

cmd_autoupdate() {
  # A line in this user's crontab runs `update --auto` (default: every hour at :17).
  # Only that line is added or removed; the rest of the crontab is left as it is.
  command -v crontab >/dev/null || die "crontab not found on this host"
  local schedule line rest
  schedule="$(env_value MIH_UPDATE_SCHEDULE)"; schedule="${schedule:-17 * * * *}"
  line="$schedule cd $(printf '%q' "$HERE") && ./mihctl.sh update --auto >>state/update.log 2>&1 && tail -n 2000 state/update.log >state/update.log.tmp && mv state/update.log.tmp state/update.log $AUTOUPDATE_MARK"
  rest="$(crontab -l 2>/dev/null | grep -vF "$AUTOUPDATE_MARK" || true)"
  case "${1:-status}" in
    on)
      mkdir -p "$STATE"
      printf '%s\n%s\n' "$rest" "$line" | sed '/^$/d' | crontab -
      say "autoupdate on: $schedule (log: deploy/vps-test/state/update.log)" ;;
    off)
      if [[ -n "$rest" ]]; then printf '%s\n' "$rest" | crontab -; else crontab -r 2>/dev/null || true; fi
      say "autoupdate off" ;;
    status)
      if crontab -l 2>/dev/null | grep -qF "$AUTOUPDATE_MARK"; then
        say "autoupdate on: $(crontab -l | grep -F "$AUTOUPDATE_MARK" | cut -d' ' -f1-5)"
        tail -n 5 "$STATE/update.log" 2>/dev/null || true
      else
        say "autoupdate off"
      fi ;;
    *) die "usage: ./mihctl.sh autoupdate on|off|status" ;;
  esac
}

cmd_status() {
  compose ps
  local port addr token
  port="$(env_value MIH_PUBLISH_PORT)"; port="${port:-18765}"
  addr="$(env_value MIH_PUBLISH_ADDR)"; addr="${addr:-127.0.0.1}"
  token="$(env_value MIH_API_TOKEN)"
  say "== live";  curl -fsS "http://$addr:$port/api/v1/health/live"; echo
  say "== ready"; curl -fsS -H "Authorization: Bearer $token" "http://$addr:$port/api/v1/health/ready"; echo
  local mcp_port mcp_token
  mcp_port="$(env_value MIH_MCP_PUBLISH_PORT)"; mcp_port="${mcp_port:-18766}"
  mcp_token="$(env_value MIH_MCP_TOKEN)"; mcp_token="${mcp_token:-$token}"
  say "== mcp"
  curl -fsS -H "Authorization: Bearer $mcp_token" -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"status","arguments":{}}}' \
    "http://127.0.0.1:$mcp_port/mcp"; echo
  say "== stats"; docker exec "$CONTAINER" mih stats
}

cmd_public() {
  # Open the web UI to the internet through Tailscale Funnel (docs/public.md): HTTPS with a
  # certificate, no port opened on the server's public address, the VPS address stays hidden.
  # Changes only this node's Funnel entry on MIH_FUNNEL_PORT; refuses a port that already
  # serves anything else.
  command -v tailscale >/dev/null || die "tailscale CLI not found"
  local fport proxy_port target serve
  fport="$(env_value MIH_FUNNEL_PORT)"; fport="${fport:-443}"
  proxy_port="$(env_value MIH_PROXY_PORT)"; proxy_port="${proxy_port:-18767}"
  target="http://127.0.0.1:$proxy_port"
  serve="$(tailscale serve status --json 2>/dev/null || echo '{}')"
  case "${1:-status}" in
    on)
      curl -fsS "$target/api/v1/health/live" >/dev/null \
        || die "nothing answers on $target: run ./mihctl.sh deploy first"
      if grep -Eq "\"$fport\"|:$fport\"" <<<"$serve" && ! grep -q "$target" <<<"$serve"; then
        die "port $fport already serves something else on this node; set MIH_FUNNEL_PORT=8443 or 10000"
      fi
      tailscale funnel --bg --https="$fport" "$target" \
        || die "tailscale refused; see docs/public.md (sudo tailscale set --operator=\$USER, Funnel in the tailnet policy)"
      tailscale funnel status
      say "next: put the https:// address above into MIH_PUBLIC_URL in .env and run ./mihctl.sh deploy"
      ;;
    off)
      grep -q "$target" <<<"$serve" || { say "not public"; return 0; }
      tailscale funnel --https="$fport" off
      say "closed; clear MIH_PUBLIC_URL in .env and run ./mihctl.sh deploy"
      ;;
    status)
      if grep -q "$target" <<<"$serve"; then tailscale funnel status; else say "not public"; fi
      ;;
    *) die "usage: ./mihctl.sh public on|off|status" ;;
  esac
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
  update) shift; cmd_update "${1:-}" ;;
  autoupdate) shift; cmd_autoupdate "${1:-status}" ;;
  logs) shift; compose logs --tail "${1:-200}" mih ;;
  backup) cmd_backup ;;
  restore) shift; cmd_restore "${1:-}" ;;
  rollback) cmd_rollback ;;
  public) shift; cmd_public "${1:-status}" ;;
  uninstall) shift; cmd_uninstall "${1:-}" ;;
  *) say "usage: ./mihctl.sh preflight|deploy|update|autoupdate on|off|status|status|logs [n]|backup|restore [file]|rollback|public on|off|status|uninstall --yes"; exit 1 ;;
esac
