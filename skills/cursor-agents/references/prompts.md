# Prompt templates for Cursor agents

All values in `<angle brackets>` are placeholders. Keep the structure: the agent starts cold and
only knows what the prompt says. Every template ends with a fixed final-answer format because
the final answer is the only result channel the coordinator can always read (`cursor_get_run`).

Rules shared by all templates, and why:
- Name the exact branch to push to, and forbid every other one: an agent left to choose may push
  to the base branch.
- Forbid reading or printing credentials: environment variables given at creation are visible to
  the agent and anything it prints lands in the run's stream.
- Bound every wait: a run blocked for hours in one `await` tends to end in `ERROR`.
- Start commands append to the log (`>>`): `>` empties it before the job's lock is checked, so a
  refused double start would still wipe the running job's log and its RESULT line.
- Never let a prompt kill a computation that is still running unless that is the explicit goal.

## 1. Worker launch (new agent)

```
Long job for this machine: worker <K> of the <tag> fleet. Do not read or print any credential.
1. Record the machine: nproc; lscpu | grep -E 'Model name|Flags' | cut -c1-300; free -g.
   <If the job needs a CPU feature: if lscpu lacks <flag>, stop here and say so in the final answer.>
2. cd /workspace && git fetch origin <code-branch> <control-branch>
   && git checkout -f <commit> -- <paths>
3. Start: cd /workspace && nohup sh <worker-script> <K> >> ~/worker-<K>.log 2>&1 &
   The worker reads <control-branch>, builds on this machine, works in chunks on all cores and
   pushes each chunk and STATUS.txt to <worker-branch>. It refuses to start twice.
4. Do not end your turn while the job runs. No single wait or await longer than 30 minutes.
   Every 15 minutes:
   - check the job is alive: kill -0 $(cat <pid-file>);
   - check that ~/worker-<K>.log and STATUS.txt advance.
   If the job died and its log has no RESULT line, rerun exactly the command of step 3.
   If a push failed, show the exact error, run git fetch origin, and push again once.
5. Push only to <worker-branch>. Never push to another branch, never open a PR.
Final answer: the RESULT line or the failure, the number of chunks pushed, the last log line,
any error found and what you did about it, git log --oneline -3 origin/<worker-branch>.
```

## 2. New job on the same agent (follow-up after a finished job)

The agent keeps its conversation and usually its machine: send only what changes.

```
Thank you, your previous job is complete. New job on the same machine (data and build may
already be there):
1. Update the scripts: cd /workspace && git fetch origin <code-branch>
   && git checkout FETCH_HEAD -- <paths>
2. Start: nohup sh <worker-script> <job-args> >> ~/job-<J>.log 2>&1 &
3. Same monitoring rules as before: no wait longer than 30 minutes; every 15 minutes check the
   job is alive and its log advances; on a push failure, show the error, fetch, push again once.
Final answer: the RESULT line or the failure, chunks pushed, git log --oneline -3 origin/<worker-branch>.
```

## 3. Resume, push and monitor

For a runner whose job may still run: a busy or silent run (send with `replace_active=true`) or
an ended run whose job is not done (plain follow-up). It never restarts a job that runs.

```
Your job <J> may still be running in the background on this machine. Do NOT kill it.
Do not read or print any credential.
1. Check it: pgrep -af '<job-pattern>'; tail -5 ~/job-<J>.log; <count of chunks done>.
2. Push what it produced (nothing new has reached <worker-branch> for <N> minutes):
   cd /workspace && git status -sb | head -2 && git push -u origin <worker-branch>.
   If it fails, show the exact error, run git fetch origin, and retry once.
3. Only if the job is NOT running and its log has no RESULT line: restart it with exactly
   <start-command> (it resumes from its last chunk).
4. Then monitor every 15 minutes, no single wait longer than 30 minutes, until a RESULT line;
   whenever a push failed or nothing new reached origin/<worker-branch> for 20 minutes, push as
   in step 2.
Final answer: chunks done, the RESULT line or the failure, git log --oneline -3 origin/<worker-branch>.
```

## 4. Coding agent on a feature branch

```
Task: <what to change and why>.
Work on branch <feature-branch> only; push there; do not merge and do not push to <base-branch>.
Invariants that must not change: <for example "outputs bit-identical on <test set>">.
Check with: <exact test and build commands>. Run them before you finish.
Do not read or print any credential.
Final answer: what changed (files, one line each), the exact test command and its last lines of
output, anything left undone or uncertain, the final commit SHA.
```

## 5. Benchmark on a given CPU

Use template 1, step 1 with the CPU check, then: give the exact build command with the same
compiler flags for the baseline and the new code (`-march=native` changes both, so decide it
explicitly), alternate baseline and new code on the same VM, run each measurement at least three
times, and report the CPU model, `nproc`, the load average before each measurement and every
individual timing, not only the mean. Compare ratios measured on one VM, not across VMs.
