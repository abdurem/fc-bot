# FC group bot

A small Python/SQLite Telegram bot. No Python dependencies, domain, public port, or separate database server. All group members can record and delete matches and start seasons. Standings are independent for each group.

## Run

1. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
2. Run `docker compose up -d --build`.
3. Add the bot to your Telegram group. Send `/help` or `/help@YourBotUsername`.

To record games without a command, send exactly `@ali 3 @sam 1`. In @BotFather, use `/setprivacy`, select your bot, and choose **Disable**, then remove and re-add the bot to your group for the change to take effect. This lets it receive ordinary group messages. Alternatively, bots that are group admins already receive them. The bot ignores ordinary conversation and only accepts a complete message containing two @usernames and two integer scores; `/result` still works too.

## Player buttons (recommended)

Each player sends `/join` once in the group. Then anyone can send `/result`, select the first player (including themselves), select the opponent, and reply to the bot's score prompt with `3 1`, `3-1`, or `3:1`. Scores follow the selected player order. No self-mention is needed. The prompt uses ForceReply, so this flow works with Telegram's default privacy mode too.

Players are stored by permanent Telegram user ID. Sending `/join` again refreshes the display name and username without splitting their match history. On first join, existing results using that exact username are linked to their account; older arbitrary names are left as they were.

Each entry belongs to the person who started it. Entries expire after 30 minutes; `/cancel` or starting another `/result` cancels your previous one. Other friends can start their own entries in parallel. Replies must target the score prompt. A season change invalidates unfinished entries. If a prompt fails to send, start again with `/result`.

Typed shortcuts still work. Known registered usernames map to permanent IDs; manually typed unknown names are not verified against Telegram accounts. Use the player buttons to avoid spelling mistakes.

## Head to head

Send `/head2head` and select two joined players with the same inline buttons. The bot shows their current-season meetings: wins, draws, goals, points earned against each other, and the last 10 scores. Both home/away orders count, and games against other opponents are excluded. This view never records a match. `/cancel` stops the selection; starting `/result` or `/head2head` replaces your previous selection. Existing databases upgrade automatically, preserving active result entries.

The bot registers its command menu with Telegram on startup. Type `/` in the group to see suggestions, including `/start` and `/commands`.

Messages use Telegram's native Rich HTML (`sendRichMessage`, Bot API 10.1+). Standings and match history render as compact striped tables with bold points. Extra help collapses into expandable sections in the command guide. Use an up-to-date Telegram client to view rich messages. Replies stay in the originating forum topic when applicable.

All outgoing bot messages use `disable_notification=true`: notifications can still appear, but without sound. This includes player buttons, score prompts, standings, and errors. Commands sent by group members follow their own Telegram send settings; the bot cannot make those incoming messages silent. Use Telegram's **Send Without Sound** option or mute the group on each member's device.

```text
/join
/result
/head2head
/cancel
/result @ali 3 @sam 1
/table
/matches
/delete 12
/newseason
/table 1
/matches 1
/chatid
```

For typed shortcuts, `ali` and `@ali` resolve to the same username. Table columns: P, W, D, L, GF, GA, GD, Pts. Ranking: points, goal difference, goals scored; alphabetical ordering of stored player keys resolves display order for exact ties.

Slash commands work with Telegram's default group privacy mode. If another bot is in the group, address commands explicitly as `/result@YourBotUsername @ali 3 @sam 1`.

Optionally set `ALLOWED_CHAT_ID` in `.env` using `/chatid`, then run `docker compose up -d --force-recreate`. No admin roles are required.

## Operations

`docker compose logs -f` shows status. `docker compose down` stops the bot and keeps data. `docker compose up -d --build` applies updates. Run only one instance per token. Long polling requires that the bot has no existing webhook configured.

SQLite lives in the `league-data` Docker volume. Do not use `docker compose down -v` unless you intend to erase all history. For a consistent backup, stop the bot first:

```sh
docker compose stop
docker compose run --rm --no-deps --entrypoint cat bot /app/data/league.sqlite3 > league-backup.sqlite3
docker compose start
```

Match changes and processed update IDs commit together, preventing Telegram retries from recording a game twice after restart. Replies are best effort: if Telegram fails after saving a game, check `/matches` before submitting again. Edited messages do not alter saved results; delete the match and submit the corrected result.

Keep `.env` private. It is excluded from Git and Docker builds. If a token is exposed publicly, replace it through BotFather and update `.env`.

Run checks: `python -m unittest -v`.

Telegram API reference: https://core.telegram.org/bots/api
