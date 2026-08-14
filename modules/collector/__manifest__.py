# modules/collector/__manifest__.py
{
    'name': 'Collector',
    'version': '1.0.0',
    'description': 'Data collector bot that fetches and stores OHLCV data from cryptocurrency exchanges.',
    'author': 'sergson',
    'license': 'GPL-3.0',
    'depends': [],
    'keywords': ['collector', 'ohlcv', 'market data', 'candles'],
    'bot_model': 'collector.bot',
    'type_model': 'collector.type'
}