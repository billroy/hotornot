"""Gunicorn entry point for the single-process Socket.IO app."""

from app import create_app

application, socketio = create_app()
