"""
We use "config" files to refer to all files that may reside in the "config" directory:
* "Settings" files (ending in '.yaml') which drive the data warehouse settings
* Environment files (with variables)
* Other files (like release notes)

This module provides global access to settings.  Always treat them nicely and read-only.
"""

from collections import defaultdict
from functools import lru_cache
import logging
import logging.config
import os
import os.path
import sys
from typing import Iterable, List, Optional, Sequence, Set

import pkg_resources
import jsonschema
import simplejson as json
import yaml

import etl.config.dw

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


# Global config objects - always use accessors
_dw_config = None

# Local temp directory used for bootstrap, temp files, etc.
ETL_TMP_DIR = "/tmp/redshift_etl"


# TODO rename package to "redshift_etl"
def package_version(package_name="redshift-etl"):
    return "{} v{}".format(package_name, pkg_resources.get_distribution(package_name).version)


def get_dw_config():
    return _dw_config


def etl_tmp_dir(path: str) -> str:
    """
    Return the absolute path within the ETL runtime directory for the selected path.
    """
    return os.path.join(ETL_TMP_DIR, path)


def configure_logging(full_format: bool=False, log_level: str=None) -> None:
    """
    Setup logging to go to console and application log file

    If full_format is True, then use the terribly verbose format of
    the application log file also for the console.  And log at the DEBUG level.
    Otherwise, you can choose the log level by passing one in.
    """
    config = load_json('logging.json')
    if full_format:
        config["formatters"]["console"]["format"] = config["formatters"]["file"]["format"]
        config["handlers"]["console"]["level"] = logging.DEBUG
    elif log_level:
        config["handlers"]["console"]["level"] = log_level
    logging.config.dictConfig(config)
    logging.captureWarnings(True)
    logging.getLogger(__name__).info('Starting log for "%s" (%s)', ' '.join(sys.argv), package_version())


def load_environ_file(filename: str) -> None:
    """
    Load additional environment variables from file.

    Only lines that look like 'NAME=VALUE' or 'export NAME=VALUE' are used,
    other lines are silently dropped.
    """
    logging.getLogger(__name__).info("Loading environment variables from '%s'", filename)
    with open(filename) as f:
        for line in f:
            tokens = [token.strip() for token in line.split('=', 1)]
            if len(tokens) == 2 and not tokens[0].startswith('#'):
                name = tokens[0].replace("export", "").strip()
                value = tokens[1]
                os.environ[name] = value


def load_settings_file(filename: str, settings: dict) -> None:
    """
    Load new settings from config file or a directory of config files
    and UPDATE settings (old settings merged with new).
    """
    logger.info("Loading settings from '%s'", filename)
    with open(filename) as f:
        new_settings = yaml.safe_load(f)
        for key in new_settings:
            # Try to update only update-able settings
            if key in settings and isinstance(settings[key], dict):
                settings[key].update(new_settings[key])
            else:
                settings[key] = new_settings[key]


def read_release_file(filename: str) -> None:
    """
    Read the release file and echo its contents to the log.
    Life's exciting. And short. But mostly exciting.
    """
    logger.debug("Loading release information from '%s'", filename)
    with open(filename) as f:
        lines = [line.strip() for line in f]
    logger.info("Release information: %s", ', '.join(lines))


def yield_config_files(config_files: Sequence[str], default_file: str=None) -> Iterable[str]:
    """
    Generate filenames from the list of files or directories in :config_files and :default_file

    If the default_file is not None, then it is always prepended to the list of files.
    (It is an error (sadly, at runtime) if the default file is not a file that's part of the package.)

    Note that files in directories are always sorted by their name.
    """
    if default_file:
        yield pkg_resources.resource_filename(__name__, default_file)

    for name in config_files:
        if os.path.isdir(name):
            files = sorted(os.path.join(name, n) for n in os.listdir(name))
        else:
            files = [name]
        for filename in files:
            yield filename


def load_config(config_files: Sequence[str], default_file: str="default_settings.yaml") -> dict:
    """
    Load settings and environment from config files (starting with the default if provided).

    If the config "file" is actually a directory, (try to) read all the
    files in that directory.

    The settings are validated against their schema before being returned.
    """
    settings = dict()
    count_settings = 0
    for filename in yield_config_files(config_files, default_file):
        if filename.endswith(".sh"):
            load_environ_file(filename)
        elif filename.endswith((".yaml", ".yml")):
            load_settings_file(filename, settings)
            count_settings += 1
        elif filename.endswith("release.txt"):
            read_release_file(filename)
        else:
            logger.info("Skipping config file '%s'", filename)

    # Need to load at least the defaults and some installation specific file:
    if count_settings < 2:
        raise RuntimeError("Failed to find enough configuration files (need at least default and local config)")

    schema = load_json("settings.schema")
    jsonschema.validate(settings, schema)

    global _dw_config
    _dw_config = etl.config.dw.DataWarehouseConfig(settings)

    return dict(settings)


def gather_setting_files(config_files: Sequence[str]) -> List[str]:
    """
    Gather all settings files (*.yaml files) -- this drops any hierarchy in the config files (!).

    It is an error if we detect that there are settings files in separate directories that have the same filename.
    So trying '-c hello/world.yaml -c hola/world.yaml' triggers an exception.
    """
    settings_found = set()  # type: Set[str]
    settings_with_path = []

    for fullname in yield_config_files(config_files):
        if fullname.endswith(('.yaml', '.yml')):
            filename = os.path.basename(fullname)
            if filename not in settings_found:
                settings_found.add(filename)
            else:
                raise KeyError("Found configuration file in multiple locations: '%s'" % filename)
            settings_with_path.append(fullname)
    return sorted(settings_with_path)


@lru_cache()
def load_json(filename: str):
    return json.loads(pkg_resources.resource_string(__name__, filename))
