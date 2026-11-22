# New pages autopost

A separate autoposting system, completely independent of INTERES WORLD: its own repository,
Meta business portfolio ("New Pages", created from the Anahit Markosyan profile), system users,
page tokens, WordPress site, queues and schedule.

Each release = a post with a photo on every page of a group + an article on the website + the article link
in the first comment. Releases run at 12:00, 15:00, 18:00, 21:00 and 00:00 (Yerevan).

## Pages (24) and groups — one story per group in every release, 3 links per release

| Group | Key | Pages |
|---|---|---|
| IT | IT | 7 |
| EN | EN | 10 |
| FR+PT | FR | 6 FR + 1 PT |

Since 23.11.2026 only Anahit Markosyan's pages from the owner's list are automated, plus
Ce que ressent le cœur and Des histoires qui restent. The other pages of the New Pages portfolio
are in the portfolio but not automated. Pages on other profiles are not connected yet.

Pages are listed in `config/pages.yaml`; links for each group go to `queue/<key>.txt`.

## Status

- Code copied from the main system; tests pass on a fixed test page set (`tests/fixture/config`).
- Schedule is **off** (`schedule_enabled: false`) until everything below is connected.

## Still to connect

1. Meta: an app and system users in the New Pages portfolio with "Content" and "Community activity"
   rights on the 24 pages → secrets `FB_APP_ID`, `FB_APP_SECRET`, `FB_USER_TOKEN` (`_2`, `_3`).
2. Run workflow "2 · Подключить Facebook" → real page IDs replace the `pending-NN` placeholders.
3. WordPress site → `site.url` in `config/settings.yaml` + secret `WP_API_KEY` (Make Connector)
   or `WP_USER` + `WP_APP_PASSWORD`.
4. Claude → secret `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.
5. Private test, public test on one page, then turn the schedule on.

Secrets are entered by the owner only: Settings → Secrets and variables → Actions.
