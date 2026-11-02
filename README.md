# Highrise Room Bot

A small Python bot for Highrise rooms. Emote names can be entered directly in chat or as commands:

The bot only speaks publicly to welcome players and say goodbye when they leave. Command responses and errors are sent privately to the player who issued the command.

- `Laugh` or `!laugh` — perform an emote on yourself
- `!emote Anime Dance` — perform an emote by its full name
- `!emote list` — show the available emotes, one name per line in separate pages
- `!emote Kiss @username` — target another player with a free emote
- `stop` or `!stop` — stop your current emote loop

Emote matching ignores spaces, hyphens, and underscores. Paid/owned emotes requested for another player run on the requester instead.

## Roles and moderation

The first player to use `!setowner` becomes the permanent bot owner. This is stored in the local, Git-ignored `bot_roles.json` file. No command can assign the `OWNER` role.
The owner can use `!rf` to refresh the bot connection.

Role order, from lowest to highest: `Guest`, `Vip`, `Admin`, `Mod`, `Mod1`, `Mod2`, `SeniorAdmin`, `Host`, `Coowner`, `OWNER`. Players can only assign a role below their own, and cannot change the role of someone at or above their level.

- `!roles` — show everyone in the room grouped into separate role sections
- `!role` or `!role @username` — show a player's role
- `!setrole @username <role>` — assign a lower role
- `!give @username <role>` — Coowner or higher; assign a lower role (OWNER cannot be assigned)
- `!kick @username` — Mod or higher
- `!mute @username [seconds]` — Mod or higher; defaults to 300 seconds
- `!unmute @username` — Mod or higher
- `!ban @username [seconds]` — Admin or higher
- `!unban <user_id>` — Admin or higher; use the player's ID if they are banned and not in the room
- `!log` — show successful moderation actions from the last hour with UTC date/time, moderator, and target
- `!flash` / `!flash off` — SeniorAdmin or higher; enable continuous click-to-teleport or turn it off
- `!summ @username` — SeniorAdmin or higher; summon one player to your position
- `!bring @user1 @user2 ...` — SeniorAdmin or higher; summon up to 10 players to your position at once
- `!create tele <name> <minimum role>` — SeniorAdmin or higher; save your current position as a teleport location and set who can use it (example: `!create tele f1 admin`)
- `!tele list` — list saved teleport locations and their minimum roles
- Type a saved location name, such as `f1`, to teleport yourself there if you meet its minimum role
- `!tele @user1 @user2 to (location)` — teleport up to 10 users to a saved location. The caller and targets must meet its minimum role; SeniorAdmin and above can teleport targets regardless of their role.

Moderation commands cannot be used against a player with an equal or higher bot role. Highrise must also grant the bot the room permissions required to perform each action.

Teleport locations are stored in the Git-ignored `bot_teleports.json` file.

## Setup

1. Install Python 3.11. The pinned Highrise SDK dependency currently fails to install on Python 3.12.
2. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install the Highrise SDK:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Create a bot and generate its API token in [Highrise account settings](https://highrise.game/account/settings). Make sure the bot can enter the room: the room must be yours or the bot must have designer rights.
5. Create a `.env` file in the project folder with your room ID and API token:

   ```text
   ROOM_ID=your-room-id
   API_TOKEN=your-api-token
   ```

6. Start the bot with either a `.env` file or inline credentials:

   ```powershell
   python main.py
   ```

   ```powershell
   python main.py <room-id> <api-token>
   ```

   ```powershell
   python main.py --room-id <room-id> --api-token <api-token>
   ```

The bot reconnects automatically if its connection drops. Keep the terminal open while it runs; press `Ctrl+C` to stop it.

## Deploy on Render

1. Push this project to a Git repository and connect it to Render.
2. Create a Blueprint using the repository's `render.yaml`.
3. Enter `ROOM_ID` and `API_TOKEN` when Render prompts for them, then deploy.

The Blueprint creates a persistent background worker and disk for owner roles and teleport locations. Render's persistent disk requires a paid worker plan.

Keep your API token private. Do not commit it to source control; `.env` is ignored by Git.
