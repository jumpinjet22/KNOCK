# Plan: a public daily game that crowd-reviews KNOCK training data

**Status:** Phase 0 is built (section 7). The game itself lives in
`jumpinjet22/Dorman-the-training-game`. This file is kept in KNOCK because
the export/import contract (section 7) is KNOCK's side of the deal. The
frontend is server-rendered HTML from the Rust API (askama + a little
vanilla JS), not a separate TypeScript app.

Working title: **"Who's At The Door?"** (rename freely).

---

## 1. Goal

Turn the Training page's review work (pick the right intent, pick the better
reply) into a free public web game, Wordle-style:

- One **daily set** of ~10 items, the same for every player.
- Many players answering the *same* items gives dense overlap, which yields a
  confident per-item consensus and a per-player reliability score.
- Reliability scores let us **pick out the trolls**: their votes are silently
  down-weighted until they stop mattering.
- Each day's consensus is exported, the disagreements are hand-reviewed, and
  the result is imported into KNOCK's existing `training_reviews.json` /
  DPO export.

## 2. Hard boundaries (not negotiable)

1. **Synthetic data only.** The game never sees a real `audit.jsonl` (real
   people at a real door). It only gets batches exported from a
   synthetic-only audit log (the `KNOCK_TRAINING_MODE` scratch machine).
   The export script refuses the default production audit-log path.
2. **One-way, offline pipeline.** KNOCK never accepts traffic from the game,
   and the game never calls KNOCK. Data moves as files that a human runs a
   script to move, in both directions.
3. **No live model in v1.** Scenarios and candidate replies are
   pre-generated overnight. Nothing on the internet can reach Ollama.
4. **Separate repo, separate language.** The game can't
   `import knock.anything`, even by accident.
5. **Players' free text never reaches training data unreviewed.** v1 has no
   free-text modes at all.

## 3. Architecture

```
 KNOCK scratch box (local, synthetic only)
   generate_scenarios.py / generate_training_data.py   (existing, overnight)
   export_game_batch.py  ──────── batch.json ────────┐       (new, section 7)
                                                      ▼
                                    ┌──────────────────────────────┐
  CDN ◀── daily/<date>.json ────────│ game-api  (Rust: Axum+sqlx)  │◀── votes ── browser
                                    │ Postgres                     │            (TS + Vite)
                                    └──────────────┬───────────────┘
                                                   │ nightly
                                    scorer (Python: Dawid-Skene, trust)
                                                   │
                                     consensus-<date>.json
                                                   ▼
 KNOCK: import_game_consensus.py → you review the disputed items → training_reviews.json
```

| Piece | Tech | Why |
|---|---|---|
| API | Rust, Axum, sqlx, tokio | Tiny, cheap, hard to knock over under abuse |
| DB | Postgres | Votes are relational; sqlx checks queries at compile time |
| Scorer | Python (numpy), nightly cron | Stats are easiest here; it sits next to KNOCK's existing scripts |
| Frontend | TypeScript, Svelte or React, Vite | A swipe/tap game; the daily set is static JSON |
| Bot gate | Cloudflare Turnstile | On session start and on vote submission |
| Hosting | Fly.io / Railway / a small VPS | One binary, one Postgres |

### Rust workspace layout (game repo)

```
crates/
  api/       # Axum routes, extractors, rate limiting, Turnstile check
  core/      # domain types: Item, Vote, Player, DailySet; no IO
  db/        # sqlx queries + migrations/
  ingest/    # CLI: load batch.json → items, build tomorrow's daily set
scorer/      # Python package, run by cron, reads/writes Postgres
web/         # frontend
```

## 4. Game design

### Modes (v1)

- **Who's At The Door?**: show a visitor line (plus the scene description,
  if there is one). The player picks an intent from 4 options: the
  consensus/generator label plus 3 plausible distractors from the intent
  list. Produces classification labels.
- **Would You Say That?**: show a visitor line and two candidate replies.
  The player picks the better one, or "both bad." Produces DPO pairs
  (chosen/rejected) for the existing DPO export.

### The daily set

