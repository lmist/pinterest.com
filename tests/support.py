"""Test helpers for faking Pinterest HTTP traffic."""

from __future__ import annotations

import json

import httpx


class Recorder:
    """MockTransport handler that records requests and replays canned responses."""

    def __init__(self, responder) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def resource_payload(
    data, *, bookmark: str | None = None, status: str = "success", code: int = 0
) -> dict:
    response = {"status": status, "code": code, "data": data, "endpoint_name": "test"}
    if bookmark:
        response["bookmark"] = bookmark
    return {
        "resource_response": response,
        "resource": "test",
        "request_identifier": "1",
    }


def json_response(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def query_params(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.url.query.decode()))


def options_of(request: httpx.Request) -> dict:
    return json.loads(query_params(request)["data"])["options"]
