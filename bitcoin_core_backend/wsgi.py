"""WSGI entry point for running the Bitcoin Core capability service."""

from src.adapters.inbound.http.app import create_app

app = create_app()
