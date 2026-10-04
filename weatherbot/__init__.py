"""weatherbot: Polymarket daily highest-temperature markets.

Mechanism: professional weather models (ECMWF, GFS, ICON, ...) forecast a
city's daily high better than the average bettor prices it. The bot turns a
multi-model forecast into a probability for each settlement bucket and trades
only where that probability beats the price after fees and slippage.

Station coordinates in stations.json are factual airport locations.
"""
