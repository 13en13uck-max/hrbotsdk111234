import asyncio
import json
import logging
import os
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

from highrise import AnchorPosition, BaseBot, Position, ResponseError
from highrise.models import Error, User

from bot_emotes import EMOTE_DATA, LEGACY_EMOTE_IDS


logger = logging.getLogger(__name__)


def _normalize(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _build_emote_lookup() -> dict[str, tuple[str, float, bool]]:
    lookup = {
        _normalize(name): (emote_id, duration, is_free)
        for name, (emote_id, duration, is_free) in EMOTE_DATA.items()
    }
    for name, emote_id in LEGACY_EMOTE_IDS.items():
        if name not in lookup:
            data = next(
                (value for value in EMOTE_DATA.values() if value[0] == emote_id),
                None,
            )
            if data is not None:
                lookup[_normalize(name)] = data
    for emote_id, duration, is_free in EMOTE_DATA.values():
        lookup.setdefault(_normalize(emote_id), (emote_id, duration, is_free))
    return lookup


EMOTES_BY_NAME = _build_emote_lookup()

ROLE_ORDER = (
    "Guest",
    "Vip",
    "Admin",
    "Mod",
    "Mod1",
    "Mod2",
    "SeniorAdmin",
    "Host",
    "Coowner",
    "OWNER",
)
ROLE_DETAILS = {
    "Guest": ("Guest👤", None),
    "Vip": ("Vip⭐", "#FFD700"),
    "SeniorAdmin": ("SeniorAdmin🌟", "#FFA500"),
    "Admin": ("Admin🎖️", "#FF7F00"),
    "Mod": ("Mod🔏", "#FFCC66"),
    "Mod1": ("Mod1🛡️", "#E86100"),
    "Mod2": ("Mod2🛡️🛡️", "#FF4500"),
    "Host": ("Host⚙️", "#B22222"),
    "Coowner": ("Coowner⚜️", "#E63946"),
    "OWNER": ("OWNER👑", "#E8D179"),
}
ROLE_BY_NORMALIZED_NAME = {
    _normalize(name): name for name in ROLE_ORDER if name != "OWNER"
}
ROLE_BY_NORMALIZED_NAME.update(
    {
        _normalize(label): name
        for name, (label, _color) in ROLE_DETAILS.items()
        if name != "OWNER"
    }
)
MODERATION_MINIMUM_ROLE = {
    "kick": "Mod",
    "mute": "Mod",
    "unmute": "Mod",
    "ban": "Admin",
    "unban": "Admin",
}
MODERATION_PAST_TENSE = {
    "kick": "kicked",
    "ban": "banned",
    "mute": "muted",
    "unmute": "unmuted",
    "unban": "unbanned",
}
DEFAULT_MUTE_SECONDS = 300
FLASH_MINIMUM_ROLE = "SeniorAdmin"
SUMMON_MINIMUM_ROLE = "SeniorAdmin"
MAX_BRING_TARGETS = 10
MAX_TELEPORT_TARGETS = 10
TELEPORT_OVERRIDE_MINIMUM_ROLE = "SeniorAdmin"
TELEPORT_CREATOR_MINIMUM_ROLE = "SeniorAdmin"


class Bot(BaseBot):
    def __init__(self) -> None:
        self.active_emote_loops: dict[str, asyncio.Task[None]] = {}
        self.flash_armed_users: set[str] = set()
        data_directory = Path(
            os.environ.get("BOT_DATA_DIR", str(Path(__file__).parent))
        )
        data_directory.mkdir(parents=True, exist_ok=True)
        self.roles_path = data_directory / "bot_roles.json"
        self.owner_id, self.user_roles = self._load_roles()
        self.teleports_path = data_directory / "bot_teleports.json"
        self.teleport_locations = self._load_teleports()
        self.moderation_log_path = data_directory / "moderation_log.json"
        self.moderation_log = self._load_moderation_log()

    async def _reply(self, user_id: str, message: str) -> None:
        await self.highrise.send_whisper(user_id, message)

    def _load_moderation_log(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self.moderation_log_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Invalid moderation log in {self.moderation_log_path}"
            ) from error

        if not isinstance(data, list) or any(
            not isinstance(entry, dict)
            or any(
                not isinstance(entry.get(field), str)
                for field in ("timestamp", "action", "actor", "target")
            )
            for entry in data
        ):
            raise ValueError(f"Invalid moderation log in {self.moderation_log_path}")

        for entry in data:
            try:
                datetime.fromisoformat(entry["timestamp"])
            except ValueError as error:
                raise ValueError(
                    f"Invalid moderation log timestamp in {self.moderation_log_path}"
                ) from error
        return data

    def _save_moderation_log(self) -> None:
        temporary_path = self.moderation_log_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(self.moderation_log, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self.moderation_log_path)

    def _recent_moderation_log(self) -> list[dict[str, str]]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=1)
        recent_entries = []
        for entry in self.moderation_log:
            timestamp = datetime.fromisoformat(entry["timestamp"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
            if cutoff <= timestamp <= now:
                recent_entries.append(entry)
        return recent_entries

    def _record_moderation_action(
        self, action: str, actor: User, target: User
    ) -> None:
        self.moderation_log = self._recent_moderation_log()
        self.moderation_log.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "action": MODERATION_PAST_TENSE[action],
                "actor": actor.username,
                "target": target.username,
            }
        )
        self._save_moderation_log()

    async def _send_moderation_log(self, user_id: str) -> None:
        self.moderation_log = self._recent_moderation_log()
        if not self.moderation_log:
            await self._reply(user_id, "📝 No moderation actions in the last hour.")
            return

        lines = [
            f"{datetime.fromisoformat(entry['timestamp']).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} — "
            f"@{entry['actor']} {entry['action']} @{entry['target']}"
            for entry in reversed(self.moderation_log)
        ]
        heading = "📝 MODERATION LOG — LAST HOUR (UTC)"
        pages: list[str] = []
        current_page = heading
        for line in lines:
            candidate = f"{current_page}\n{line}"
            if len(candidate) > 180 and current_page != heading:
                pages.append(current_page)
                current_page = f"{heading}\n{line}"
            else:
                current_page = candidate
        pages.append(current_page)

        for index, page in enumerate(pages):
            if len(pages) > 1:
                page = page.replace(heading, f"{heading} ({index + 1}/{len(pages)})", 1)
            await self._reply(user_id, page)
            if index < len(pages) - 1:
                await asyncio.sleep(0.5)

    def _load_teleports(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.teleports_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Invalid teleport locations in {self.teleports_path}"
            ) from error

        if not isinstance(data, dict):
            raise ValueError(f"Invalid teleport locations in {self.teleports_path}")

        locations: dict[str, dict[str, object]] = {}
        for key, location in data.items():
            if (
                not isinstance(key, str)
                or not isinstance(location, dict)
                or not isinstance(location.get("name"), str)
                or location.get("min_role") not in ROLE_ORDER
                or location.get("min_role") == "OWNER"
                or any(
                    not isinstance(location.get(axis), (int, float))
                    for axis in ("x", "y", "z")
                )
                or not isinstance(location.get("facing"), str)
            ):
                raise ValueError(
                    f"Invalid teleport location {key!r} in {self.teleports_path}"
                )
            locations[key] = location
        return locations

    def _save_teleports(self) -> None:
        temporary_path = self.teleports_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(self.teleport_locations, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self.teleports_path)

    def _load_roles(self) -> tuple[str | None, dict[str, str]]:
        try:
            data = json.loads(self.roles_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, {}
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid role data in {self.roles_path}") from error

        if not isinstance(data, dict):
            raise ValueError(f"Invalid role data in {self.roles_path}")
        owner_id = data.get("owner_id")
        user_roles = data.get("user_roles", {})
        if owner_id is not None and not isinstance(owner_id, str):
            raise ValueError(f"Invalid owner ID in {self.roles_path}")
        if not isinstance(user_roles, dict) or any(
            not isinstance(user_id, str)
            or role not in ROLE_ORDER
            or role == "OWNER"
            for user_id, role in user_roles.items()
        ):
            raise ValueError(f"Invalid user roles in {self.roles_path}")
        return owner_id, user_roles

    def _save_roles(self) -> None:
        data = {"owner_id": self.owner_id, "user_roles": self.user_roles}
        temporary_path = self.roles_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_path.replace(self.roles_path)

    def _role_for(self, user_id: str) -> str:
        if user_id == self.owner_id:
            return "OWNER"
        return self.user_roles.get(user_id, "Guest")

    @staticmethod
    def _role_rank(role: str) -> int:
        return ROLE_ORDER.index(role)

    @staticmethod
    def _format_role(role: str) -> str:
        label, color = ROLE_DETAILS[role]
        return f"<{color}>{label}" if color else label

    async def _handle_role_command(self, user: User, command: str) -> None:
        try:
            parts = shlex.split(command)
        except ValueError:
            await self._reply(user.id, "Invalid command syntax.")
            return
        name = parts[0].casefold()

        if name == "!setowner":
            if len(parts) != 1:
                await self._reply(user.id, "Usage: !setowner")
                return
            if self.owner_id is None:
                self.owner_id = user.id
                self.user_roles.pop(user.id, None)
                self._save_roles()
                await self._reply(
                    user.id,
                    f"{self._format_role('OWNER')} has been permanently set to @{user.username}."
                )
            elif self.owner_id == user.id:
                await self._reply(user.id, "You are already the bot owner.")
            else:
                await self.highrise.send_whisper(
                    user.id, "The bot owner has already been set."
                )
            return

        if name in {"!role", "!myrole"} and len(parts) in {1, 2}:
            target = user
            if len(parts) == 2:
                target = await self._find_user(parts[1].lstrip("@"))
                if target is None:
                    await self._reply(user.id, f"{parts[1]} is not in the room.")
                    return
            role = self._role_for(target.id)
            await self._reply(
                user.id,
                f"@{target.username}'s role: {self._format_role(role)}"
            )
            return

        if name == "!roles" and len(parts) == 1:
            await self._send_role_roster(user.id)
            return

        if name in {"!role", "!myrole"}:
            await self._reply(user.id, "Usage: !role [@username]")
            return

        is_give_command = name == "!give"
        if name not in {"!setrole", "!give"}:
            await self._reply(user.id, "Usage: !setrole @username <role>")
            return

        if is_give_command and self._role_rank(self._role_for(user.id)) < self._role_rank(
            "Coowner"
        ):
            await self.highrise.send_whisper(
                user.id, "Only Coowner or higher can use !give."
            )
            return

        if len(parts) < 3:
            usage = "!give @username <role>" if is_give_command else "!setrole @username <role>"
            await self._reply(user.id, f"Usage: {usage}")
            return

        target = await self._find_user(parts[1].lstrip("@"))
        if target is None:
            await self._reply(user.id, f"{parts[1]} is not in the room.")
            return

        requested_role = ROLE_BY_NORMALIZED_NAME.get(
            _normalize(" ".join(parts[2:]))
        )
        if requested_role is None:
            await self._reply(
                user.id,
                "Unknown role. Use !roles to see assignable roles."
            )
            return

        actor_role = self._role_for(user.id)
        if self._role_rank(actor_role) <= self._role_rank(requested_role):
            await self.highrise.send_whisper(
                user.id, "You can only assign roles lower than your own."
            )
            return
        if self._role_rank(actor_role) <= self._role_rank(
            self._role_for(target.id)
        ):
            await self.highrise.send_whisper(
                user.id,
                "You cannot change the role of someone with an equal or higher role.",
            )
            return

        self.user_roles[target.id] = requested_role
        self._save_roles()
        await self._reply(
            user.id,
            f"@{target.username} is now {self._format_role(requested_role)}."
        )

    async def _handle_create_teleport(self, user: User, command: str) -> None:
        match = re.fullmatch(
            r"!create\s+tele\s+(.+?)\s+(\S+)",
            command,
            flags=re.IGNORECASE,
        )
        if match is None:
            await self._reply(user.id, "Usage: !create tele <name> <minimum role>")
            return

        if self._role_rank(self._role_for(user.id)) < self._role_rank(
            TELEPORT_CREATOR_MINIMUM_ROLE
        ):
            await self.highrise.send_whisper(
                user.id,
                f"You need {self._format_role(TELEPORT_CREATOR_MINIMUM_ROLE)} or higher to create teleport locations.",
            )
            return

        location_name = match.group(1).strip()
        role_name = ROLE_BY_NORMALIZED_NAME.get(_normalize(match.group(2)))
        if not location_name:
            await self._reply(user.id, "Location name cannot be empty.")
            return
        if role_name is None:
            await self._reply(
                user.id,
                "Unknown minimum role. OWNER cannot be used as a location role."
            )
            return

        response = await self.highrise.get_room_users()
        if isinstance(response, Error):
            logger.error("Could not fetch room position to create teleport: %s", response)
            await self.highrise.send_whisper(
                user.id, "Could not read your position. Please try again."
            )
            return

        entry = next(
            (entry for entry in response.content if entry[0].id == user.id), None
        )
        if entry is None or not isinstance(entry[1], Position):
            await self.highrise.send_whisper(
                user.id, "Could not determine your current room position."
            )
            return

        position = entry[1]
        location_key = _normalize(location_name)
        self.teleport_locations[location_key] = {
            "name": location_name,
            "x": position.x,
            "y": position.y,
            "z": position.z,
            "facing": position.facing,
            "min_role": role_name,
        }
        self._save_teleports()
        await self._reply(
            user.id,
            f"Teleport location ({location_name}) created. "
            f"Type {location_name} to teleport there. "
            f"Minimum role: {self._format_role(role_name)}."
        )

    def _position_for_teleport(self, location: dict[str, object]) -> Position:
        return Position(
            float(location["x"]),
            float(location["y"]),
            float(location["z"]),
            str(location["facing"]),
        )

    async def _send_teleport_list(self, user_id: str) -> None:
        if not self.teleport_locations:
            await self._reply(
                user_id, "📍 No teleport locations have been created yet."
            )
            return

        entries = [
            f"• {location['name']} — {self._format_role(str(location['min_role']))}+"
            for location in sorted(
                self.teleport_locations.values(),
                key=lambda location: str(location["name"]).casefold(),
            )
        ]
        pages: list[str] = []
        current_page = "📍 TELEPORT LOCATIONS"
        for entry in entries:
            candidate = f"{current_page}\n{entry}"
            if len(candidate) > 180 and current_page != "📍 TELEPORT LOCATIONS":
                pages.append(current_page)
                current_page = "📍 TELEPORT LOCATIONS\n" + entry
            else:
                current_page = candidate
        pages.append(current_page)

        for index, page in enumerate(pages):
            if len(pages) > 1:
                page = page.replace(
                    "📍 TELEPORT LOCATIONS",
                    f"📍 TELEPORT LOCATIONS ({index + 1}/{len(pages)})",
                    1,
                )
            await self._reply(user_id, page)
            if index < len(pages) - 1:
                await asyncio.sleep(0.5)

    async def _handle_location_shortcut(self, user: User, message: str) -> bool:
        location = self.teleport_locations.get(_normalize(message))
        if location is None:
            return False

        minimum_role = str(location["min_role"])
        if self._role_rank(self._role_for(user.id)) < self._role_rank(minimum_role):
            await self.highrise.send_whisper(
                user.id,
                f"You need {self._format_role(minimum_role)} or higher to use this teleport.",
            )
            return True

        try:
            await self.highrise.teleport(
                user.id, self._position_for_teleport(location)
            )
        except ResponseError as error:
            logger.warning(
                "Teleport of player %s to %s failed: %s",
                user.id,
                location["name"],
                error,
            )
            await self.highrise.send_whisper(
                user.id, f"Highrise could not complete the teleport: {error}"
            )
            return True

        await self._reply(
            user.id,
            f"@{user.username} was teleported to ({location['name']})."
        )
        return True

    async def _handle_teleport_command(self, user: User, command: str) -> None:
        match = re.fullmatch(
            r"!tele\s+(.+?)\s+to\s+\((.+?)\)",
            command,
            flags=re.IGNORECASE,
        )
        if match is None:
            await self._reply(
                user.id,
                "!tele @user1 @user2 to (location) (up to 10 users)"
            )
            return

        target_tokens = match.group(1).split()
        location_name = match.group(2).strip()
        if (
            not target_tokens
            or len(target_tokens) > MAX_TELEPORT_TARGETS
            or any(not name.startswith("@") or len(name) == 1 for name in target_tokens)
        ):
            await self._reply(
                user.id,
                "!tele @user1 @user2 to (location) (up to 10 users)"
            )
            return
        target_names = [name[1:] for name in target_tokens]

        location_key = _normalize(location_name)
        location = self.teleport_locations.get(location_key)
        if location is None:
            await self._reply(
                user.id, f"Teleport location ({location_name}) not found."
            )
            return

        actor_role = self._role_for(user.id)
        minimum_role = str(location["min_role"])
        if self._role_rank(actor_role) < self._role_rank(minimum_role):
            await self.highrise.send_whisper(
                user.id,
                f"You need {self._format_role(minimum_role)} or higher to use this teleport.",
            )
            return

        response = await self.highrise.get_room_users()
        if isinstance(response, Error):
            logger.error("Could not fetch room users for teleport: %s", response)
            await self.highrise.send_whisper(
                user.id, "Could not load room users. Please try again."
            )
            return

        users_by_name: dict[str, User] = {}
        for room_user, _position in response.content:
            users_by_name[_normalize(room_user.username)] = room_user
            users_by_name[_normalize(room_user.id)] = room_user

        targets: list[User] = []
        missing_names: list[str] = []
        seen_ids: set[str] = set()
        for target_name in target_names:
            target = users_by_name.get(_normalize(target_name))
            if target is None:
                missing_names.append(target_name)
            elif target.id not in seen_ids:
                targets.append(target)
                seen_ids.add(target.id)

        if missing_names:
            await self._reply(
                user.id,
                "Not in the room: " + ", ".join(f"@{name}" for name in missing_names)
            )
            return

        if self._role_rank(actor_role) < self._role_rank(
            TELEPORT_OVERRIDE_MINIMUM_ROLE
        ):
            unauthorized_targets = [
                f"@{target.username}"
                for target in targets
                if self._role_rank(self._role_for(target.id))
                < self._role_rank(minimum_role)
            ]
            if unauthorized_targets:
                await self.highrise.send_whisper(
                    user.id,
                    "These users do not meet the location's minimum role: "
                    + ", ".join(unauthorized_targets),
                )
                return

        destination = self._position_for_teleport(location)
        failures: list[str] = []
        for target in targets:
            try:
                await self.highrise.teleport(target.id, destination)
            except ResponseError as error:
                logger.warning(
                    "Teleport of player %s to %s failed: %s",
                    target.id,
                    location_name,
                    error,
                )
                failures.append(f"@{target.username}")

        if failures:
            await self.highrise.send_whisper(
                user.id,
                "Could not teleport: " + ", ".join(failures),
            )

    async def _send_role_roster(self, user_id: str) -> None:
        response = await self.highrise.get_room_users()
        if isinstance(response, Error):
            logger.error("Could not fetch room users for role roster: %s", response)
            await self._reply(user_id, "Could not load the room role list.")
            return

        grouped_users: dict[str, list[str]] = {role: [] for role in ROLE_ORDER}
        for room_user, _position in response.content:
            grouped_users[self._role_for(room_user.id)].append(
                f"@{room_user.username}"
            )

        messages: list[str] = []
        for role in ROLE_ORDER:
            heading = self._format_role(role)
            members = sorted(grouped_users[role], key=str.casefold)
            if not members:
                messages.append(f"{heading}\nNo users")
                continue

            current_members: list[str] = []
            for member in members:
                candidate = f"{heading}\n" + "\n".join(
                    [*current_members, member]
                )
                if len(candidate) > 210 and current_members:
                    messages.append(f"{heading}\n" + "\n".join(current_members))
                    current_members = [member]
                else:
                    current_members.append(member)
            if current_members:
                messages.append(f"{heading}\n" + "\n".join(current_members))

        for index, message in enumerate(messages):
            await self._reply(user_id, message)
            if index < len(messages) - 1:
                await asyncio.sleep(0.5)

    async def _handle_moderation_command(
        self, user: User, command: str
    ) -> None:
        try:
            parts = shlex.split(command)
        except ValueError:
            await self._reply(user.id, "Invalid command syntax.")
            return

        action = parts[0][1:].casefold()
        minimum_role = MODERATION_MINIMUM_ROLE[action]
        if self._role_rank(self._role_for(user.id)) < self._role_rank(
            minimum_role
        ):
            await self.highrise.send_whisper(
                user.id,
                f"You need {self._format_role(minimum_role)} or higher to use !{action}.",
            )
            return

        if len(parts) < 2 or (
            action in {"kick", "unmute", "unban"} and len(parts) != 2
        ):
            usage = {
                "kick": "!kick @username",
                "ban": "!ban @username [seconds]",
                "mute": "!mute @username [seconds]",
                "unmute": "!unmute @username",
                "unban": "!unban @username",
            }[action]
            await self._reply(user.id, f"Usage: {usage}")
            return

        target = await self._find_user(parts[1].lstrip("@"))
        if target is None:
            if action == "unban":
                target = User(id=parts[1].lstrip("@"), username=parts[1])
            else:
                await self._reply(user.id, f"{parts[1]} is not in the room.")
                return
        if target.id == user.id:
            await self._reply(user.id, "You cannot moderate yourself.")
            return

        actor_role = self._role_for(user.id)
        target_role = self._role_for(target.id)
        if self._role_rank(actor_role) <= self._role_rank(target_role):
            await self.highrise.send_whisper(
                user.id, "You cannot moderate someone with an equal or higher role."
            )
            return

        action_length = None
        if action in {"mute", "ban"} and len(parts) > 2:
            if len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) <= 0:
                await self._reply(
                    user.id,
                    f"Usage: !{action} @username [duration in seconds]"
                )
                return
            action_length = int(parts[2])
        elif action == "mute":
            action_length = DEFAULT_MUTE_SECONDS

        try:
            await self.highrise.moderate_room(
                target.id, action, action_length
            )
        except ResponseError as error:
            logger.warning(
                "Moderation action %s on %s failed: %s",
                action,
                target.id,
                error,
            )
            await self.highrise.send_whisper(
                user.id, f"Highrise could not complete !{action}: {error}"
            )
            return

        try:
            self._record_moderation_action(action, user, target)
        except OSError:
            logger.exception("Could not save moderation log entry for !%s.", action)
            await self.highrise.send_whisper(
                user.id,
                "The moderation action succeeded, but its log entry could not be saved.",
            )

        await self._reply(
            user.id, f"@{target.username} was {MODERATION_PAST_TENSE[action]}."
        )

    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()
        if not text:
            return

        if text.casefold() == "!rf":
            if user.id != self.owner_id:
                await self.highrise.send_whisper(
                    user.id, "Only the bot owner can refresh the bot."
                )
                return
            await self._reply(user.id, "Bot refresh requested. Reconnecting now.")
            await self.highrise.ws.close()
            return

        if text.casefold() in {"!flash", "!flash on"}:
            if self._role_rank(self._role_for(user.id)) < self._role_rank(
                FLASH_MINIMUM_ROLE
            ):
                await self.highrise.send_whisper(
                    user.id,
                    f"You need {self._format_role(FLASH_MINIMUM_ROLE)} or higher to use !flash.",
                )
                return
            if user.id in self.flash_armed_users:
                await self._reply(
                    user.id,
                    f"⚡ @{user.username}, flash is already on. Use !flash off to disable it."
                )
                return
            self.flash_armed_users.add(user.id)
            await self._reply(
                user.id,
                f"⚡ @{user.username}, flash is on. Click where you want to go; use !flash off to disable it."
            )
            return

        if text.casefold() == "!flash off":
            self.flash_armed_users.discard(user.id)
            await self._reply(user.id, f"⚡ @{user.username}, flash is off.")
            return

        command = text.split(maxsplit=1)[0].casefold()
        if command == "!create":
            await self._handle_create_teleport(user, text)
            return
        if text.casefold() == "!tele list":
            await self._send_teleport_list(user.id)
            return
        if command == "!tele":
            await self._handle_teleport_command(user, text)
            return
        if text.casefold() == "!log":
            await self._send_moderation_log(user.id)
            return
        if command in {"!setowner", "!setrole", "!give", "!role", "!myrole", "!roles"}:
            await self._handle_role_command(user, text)
            return
        if command in {"!bring", "!summ"}:
            await self._handle_summon_command(user, text)
            return
        if command.lstrip("!") in MODERATION_MINIMUM_ROLE:
            await self._handle_moderation_command(user, text)
            return

        if await self._handle_location_shortcut(user, text):
            return

        if text.casefold() in {"stop", "!stop"}:
            await self._stop_emote(user.id)
            return

        if text.casefold().startswith("!emote"):
            await self._handle_emote_command(user, text)
            return

        is_command = text.startswith("!")
        emote_text = text[1:].strip() if is_command else text
        if emote_text.startswith("(") and emote_text.endswith(")"):
            emote_text = emote_text[1:-1].strip()

        target_name = None
        parts = emote_text.split()
        if len(parts) > 1 and parts[-1].startswith("@"):
            target_name = parts.pop()[1:]
        emote = EMOTES_BY_NAME.get(_normalize(" ".join(parts)))
        if emote is None:
            return

        target = user
        if target_name:
            target = await self._find_user(target_name)
            if target is None:
                await self._reply(user.id, f"@{target_name} is not in the room.")
                return

        await self._start_emote(user, target, emote)

    async def _handle_summon_command(self, user: User, command: str) -> None:
        try:
            parts = shlex.split(command)
        except ValueError:
            await self._reply(user.id, "Invalid command syntax.")
            return

        command_name = parts[0].casefold()
        if self._role_rank(self._role_for(user.id)) < self._role_rank(
            SUMMON_MINIMUM_ROLE
        ):
            await self.highrise.send_whisper(
                user.id,
                f"You need {self._format_role(SUMMON_MINIMUM_ROLE)} or higher to use {command_name}.",
            )
            return

        target_names = [part.lstrip("@") for part in parts[1:]]
        if (
            not target_names
            or any(not name for name in target_names)
            or (command_name == "!summ" and len(target_names) != 1)
            or len(target_names) > MAX_BRING_TARGETS
        ):
            await self._reply(
                user.id,
                "!summ @username | !bring @username1 @username2 ... "
                f"(up to {MAX_BRING_TARGETS} users)"
            )
            return

        response = await self.highrise.get_room_users()
        if isinstance(response, Error):
            logger.error("Could not fetch room positions for summon: %s", response)
            await self.highrise.send_whisper(
                user.id, "Could not read room positions. Please try again."
            )
            return

        room_users = response.content
        requester_entry = next(
            (entry for entry in room_users if entry[0].id == user.id), None
        )
        if requester_entry is None or not isinstance(requester_entry[1], Position):
            await self.highrise.send_whisper(
                user.id, "Could not determine your current room position."
            )
            return

        requester_position = requester_entry[1]
        requested_names = {_normalize(name) for name in target_names}
        targets = [
            room_user
            for room_user, _position in room_users
            if _normalize(room_user.username) in requested_names
            or _normalize(room_user.id) in requested_names
        ]
        found_names = {
            _normalize(room_user.username) for room_user in targets
        } | {_normalize(room_user.id) for room_user in targets}
        missing_names = [
            name for name in target_names if _normalize(name) not in found_names
        ]
        if missing_names:
            await self._reply(
                user.id,
                "Not in the room: " + ", ".join(f"@{name}" for name in missing_names)
            )
            return

        failures: list[str] = []
        for target in targets:
            if target.id == user.id:
                failures.append(f"@{target.username} (cannot summon yourself)")
                continue
            try:
                await self.highrise.teleport(
                    target.id,
                    Position(
                        requester_position.x,
                        requester_position.y,
                        requester_position.z,
                        requester_position.facing,
                    ),
                )
            except ResponseError as error:
                logger.warning("Summon of player %s failed: %s", target.id, error)
                failures.append(f"@{target.username} (Highrise rejected it)")

        if failures:
            await self.highrise.send_whisper(
                user.id, "Could not summon: " + ", ".join(failures)
            )

    async def on_user_join(
        self, user: User, position: Position | AnchorPosition
    ) -> None:
        await self.highrise.chat(f"Welcome @{user.username}")

    async def on_user_leave(self, user: User) -> None:
        await self.highrise.chat(f"Goodbye @{user.username}")

    async def on_user_move(
        self, user: User, position: Position | AnchorPosition
    ) -> None:
        if user.id not in self.flash_armed_users:
            return
        if self._role_rank(self._role_for(user.id)) < self._role_rank(
            FLASH_MINIMUM_ROLE
        ):
            self.flash_armed_users.discard(user.id)
            return
        if not isinstance(position, Position):
            return

        await self.highrise.teleport(
            user.id,
            Position(position.x, position.y, position.z, position.facing),
        )
    async def _handle_emote_command(self, user: User, message: str) -> None:
        args = message.split(maxsplit=1)
        if len(args) == 1:
            await self._reply(user.id, "Use !emote <name> or !emote list.")
            return

        argument = args[1].strip()
        if argument.casefold() == "list":
            await self._send_emote_catalog(user.id)
            return

        await self._run_emote(user, argument)

    async def _run_emote(self, user: User, argument: str) -> None:
        parts = argument.split()
        target_name = None
        if len(parts) > 1 and parts[-1].startswith("@"):
            target_name = parts.pop()[1:]

        emote_name = " ".join(parts).strip().strip("()").strip()
        emote = EMOTES_BY_NAME.get(_normalize(emote_name))
        if emote is None:
            await self._reply(user.id, f"Unknown emote: {emote_name}")
            return

        target = user
        if target_name:
            target = await self._find_user(target_name)
            if target is None:
                await self._reply(user.id, f"@{target_name} is not in the room.")
                return

        await self._start_emote(user, target, emote)

    async def _find_user(self, username: str) -> User | None:
        response = await self.highrise.get_room_users()
        if isinstance(response, Error):
            logger.error("Could not fetch room users: %s", response)
            return None

        requested = username.casefold()
        return next(
            (
                room_user
                for room_user, _position in response.content
                if room_user.username.casefold() == requested
                or room_user.id.casefold() == requested
            ),
            None,
        )

    async def _start_emote(
        self, requester: User, target: User, emote: tuple[str, float, bool]
    ) -> None:
        emote_id, duration, is_free = emote
        if target.id != requester.id and not is_free:
            target = requester

        self._cancel_emote_loop(target.id)
        self.active_emote_loops[target.id] = asyncio.create_task(
            self._emote_loop(target.id, emote_id, duration)
        )

    async def _emote_loop(
        self, user_id: str, emote_id: str, duration: float
    ) -> None:
        task = asyncio.current_task()
        try:
            while True:
                await self.highrise.send_emote(
                    emote_id, target_user_id=user_id
                )
                await asyncio.sleep(duration)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Emote playback failed for player %s", user_id)
            await self.highrise.send_whisper(
                user_id, "The emote failed. Please try again later."
            )
        finally:
            if self.active_emote_loops.get(user_id) is task:
                del self.active_emote_loops[user_id]

    async def _send_emote_catalog(self, user_id: str) -> None:
        names = sorted(EMOTE_DATA, key=str.casefold)
        pages: list[str] = []
        current_page = ""
        for name in names:
            entry = f"({name})"
            candidate = f"{current_page}\n{entry}" if current_page else entry
            if len(candidate) > 180:
                pages.append(current_page)
                current_page = entry
            else:
                current_page = candidate
        if current_page:
            pages.append(current_page)

        for page_number, page in enumerate(pages, start=1):
            await self._reply(
                user_id,
                f"EMOTES {page_number}/{len(pages)}\n{page}"
            )
            if page_number < len(pages):
                await asyncio.sleep(1)

    async def _stop_emote(self, user_id: str) -> None:
        task = self.active_emote_loops.get(user_id)
        if task is None or task.done():
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await self.highrise.send_emote("idle", target_user_id=user_id)

    def _cancel_emote_loop(self, user_id: str) -> None:
        task = self.active_emote_loops.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()
