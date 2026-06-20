# Security policy

## Scope and threat model

`reservoarr.py` is a per-channel-tune subprocess spawned by Dispatcharr. It:

- Reads two CLI args: the upstream URL and a User-Agent string (both come from Dispatcharr's Stream Profile substitution).
- Makes a single outbound HTTP GET against the upstream URL using `urllib.request`.
- Spawns `ffmpeg` as a child subprocess with a fixed argv (the upstream payload arrives via stdin, not as args).
- Writes a log file under `RESV_LOG_DIR`.
- Writes the remuxed TS stream to stdout.

It does **not**:

- Touch the Dispatcharr database, settings, or REST API.
- Read or write credentials.
- Make outbound network calls beyond the upstream URL Dispatcharr handed it.
- Listen on any port.
- Execute any code passed in via the URL or User-Agent.

The script runs with whatever privileges the Dispatcharr container has. There is no separate authentication surface.

## Known sharp edges

- **Log-tag derived from URL path.** `STREAM_ID` is built from the last path segment of the upstream URL (`split("?", 1)[0]` strips query strings to prevent embedded keys from leaking into log filenames). If a provider embeds secrets in a path segment rather than a query parameter, those would land in `delaybuf.log`. The cap (`[:48]`) limits exposure but doesn't redact. If your provider does this, set `RESV_LOG_DIR` to a directory not readable by other users.
- **ffmpeg argv is fixed.** The upstream URL and User-Agent are never interpolated into the ffmpeg command line — they go to `urllib.request` and to an HTTP header respectively. There is no shell-exec gateway from the URL or UA into ffmpeg.
- **No URL validation.** The script trusts the URL Dispatcharr provides. A misconfigured Stream Profile pointing at an internal address would be reachable from the container.

## Reporting a vulnerability

Use GitHub's private security advisory flow on the [reservoarr repo](https://github.com/brko7/reservoarr/security/advisories/new). Please include:

- A clear description of the issue and its impact.
- A minimal reproduction (input, env vars, Dispatcharr version).
- Whether the issue is exploitable without local container access.

Do not open a public issue for security reports.

## Versioning and disclosure

reservoarr does not currently maintain multiple support branches. Security fixes ship on the latest tagged release. After a private fix lands, the advisory is published with a CVE if applicable.
