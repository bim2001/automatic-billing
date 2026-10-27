import re


_PH_MOBILE_FORMAT = re.compile(r'^(?:09[0-9]{9}|\+639[0-9]{9})$')
_PH_MOBILE_ALLOWED_INPUT = re.compile(r'^[0-9+()\-\s]+$')


def normalize_philippine_mobile_number(value):
    """Return a Philippine mobile number as 09XXXXXXXXX, or None if invalid."""
    value = (value or '').strip()
    if not value:
        return ''
    if not _PH_MOBILE_ALLOWED_INPUT.fullmatch(value):
        return None

    compact = re.sub(r'[\s()-]', '', value)
    if not _PH_MOBILE_FORMAT.fullmatch(compact):
        return None
    if compact.startswith('+63'):
        compact = '0' + compact[3:]
    return compact
