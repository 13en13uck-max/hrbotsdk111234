import argparse
import logging
import os
import sys
import time

from highrise.__main__ import run


def load_dotenv() -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    env_room_id = os.environ.get("ROOM_ID", "")
    env_api_token = os.environ.get("API_TOKEN", "")

    room_id = args.room_id_opt or args.room_id or env_room_id
    api_token = args.api_token_opt or args.api_token or env_api_token
    return room_id, api_token


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Highrise bot with either a .env file or inline credentials."
    )
    parser.add_argument("room_id", nargs="?", help="Highrise room ID")
    parser.add_argument("api_token", nargs="?", help="Highrise bot API token")
    parser.add_argument("--room-id", dest="room_id_opt", help="Highrise room ID")
    parser.add_argument(
        "--api-token",
        dest="api_token_opt",
        help="Highrise bot API token",
    )
    args = parser.parse_args()

    load_dotenv()
    room_id, api_token = _resolve_credentials(args)
    if not room_id or not api_token:
        raise SystemExit(
            "Missing bot credentials. Provide ROOM_ID and API_TOKEN as arguments or in .env.\n"
            "Usage: python main.py <room_id> <api_token>"
        )

    sys.argv = ["highrise", "bot:Bot", room_id, api_token]
    try:
        while True:
            try:
                run()
            except Exception:
                logging.exception("Bot runner failed.")
            print("Bot disconnected. Reconnecting in 10 seconds. Press Ctrl+C to stop.")
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nBot stopped.")


if __name__ == "__main__":
    main()
