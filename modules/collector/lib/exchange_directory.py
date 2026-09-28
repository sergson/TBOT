# modules/collector/lib/exchange_directory.py
"""
T.B.O.T project collector bot exchange setting directory.
"""

import ccxt


# Default markets list
MARKETS = [
                {'label': 'Spot', 'value': 'spot'},
                {'label': 'Futures', 'value': 'futures'}
            ]

#Default exchanges and default exchange
EXCHANGES = [
    {'label': 'Binance', 'value': 'binance', 'default':True,
     'defaults':{'market':'spot',
                'symbol':'BTC/USDT'},
     },
    {'label': 'KuCoin', 'value': 'kucoin'},
    {'label': 'MEXC', 'value': 'mexc'},
    {'label': 'OKX', 'value': 'okx','markets':MARKETS},
    {'label': 'Bybit', 'value': 'bybit'}
]


def get_exchange_options():
    """
    Возвращает полный список опций для dropdown с выбором биржи.
    Объединяет ccxt.exchanges со статическими настройками из EXCHANGES.
    """
    # Получаем все биржи из ccxt
    try:
        all_exchange_ids = ccxt.exchanges  # список строк
    except Exception:
        # Если ccxt недоступен, используем только статический список
        all_exchange_ids = [item['value'] for item in EXCHANGES]

    # Создаём словарь кастомных настроек по value для быстрого поиска
    custom = {item['value']: item for item in EXCHANGES}

    options = []
    for ex_id in all_exchange_ids:
        if ex_id in custom:
            # Есть кастомная запись — берём label и все дополнительные поля
            entry = custom[ex_id]
            options.append({
                'label': entry.get('label', ex_id.capitalize()),
                'value': ex_id,
                'defaults': entry.get('defaults', {}),
                'markets': entry.get('markets', None),
                # можно добавить любые другие поля, если нужно
            })
        else:
            # Нет кастомной записи — генерируем label автоматически
            options.append({
                'label': ex_id.capitalize(),
                'value': ex_id,
                'defaults': {},
                'markets': None,
            })

    # Сортируем по label для удобства (опционально)
    options.sort(key=lambda x: x['label'])

    return options

def get_default_exchange_value():
    """Возвращает value биржи по умолчанию (из static EXCHANGES или первую из ccxt)."""
    # Ищем биржу с default=True в статическом списке
    for item in EXCHANGES:
        if item.get('default'):
            return item['value']
    # Иначе возвращаем первую доступную
    options = get_exchange_options()
    return options[0]['value'] if options else None