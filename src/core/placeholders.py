import re

PLACEHOLDER_RE = re.compile(r"^\{[A-Za-z0-9_\-]{1,100}\}$")


def is_placeholder(value) -> bool:
    """
    Returns True if value is a placeholder for an entity created in the same batch:

    {Person_A}
    {item-1}
    """
    return isinstance(value, str) and PLACEHOLDER_RE.match(value) is not None
