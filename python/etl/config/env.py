import getpass
import os
from typing import Union

# Parameter store
#
#   prefix: DW-ETL/
#   hierarchical search:
#     general: <prefix>/<env_type>/<prefix>/<name>
#
#     e.g. looking for connection to "HYPPO_PRODUCTION" in environment development when running validation
#     look for DW-ETL/dev/development/validation/HYPPO_PRODUCTION
#              DW-ETL/dev/development/HYPPO_PRODUCTION
#              DW-ETL/dev/HYPPO_PRODUCTION
#              DW-ETL/HYPPO_PRODUCTION
#
#     when do overrides occur?
#       - pick different base stack
#       - pick different environment (development vs. tom vs. tom/validation)


def get(name: str, default: Union[str, None]=None) -> str:
    """
    Retrieve environment variable or error out if variable is not set.
    This is mildly more readable than direct use of os.environ.
    """
    value = os.environ.get(name, default)
    if value is None:
        raise KeyError('environment variable "%s" not set' % name)
    if not value:
        raise ValueError('environment variable "%s" is empty' % name)
    return value


def get_default_prefix() -> str:
    """
    Return default prefix which is the first non-emtpy value of:
      - the environment variable ARTHUR_DEFAULT_PREFIX
      - the environment variable USER
      - the "user name" as determined by the getpass module

    >>> os.environ["ARTHUR_DEFAULT_PREFIX"] = "doctest"
    >>> get_default_prefix()
    'doctest'
    """
    try:
        default_prefix = get("ARTHUR_DEFAULT_PREFIX")
    except (KeyError, ValueError):
        default_prefix = os.environ.get("USER", "")
        if len(default_prefix) == 0:
            default_prefix = getpass.getuser()
    return default_prefix


if __name__ == "__main__":
    prefix = get_default_prefix()
    print("Default prefix = {}".format(prefix))
