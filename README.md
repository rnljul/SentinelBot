# Telegram Media Guard Bot

This bot temporarily blocks selected users from posting images or videos while still allowing text messages.

Important Telegram limitation: this is for a group or supergroup, including a channel's linked discussion group. Regular users cannot post directly in a broadcast channel, and Telegram bots cannot apply per-user media-only restrictions to channel subscribers.

## Features

- Admins can restrict a user from posting images/videos for a duration.
- Restricted users can still send text messages.
- If a restricted user posts a photo/video, the bot deletes it and sends a notice with the remaining restriction time.
- Admins can delete the latest stored media posts from a user.
- Expired restrictions are lifted automatically the next time the user posts or an admin lists restrictions.
- Restrictions are persisted in SQLite.
- Admins can use private chat commands after `MODERATION_CHAT_ID` is configured.

## Setup

1. Create a bot with [BotFather](https://t.me/BotFather), then copy the token.
2. In BotFather, turn off privacy mode for the bot:

   ```text
   /setprivacy -> choose your bot -> Disable
   ```

3. Add the bot to your group or linked discussion group.
4. Promote the bot to admin and allow it to delete messages.
5. Install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

6. Configure the token:

   ```bash
   cp .env.example .env
   export TELEGRAM_BOT_TOKEN="123456:replace-me"
   ```

7. Start the bot:

   ```bash
   python bot.py
   ```

8. In the group, run:

   ```text
   /chat_id
   ```

   Put the returned value into `.env` as `MODERATION_CHAT_ID`. Restart the bot after changing `.env`.

## Admin Commands

Use the reply-based commands when working directly in the group. In private chat with the bot, use `@username` after the bot has seen that user post in the group.

Telegram bots cannot reliably convert any random `@username` into a user id. SentinelBot stores a local known-users table from messages it sees in the group. If a user has never posted while the bot was running, ask them to post once or use a reply-based group command.

### Restrict a user

Reply to a user's message:

```text
/restrict_media 4h posted prohibited media
```

Or use a numeric Telegram user id:

```text
/restrict_media 123456789 4h posted prohibited media
```

In private chat with the bot, after `MODERATION_CHAT_ID` is set:

```text
/restrict_media @someuser
/restrict_media @someuser 4h posted prohibited media
```

If duration and reason are omitted, SentinelBot uses:

- Duration: `4h`
- Reason: `irrelevant content`

Supported durations:

- `30m`
- `4h`
- `2d`

### Remove a restriction

Reply to a user's message:

```text
/unrestrict_media
```

Or use a numeric Telegram user id:

```text
/unrestrict_media 123456789
```

In private chat with the bot:

```text
/unrestrict_media @someuser
```

### Delete latest media posts

In private chat with the bot:

```text
/del_post @someuser
/del_post @someuser 3
```

The optional `count` argument controls how many of that user's latest media posts to delete. It defaults to `1`.

The clearer alias also works:

```text
/delete_media @someuser 3
```

You can also reply to a user's message in the group:

```text
/del_post
/del_post 3
```

SentinelBot can delete only media posts it has seen while running. Telegram bots cannot search old chat history by user.

### Find a user

In private chat with the bot:

```text
/find_user someuser
```

This searches users the bot has already seen in the configured group and returns usernames plus numeric user IDs.

### Get the group chat id

In the group:

```text
/chat_id
```

Copy the returned `MODERATION_CHAT_ID` value into `.env`, then restart the bot. Private admin commands need this because a private chat does not tell the bot which group you want to moderate.

### List active restrictions

```text
/media_restrictions
```

## How It Works

The bot uses a soft restriction. It does not apply Telegram's native `restrictChatMember` media permission, because native restrictions prevent the media message before the bot can see it and therefore prevent the custom warning message.

Instead:

1. Admin adds a temporary media restriction.
2. User can still send text.
3. If the user sends photo/video content, the bot deletes it.
4. The bot posts a warning with the remaining time.
5. Once the timeout expires, the restriction is ignored and removed.

## Deploying

For production, run the bot under a process manager such as `systemd`, Docker, or a hosting platform that supports long-running Python processes.

## Docker

The project includes a `Dockerfile` and `docker-compose.yml` for deployment from GitHub.

### Run locally with Docker Compose

Create `.env` from the example and set your bot token:

```bash
cp .env.example .env
```

Start the bot:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f sentinel-bot
```

Stop the bot:

```bash
docker compose down
```

Restriction data is stored in a Docker volume at `/app/data`, so it survives container rebuilds.

### Build and run without Compose

```bash
docker build -t sentinel-bot .
docker run -d \
  --name sentinel-bot \
  --restart unless-stopped \
  --env-file .env \
  -v sentinel-bot-data:/app/data \
  sentinel-bot
```

### Deploy from GitHub

On your server:

```bash
git clone git@github.com:rnljul/SentinelBot.git
cd SentinelBot
cp .env.example .env
```

Edit `.env` with your real `TELEGRAM_BOT_TOKEN`, then run:

```bash
docker compose up -d --build
```
