# New pages autopost

A separate autoposting system for 60 Facebook pages. It is completely independent of INTERES WORLD:
its own repository, Meta business portfolio, system users, page tokens, WordPress site, queues and schedule.

Each release = a post with a photo on every page of a group + an article on the website + the article link
in the first comment. Releases run at 12:00, 15:00, 18:00, 21:00 and 00:00 (Yerevan).

## Page groups (one story per group in every release — 7 links per release)

| Group | Key | Pages |
|---|---|---|
| IT-1 | IT | 11 |
| IT-2 | IT2 | 11 |
| EN-1 | EN | 10 |
| EN-2 | EN2 | 10 |
| FR | FR | 11 |
| DE | DE | 4 |
| PT+ES | PT | 3 |

Pages are listed in `config/pages.yaml`. Links for each group go to `queue/<key>.txt`.

## Status

- Code copied from the main system; tests pass on a fixed test page set (`tests/fixture/config`).
- Schedule is **off** (`schedule_enabled: false`) until everything below is connected.

## Still to connect

1. Meta: new business portfolio, the 60 pages added, system users with "Content" and "Community activity"
   rights, a Meta app → secrets `FB_APP_ID`, `FB_APP_SECRET`, `FB_USER_TOKEN` (`_2`, `_3`).
2. Run workflow "2 · Подключить Facebook" → real page IDs replace the `pending-NN` placeholders.
3. WordPress site → `site.url` in `config/settings.yaml` + secret `WP_API_KEY` (Make Connector)
   or `WP_USER` + `WP_APP_PASSWORD`.
4. Claude → secret `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.
5. Private test, public test on one page, then turn the schedule on.

Secrets are entered by the owner only: Settings → Secrets and variables → Actions.
