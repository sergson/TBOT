# modules/collector/__manifest__.py
{
    'name': 'Collector',
    'version': '0.5.5',
    'description': 'Data collector bot that fetches and stores OHLCV data from cryptocurrency exchanges.',
    'author': 'sergson',
    'license': 'GPL-3.0',
    'depends': ['broker'],
    'keywords': ['collector', 'ohlcv', 'market data', 'candles'],
    'bot_model': 'collector.bot',
    'type_model': 'collector.type'
}