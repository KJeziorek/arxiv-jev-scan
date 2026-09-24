"""Desktop notification after a scan. Never fails the scan: without a
notification backend (e.g. on a headless server) the message is only logged."""

import logging

log = logging.getLogger(__name__)


def notify(title: str, message: str) -> None:
    log.info("%s: %s", title, message)
    try:
        from plyer import notification

        notification.notify(title=title, message=message, app_name="arxiv-jev-scan", timeout=10)
    except Exception as error:
        log.debug("Desktop notification not available: %s", error)
