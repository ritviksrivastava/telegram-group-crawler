# Telegram Group & Channel Member Crawler

Lists members of the Telegram **groups and broadcast channels** you belong to,
splits each into **admins** and **members/subscribers**, and exports every chat to
its own CSV + JSON.

## What it collects per member
`id`, `username`, `first_name`, `last_name`, `full_name`, `phone` (only if the user's
privacy allows it — usually empty), `is_bot`, `is_premium`, `is_verified`, `is_scam`,
`is_deleted`, and last-seen `status`.

## Setup

1. **Get API credentials** — go to https://my.telegram.org → *API development tools*,
   create an app, and copy the `api_id` and `api_hash`.

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure** — copy `.env.example` to `.env` and fill in `API_ID`, `API_HASH`,
   and your `PHONE` (international format).

## Usage

```bash
python crawler.py                        # interactive — pick from groups + channels
python crawler.py --type channels        # only broadcast channels
python crawler.py --type groups          # only groups/supergroups
python crawler.py --all                  # crawl every chat of the chosen type
python crawler.py --type channels --all  # crawl all your channels in one run
python crawler.py --chat @somechat       # one group/channel by @username
python crawler.py --chat -1001234567890  # or by numeric id  (--group still works)
python crawler.py --list                 # just list your groups/channels
```

On the **first run** Telegram sends a login code to your Telegram app — enter it when
prompted. A `session` file is saved so you won't be asked again.

Each chat produces up to two file pairs in `./output/`:
`<chat>_admins_<ts>.csv/.json` and `<chat>_members_<ts>.csv/.json`
(channels use `_subscribers_` instead of `_members_`).

## Limits & rules (please read)

- You log in **as yourself** — this uses Telegram's official user API, not a bot and
  not anonymous scraping.
- You must **belong to** the group/channel.
- For supergroups/channels with **> ~200 members, only admins** can pull the full
  participant list. Non-admins get a partial set.
- In a **broadcast channel, only admins can list subscribers at all** — as a normal
  subscriber you'll still get the admin roster but an empty members list. Telegram
  restriction, not a bug.
- `phone` is almost always empty unless the member exposes it (typically mutual
  contacts only).
- Telegram's ToS forbids harvesting user data for spam/resale. Use this for groups you
  own/administer or belong to, for legitimate purposes, and comply with privacy law
  (e.g. GDPR/local equivalents) where it applies.
