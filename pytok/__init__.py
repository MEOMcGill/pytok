"""
.. include:: ../README.md
"""
__docformat__ = "restructuredtext"

# zendriver's generated CDP bindings don't cover every event Chrome emits (e.g.
# DOM.adoptedStyleSheetsModified). Its event parser raises KeyError on unknown
# methods, which the connection listener catches but logs as a noisy traceback
# for every single occurrence. Patch the parser to skip unknown events quietly,
# exactly as the listener already does for known events that have no registered
# handler (parse -> no matching handler -> continue).
import zendriver.cdp.util as _zcdp_util

_pytok_orig_parse_json_event = _zcdp_util.parse_json_event


def _pytok_tolerant_parse_json_event(json):
    if json.get("method") not in _zcdp_util._event_parsers:
        return None
    return _pytok_orig_parse_json_event(json)


_zcdp_util.parse_json_event = _pytok_tolerant_parse_json_event