- About 10 items: **6 new** (everyone answers them; this is the data),
  **2–3 gold** (known answer, indistinguishable from the rest), and
  **1–2 carry-overs** (yesterday's most disputed items, to firm them up).
- The crowd's vote split is shown **only after the player submits** each
  item. The full breakdown is revealed the next day. This blunts
  copy-the-crowd behavior and spoilers.
- The share card is a spoiler-free grid with a "streak" line, e.g.
  `🚪 #142  🟩🟩🟨🟩🟩🟩🟥🟩🟩🟩`. It's scored against gold plus
  end-of-day consensus, so a player's score settles the next morning.

### Overtime (volume)

After the daily set, an endless mode serves less-overlapped items from the
backlog. Each Overtime vote is weighted by the trust the player earned in
dailies. Dailies calibrate people; Overtime farms data.

### Progression

Streaks, ranks (Rookie → Doorman → Bouncer → Head of Security), and a weekly
leaderboard ranked by **accuracy against settled consensus**, not volume, so
grinding garbage doesn't pay.

## 5. Trust and consensus (picking out the trolls)

Every player has a trust weight `w ∈ [0, 1]`. The nightly scorer recomputes
it from everything that player has ever answered.

1. **Gold accuracy** (strongest signal early on): a beta posterior over
   correct/incorrect gold answers. A new account starts at a low prior, so a
   fresh account's vote is worth roughly 0.1 of a veteran's.
2. **Dawid-Skene** over all non-gold votes: jointly estimates each item's
   true label and each player's confusion matrix. A player who agrees with
   the weighted crowd on clear items gains trust; a player who's
   systematically off loses it.
3. **Behavior flags** (multiply `w` down rather than banning):
   - a median answer time below a floor (e.g. < 1.2 s per item)
   - the same answer position far above chance, or alternating patterns
   - vote bursts from many new accounts on the same items (brigading)
4. **Silent.** Players are never told their weight. Trolls keep playing
   and stop mattering.

An item **settles** when its top label has ≥ 0.8 weighted share **and**
total weight ≥ a minimum (tunable; start around 5.0). Items that don't
settle within 3 days are marked **disputed** and exported for human review.
Disputed items are often the most valuable data.

Brigade guard: a settled label that **disagrees with KNOCK's own judge**
(`judge_training_data.py` scores, included in the batch) is downgraded to
disputed rather than auto-accepted.

## 6. Data model (Postgres, first cut)

```sql
players     (id uuid pk, created_at, handle text null, email text null,
             trust real not null default 0.1, flags jsonb not null default '{}')
items       (id uuid pk, knock_key text not null unique,   -- KNOCK example_key
             mode text not null,                           -- 'intent' | 'pair'
             payload jsonb not null,                       -- text, scene, options/replies
             gold_answer text null, batch text not null, created_at)
daily_sets  (day date pk, item_ids uuid[] not null, published_at)
votes       (id bigserial pk, player_id uuid fk, item_id uuid fk, day date null,
             answer text not null, ms_to_answer int not null, created_at,
             unique (player_id, item_id))
consensus   (item_id uuid pk fk, label text null, share real, total_weight real,
             status text not null,                         -- open|settled|disputed
             updated_at)
```

Anonymous play gets a random `players` row behind a signed cookie. An
account (optional, email magic link) only adds cross-device streaks. Store
nothing else: no IP addresses in the database, and rate-limit state is kept
in memory.

## 7. KNOCK side: the export/import contract

Both scripts exist, with tests in `tests/test_game_batch_scripts.py`. Their
docstrings are the format spec (`knock-game-batch` v1 and
`knock-game-consensus` v1).

**`scripts/export_game_batch.py`**
- `--audit-log PATH` is required. It refuses
  `~/.local/share/knock/audit.jsonl`. "Marked synthetic" means the log has
  the `<stem>.manifest.jsonl` that `generate_training_data.py` writes, and
  only entries whose key is in that manifest are exported.
- Pair items are keyed `pair:<keyA>:<keyB>`. Their answer is one reply's key
  or `both_bad`. Entries with a judge voice veto, and replies a filter
  replaced, are left out.
- Emits `batch.json`: for each entry, `knock_key` (= `example_key()`),
  visitor text, scene description, generator intent, the candidate replies
  for pair items, and the judge scores. Gold items are taken from entries
  already `approved` in `training_reviews.json`.
- It contains no timestamps, no camera/door IDs, no names, nothing from
  real config.

**`scripts/import_game_consensus.py`**
- Reads `consensus-<date>.json` from the game.
- `settled` items: writes a `TrainingReview` (`approved`, with
  `intent_override` when the crowd disagreed with the generator) and a
  `comment` noting crowd share and weight.
- `disputed` items: left `pending` with a comment, so they show up on the
  Training page's existing needs-attention queue for you.
- A settled pair approves the winning reply. A `both_bad` verdict is
  handled like a dispute.
- It never overwrites a review a human already made, and it only writes
  keys that exist in the `--audit-log` it is given.

## 8. Public-facing obligations

- **Terms**: contributions are licensed to the project for model training.
- **Privacy policy**: anonymous ID, votes, and optionally an email. That's
  it.
- **Age gate**: 13+ (COPPA). If EU traffic is expected, GDPR basics:
  deletion on request, which is easy because so little is stored.
- **Content review**: every synthetic batch gets a human skim before
  publishing. Generated scenarios can be weird, and the game is public.

## 9. Phases

| Phase | Scope |
|---|---|
| 0 | KNOCK export/import scripts + tests; batch format frozen |
| 1 | Rust API + Postgres + ingest CLI; intent mode only; anonymous play; no frontend polish |
| 2 | Frontend, daily set, share card, Turnstile, deploy. **Soft launch to friends**, since gold-heavy dailies seed trust |
| 3 | Python scorer: gold posterior + Dawid-Skene + behavior flags; consensus export |
| 4 | "Would You Say That?" pair mode → DPO; Overtime; ranks/leaderboard |
| 5 (maybe) | "Don't Open The Door" red-team mode against a budget-capped hosted model; successful attacks feed `tests/safety_corpus.json` after human review |

## 10. Open questions

1. Name and domain.
2. Hosting budget ceiling. This decides whether Phase 5 is ever viable.
3. Daily reset time zone (UTC midnight is the simplest choice).
4. Who hand-reviews disputed items and new batches: just you, or trusted
   high-rank players later?
