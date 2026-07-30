# contrib

Ready-to-adapt integration files. None of them are installed by the package —
copy the one that matches your system and edit the paths.

| Init system | Scheduling | Use |
|---|---|---|
| systemd | built-in timer | [`systemd/`](systemd/) |
| OpenRC (Gentoo, Alpine, Artix) | **cron** — OpenRC has no timers | [`openrc/`](openrc/) + [`cron/`](cron/) |
| runit, s6, SysV, BSD | cron | [`cron/`](cron/) |
| macOS | launchd | [`launchd/`](launchd/) |
| anything, no root | `crontab -e` | [`cron/`](cron/) |

## systemd

Templated: one instance per forge.

```bash
cp systemd/draupnir@.service systemd/draupnir@.timer /etc/systemd/system/
mkdir -p /etc/draupnir
cat > /etc/draupnir/git.example.org.conf <<'EOF'
FORGE_URL=https://git.example.org
OUTPUT=/srv/mirror/git.example.org
DRAUPNIR_ARGS=--mode mirror --jobs 8 --quiet
EOF
systemctl enable --now draupnir@git.example.org.timer
```

The unit ships with hardening on (`ProtectSystem=strict`, `NoNewPrivileges`,
`SystemCallFilter=@system-service`); widen `ReadWritePaths=` if your mirror
lives elsewhere.

## OpenRC

OpenRC has no timer mechanism, so it covers *running* the job and cron covers
*scheduling* it.

```bash
cp openrc/draupnir       /etc/init.d/draupnir
cp openrc/draupnir.confd /etc/conf.d/draupnir
chmod +x /etc/init.d/draupnir
$EDITOR /etc/conf.d/draupnir
```

```bash
rc-service draupnir start      # sync now
rc-service draupnir verify     # git fsck the whole mirror
rc-service draupnir report     # what is on disk
```

Then schedule it — either through the service:

```cron
# /etc/cron.d/draupnir
30 3 * * *  root  /sbin/rc-service draupnir start
```

…or skip the init script entirely and use the cron wrapper below, which is
self-contained.

Multiple forges: symlink the init script once per instance. Each symlink reads
its own `conf.d` file.

```bash
ln -s draupnir /etc/init.d/draupnir.example
$EDITOR /etc/conf.d/draupnir.example
```

## cron (portable)

`cron/draupnir-mirror` is POSIX `sh` and assumes nothing about your init
system. It adds log rotation, load splaying, a weekly `verify`, and exit-code
mapping so cron only mails you when something is genuinely wrong.

```bash
cp cron/draupnir-mirror /usr/local/sbin/
chmod +x /usr/local/sbin/draupnir-mirror
cat > /etc/default/draupnir <<'EOF'
FORGE_URL=https://git.example.org
OUTPUT=/srv/mirror
ARGS=--mode mirror --jobs 4 --quiet
VERIFY_ON=0
EOF
```

```cron
# /etc/cron.d/draupnir
30 3 * * *  mirror  /usr/local/sbin/draupnir-mirror
```

Or drop it in `/etc/cron.daily/` and let the distribution schedule it.

Unprivileged, no root at all:

```bash
crontab -e
# 30 3 * * * DRAUPNIR_TOKEN=... draupnir sync https://git.example.org -o ~/mirror --quiet
```

## macOS

```bash
cp launchd/org.franckferman.draupnir.plist ~/Library/LaunchAgents/
$EDITOR ~/Library/LaunchAgents/org.franckferman.draupnir.plist
launchctl load ~/Library/LaunchAgents/org.franckferman.draupnir.plist
```

## Notes for any scheduler

- **Overlapping runs are safe.** draupnir locks its output directory and a
  second run exits `2` rather than corrupting the mirror. The cron wrapper
  turns that collision into a silent no-op.
- **Exit codes**: `0` all good · `1` some repositories failed · `2` could not
  start · `3` mirror damaged (`verify`) · `130` interrupted.
- **Tokens**: prefer `--token-file` with a `0600` root-owned file over putting
  the secret in a config file or on the command line.
- **First run is the slow one.** Later runs skip untouched repositories, so a
  nightly job is usually a few seconds.
