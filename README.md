# Group Manager Bot (Rose-style) — Vercel

## Environment variables (Vercel → Project → Settings → Environment Variables)
| Name | Value |
|---|---|
| BOT_TOKEN | token from @BotFather |
| WEBHOOK_SECRET | any random string, only letters/numbers/_/- (e.g. `MySecret_84721xyz`) |
| UPSTASH_REDIS_REST_URL | from your free Upstash Redis database (REST API section) |
| UPSTASH_REDIS_REST_TOKEN | same place |

## Deploy
1. Push this folder to a GitHub repo (keep the `api/` folder), import it in Vercel, add the variables above, Deploy.
2. Open `https://YOUR-APP.vercel.app/api/setwebhook?key=YOUR_WEBHOOK_SECRET`  → must show `"ok":true`.
3. Open `https://YOUR-APP.vercel.app/api/health` → everything should be set / PONG.
4. Add the bot to your group and make it admin (delete messages, ban users, pin, invite, change info).
   In @BotFather: /setprivacy → your bot → Disable.
5. Send /help in the group.

If you change environment variables, redeploy, then open the setwebhook link again.
