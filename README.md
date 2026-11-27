# New pages autopost

A separate autoposting system, completely independent of INTERES WORLD: its own repository,
Meta business portfolio ("New Pages", created from the Anahit Markosyan profile), system users,
page tokens, WordPress site, queues and schedule.

Each release = a post with a photo on every page of a group + an article on the website + the article link
in the first comment. Releases run at 12:00, 15:00, 18:00, 21:00 and 00:00 (Yerevan).

## Pages (49) and groups — one story per group in every release, 4 links per release

| Group | Key | Pages |
|---|---|---|
| IT | IT | 14 |
| EN | EN | 11 |
| FR | FR | 15 |
| DE+PT+ES | PT | 4 DE + 4 PT + 1 ES |

Since 23.11.2026 (23:55): Anahit Markosyan's 22 pages from the owner's list, 4 more of her pages
(Ce que ressent le cœur, Des histoires qui restent, Histoires à Travers la Vie, La vie, en vrai) and 4 pages
added from the Susi Ghevondyan profile (La Vie qui Passe, Verità Mai Confessate, The Life Edit, Além das Aparências).
On 27.11.2026, 19 pages were added from the Shiraz Kirakosyan profile (the pages where Shiraz has full control).
7 more of Shiraz's pages can be added after 03.12 ~15:00 (Meta's 7-day admin rule); 11 pages from the owner's list
need Lilit Minasyan, who has full control of them.
The other pages of the New Pages portfolio are in the portfolio but not automated.

Pages are listed in `config/pages.yaml`; links for each group go to `queue/<key>.txt`.
Articles are published on **1tari.ru** (via the Make Connector plugin).

## Status

- Code copied from the main system; tests pass on a fixed test page set (`tests/fixture/config`).
- 24.11.2026: Facebook, 1tari.ru and Claude connected; private and public tests passed.
- Schedule is **on**. Releases need links in `queue/IT.txt`, `queue/EN.txt`, `queue/FR.txt`, `queue/PT.txt`.
- 25.11.2026: all 30 pages have tokens; the Meta app was switched to Live (in Development mode its posts were visible only to app admins).
- 28.11.2026: 49 pages, tokens 49/49; the 2 PT pages moved from the FR group to the new DE+PT+ES group.

## Still to connect

1. Meta: an app and system users in the New Pages portfolio with "Content" and "Community activity"
   rights on the 30 pages → secrets `FB_APP_ID`, `FB_APP_SECRET`, `FB_USER_TOKEN` (`_2`, `_3`).
2. Run workflow "2 · Подключить Facebook" → real page IDs replace the `pending-NN` placeholders.
3. WordPress 1tari.ru → secret `WP_API_KEY` (Make Connector plugin key). The application password
   does not work there: the hosting strips the Authorization header.
4. Claude → secret `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.
5. Private test, public test on one page, then turn the schedule on.

Secrets are entered by the owner only: Settings → Secrets and variables → Actions.
