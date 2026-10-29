#!/bin/sh
# Runs radiusd in the foreground and restarts it when the NAS clients file
# rendered by the backend changes. A changed file is config-checked first;
# a file that fails the check is ignored (the running server keeps serving)
# rather than taking authentication down.
set -eu

CLIENTS=/etc/freeradius-clients/nas.conf
BIN=$(command -v freeradius || command -v radiusd)

if [ ! -f "$CLIENTS" ]; then
	mkdir -p "$(dirname "$CLIENTS")" 2>/dev/null || true
	printf '# no NAS registered yet\n' > "$CLIENTS" 2>/dev/null || {
		echo "clients file missing and volume not writable: $CLIENTS" >&2
		exit 1
	}
fi

checksum() { md5sum "$CLIENTS" 2>/dev/null | cut -d' ' -f1; }

# Debug mode passthrough: `docker compose run freeradius -X`
if [ "${1:-}" = "-X" ]; then
	exec "$BIN" -X
fi

while true; do
	last=$(checksum)
	"$BIN" -f -l stdout &
	pid=$!
	while kill -0 "$pid" 2>/dev/null; do
		sleep 5
		now=$(checksum)
		if [ "$now" != "$last" ]; then
			if "$BIN" -C >/dev/null 2>&1; then
				echo "NAS clients changed; restarting radiusd"
				kill -TERM "$pid" 2>/dev/null || true
				wait "$pid" 2>/dev/null || true
				break
			fi
			echo "NAS clients file fails config check; keeping current server" >&2
			last=$now
		fi
	done
	wait "$pid" 2>/dev/null || true
	sleep 1
done
