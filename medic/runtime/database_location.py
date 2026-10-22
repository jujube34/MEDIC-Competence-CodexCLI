"""Classify supported database locations before any filesystem side effect."""
import os
import re


def classify(value):
    try:
        location = os.fspath(value)
    except TypeError:
        raise ValueError('invalid_database_location') from None
    if (not isinstance(location,str) or not location or location!=location.strip()
            or '\x00' in location or location==':memory:'):
        raise ValueError('invalid_database_location')
    if location.startswith(('postgresql://','postgres://')):
        return 'postgres',location
    # libpq keyword strings must not become local files containing credentials.
    # Other URL schemes and ambiguous drive-relative paths are not SQLite paths.
    keyword = re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*=',location)
    scheme = re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:',location)
    windows_absolute = re.match(r'^[A-Za-z]:[\\/]',location)
    if keyword or (scheme and not windows_absolute):
        raise ValueError('unsupported_database_connection_format')
    return 'sqlite',location
