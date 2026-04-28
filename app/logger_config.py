"""Centralized logging configuration.

Usage:
    from config.settings import config
    from logger_config import setup_logging, get_logger
    setup_logging(config)
    logger = get_logger(__name__)
"""
import logging
import logging.handlers
import os
from typing import Optional


def setup_logging(config) -> None:
    """Configure root logger using settings from `config`.

    This sets up a rotating file handler and a console handler. Safe to call multiple times.
    """
    log_file = getattr(config, 'LOG_FILE', '/app/logs/bot.log')
    log_level = getattr(config, 'LOG_LEVEL', 'INFO').upper()
    max_bytes = getattr(config, 'LOG_MAX_BYTES', 10 * 1024 * 1024)
    backup_count = getattr(config, 'LOG_BACKUP_COUNT', 5)

    # Ensure directory exists
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
    except Exception:
        pass

    root = logging.getLogger()

    # Avoid adding handlers multiple times when called repeatedly
    if getattr(root, '_configured_by_logger_config', False):
        # Update level in case environment changed
        try:
            root.setLevel(getattr(logging, log_level))
        except Exception:
            root.setLevel(logging.INFO)
        return

    # Formatter with time and module
    fmt = logging.Formatter('%(asctime)s | %(levelname)-7s | %(name)s | %(message)s')

    # Rotating file handler
    try:
        fh = logging.handlers.RotatingFileHandler(
            filename=log_file,
            maxBytes=int(max_bytes),
            backupCount=int(backup_count),
            encoding='utf-8'
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        # If file handler fails, continue with console only
        pass

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Set level
    try:
        root.setLevel(getattr(logging, log_level))
    except Exception:
        root.setLevel(logging.INFO)

    root._configured_by_logger_config = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a logger instance (thin wrapper)."""
    return logging.getLogger(name)
