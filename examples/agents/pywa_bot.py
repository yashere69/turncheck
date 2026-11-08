"""A bot built on pywa (https://github.com/david-lev/pywa), the Python WhatsApp framework.

turncheck tests it unchanged: pywa receives the webhooks, checks their signatures and
skips duplicates itself. The one test-only piece is the httpx transport below, which
sends pywa's Graph API calls to turncheck's mock instead of graph.facebook.com.

    pip install pywa fastapi uvicorn
    turncheck run examples/compat --agent "python examples/agents/pywa_bot.py"
"""

from __future__ import annotations

import os
import sys

import httpx
import uvicorn
from fastapi import FastAPI
from pywa import WhatsApp, filters, types


class ToMock(httpx.HTTPTransport):
    """Route every Graph API request to GRAPH_API_BASE, keeping its path."""

    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = httpx.URL(base)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme=self.base.scheme, host=self.base.host, port=self.base.port
        )
        return super().handle_request(request)


app = FastAPI()
wa = WhatsApp(
    phone_id=os.environ["PHONE_NUMBER_ID"],
    token=os.environ["WHATSAPP_TOKEN"],
    server=app,
    webhook_endpoint="/webhook",
    verify_token=os.environ["VERIFY_TOKEN"],
    app_secret=os.environ["APP_SECRET"],
    session=httpx.Client(transport=ToMock(os.environ["GRAPH_API_BASE"])),
)


@wa.on_message(filters.text)
def answer(_: WhatsApp, msg: types.Message) -> None:
    msg.reply_text(f"Namaste {msg.from_user.name}! You said: {msg.text}")


@wa.on_message_status(filters.failed)
def undelivered(_: WhatsApp, status: types.MessageStatus) -> None:
    print(f"undelivered {status.id}: {status.error}", file=sys.stderr)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")
