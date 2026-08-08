# Retrospective: the 2026-08-07/08 seller-scan buying session and Gixen outage

Process retrospective on the Claude Code session in transcript
`~/.claude/projects/-Users-hsukenooi-Projects-comic-pipeline/e0954764-3eff-40bc-b93d-20ec341cb792.jsonl`
(1,774 events at review time). Written by a fresh session with read-only access; nothing live was
touched. Citations are `L<n>` = transcript line number; timestamps are UTC (the Mac Mini's server
logs run UTC+8, so "hours 05–09 local" = 21:00–01:00 UTC).

The session's technical conclusions — the `home_2.php` mid-body stall and the BUI-697
timeout-reports-committed-write-as-failed bug — were verified against the transcript and are
correct as established. This document is about the *process*.

---

## 1. Timeline

| UTC | Phase |
|---|---|
| 04:36–04:48 | `/comic:seller-scan` on 10 sellers → 229 matches (L7–L79) |
| 04:48–05:09 | Filtering, exposure math, collection check (re-done both with/without year after user push at L231), grading 7 lots (L82–L370) |
| 05:09–05:26 | FMV batch: all 202 books in one invocation → 157/202 breaker-affected, only 63 priced (L426–L449) |
| 05:26–05:52 | Diagnosis "quota exhausted, wait"; 62 snipes added ($2,705); first Gixen timeouts; cap-edit retries; ASM #35 vanish-and-readd; 0/62 linkage false alarm caught in 14s (L447–L591) |
| 05:52–09:50 | User away. **"I still have quota"** pushback on return (L595) |
| 09:50–10:01 | Diagnosis corrected to 60 req/min rate limit; re-run + chunked pacing → 100/202 priced; "zero 429s" misreport and correction (L597–L737) |
| 10:01–10:36 | Second add-batch: 36/36 report failed, 11 landed; retry lands 0; bounded retry loop started (L743–L788) |
| 10:36–10:38 | **`/compact` — compaction boundary at L790–L795** |
| 10:39–10:56 | User: "Investigate why gixen is down." Four-line-of-evidence diagnosis: `home_2.php` stalls mid-body, 12.95s vs 15s timeout; morning-window (05–09 local) recommendation (L812–L1029) |
| 10:57–11:20 | User: follow recommendation, price anchor-diverges at 10% over current, fix dashboard blanks, "use sub-agents." Two sub-agents spawned and abandoned; 36-row FMV link repair; BUI-697 filed; `auction_end_at` near-miss; 102-unpriceable analysis (L1032–L1379) |
| 11:21–11:27 | User: "Make ASM #61 $20." Operator edits the queue file only; reports "set to $20 … isn't live yet" (L1381–L1459) |
| 11:31–11:37 | User: "authorize for unattended." Morning job built; plist write and even a `plutil` lint blocked by classifier; staged for user install — install never happens (L1462–L1496) |
| 11:37–01:30 | User away ~14h. Overnight, the killed retry loops' timed-out writes commit on Gixen's side |
| 01:30–02:08 (08-08) | User: "Did it run?" → job never ran; **ASM #61 live at $1.09**; morning window disproved (curl-28s through hours 02–09 local, 16 at 06:00); persistent retry fixes caps ($20, $40); groups never form; duplicate removal; this retrospective spawned (L1498–L1774) |

End state at review: ~100 live PENDING snipes, ~$5,328 committed, one duplicate-removal retry loop
still running in the background.

---

## 2. What went right, and the practice behind it

**The Gixen diagnosis (L812–L1029) was genuinely excellent.** In 17 minutes it went from "is it
us?" to a measured mechanism: every failure `curl exit 28` (L868); login endpoint 0/1,358 failures
vs 322/328 timeouts on `home_2.php` (L944); a same-day *control* (the 13:30 batch at a faster
write rate succeeded 25/25, L1029); failures normalized as failures-per-login so its own volume
cancelled out; and a decisive experiment — a 90s-timeout probe showing HTTP 200 in 12.95s on a
399,838-byte page against the 15s client timeout (L920), then a stalled transfer delivering 250KB
of 400KB (L932–L935). The practice: **find a control case, normalize out your own behaviour, then
measure the mechanism directly instead of re-testing the symptom.**

**Live-state reconciliation on money writes.** Every retry re-read `/api/comics/snipes` and diffed
before writing (L766, L779), which is why zero double-adds occurred all session despite dozens of
retries into a flapping service. The same count-reconcile caught Gixen silently dropping ASM #35
after it reported `added` (L564: "62 intended, 61 live"). Practice: **the write report is not the
state; the re-read is.**

