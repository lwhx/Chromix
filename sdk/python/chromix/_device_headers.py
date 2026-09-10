"""Compare observed HTTP Client Hints with the same context's native JS identity."""
import json
import re

STRING = r'"(?:[^"\\\x00-\x1f\x7f]|\\["\\])*"'
BRAND = rf'({STRING});v=({STRING})'
FIELDS = {'brands':'sec-ch-ua', 'mobile':'sec-ch-ua-mobile',
          'platform':'sec-ch-ua-platform', 'architecture':'sec-ch-ua-arch',
          'bitness':'sec-ch-ua-bitness', 'platformVersion':'sec-ch-ua-platform-version',
          'fullVersionList':'sec-ch-ua-full-version-list', 'model':'sec-ch-ua-model',
          'wow64':'sec-ch-ua-wow64'}


def parse_hint(value, field):
    if not isinstance(value, str):
        raise ValueError('missing Client Hint')
    if field in ('brands', 'fullVersionList'):
        if not re.fullmatch(rf'{BRAND}(?:[ \t]*,[ \t]*{BRAND})*', value):
            raise ValueError('malformed brand list')
        return sorted((json.loads(m[1]), json.loads(m[2])) for m in re.finditer(BRAND, value))
    if field in ('mobile', 'wow64'):
        if value not in ('?0', '?1'):
            raise ValueError('malformed boolean')
        return value == '?1'
    if not re.fullmatch(STRING, value):
        raise ValueError('malformed quoted string')
    return json.loads(value)


def header_errors(scope, require_hints=True):
    identity = scope.get('identity', {}).get('value', {})
    http = scope.get('http', {})
    if http.get('status') != 'observed':
        return ['HTTP echo unavailable or failed']
    headers = http.get('value', {}).get('headers', {})
    errors = []
    if not identity.get('ua') or identity['ua'] != headers.get('user-agent'):
        errors.append('HTTP User-Agent differs from JS')
    languages = [v.lower() for v in identity.get('languages', [])]
    tags = [v.split(';', 1)[0].strip().lower() for v in headers.get('accept-language', '').split(',') if v.strip()]
    permitted = set(languages) | {v.split('-')[0] for v in languages}
    if not tags or not languages or tags[0] != languages[0] or not set(tags) <= permitted:
        errors.append('Accept-Language differs from JS')
    data = identity.get('uaData')
    if data is None:
        if any(key.startswith('sec-ch-ua') for key in headers):
            errors.append('HTTP UA hints exposed without JS UAData')
        return errors
    for field, header in FIELDS.items():
        if not require_hints and header not in headers:
            continue
        try:
            expected = data[field]
            if field in ('brands', 'fullVersionList'):
                expected = sorted((v['brand'], v['version']) for v in expected)
            actual = parse_hint(headers.get(header), field)
            if type(actual) is not type(expected) or actual != expected:
                errors.append(f'{header} differs from JS')
        except (ValueError, KeyError, TypeError):
            errors.append(f'{header} missing or malformed')
    return errors
