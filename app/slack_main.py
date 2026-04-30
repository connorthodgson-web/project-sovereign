"""Runtime entrypoint for the thin Slack interface."""

from integrations.slack_client import SlackClient
from integrations.reminders.service import reminder_scheduler_service


def main() -> None:
    """Launch the Slack Socket Mode process."""
    reminder_scheduler_service.start()
    SlackClient().start()


if __name__ == "__main__":
    main()