**The `auction_end_at` near-miss was process, not luck.** Mid-way into "let me check whether I can
backfill end times without Gixen, since I already have them" (L1324), the operator read the
writer and its consumers first, hit the BUI-417 invariant comment (L1333), then read
`get_bids_ready_to_snipe` (L1338) and stopped: writing 25 end dates would have armed the local
sniper for 25 auctions (L1379). Two layers of process manufactured this catch: a past investment
(the invariant is *documented at the write site*) and a present habit (read the writer before
writing a column — the BUI-626 lesson generalized). It was then recorded to memory (L1343).

**Proactive risk surfacing.** The operator flagged, unprompted: `POLICY_EXPOSURE_CEILING` unset
(L591), the Invincible #19 12.5× anchor divergence (L497), and — critically — that ASM #61 at
$1.09 "cannot win a book whose own comps sit at $38–80" (L1213). The user's "$20" instruction
exists only because the operator flagged its own literal-but-absurd implementation of "10% over
current." Practice: **execute as instructed, but say when the instruction produces nonsense.**

**Knowledge capture with dedupe.** BUI-697 was filed only after searching Linear and
`docs/solutions/` for prior art (L1117–L1132), and it names the exact doc line it falsifies
(`add-batch-row-status-contract.md:58`, L1136). Two memory files written. The catch-before-report
instances (0/62 at L574, the zero-comps misread at L1287, the `cap>10× current` heuristic
self-audit at L727, "Zero hits is suspicious" at L263) all share one shape: **when a probe returns
an alarming or surprising number, verify the probe before believing the number.** That habit,
applied asymmetrically, is also the root of several findings below.

---

## 3. Findings

Ranked by cost. Each: evidence → the reasoning pattern → the earlier available cue → a rule that
would have prevented it.

### F1. The $1.09 money error — editing under an indeterminate write (highest cost class: real money)

