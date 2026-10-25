"""Validate Telegram Mini App initData without trusting client-supplied user IDs."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    if not init_data or not bot_token:
        raise ValueError('Отсутствуют данные авторизации')
    if len(init_data) > 16384:
        raise ValueError('Слишком длинные данные авторизации')
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError('Повторяющиеся поля авторизации')
    supplied_hash = data.pop('hash', '')
    data.pop('signature', None)
    if len(supplied_hash) != 64:
        raise ValueError('Подпись отсутствует')
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    check = '\n'.join(f'{key}={value}' for key, value in sorted(data.items()))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise ValueError('Неверная подпись Telegram')
    try:
        auth_date = int(data['auth_date'])
        age = int(time.time()) - auth_date
        if age < -60 or age > max_age:
            raise ValueError('Срок авторизации истёк')
        user = json.loads(data['user'])
        if not isinstance(user, dict) or not isinstance(user.get('id'), int):
            raise ValueError('Некорректный пользователь')
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Некорректные данные пользователя') from exc
    data['user'] = user
    return data
