_PARSERS = {}


def register(key):
    """Decora una función ``parse(subject, text, sender) -> ParsedEmail``."""

    def decorator(func):
        _PARSERS[key] = func
        return func

    return decorator


def get_parser(key):
    return _PARSERS.get(key)


def registered_keys():
    return sorted(_PARSERS)