**What happened.** At 11:02 the operator queued ASM #61 at $1.09 per the user's "10% over the
current" (L1078–L1083). Five add attempts (11:03 direct, then retry-loop attempts 1–4, L1084,
L1104–L1224) all reported "Server timed out." At 11:21 the user said "Make ASM #61 $20" (L1381).
The operator's entire execution was a local file edit: `if r['item_id']=='147483139204':
r['max_bid']=20.0` (L1387), then reported **"ASM #61 set to $20. Note it isn't live yet"**
(L1459). No $20 write was ever submitted that night. Overnight the timed-out $1.09 attempts
committed on Gixen's side — next morning: "ASM #61 landed at $1.09, not $20. The adds that finally
committed were the ones queued *before* you gave me $20 — I updated the file afterwards, so the
in-flight write carried the old cap" (L1513). Invincible #35 ($40), added to the queue file after
the retries stopped, was likewise never submitted (L1513).

**The reasoning pattern.** "NOT PRESENT" from a live re-read (L1244) was treated as *stable
absence* — but under BUI-697 semantics, which the operator itself had articulated, a timed-out
write is *indeterminate in the positive direction*: it may still appear. The operator applied its
own lesson to retries (never double-add) but not to *edits* (a pending old-value write can
outrace a file change that nothing ever submits).

**The earlier cue — overwhelming.** By 05:41: "2 came back `updated` — meaning the first attempt
had actually written before timing out" (L531). By 10:19, in bold, to the user: "`add-batch`
reported 36/36 failed when 11 had committed … the only trustworthy source is a live re-read"
(L785). At 10:52: "the writes were committing all along while the responses timed out" (L984).
BUI-697 was filed at 11:07 — **16 minutes before the $20 instruction** (L1141). The compaction
summary preserved all of this (L795 §Errors), so context loss is not an excuse.

**Rule.** *A timed-out money write is live until reconciled.* When the target value of a pending
write changes: (1) submit the new value through the same idempotent upsert path immediately —
even into a flapping service, since the upsert is what corrects a late-landing stale write; (2)
tell the user the old value may still surface; (3) schedule/perform a reconcile check before
declaring the change done. Editing the queue file alone is none of these.

### F2. The morning-window inference — sound data, overconfident causal claim, durable artifacts written before disconfirmation

**What happened.** From the hour-of-day histogram (L936–L937: failures/login 0.00–0.02 at hours
05–09 local vs 0.41–0.72 at 17–22), the operator concluded: "The evidence says this is a **clock
problem, not a load problem**, so the fix is to reschedule by time of day" (L1029). Within 40
minutes that claim was written at law-strength into the morning-gixen.sh header ("It is a
time-of-day problem, not a rate problem", L1100), the plist comment (L1483), and long-term memory
(L956: "RESCHEDULE BULK WRITES BY CLOCK (mornings), never by backing off"). The next morning,
hours 02–09 local *all* failed — 18, 20, 10, 10, **16**, 10 curl-28s (L1515) — and the operator
said so plainly: "My morning-window recommendation was wrong … sustained degradation, not a
diurnal dip" (L1543), "Today disproved the premise I built it on" (L1601).

**The reasoning pattern.** A correct *observation* (historical evening clustering) was promoted to
a *causal law* without checking the confound: the histogram pools all days, and the per-day table
(L873) shows curl-28s concentrated on a handful of incident days. A few evening-onset outages
make evenings look cursed without any diurnal mechanism. The operator even held the disconfirming
seed — "Failures began during a 2-hour idle period" at 15:44 (L1029 point 2), which fits
"an outage started mid-afternoon," not "evenings are bad."

**The earlier cue.** Its own per-day breakdown (L873) invited the within-day cross-check ("does
the hour pattern hold across independent days, or is it driven by 2–3 incident days?") — one
query it never ran. Also, hedged operational design *was* present (the script retried until 09:30
and self-retired), so the failure is specifically epistemic: the *framing* and the *memory write*.

**The residue — corrected late, and only under retrospective pressure.** The "I was wrong" turn
(01:31–01:34, L1518/L1543) did *not* carry the correction into the durable stores: no memory edit
occurred between 01:31 and this retrospective's spawn at 02:08 (transcript-verified), and at
02:08 the memory file still said "reschedule it by clock. Run bulk Gixen writes in hours 05–09
local." The correction finally landed at 02:22 — fourteen hours after the disconfirming data,
after the user commissioned this retrospective. Good that it happened; the gap is the finding:
for ~14 hours any session recalling that memory would have inherited a claim its author had
declared wrong. The offered 15s-timeout ticket ("Say the word and I'll write it up", L1601)
remained unfiled until this review; it is now BUI-699.

**Rule.** *A causal claim born during an incident is provisional until it survives one
disconfirmation opportunity.* Phrase it as correlation ("historically clean 05–09; tonight is a
new regime — schedule the retry AND keep a fallback"), and never write it to memory/scripts as
mechanism until it has predicted something once. When a recommendation is disproven, the same
turn that says "I was wrong" must also correct every durable artifact carrying the claim.

### F3. "Quota exhausted, wait" — a named diagnosis never checked against the provider (cost: ~4 idle hours + wrong plan, corrected only by the user)

**What happened.** After the 202-book FMV batch collapsed, the operator diagnosed: "202 books was
too large for the available quota … the 78 error rows can be re-run once the provider quota
recovers … a re-run tomorrow still makes them" (L497, L591). The user returned and pushed back:
"Check the policy for sold-comps. **I still have quota.** Then re-run the FMV for the 78 error
rows now" (L595). One grep and one probe later: "HTTP 200 in 3.9s — key works … **this was never
quota exhaustion.** sold-comps.com enforces a 60 requests/minute rate limit, not a daily cap …
my 'wait for quota to recover' advice was wrong — waiting wasn't the fix, and you were right to
push" (L648–L659). Chunked pacing then priced 36/36 with zero 429s (L700): "pacing was the fix,
not quota" (L703).

**The reasoning pattern.** The error text said `429 Too Many Requests` (L443) — the
characteristically *rate*-shaped status — but the diagnosis reached for the "budget/quota" frame
supplied by prior memory (BUI-565/570's "provider request budget") and by the contract's warning
about batch size. The distinguishing test (a single probe, free, 4 seconds) was available at
05:26 and not run until the user demanded it at 09:50.

**The earlier cue.** The 429 status itself, plus the source file header the operator later
grepped in 30 seconds: "sold-comps.com rate-limits at 60 req/min" was *commented in the repo's
own code* (compaction summary §3, L795). The cost of checking was two tool calls.

**Rule.** *Before recommending "wait," name the resource and verify its reset semantics.* A
diagnosis that prescribes waiting must state what recovers, when, and how you confirmed it —
otherwise run the one-request probe first. (Also credit: once corrected, the operator's
correction was exemplary — explicit, quantified, and it updated the batch-size contract caveat,
L737.)

### F4. "Re-running comic-fmv would change nothing" — a correct mechanism overextended into a flat universal (cost: nearly left a winnable lot unbid)

**What happened.** At 11:16 the operator stated: "**Re-running `comic-fmv` would change nothing.**
The window is already at the ±2.0 ceiling for 88 of 96 … This is a data-shape limit, not a
transient failure" (L1296; earlier flat versions at L737 and L785: "no re-run fixes them"). Ten
minutes later, prompted by the user's questions, it re-ran just the 5 zero-comp lots with
`--force` and reported: "This **corrects my earlier blanket 're-running won't help'** … there's a
documented class (BUI-565/570) where a per-book crash surfaces as a clean n=0" (L1459).
Invincible #35 went from 0 comps to priced $35–50, cap $40 — and won a place in the queue; #115
went 0→6 comps, #11 0→1 (L1459 table).

**The reasoning pattern.** The mechanism was genuinely verified for the 80 flagged rows
(window at ceiling, ungraded comps carry no grade to widen into — L1296). The claim was then
stated over all 102, including the 16 `n=0` rows whose failure mode was *different* — and for
which a *known, documented* alternate cause existed.

**The earlier cue.** The operator's own memory index, loaded every session, says: "a THIRD class —
a per-book crash that surfaced as a clean `n=0`" (`project_fmv_silent_fetch_failure`). The claim
"the 7-day cache would mostly re-serve the same responses" (L1296) was an argument against
re-running *without* `--force`, not against re-running.

**Rule.** *Scope negative claims to the mechanism you verified.* "A re-run won't help the 80
flagged rows (window at ceiling); the 16 n=0 rows are a different failure mode with a documented
crash class — testing 5 of them costs one paced invocation" was available at the same evidence
level. Flat universals about what *won't* work need the same per-class verification as claims
about what will.

### F5. Failed delegation — two sub-agents spawned, raced, and abandoned (cost: ~16 min + coordination noise; zero output)

**What happened.** Following the user's "Use sub-agents to work on them in parallel if that helps"
(L1035), the operator spawned `fmv-analyst` (11:00:32, L1051) and `dashboard-diagnostician`
(11:00:57, L1053) as background teammates. Both went idle without reporting (11:08:30 and
11:11:59, L1215/L1248); the operator sent each a "Please send me your full findings report now"
(L1220, L1251); both went idle *again* without reporting (L1298). Verdict at L1296: "Both
sub-agents went idle without returning substantive reports, so I verified this directly against
the source instead." The operator's own later assessment: "both sub-agents I spawned today went
idle twice without ever delivering a report … that's a real reliability concern" (L1644).

**What was wrong with the setup.** Three things, in order of importance:

1. **The operator raced its own delegation.** The diagnostician's deliverable was "a diagnosis
   plus an exact, verified repair PLAN that I will execute myself" (L1053) — yet the operator
   root-caused and executed that exact repair itself by 11:09 (L1177), eight minutes after the
   spawn, then messaged the agent asking it to "confirm or CORRECT what I did" (L1220). Same for
   the FMV analysis: the operator did the threshold verification itself at 11:14–11:15 (L1257–
   L1292) while the analyst idled. The delegation was never load-bearing, so nothing was lost
   when it failed — but nothing could ever have been gained either. Parallelism without a
   division of labour is decoration.
2. **No return contract.** The spawn prompts specify the analysis precisely (read-only scope,
   data paths, leads) but the transcript shows no agreed completion signal; when the agents went
   `idle … available` — which in this teammate setup means "finished a turn," not "delivered" —
   there was nothing to collect and no way to distinguish "done, report lost" from "waiting for
   input."
3. **No monitoring between spawn and idle.** First contact with either agent was after its idle
   notification; there was no intermediate check that they were producing anything.

**Rule.** Delegate only what you will *not* also do yourself, define the return artifact ("write
findings to `<path>`; message me when written"), and check one intermediate output early. If you
find yourself re-deriving the delegated work in parallel, kill the delegation explicitly.

### F6. Retries into a known-stalling endpoint — high volume, oscillating policy, but cheaper than it looks (cost: attention/noise more than wall-clock)

**The count.** Client-side Gixen write invocations *after* the stall was first visible (10:10,
L745) — reconstructed from loop logs, conservative: the 25-row retry (L766, landed 0), the
single-add probe (L774), retry_loop.sh attempts 1–3 before it was killed (L779–L913), 2×
`gixen group` (L1012, L1022), the anchor add (L1083), retry-loop attempts 1–4 (L1104–L1224),
and next morning: fix_rows add (L1524), loop tries 1–4 (L1539–L1553), group-via-upsert add
(L1577), `gixen remove` (L1616), plus two removal loops (L1634, L1736) — **≈20 invocations
covering ~90 row-writes**, essentially all reporting "Server timed out." Add the pre-diagnosis
cap-edit storm (24 edits + 11×3 retries, 05:41–05:49, L532–L550). Wall-clock inside dedicated
retry windows: roughly 10:10–11:13 and 01:33–02:08 interleaved with other work.

**The nuance the count hides.** The retries were idempotent and reconciled, so they cost no money
and no duplicates — and the *add-path* retries ultimately worked: "persistent bounded retry …
is what actually landed everything today" (L1543). The genuinely futile subset was everything
needing the snipe-table read: `gixen group` failed 100% all session (groups 1 and 5 never formed,
L1601), including the clever group-via-upsert attempt (L1577–L1582).

**The real finding is the oscillation.** The operator declared "stop hammering" and recommended
the morning window at 10:56 (L1029); the user said "Follow your recommendation" (L1032); at
11:04 it repeated "Per my own recommendation, I'll stop hammering and automate the morning
window" (L1092) — then at 11:06, after the classifier block, started a 6-attempt in-session
retry loop anyway (L1104), and at 11:12 killed it citing the same evidence that existed at 11:04
("hour 19 runs 0.59 failures/login … more retries are just burning logins", L1235). Back off /
retry / back off, each justified locally, no stable policy. Next morning the policy flipped
again — correctly, but by trial rather than by decision.

**Rule.** When you announce a retry policy, bind it: state the condition under which you will
retry again *before* the next attempt, and let a state change (not restlessness) trigger the
change. One probe per window to detect recovery is measurement; repeated batches into a known
stall are not.

### F7. The false "zero 429s" — absence measured in the wrong stream, twice stated before checking the right one (cost: minutes; corrected well)

**What happened.** Mid-re-run: `grep -c "429" $SP/fmv2.err` → "Zero 429s so far" (L660–L666).
After completion, on the same evidence: "Zero 429s — so rate limiting wasn't the binding
constraint this time" (L676). Ninety seconds later, after actually reading the per-query JSON:
"Correction: there **were** 429s — 33 of them. They go to the JSON trail, not stderr, so my
`grep` on stderr missed them" (L681), corrected explicitly to the user at L692.

**The reasoning pattern and the cue.** A zero from a probe was read as a property of the world,
not of the probe — and the operator *knew* the right stream: at 05:26 it had diagnosed the first
429 storm by reading `queries_used` in the JSON output (L442–L443), the very trail it now
ignored. Contrast with the 0/62 handling minutes-equivalent earlier (F8): there, the alarming
number triggered probe-verification; here, the reassuring number shipped unverified. Skepticism
was proportional to alarm, not to probe weakness.

**Rule.** *A claim of absence must name where presence would have appeared* — and that channel
must be one where you have previously seen the thing present. Apply the same verification to good
news as to bad.

### F8. The "0/62 linkage failure" — verified against the transcript: caught *before* being asserted (near-zero cost; a model to reuse)

The brief lists this as "reported a total failure that hadn't happened." The transcript is
gentler: the query returned `linked (bids -> fmv): 0 / 62` (L571), and the operator's next visible
text was "0/62 would be a total linkage failure — **but let me verify my query is reading the
right fields before believing it**" (L574, 05:50:52). Fourteen seconds later: "My check was wrong
— the endpoint exposes `needs_linking` and `fmv_low`/`fmv_high`, not `fmv_id`. The linkage is
there" (L583), confirmed with `needs_linking: 0` (L585). No failure was ever asserted as fact.
This is the *correct* handling of an alarming probe result and the standard F7 should have met.
(The user did see the intermediate "0/62 would be…" line — hedged claims mid-verification are
fine; unhedged ones are the problem.)

### F9. Blocked automation — handled correctly at each step, but the loop was never closed (cost: contributed to F1's overnight window)

**What happened.** Attempt 1 (11:05, pre-authorization): writing `morning-gixen.sh` was blocked;
the operator accepted it — "that's a fair guardrail, so I won't work around it" (L1103) — and
surfaced it to the user twice (L1213, L1246). The user then said "authorize for unattended"
(L1462). Attempt 2: the script wrote successfully, but the plist into `~/Library/LaunchAgents/`
was blocked (L1484), and then even a read-only `plutil -lint` was blocked (L1494). The operator
stopped — "I'm stopping rather than hunting for a way around it" (L1496) — staged the plist, and
gave the user two copy-paste install commands with full safety-property documentation (L1496).

**Assessment.** Refusing to route around the classifier, both before *and after* user
authorization, was right: user authorization changes intent, not the harness's permission
boundary, and the classifier cannot see the authorization. Staging + handoff was the correct
mechanism. Two gaps: (1) **the loop was never closed** — the session ended with no confirmation
the user ran the install, no `AskUserQuestion`, and no morning follow-up scheduled; the user's
next-day "Did it run?" (L1498) shows they believed it was armed. A one-line "reply 'installed'
once you've run these, or I'll assume it's not scheduled" would have converted the ambiguity into
a signal. (2) Per F2, the job's *premise* was disproved the next morning, and the operator's own
post-mortem — "a clock-scheduled one-shot was the wrong shape … what actually worked was
persistent bounded retry" (L1543) — is the right verdict on the design: the correct unattended
artifact was a retry-until-reconciled loop with the same $500/idempotency guards, not a
calendar trigger. (Classifier over-breadth — blocking a read-only `plutil` lint — is a genuine
tooling gap; see §5c.)

### F10. The compaction boundary — noted, and largely exonerated

`/compact` fell at 10:36–10:38 (L790–L795), between starting the first retry loop and the Gixen
investigation. The summary (L795) is unusually good: it preserved the two facts later errors
would otherwise be blamed on — "Gixen 'Server timed out' does not mean the write failed …
Always reconcile against `/api/comics/snipes` before retrying" and the ASM #61 quarantine
($60 cap, anchor $38). **No post-compaction error in this session traces to compaction loss**;
F1 happened with the relevant lesson demonstrably inside the operator's context (it filed
BUI-697 on that lesson 16 minutes prior). The one compaction-adjacent stumble was trivial: the
operator briefly cited the wrong continuation-transcript ID when preparing this retrospective's
brief, and caught it (L1766). The honest conclusion is uncomfortable but useful: the $1.09 error
was not a memory failure — it was a failure to *apply* a held fact while multitasking across
six workstreams (retry loop, two idle agents, ticket, memory compaction, dashboard repair,
user Q&A) in the 11:00–11:30 window.

---

## 4. Patterns

Four behaviours generate most of the findings above.

### P1. Lessons were captured as narrative, not bound to actions

The timeout-is-indeterminate lesson was learned at 05:41 (L531), bolded to the user at 10:19
(L785), filed as BUI-697 at 11:07 (L1141) — and violated at 11:23 (F1). "Stop hammering" was
declared at 10:56 and 11:04 and violated at 11:06 (F6). The knowledge existed as *prose about
the world*; nothing turned it into a *precondition on the operator's own next action* ("before
changing a queued value, enumerate pending indeterminate writes"; "before the next retry, state
the trigger for retrying"). The repo's own philosophy says this exactly — `mechanized_by:` exists
because "a documented grep heuristic that stays prose is closed only by discipline" (CLAUDE.md).
The same is true one level up: an operator lesson that stays prose is closed only by discipline,
and discipline degrades precisely when it's needed — under multitasking (F1 happened at peak
workstream count) and under mode switches (incident → normal ops).

### P2. Skepticism scaled with alarm, not with probe weakness

Alarming numbers got the full treatment: 0/62 → verify the query first (L574); zero condition-
filter hits → "suspicious" → dig (L263); zero-comps bucket → "Correction before I report that"
(L1287). Reassuring numbers shipped: "zero 429s" from a stream the operator knew doesn't carry
429s (F7); "it isn't live yet" as if absence were stable (F1); "mornings are clean" as if a
pooled histogram were a mechanism (F2). The asymmetry is the tell: verification effort was
allocated by *how bad the number felt*, not by *how weak the measurement was*. The fix is a
symmetric rule — every claim of absence names the channel where presence would show, every
reassuring aggregate gets one disconfirmation probe — because the expensive errors of this
session all wore good news's clothing.

### P3. Verified mechanisms were promoted to laws at write-time

Three times, a correctly-established local mechanism was restated at universal strength within
minutes and committed to durable artifacts: the ±2.0-window mechanism (true for 80 rows) became
"re-running would change nothing" over 102 (F4); the evening-failure correlation became "a clock
problem, not a load problem" in a script header, a plist comment, and a memory file (F2); the
first-run breaker post-mortem became "wait for quota to recover" (F3). In all three, the
overreach was in the *scope of the sentence*, not the underlying analysis — and in two of three,
the disconfirming subclass or confound was already in the operator's own context (the n=0 crash
class in memory; the per-day incident table). Durable stores amplified the damage: the falsified
morning-window claim sat in memory for 14 hours after its author declared it wrong, corrected
only once a retrospective was commissioned (F2). Claims should enter scripts/memory/tickets at the strength of their
evidence — "observed", "holds for class X", "provisional until it predicts once" — and every
"I was wrong" must be chased into the artifacts that carry the original claim *in the same turn*:
here the memory correction arrived 14 hours after the disconfirmation, and only once a
retrospective had been commissioned (F2).

### P4. Parallelism without ownership boundaries

At the session's worst stretch (11:00–11:30) the operator ran: a background bid-retry loop, two
sub-agents whose tasks it was simultaneously doing itself (F5), a Linear filing, a memory-index
compaction, the dashboard repair, and a four-part user reply — and inside that window committed
its only real-money error (F1) and abandoned its only delegation (F5). Concurrency was treated
as free; each strand individually was managed well (bounded loops, idempotent writes), but
nothing owned the *interactions between strands* — which is exactly where F1 lived (a background
strand's stale write vs a foreground strand's file edit). When strands share mutable state
(a queue file that a loop reads), a change to that state needs a check of every strand that
touches it — or fewer strands.

---

## 5. Recommendations

### (a) Mechanizable — repo checks, docs, tickets

1. **Stamp the falsified doc.** `docs/solutions/conventions/add-batch-row-status-contract.md`
   still has no `status:` key (verified at review time) even though the session established its
   line 58 ("failed = did not land") is false and BUI-697's text says the stamp is included —
   the stamp is in the *ticket*, not on the *doc*, where BUI-608 requires it. Action: add
   `status: corrected` + `superseded_by: BUI-697 …` to the doc's frontmatter. One-line PR.
2. **Promote BUI-697 out of Someday and widen it one notch.** Beyond fixing the misreport, add an
   `INDETERMINATE` row status for client timeouts and make `add-batch` (or a new
   `gixen reconcile`) poll until each row settles, so "settled state" is a tool output instead of
   an operator discipline. This mechanizes F1's rule where it bit.
3. **New solutions doc** (BUI-605 contract: `mechanized_by: test`, the test being the
   reconcile-loop behaviour in #2): *"A timed-out Gixen write is indeterminate — reconcile before
   any dependent action, including editing the queue that produced it."* Evidence base: L531,
   L785, L984, L1513. This is a solutions doc, not just a ticket, because it's a *learning about
   a failure mode* that already bit twice in one session.
4. **File the 15s-timeout ticket** the operator offered and never filed (L1601): `GixenClient`'s
   15s timeout vs a 12.95s good-case fetch of a ~400KB/91-row page is a ~2s margin on a
   real-money path. Distinct from BUI-697 (value vs reporting). Verified absent from Linear at
   review time; **filed 2026-08-08 as BUI-699**.
5. **Memory correction — done during this review's window, keep the habit.** The operator
   corrected `project_gixen_flapping_not_rate_limited.md` at 02:22 (a "CORRECTION 2026-08-08"
   section demoting the clock rule and promoting persistent bounded reconcile-retry) — 14 hours
   after the disconfirmation and only after this retrospective was commissioned. No further
   action on the file; the process gap it exposes is (b)3 below. Memory has no BUI-608 stamping
   mechanism, which is itself worth adopting as a habit: date-stamp causal claims and mark
   incident-era entries provisional at write time.
6. **Ticket (smaller): morning-window artifacts cleanup** — `~/.comics-server/ops/morning-gixen.sh`
   still exists with "It is a time-of-day problem, not a rate problem" in its header and the
   user was told "Don't install the launchd job" (L1543); the script should be deleted or its
   header corrected so it is never cargo-culted later. (Not done by this retrospective:
   read-only constraint.)

> **Postscript (2026-08-08): follow-ups filed.** The mechanizable recommendations above are now
> tracked: **BUI-698** (`/api/comics/comps` 400s on every ledger advisory read — the defect the
> operator flagged three times and never filed; a gap in this retrospective's first draft too),
> **BUI-699** (the 15s `GixenClient` timeout, §(a)4), **BUI-700** (form bid groups without the
> `home_2.php` table fetch, §(c)4 — includes the open upsert-`group` question from L1577–L1582),
> **BUI-701** (enforce sold-comps 60 req/min pacing in code, mechanizing F3's manual workaround),
> **BUI-702** (retire `morning-gixen.sh`, §(a)6). **BUI-697** was promoted Someday → Soon with
> widened acceptance criteria covering §(a)1 (the doc stamp), §(a)2 (`INDETERMINATE` status +
> end-of-batch reconcile), and §(a)3 (the solutions doc at close-out).

### (b) Operator-behaviour rules

1. **Indeterminate-write fence** (from F1): after any timed-out money write, the value is *live*
   until a reconcile read settles it. Changing the intended value requires submitting the new
   value through the idempotent path and saying "the old value may still land" until reconciled.
2. **Absence names its channel** (from F7/F2): any "zero/none/clean" claim must state where the
   thing would have appeared and that you've seen it appear there before. Reassuring results get
   the same probe-verification alarming ones already get.
3. **Evidence-strength phrasing at write-time** (from F2/F3/F4): mechanisms verified for a class
   are stated *for that class*; incident-era causal claims are marked provisional in every
   durable artifact; "wait" recommendations must name the recovering resource and how its reset
   was confirmed.
4. **Close authorization loops** (from F9): when a safety handoff leaves the user holding the
   last step, get explicit confirmation ("installed"/"skipped") before treating the plan as armed
   — AskUserQuestion exists for exactly this.
5. **Delegation is exclusive or it is nothing** (from F5): a delegated task is one you stop doing
   yourself; define the return artifact and one early checkpoint; kill and reclaim explicitly
   when it fails.
6. **Retry policy is a decision, not a mood** (from F6): announce the trigger for the next
   attempt before making it; one probe per window measures recovery, repeated batches don't.

### (c) Tooling gaps

1. **Background teammate agents can go idle without delivering** — twice each here, even after a
   direct request (L1215–L1298). Idle-with-no-artifact should be surfaced as a distinct failure
   state, and spawn prompts need a file-based return contract until it is. (Harness-level; the
   operator's own Option-C warning at L1644 documents the reliability concern.)
2. **Classifier over-breadth on the scheduling path**: blocking the plist write is defensible;
   blocking a read-only `plutil -lint` of a scratchpad file (L1494) is not, and it pushed the
   operator to abandon even validation. Worth reporting upstream.
3. **No settle-state primitive in gixen-cli** (same as (a)2, listed here as the gap): every
   reconcile in this session was hand-rolled bash-around-curl; a first-class
   `gixen reconcile <rows.json>` returning settled/missing/mismatched would have made F1's rule
   one command.
4. **`gixen group`/`remove` require the stalling table read** — the whole class of "everything
   except add is down when `home_2.php` is down" (groups never formed all session, L1601). A
   server-mode group/remove path, or group-at-add-time via the `group` key made to actually
   commit (the L1577 attempt timed out), removes the single-point dependency.

---

## Appendix: session cost and steering

**Tool calls** (from the transcript): ~310 total — 270 Bash, 17 Read, 7 Write, 7 Edit, 4 Agent,
2 SendMessage, 1 each Skill/ToolSearch/AskUserQuestion; ~199 assistant messages. Active operator
time ≈ 3.7h across three windows (04:36–05:52, 09:50–11:37, 01:30–02:08) inside a 21.5h span.
Rework attributable to the operator's own errors (quota re-derivation, 429/0-62 corrections,
$1.09 repair, morning-job build-block-stage, delegation overhead) ≈ 40–50 calls; rework forced by
the environment (provider 429 storm, Gixen outage response) is several times larger and mostly
unavoidable — though F3 turned ~15 minutes of provider rework into 4 idle hours.

**Load-bearing user interventions** — three, each catching something the operator had wrongly
closed:

1. **"We'll need to do a collection check"** (L231, 05:02) — the first pass had keyed the check on
   `wish_name` rather than identified identity; the redo ran both with and without year and
   exposed the year-gate false-negative live (0 vs 3 owned, L342). Duplicate-buy risk, caught by
   the user.
2. **"I still have quota"** (L595, 09:50) — flipped F3's diagnosis; 63→100 lots priced within
   70 minutes of the push.
3. **The dashboard screenshot — "tons of entries missing information. Address all of these"**
   (L1035, 10:58) — drove the 36-row link repair, exposed the add-path linking bug inside
   BUI-697, and led (via the writer-audit) to the `auction_end_at` near-miss catch.

("Did it run?", L1498, also functioned as the reveal of F1/F9 — the operator checked because the
user asked.)

**Verification-discipline ledger** — reported-then-corrected: zero-429s (~90s to correction, F7),
quota/wait (~4.3h, user-forced, F3), "re-run changes nothing" (~10min, self-corrected after
testing, F4), "set to $20" (~14h, revealed by state, F1), morning window (~14h, revealed by
state, F2). Caught-before-asserting: 0/62 (F8), zero-comps misread (L1287), `cap>10×` heuristic
(L727), condition-filter zero hits (L263), snipes-are-really-on-Gixen check (L1424). The second
list is why this session, for all of the above, ended with correct caps, zero duplicate buys, and
zero unintended exposure.
