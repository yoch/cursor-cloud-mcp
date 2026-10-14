# Coordinator-side pitfalls

Failures seen on the machine that runs the supervisor and the periodic checks, not on the
Cursor agents. Each row: what you see, why, what fixes it.

| symptom | cause | fix |
|---|---|---|
| `git fetch` fails in under a second after the coordinator session restarted | long-lived loops kept the old proxy settings (`HTTPS_PROXY` port) in their environment | kill them and restart them from a fresh shell; compare `HTTPS_PROXY` in `/proc/<pid>/environ` with the shell's |
| a restarted loop exits silently at once | the old loop's `sleep` child still holds the `flock` file descriptor | find the holder (`ls -l /proc/*/fd/<n>` on the lock file), kill it, restart |
| a later step fails on a 0-byte chunk | a collect interrupted mid-write left an empty file that the next collect treats as present | delete it and collect again; better, write to a temporary name then rename |
| a monitor never ends | `pgrep -f NAME` also matches the monitor's own command line | test for a result file, or `ps -eo pid,args \| grep "[N]AME"` |
| `pkill -f NAME` kills your own shell | same reason | use a pid file, or the bracket trick |
| local git grows without bound | every fetched chunk blob stays in the clone | drop the local worker refs, prune unreachable packs, fetch worker refs blobless (`--filter=blob:none`); everything stays on the remote |
| all workers show no progress on a new task for one chunk length | workers finish their current chunk before reading the control branch | wait one chunk length before concluding, then look at push ages |
| the supervisor relaunches workers you just removed | it was still running while its assignment file was edited | stop it first, edit, restart it only if it has other duties |
| a coordinator push is rejected on the base branch | a runner pushed there despite its prompt | `git pull --rebase` before every coordinator push; give runners disjoint directories so the rebase is clean |
| every supervisor call takes several seconds | one MCP server started per tool call | batch the calls over one session (`scripts/ccm.py --batch`) |

A periodic check (hourly, started by cron or a loop that survives the session) should:
restart the supervisor if it died, import finished work, print one status line per worker
(run status, push age), and run `cursor_get_usage` when the user wants usage tracked.
