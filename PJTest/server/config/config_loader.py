#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Load shared PJTest settings from config/pjtest.ini.

Environment variables remain the highest-priority override so existing startup
scripts keep working.  Relative paths in the INI file are resolved from the
PJTest project root, not from the caller's current working directory.
"""

import configparser
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(
    os.environ.get(
        "PJTEST_CONFIG_FILE",
        str(PROJECT_ROOT / "config" / "pjtest.ini"),
    )
).expanduser()

if not CONFIG_FILE.is_absolute():
    CONFIG_FILE = (PROJECT_ROOT / CONFIG_FILE).resolve()

_PARSER = configparser.ConfigParser(interpolation=None)
_PARSER.optionxform = str.lower

if not CONFIG_FILE.is_file():
    raise RuntimeError(
        "PJTest config file not found: %s. "
        "Set PJTEST_CONFIG_FILE or restore config/pjtest.ini." % CONFIG_FILE
    )

with CONFIG_FILE.open("r", encoding="utf-8") as stream:
    _PARSER.read_file(stream)


def _from_env(env_name):
    """Return an environment override or None."""
    if env_name and env_name in os.environ:
        return os.environ[env_name]
    return None


def get_value(section, option, default=None, env_name=None):
    """Return a string setting with optional environment override."""
    env_value = _from_env(env_name)
    if env_value is not None:
        return env_value

    if _PARSER.has_option(section, option):
        return _PARSER.get(section, option)

    if default is None:
        raise KeyError("Missing config value [%s] %s" % (section, option))
    return str(default)


def get_int(section, option, default=None, env_name=None, minimum=None):
    """Return an integer setting."""
    value = int(get_value(section, option, default, env_name))
    if minimum is not None and value < minimum:
        value = minimum
    return value


def get_float(section, option, default=None, env_name=None, minimum=None):
    """Return a float setting."""
    value = float(get_value(section, option, default, env_name))
    if minimum is not None and value < minimum:
        value = minimum
    return value


def get_bool(section, option, default=None, env_name=None):
    """Return a boolean setting."""
    raw = get_value(section, option, default, env_name).strip().lower()
    if raw in ("1", "true", "yes", "on", "enabled"):
        return True
    if raw in ("0", "false", "no", "off", "disabled"):
        return False
    raise ValueError(
        "Invalid boolean config [%s] %s=%s" % (section, option, raw)
    )


def get_path(section, option, default=None, env_name=None):
    """Return an expanded absolute Path.

    Relative paths are resolved against PROJECT_ROOT.
    """
    raw = get_value(section, option, default, env_name)
    expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not expanded.is_absolute():
        expanded = PROJECT_ROOT / expanded
    return expanded.resolve()


def get_list(section, option, default=None, env_name=None):
    """Return a list split by commas or line breaks."""
    raw = get_value(section, option, default or "", env_name)
    normalized = raw.replace("\r", "\n").replace(",", "\n")
    return [item.strip() for item in normalized.split("\n") if item.strip()]


def get_path_list(section, option, default=None, env_name=None):
    """Return a list of expanded absolute Paths."""
    paths = []
    for raw in get_list(section, option, default, env_name):
        expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not expanded.is_absolute():
            expanded = PROJECT_ROOT / expanded
        paths.append(expanded.resolve())
    return paths


def get_section(section):
    """Return one INI section as a plain dictionary."""
    if not _PARSER.has_section(section):
        raise KeyError("Missing config section [%s]" % section)
    return dict(_PARSER.items(section))
