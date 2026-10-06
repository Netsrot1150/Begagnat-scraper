"""Where a hit gets sent.

Every notifier takes the same list of listings and is best-effort: a dead
webhook must never abort a watch loop.
"""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage

import requests

from .models import Listing

log = logging.getLogger(__name__)


def format_listing(listing: Listing) -> str:
    gpu = listing.gpu
    bits = [listing.title]
    if listing.price_sek is not None:
        price = f"{listing.price_sek:,.0f} kr".replace(",", " ")
        if listing.import_cost_sek:
            price += f" (inkl. {listing.import_cost_sek:,.0f} kr moms/avgift)".replace(",", " ")
        bits.append(price)
    if gpu:
        detail = f"{gpu.name} - {gpu.total_vram_gb} GB"
        if gpu.quantity > 1:
            detail += f" ({gpu.quantity}x{gpu.vram_gb})"
        if listing.kr_per_gb:
            detail += f", {listing.kr_per_gb:.0f} kr/GB"
        bits.append(detail)
    if listing.location:
        bits.append(listing.location)
    bits.append(listing.url)
    return " | ".join(bits)


def format_batch(listings: list[Listing], title: str = "Nya träffar") -> str:
    lines = [f"{title} ({len(listings)}):", ""]
    lines += [f"- {format_listing(listing)}" for listing in listings]
    return "\n".join(lines)


class Notifier:
    def send(self, listings: list[Listing]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def send(self, listings: list[Listing]) -> None:
        if listings:
            print(format_batch(listings))


class NtfyNotifier(Notifier):
    """ntfy.sh - the least-effort way to get a push notification on a phone."""

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str = "") -> None:
        self.url = f"{server.rstrip('/')}/{topic.lstrip('/')}"
        self.token = token

    def send(self, listings: list[Listing]) -> None:
        if not listings:
            return
        headers = {"Title": f"{len(listings)} nya GPU-traffar", "Tags": "computer,moneybag"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        best = listings[0]
        if best.url:
            headers["Click"] = best.url
        try:
            requests.post(
                self.url,
                data=format_batch(listings).encode("utf-8"),
                headers=headers,
                timeout=15,
            ).raise_for_status()
        except requests.RequestException as exc:
            log.error("ntfy notification failed: %s", exc)


class WebhookNotifier(Notifier):
    """Discord/Slack-compatible webhook (both accept a ``content``/``text`` field)."""

    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, listings: list[Listing]) -> None:
        if not listings:
            return
        body = format_batch(listings)
        payload = {"content": body, "text": body}
        try:
            requests.post(self.url, json=payload, timeout=15).raise_for_status()
        except requests.RequestException as exc:
            log.error("webhook notification failed: %s", exc)


class EmailNotifier(Notifier):
    def __init__(self, host: str, port: int, user: str, password: str, to: str, use_tls: bool = True) -> None:
        self.host, self.port, self.user, self.password, self.to = host, port, user, password, to
        self.use_tls = use_tls

    def send(self, listings: list[Listing]) -> None:
        if not listings:
            return
        message = EmailMessage()
        message["Subject"] = f"{len(listings)} nya GPU-träffar"
        message["From"] = self.user
        message["To"] = self.to
        message.set_content(format_batch(listings))
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.password:
                    smtp.login(self.user, self.password)
                smtp.send_message(message)
        except Exception as exc:  # noqa: BLE001 - notification must never break a watch
            log.error("email notification failed: %s", exc)


class JsonlNotifier(Notifier):
    """Append hits to a JSONL file, for piping into something else."""

    def __init__(self, path: str) -> None:
        self.path = path

    def send(self, listings: list[Listing]) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            for listing in listings:
                handle.write(json.dumps(listing.to_dict(), ensure_ascii=False) + "\n")


def build_notifiers(config: dict) -> list[Notifier]:
    """Construct notifiers from the ``notify`` block of a config file."""
    notifiers: list[Notifier] = []
    if config.get("console", True):
        notifiers.append(ConsoleNotifier())
    if ntfy := config.get("ntfy"):
        notifiers.append(
            NtfyNotifier(ntfy["topic"], ntfy.get("server", "https://ntfy.sh"), ntfy.get("token", ""))
        )
    if webhook := config.get("webhook"):
        notifiers.append(WebhookNotifier(webhook if isinstance(webhook, str) else webhook["url"]))
    if email := config.get("email"):
        notifiers.append(
            EmailNotifier(
                email["host"], int(email.get("port", 587)), email["user"],
                email.get("password", ""), email["to"], email.get("tls", True),
            )
        )
    if path := config.get("jsonl"):
        notifiers.append(JsonlNotifier(path))
    return notifiers
